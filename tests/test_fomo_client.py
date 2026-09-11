import pytest

from app.core.config import settings
from app.fomo.client import FomoAuthError, FomoClient, FomoError, FomoRateLimited


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class NonJsonResponse(FakeResponse):
    def json(self):
        raise ValueError("not json")


class FakeHttpClient:
    """Pops responses in order, so one test can script a sequence of calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        return self._responses.pop(0)


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "fomo_jwt", "test-jwt-value")
    monkeypatch.setattr(settings, "fomo_base_url", "https://prod-api.fomo.family")
    monkeypatch.setattr(settings, "fomo_supported_chains", "4663")
    monkeypatch.setattr(settings, "fomo_cache_seconds", 30.0)
    monkeypatch.setattr(settings, "fomo_backoff_start_seconds", 60.0)
    monkeypatch.setattr(settings, "fomo_backoff_max_seconds", 300.0)
    monkeypatch.setattr(settings, "fomo_leaderboard_limit", 50)


async def test_response_object_wrapper_is_unwrapped():
    http = FakeHttpClient([FakeResponse({"responseObject": {"leaderboard": [1, 2]}})])
    client = FomoClient(http=http)

    result = await client.leaderboard()

    assert result == {"leaderboard": [1, 2]}


async def test_missing_session_raises_without_touching_the_network():
    http = FakeHttpClient([])
    client = FomoClient(jwt="", http=http)

    with pytest.raises(FomoAuthError):
        await client.leaderboard()
    assert http.calls == []


async def test_401_raises_auth_error():
    http = FakeHttpClient([FakeResponse({"error": "unauthorized"}, status_code=401)])
    client = FomoClient(http=http)

    with pytest.raises(FomoAuthError):
        await client.leaderboard()


async def test_non_json_response_is_a_fomo_error():
    http = FakeHttpClient([NonJsonResponse("<html>not json</html>")])
    client = FomoClient(http=http)

    with pytest.raises(FomoError):
        await client.leaderboard()


async def test_5xx_is_a_fomo_error_with_truncated_body():
    http = FakeHttpClient([FakeResponse("x" * 500, status_code=500)])
    client = FomoClient(http=http)

    with pytest.raises(FomoError) as exc:
        await client.leaderboard()
    assert len(str(exc.value)) < 300


async def test_repeated_calls_share_one_upstream_request():
    http = FakeHttpClient([FakeResponse({"leaderboard": []})])
    client = FomoClient(http=http)

    await client.leaderboard()
    await client.leaderboard()

    assert len(http.calls) == 1


async def test_429_without_a_cached_response_raises_rate_limited():
    http = FakeHttpClient([FakeResponse({"error": "slow down"}, status_code=429)])
    client = FomoClient(http=http)

    with pytest.raises(FomoRateLimited):
        await client.leaderboard()


async def test_429_after_a_cached_success_serves_the_cache_instead(monkeypatch):
    # Cache_seconds=0 so the second call would otherwise reach the network.
    monkeypatch.setattr(settings, "fomo_cache_seconds", 0.0)
    http = FakeHttpClient([
        FakeResponse({"leaderboard": ["ok"]}),
        FakeResponse({"error": "slow down"}, status_code=429),
    ])
    client = FomoClient(http=http)

    first = await client.leaderboard()
    second = await client.leaderboard()

    assert first == second == {"leaderboard": ["ok"]}
    assert len(http.calls) == 2


async def test_second_call_during_an_active_backoff_window_does_not_hit_the_network():
    http = FakeHttpClient([FakeResponse({"error": "slow"}, status_code=429)])
    client = FomoClient(http=http)

    with pytest.raises(FomoRateLimited):
        await client.leaderboard()
    with pytest.raises(FomoRateLimited):
        await client.leaderboard()

    assert len(http.calls) == 1


async def test_headers_carry_the_configured_session_and_chains():
    http = FakeHttpClient([FakeResponse({"leaderboard": []})])
    client = FomoClient(http=http)

    await client.leaderboard()

    headers = http.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer test-jwt-value"
    assert headers["X-Supported-Chains"] == "4663"


async def test_explicit_jwt_overrides_settings():
    http = FakeHttpClient([FakeResponse({"leaderboard": []})])
    client = FomoClient(jwt="pasted-token", http=http)

    await client.leaderboard()

    assert http.calls[0]["headers"]["Authorization"] == "Bearer pasted-token"


async def test_error_messages_never_leak_the_jwt():
    http = FakeHttpClient([FakeResponse({"error": "nope"}, status_code=401)])
    client = FomoClient(http=http)

    with pytest.raises(FomoAuthError) as exc:
        await client.leaderboard()
    assert "test-jwt-value" not in str(exc.value)


async def test_holders_encodes_the_tokens_query_param():
    http = FakeHttpClient([FakeResponse([{"topHolders": []}])])
    client = FomoClient(http=http)

    await client.holders("0xabc", 4663)

    params = http.calls[0]["params"]
    assert params["tokens"] == '[{"address": "0xabc", "networkId": 4663}]'
