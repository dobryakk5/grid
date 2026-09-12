"""Collect names using the site's own browser session; never export credentials.

Only the optional CLI imports Playwright. This module is also usable with a
fake page/transport, without a browser or a FOMO account.
"""

from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx

from app.fomo.activity import SWAP_FIELDS, normalize_swap, swap_rows
from app.fomo.schema import normalize_leaderboard
from app.fomo.theses import public_thesis, thesis_rows

FOMO_ORIGIN = "https://fomo.family"
API_ORIGIN = "https://prod-api.fomo.family"
# Probed against the live endpoint: 101 and above answer HTTP 400.
PAGE_LIMIT = 100
# A stop, not a target. One trader's whole history is ~600 swaps today; this
# bounds the POST body if an account turns out to have orders of magnitude more.
MAX_SWAPS_PER_TRADER = 5000
# Theses are read per coin, so the cost of the pass is the number of coins the
# cohort touched in the window, not the number of people in it.
THESIS_PAGE_LIMIT = 80
THESIS_WINDOW_HOURS = 24
MAX_THESIS_TOKENS = 300
# Anything but "no minimum": the threshold filters the feed by trade size, and
# a small trade's thesis is still a thesis.
THESIS_THRESHOLD = 0
# The session is gone or the endpoint is refusing everyone; retrying the next
# coin cannot help, and it must not look like "this coin has no theses".
FATAL_STATUSES = frozenset({401, 403, 429, 430, 431})


class BrowserSyncError(RuntimeError):
    """``status`` is the upstream HTTP code when there was one, else None.

    It exists so an optional pass (theses) can tell "this one coin answered
    404" from "the session died", and only give up on the second.
    """

    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def unwrap(payload):
    return payload.get("responseObject", payload) if isinstance(payload, dict) else payload


