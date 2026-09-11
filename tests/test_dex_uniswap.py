import json
from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.tokens import resolve_pair
from app.dex.uniswap import QuoteResult, UniswapClient, UniswapError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        payload, status = self.responses.pop(0)
        return FakeResponse(payload, status)


def quote_payload(*, routing="CLASSIC", amount_out="4500000000000000000", permit=None):
    payload = {
        "routing": routing,
        "quote": {
            "input": {"amount": "1000000000000000"},
            "output": {"amount": amount_out},
            "quoteId": "q-1",
        },
    }
    if permit is not None:
        payload["permitData"] = permit
    return payload


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "uniswap_api_key", "key-123")
    monkeypatch.setattr(settings, "uniswap_api_base", "https://api.example/v1")
    monkeypatch.setattr(settings, "rh_chain_id", 4663)
    monkeypatch.setattr(settings, "rh_universal_router_version", "2.1.1")
    monkeypatch.setattr(settings, "dex_max_slippage_pct", Decimal("0.5"))
    monkeypatch.setattr(settings, "dex_max_quote_age_seconds", 30.0)
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


async def quote_once(http) -> QuoteResult:
    client = UniswapClient(http=http)
    return await client.quote_exact_in(
        pair=resolve_pair("PONSETH"),
        side="Buy",
        amount_in_wei=1_000_000_000_000_000,
        swapper="0x" + "99" * 20,
    )


async def test_quote_pins_the_router_version_and_restricts_routing():
    http = FakeHttpClient((quote_payload(), 200))
    await quote_once(http)

    call = http.calls[0]
    assert call["url"] == "https://api.example/v1/quote"
    assert call["headers"]["x-universal-router-version"] == "2.1.1"
    assert call["headers"]["x-api-key"] == "key-123"
    assert call["json"]["protocols"] == ["V2", "V3", "V4"]
    assert call["json"]["type"] == "EXACT_INPUT"
    assert call["json"]["tokenInChainId"] == 4663


async def test_executable_price_is_derived_from_the_quoted_amounts():
    quote = await quote_once(FakeHttpClient((quote_payload(), 200)))

    # 0.001 ETH in, 4.5 PONS out.
    assert quote.price(resolve_pair("PONSETH")) == Decimal("0.001") / Decimal("4.5")


async def test_a_uniswapx_route_is_refused_because_it_is_not_signable_here():
    with pytest.raises(UniswapError) as exc:
        await quote_once(FakeHttpClient((quote_payload(routing="DUTCH_V3"), 200)))
    assert "CLASSIC" in str(exc.value)


async def test_a_zero_output_quote_is_refused():
    with pytest.raises(UniswapError):
        await quote_once(FakeHttpClient((quote_payload(amount_out="0"), 200)))


async def test_a_permit_requiring_quote_is_not_sent_unsigned():
    http = FakeHttpClient(
        (quote_payload(permit={"domain": {}, "values": {}}), 200),
        ({"swap": {"to": "0x1", "data": "0x2"}}, 200),
    )
    client = UniswapClient(http=http)
    quote = await client.quote_exact_in(
        pair=resolve_pair("PONSETH"), side="Buy",
        amount_in_wei=10**15, swapper="0x" + "99" * 20,
    )

    assert quote.needs_permit
    with pytest.raises(UniswapError) as exc:
        await client.build_swap(quote)
    assert "Permit2" in str(exc.value)
    # The swap endpoint was never called.
    assert len(http.calls) == 1


async def test_a_stale_quote_is_refused_rather_than_sent(monkeypatch):
    http = FakeHttpClient(
        (quote_payload(), 200), ({"swap": {"to": "0x1", "data": "0x2"}}, 200)
    )
    client = UniswapClient(http=http)
    quote = await client.quote_exact_in(
        pair=resolve_pair("PONSETH"), side="Buy",
        amount_in_wei=10**15, swapper="0x" + "99" * 20,
    )
    monkeypatch.setattr(settings, "dex_max_quote_age_seconds", -1.0)

    with pytest.raises(UniswapError) as exc:
        await client.build_swap(quote)
    assert "old" in str(exc.value)


async def test_build_swap_returns_the_transaction_fields():
    http = FakeHttpClient(
        (quote_payload(), 200),
        ({"swap": {"to": "0xrouter", "data": "0xdead", "value": "0x38d7ea4c68000"}}, 200),
    )
    client = UniswapClient(http=http)
    quote = await client.quote_exact_in(
        pair=resolve_pair("PONSETH"), side="Buy",
        amount_in_wei=10**15, swapper="0x" + "99" * 20,
    )
    swap = await client.build_swap(quote)

    assert swap["to"] == "0xrouter"
    assert http.calls[1]["url"].endswith("/swap")


async def test_a_swap_response_without_calldata_is_an_error():
    http = FakeHttpClient((quote_payload(), 200), ({"swap": {}}, 200))
    client = UniswapClient(http=http)
    quote = await client.quote_exact_in(
        pair=resolve_pair("PONSETH"), side="Buy",
        amount_in_wei=10**15, swapper="0x" + "99" * 20,
    )
    with pytest.raises(UniswapError):
        await client.build_swap(quote)


async def test_http_failures_carry_the_body_into_the_error():
    with pytest.raises(UniswapError) as exc:
        await quote_once(FakeHttpClient(({"detail": "bad swapper"}, 422)))
    assert "422" in str(exc.value)
    assert "bad swapper" in str(exc.value)


async def test_a_missing_api_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "uniswap_api_key", "")
    http = FakeHttpClient((quote_payload(), 200))

    with pytest.raises(UniswapError):
        await quote_once(http)
    assert http.calls == []
