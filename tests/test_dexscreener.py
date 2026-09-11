from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.dexscreener import DexScreenerClient, DexScreenerError
from app.dex.tokens import resolve_pair


PONS = "0x39dbed3a2bd333467115de45665cc57f813c4571"
WETH = "0x" + "11" * 20
USDG = "0x" + "ab" * 20


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append(url)
        return FakeResponse(self.payload, self.status_code)


def pool(
    *,
    quote_address,
    quote_symbol,
    price_native,
    liquidity,
    volume,
    base_address=PONS,
    pair_address="0xpair",
):
    return {
        "pairAddress": pair_address,
        "dexId": "uniswap",
        "baseToken": {"address": base_address, "symbol": "PONS"},
        "quoteToken": {"address": quote_address, "symbol": quote_symbol},
        "priceNative": price_native,
        "priceUsd": "0.55",
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume},
    }


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dexscreener_cache_seconds", 10.0)
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


async def test_price_comes_from_the_pool_we_would_actually_trade(monkeypatch):
    monkeypatch.setattr(
        settings, "dex_tokens", f'{{"USDG": {{"address": "{USDG}", "decimals": 6}}}}'
    )
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="7000000", volume="6000000", pair_address="0xweth"),
        pool(quote_address=USDG, quote_symbol="USDG", price_native="0.55",
             liquidity="3000000", volume="2000000", pair_address="0xusdg"),
    ])
    client = DexScreenerClient(http=http)

    snapshot = await client.snapshot(resolve_pair("PONSUSDG"))

    # The USDG pool is the shallower one, but it is the one being traded.
    assert snapshot.price_quote == Decimal("0.55")
    assert snapshot.pair_address == "0xusdg"
    assert snapshot.pair_liquidity_usd == Decimal("3000000")
    # Risk is judged on the token across every pool, not on that one pool.
    assert snapshot.token_liquidity_usd == Decimal("10000000")
    assert snapshot.token_volume_h24 == Decimal("8000000")
    assert snapshot.pools_considered == 2


async def test_native_eth_matches_the_wrapped_pool_without_a_weth_address():
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="7000000", volume="6000000"),
    ])
    client = DexScreenerClient(http=http)

    snapshot = await client.snapshot(resolve_pair("PONSETH"))

    assert snapshot.price_quote == Decimal("0.00022")


async def test_pools_where_our_token_is_the_quote_side_are_ignored():
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="7000000", volume="6000000", base_address="0x" + "cd" * 20),
    ])
    client = DexScreenerClient(http=http)

    with pytest.raises(DexScreenerError) as exc:
        await client.snapshot(resolve_pair("PONSETH"))
    assert "as base token" in str(exc.value)


async def test_missing_quote_pool_says_what_quotes_exist(monkeypatch):
    monkeypatch.setattr(
        settings, "dex_tokens", f'{{"USDG": {{"address": "{USDG}", "decimals": 6}}}}'
    )
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="7000000", volume="6000000"),
    ])
    client = DexScreenerClient(http=http)

    with pytest.raises(DexScreenerError) as exc:
        await client.snapshot(resolve_pair("PONSUSDG"))
    assert "WETH" in str(exc.value)


async def test_zero_liquidity_pools_do_not_count_as_a_market():
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="0", volume="0"),
    ])
    client = DexScreenerClient(http=http)

    with pytest.raises(DexScreenerError):
        await client.snapshot(resolve_pair("PONSETH"))


async def test_repeated_snapshots_share_one_upstream_call():
    http = FakeHttpClient([
        pool(quote_address=WETH, quote_symbol="WETH", price_native="0.00022",
             liquidity="7000000", volume="6000000"),
    ])
    client = DexScreenerClient(http=http)
    pair = resolve_pair("PONSETH")

    await client.snapshot(pair)
    await client.snapshot(pair)

    assert len(http.calls) == 1
    assert http.calls[0].endswith(f"/token-pairs/v1/robinhood/{PONS}")


async def test_http_error_is_reported_as_a_dex_error():
    client = DexScreenerClient(http=FakeHttpClient({"error": "nope"}, status_code=503))

    with pytest.raises(DexScreenerError) as exc:
        await client.snapshot(resolve_pair("PONSETH"))
    assert "503" in str(exc.value)