def validate_base(value: str) -> str:
    base = value.rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise BrowserSyncError("FOMO_API_BASE должен быть URL API без пароля, query и fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise BrowserSyncError("Для удалённого API используйте HTTPS или локальный SSH-туннель")
    return base


def api_headers() -> dict:
    """Bearer for the Grid API, or nothing when it has no auth configured.

    The collector is a machine caller with no password prompt, so it carries
    ``AUTH_SERVICE_TOKEN`` (as ``GRID_API_TOKEN``) rather than logging in.
    Empty is fine and stays fine: an API with no auth configured accepts it.
    """
    token = os.environ.get("GRID_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def local_json(http, method: str, url: str, **kwargs):
    kwargs["headers"] = {**api_headers(), **kwargs.get("headers", {})}
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise BrowserSyncError(
            "API недоступен. Проверьте FOMO_API_BASE и запустите API/SSH-туннель."
        ) from None
    if response.status_code in (401, 403):
        raise BrowserSyncError(
            "Grid API требует авторизации: передайте GRID_API_TOKEN="
            "<AUTH_SERVICE_TOKEN из .env сервера>"
        )
    if not response.is_success:
        raise BrowserSyncError(f"Grid API: HTTP {response.status_code} ({method} {urlsplit(url).path})")
    try:
        return response.json()
    except ValueError:
        raise BrowserSyncError("Grid API вернул не JSON; проверьте порт и префикс /grid") from None


async def check_api(http, base: str) -> str:
    """Confirm the Grid API answers, and report which database it writes to."""
    status = await local_json(http, "GET", base + "/api/fomo/activity")
    target = status.get("database") if isinstance(status, dict) else None
    return target if isinstance(target, str) else "неизвестно"


# Fetch remains in the FOMO origin, with its actual cookies and current bearer.
# No token, cookie or raw upstream response is sent to the Grid server.
FETCH_JS = """async ({url, headers}) => {
  if (location.origin !== 'https://fomo.family') return {status: 0};
  try {
    const r = await fetch(url, {headers, credentials: 'include',
                               signal: AbortSignal.timeout(25000)});
    let data = null;
    if (r.ok) { try { data = await r.json(); } catch (_) {} }
    return {status: r.status, data};
  } catch (_) { return {status: 0}; }
}"""


def _log(message):
    # A full history takes minutes per run; buffered progress that only appears
    # at the end is the same as no progress at all when the output is a file.
    print(message, flush=True)


class BrowserCollector:
    def __init__(self, page, *, log=_log, sleep=asyncio.sleep):
        self.page = page
        self.log = log
        self.sleep = sleep
        self._headers = {}
        self._ready = asyncio.Event()
        self._tasks = set()

    def listen(self):
        def response_seen(response):
            task = asyncio.create_task(self.observe(response))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        self.page.on("response", response_seen)

    async def observe(self, response):
        parsed = urlsplit(response.url)
        if f"{parsed.scheme}://{parsed.netloc}" != API_ORIGIN or not 200 <= response.status < 300:
            return
        try:
            headers = await response.request.all_headers()
            authorization = headers.get("authorization", "")
            if authorization.startswith("Bearer "):
                self._headers = {k: v for k, v in headers.items()
                                 if k in {"authorization", "x-supported-chains"}}
                self._ready.set()
        except Exception:
            # Navigating/closing a tab can destroy a response body. Never log
            # Playwright's exception, which can contain request credentials.
            return

    async def wait_session(self, timeout: float):
        deadline = time.monotonic() + timeout
        while not self._ready.is_set():
            if self.page.is_closed():
                raise BrowserSyncError("Окно FOMO закрыто")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrowserSyncError("Не дождался сессии: войдите в FOMO в открытом окне и повторите запуск")
            try:
                await asyncio.wait_for(self._ready.wait(), min(30, remaining))
            except asyncio.TimeoutError:
                self.log("Ожидаю вход в FOMO и успешный запрос сайта…")

    async def get(self, path: str, params=None):
        if not path.startswith("/") or path.startswith("//"):
            raise BrowserSyncError("Некорректный путь FOMO API")
        url = API_ORIGIN + path + ("?" + urlencode(params) if params else "")
        for attempt in range(2):
            result = await self.page.evaluate(FETCH_JS, {"url": url, "headers": self._headers})
            status = result["status"]
            if status == 429 and attempt == 0:
                self.log("FOMO: 429, пауза 60 секунд и повтор того же запроса")
                await self.sleep(60)
                continue
            if status in {401, 403, 430, 431}:
                raise BrowserSyncError(
                    f"FOMO: HTTP {status} в самом браузере. Проверьте вход на сайте; сбор остановлен.",
                    status=status,
                )
            if status == 429:
                raise BrowserSyncError("FOMO продолжает ограничивать запросы (429); повторите позднее",
                                       status=429)
            if not 200 <= status < 300 or result.get("data") is None:
                raise BrowserSyncError(f"FOMO: HTTP {status} или не JSON ({path})", status=status or None)
            await self.sleep(0.5)
            return unwrap(result["data"])

    async def swaps(self, user_id: str, *, max_swaps=MAX_SWAPS_PER_TRADER):
        """Every swap FOMO will hand out for one user, oldest page last.

        The endpoint pages by the id of the last row already seen
        (``lastSwapId``); ``page``/``offset``/``skip`` are silently ignored and
        re-serve page one, which is how the old single-page collect looked like
        a complete history while it was returning 25 of 625 rows. ``limit``
        above 100 is rejected with HTTP 400. Both facts come from probing the
        live endpoint, not from a guess: a cursor that stops producing new ids
        is treated as "paging is not working" and ends the walk, so a future
        rename degrades to the old behaviour instead of looping.
        """
        path = f"/v2/users/{quote(user_id, safe='')}/swaps"
        collected, seen, cursor = [], set(), None
        while True:
            params = {"limit": PAGE_LIMIT} | ({"lastSwapId": cursor} if cursor else {})
            try:
                rows, more = swap_rows(await self.get(path, params))
            except ValueError as exc:
                raise BrowserSyncError(str(exc)) from None
            fresh = [row for row in rows
                     if isinstance(row, dict) and row.get("id") not in seen]
            seen.update(row["id"] for row in fresh if isinstance(row.get("id"), str))
            collected.extend(fresh)
            if len(collected) >= max_swaps:
                return collected[:max_swaps], True
            if more is False:
                return collected, False
            last_id = fresh[-1].get("id") if fresh else None
            if not fresh or not isinstance(last_id, str):
                # The cursor stopped yielding new rows while the API still
                # claims more (or stopped saying). Stop rather than loop, and
                # pass the claim through so coverage stays honest.
                return collected, more
            cursor = last_id

    async def token_theses(self, chain_id, token_address, *, after_ms, before_ms):
        """One page of theses written about one coin inside the window.

        Deliberately a single request, not a walk. ``sortedThesis`` takes a
        time window but no cursor that has been verified, and its sort order
        is a guess; moving ``beforeTime`` down would silently skip rows if it
        does not sort by time. So the page is taken as it comes and
        ``hasNextPage`` is passed up to be reported, not quietly dropped.
        """
        params = {
            "tokenAddress": token_address, "networkId": chain_id,
            "afterTime": after_ms, "beforeTime": before_ms,
            "limit": THESIS_PAGE_LIMIT, "threshold": THESIS_THRESHOLD,
        }
        try:
            rows, more = thesis_rows(await self.get("/feed/token/sortedThesis", params))
        except ValueError as exc:
            raise BrowserSyncError(str(exc), status=200) from None
        return [public_thesis(row) for row in rows if isinstance(row, dict)], more

    async def theses(self, traders, *, window_hours=THESIS_WINDOW_HOURS,
                     max_tokens=MAX_THESIS_TOKENS, now_ms=None):
        """Theses about every coin the cohort traded inside the window.

        FOMO publishes theses per coin, not per person: there is no verified
        "what did this user write" endpoint. So the coins the cohort just
        traded are the way in, and the server keeps the rows whose author is
        in the cohort. A thesis is written next to a trade, so a member who
        traded in the window is reachable this way; one who only wrote about a
        coin nobody in the cohort touched is not, and that is the honest limit
        of this pass, recorded in ``coverage`` rather than glossed over.

        Never fatal: theses are an enrichment on top of a swap history that
        can take a quarter of an hour to read. One coin answering 404 costs
        that coin; only a dead session or a rate limit stops the pass.
        """
        before_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        after_ms = before_ms - int(window_hours * 3600_000)
        seen = {}
        for trader in traders:
            for row in trader["swaps"]:
                for leg in normalize_swap(row):
                    if leg["occurred_at_ms"] >= after_ms:
                        key = (leg["chain_id"], leg["token_address"])
                        seen[key] = max(seen.get(key, 0), leg["occurred_at_ms"])
        # Most recently traded first, so a cap cuts the stalest coins; the
        # address breaks ties so two runs on the same data ask the same coins.
        wanted = sorted(seen, key=lambda key: (-seen[key], key[0], key[1]))
        coverage = {
            "window_hours": window_hours, "after_ms": after_ms, "before_ms": before_ms,
            "tokens_in_window": len(wanted), "tokens_read": min(len(wanted), max_tokens),
            "tokens_skipped": max(0, len(wanted) - max_tokens),
            "tokens_with_more": 0, "tokens_failed": 0, "items": 0,
        }
        if not wanted:
            return [], coverage
        self.log(f"Тезисы за {window_hours} ч: {coverage['tokens_read']} монет"
                 + (f" из {len(wanted)}" if coverage["tokens_skipped"] else ""))
        groups = []
        for chain_id, token_address in wanted[:max_tokens]:
            try:
                items, more = await self.token_theses(
                    chain_id, token_address, after_ms=after_ms, before_ms=before_ms)
            except BrowserSyncError as exc:
                if exc.status is None or exc.status in FATAL_STATUSES:
                    raise
                coverage["tokens_failed"] += 1
                continue
            coverage["tokens_with_more"] += more is True
            if items:
                coverage["items"] += len(items)
                groups.append({"chain_id": chain_id, "token_address": token_address,
                               "items": items})
        self.log(f"Тезисы: {coverage['items']} записей по {len(groups)} монетам"
                 + (f"; не ответили: {coverage['tokens_failed']}" if coverage["tokens_failed"] else ""))
        return groups, coverage

    async def collect(self, *, period="30d", limit=30, max_swaps=MAX_SWAPS_PER_TRADER,
                      thesis_hours=THESIS_WINDOW_HOURS, max_thesis_tokens=MAX_THESIS_TOKENS):
        # /trades is a position summary, not an execution stream. Rank cohort
        # comes strictly from the selected leaderboard, never token holders.
        ranks = normalize_leaderboard(await self.get(f"/v2/leaderboard/{period}", {"limit": limit}))
        if not ranks:
            raise BrowserSyncError("Лидерборд пуст или формат изменился; прежний сбор сохранён")
        selected = sorted(ranks, key=lambda r: r.rank or 10**9)[:limit]
        self.log(f"Лидерборд {period}: {len(selected)} из {limit}. "
                 f"Читаю полную историю swaps по всем сетям…")
        traders = []
        for index, row in enumerate(selected, 1):
            swaps, more = await self.swaps(row.user_id, max_swaps=max_swaps)
            # Whitelist public swap fields. Never serialize session headers or
            # cookies.
            traders.append({
                "user_id": row.user_id, "handle": row.handle,
                "display_name": row.display_name, "rank": row.rank or index,
                "has_more": more,
                "swaps": [{k: swap.get(k) for k in SWAP_FIELDS} for swap in swaps],
            })
            self.log(f"{index}/{len(selected)} @{row.handle or row.display_name or row.user_id}: "
                     f"{len(swaps)} swaps" + (f" (упёрлись в предел {max_swaps})" if more else ""))
        payload = {"period": period, "requested_limit": limit, "traders": traders}
        if thesis_hours:
            try:
                payload["theses"], payload["thesis_coverage"] = await self.theses(
                    traders, window_hours=thesis_hours, max_tokens=max_thesis_tokens)
            except BrowserSyncError as exc:
                # The swap history above can take a quarter of an hour to read.
                # A thesis feed that dies mid-pass must not take it with it --
                # the import still lands, saying plainly that this part failed.
                payload["theses"] = []
                payload["thesis_coverage"] = {"window_hours": thesis_hours, "stopped": str(exc)}
                self.log(f"Тезисы не собраны: {exc}")
        return payload

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def import_activity(http, base: str, payload: dict):
    # A full history is megabytes of JSON, and the server names every new coin
    # it meets before answering. The client's default timeout is sized for the
    # health check, not for this; timing out here would throw away a collection
    # that took a quarter of an hour to read.
    return await local_json(http, "POST", base + "/api/fomo/activity/import",
                            json=payload, timeout=900)
