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

from app.fomo.activity import SWAP_FIELDS, swap_rows
from app.fomo.schema import normalize_leaderboard

FOMO_ORIGIN = "https://fomo.family"
API_ORIGIN = "https://prod-api.fomo.family"
# Probed against the live endpoint: 101 and above answer HTTP 400.
PAGE_LIMIT = 100
# A stop, not a target. One trader's whole history is ~600 swaps today; this
# bounds the POST body if an account turns out to have orders of magnitude more.
MAX_SWAPS_PER_TRADER = 5000


class BrowserSyncError(RuntimeError):
    pass


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
                    f"FOMO: HTTP {status} в самом браузере. Проверьте вход на сайте; сбор остановлен."
                )
            if status == 429:
                raise BrowserSyncError("FOMO продолжает ограничивать запросы (429); повторите позднее")
            if not 200 <= status < 300 or result.get("data") is None:
                raise BrowserSyncError(f"FOMO: HTTP {status} или не JSON ({path})")
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

    async def collect(self, *, period="30d", limit=30, max_swaps=MAX_SWAPS_PER_TRADER):
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
        return {"period": period, "requested_limit": limit, "traders": traders}

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
