import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.fomo.browser import (
    API_ORIGIN, BrowserCollector, BrowserSyncError, check_api,
    import_activity, validate_base,
)


class Page:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def evaluate(self, script, args):
        self.calls.append(args)
        return next(self.responses)


async def test_collects_only_selected_top_thirty_and_sends_no_credentials_to_grid():
    page = Page([
        {"status": 200, "data": {"leaderboard": [
            {"id": "bob", "rank": 2, "userHandle": "bob"},
            {"id": "alice", "rank": 1, "userHandle": "alice"},
        ]}},
        {"status": 200, "data": {"swaps": [{"id": "s1", "secret": "must-not-export"}], "hasNextPage": True}},
        {"status": 200, "data": {"swaps": [], "hasNextPage": False}},
    ])
    collector = BrowserCollector(page, sleep=AsyncMock(), log=lambda _: None)
    collector._headers = {"authorization": "Bearer keep-in-browser"}
    payload = await collector.collect()
    assert page.calls[0]["url"] == API_ORIGIN + "/v2/leaderboard/30d?limit=30"
    assert page.calls[1]["url"].endswith("/v2/users/alice/swaps")
    assert payload["traders"][0]["has_more"] is True
    assert [t["handle"] for t in payload["traders"]] == ["alice", "bob"]
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"traders": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await import_activity(http, "http://localhost:9000/grid", payload)
    assert str(captured[0].url) == "http://localhost:9000/grid/api/fomo/activity/import"
    assert "authorization" not in captured[0].headers
    assert "keep-in-browser" not in captured[0].content.decode()
    assert "must-not-export" not in json.dumps(payload)


async def test_429_retries_the_same_request_once_and_preserves_rows():
    page = Page([{"status": 429}, {"status": 200, "data": {"swaps": [1]}}])
    sleep = AsyncMock()
    collector = BrowserCollector(page, sleep=sleep, log=lambda _: None)
    assert await collector.get("/v2/users/a/swaps") == {"swaps": [1]}
    assert page.calls[0] == page.calls[1]
    sleep.assert_any_await(60)


@pytest.mark.parametrize("status", [401, 403, 430, 431, 500])
async def test_upstream_error_is_not_imported_as_an_empty_history(status):
    collector = BrowserCollector(Page([{"status": status}]), sleep=AsyncMock())
    with pytest.raises(BrowserSyncError, match=str(status)):
        await collector.get("/v2/users/a/swaps")


async def test_only_successful_requests_to_exact_fomo_api_can_supply_session():
    collector = BrowserCollector(None)
    request = SimpleNamespace(all_headers=AsyncMock(return_value={
        "authorization": "Bearer session", "cookie": "private", "x-supported-chains": "1,8453",
    }))
    for url, status in [(API_ORIGIN + ".evil.test/users", 200), (API_ORIGIN + "/users", 430)]:
        await collector.observe(SimpleNamespace(url=url, status=status, request=request))
    assert not collector._ready.is_set()
    await collector.observe(SimpleNamespace(url=API_ORIGIN + "/users", status=200, request=request))
    assert collector._ready.is_set()
    assert collector._headers == {"authorization": "Bearer session", "x-supported-chains": "1,8453"}


async def test_unreachable_local_api_fails_before_browser_launch():
    def handle(request):
        raise httpx.ConnectError("connection refused", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(BrowserSyncError, match="API недоступен"):
            await check_api(http, "http://127.0.0.1:9000")


@pytest.mark.parametrize("base", ["javascript:alert(1)", "https://user:pass@host", "http://remote.test", "https://host?secret=x"])
def test_bad_destination_is_rejected(base):
    with pytest.raises(BrowserSyncError):
        validate_base(base)


def test_remote_https_prefix_is_retained():
    assert validate_base("https://lebedeve.ru/grid/") == "https://lebedeve.ru/grid"
