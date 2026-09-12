"""Collect names using the site's own browser session; never export credentials.

Only the optional CLI imports Playwright. This module is also usable with a
fake page/transport, without a browser or a FOMO account.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx

from app.fomo.activity import SWAP_FIELDS, swap_rows
from app.fomo.schema import normalize_leaderboard

FOMO_ORIGIN = "https://fomo.family"
API_ORIGIN = "https://prod-api.fomo.family"


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


async def local_json(http, method: str, url: str, **kwargs):
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError:
        raise BrowserSyncError(
            "API недоступен. Проверьте FOMO_API_BASE и запустите API/SSH-туннель."
        ) from None
    if not response.is_success:
        raise BrowserSyncError(f"Grid API: HTTP {response.status_code} ({method} {urlsplit(url).path})")
    try:
        return response.json()
    except ValueError:
        raise BrowserSyncError("Grid API вернул не JSON; проверьте порт и префикс /grid") from None


async def check_api(http, base: str):
    await local_json(http, "GET", base + "/api/fomo/activity")


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


class BrowserCollector:
    def __init__(self, page, *, log=print, sleep=asyncio.sleep):
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

    async def collect(self, *, period="30d", limit=30):
        # /trades is a position summary, not an execution stream. Rank cohort
        # comes strictly from the selected leaderboard, never token holders.
        ranks = normalize_leaderboard(await self.get(f"/v2/leaderboard/{period}", {"limit": limit}))
        if not ranks:
            raise BrowserSyncError("Лидерборд пуст или формат изменился; прежний сбор сохранён")
        selected = sorted(ranks, key=lambda r: r.rank or 10**9)[:limit]
        self.log(f"Лидерборд {period}: {len(selected)} из {limit}. Читаю swaps по всем сетям…")
        traders = []
        for index, row in enumerate(selected, 1):
            payload = await self.get(f"/v2/users/{quote(row.user_id, safe='')}/swaps")
            try:
                swaps, more = swap_rows(payload)
            except ValueError as exc:
                raise BrowserSyncError(str(exc)) from None
            if len(swaps) > 2000:
                swaps, more = swaps[:2000], True
            # Whitelist public swap fields. Never serialize session headers or
            # cookies, and never fabricate pagination parameters.
            traders.append({
                "user_id": row.user_id, "handle": row.handle,
                "display_name": row.display_name, "rank": row.rank or index,
                "has_more": more,
                "swaps": [{k: swap.get(k) for k in SWAP_FIELDS}
                          if isinstance(swap, dict) else {} for swap in swaps],
            })
            self.log(f"{index}/{len(selected)} @{row.handle or row.display_name or row.user_id}: "
                     f"{len(swaps)} swaps" + (" (есть ещё история)" if more else ""))
        return {"period": period, "requested_limit": limit, "traders": traders}

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def import_activity(http, base: str, payload: dict):
    return await local_json(http, "POST", base + "/api/fomo/activity/import", json=payload)
