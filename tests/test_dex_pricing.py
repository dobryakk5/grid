from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.dexscreener import MarketSnapshot
from app.dex.pricing import PricingError, convert_gas, implied_quote_usd
from app.dex.tokens import resolve_pair


USDG = "0x" + "ab" * 20


def snapshot(*, symbol, price_quote, price_usd="0.55") -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        observed_at_ms=0,
        price_quote=Decimal(price_quote),
        price_usd=Decimal(price_usd),
        pair_address="0xpair",
        pair_liquidity_usd=Decimal("7000000"),
        token_liquidity_usd=Decimal("7000000"),
        token_volume_h24=Decimal("6000000"),
        pools_considered=1,
    )


class FakeMarket:
    """Answers with the pool matching the requested quote token."""

    def __init__(self, *, eth_pool=True):
        self.eth_pool = eth_pool
        self.requested = []

    async def snapshot(self, pair):
        self.requested.append(pair.quote.symbol)
        if pair.quote.native:
            if not self.eth_pool:
                raise RuntimeError("no ETH pool")
            # PONS at $0.55 and 0.00022 ETH implies ETH = $2500.
            return snapshot(symbol="PONSETH", price_quote="0.00022")
        return snapshot(symbol="PONSUSDG", price_quote="0.55")


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(
        settings, "dex_tokens", f'{{"USDG": {{"address": "{USDG}", "decimals": 6}}}}'
    )
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


def test_a_stablecoin_quote_is_implied_at_about_a_dollar():
    assert implied_quote_usd(snapshot(symbol="PONSUSDG", price_quote="0.55")) == Decimal("1")


def test_an_eth_quote_implies_the_eth_price():
    assert implied_quote_usd(snapshot(symbol="PONSETH", price_quote="0.00022")) == Decimal("2500")


def test_a_pool_without_prices_cannot_imply_a_rate():
    with pytest.raises(PricingError):
        implied_quote_usd(snapshot(symbol="PONSETH", price_quote="0"))


async def test_gas_needs_no_conversion_when_the_pair_is_quoted_in_it():
    market = FakeMarket()
    cost = await convert_gas(
        market, resolve_pair("PONSETH"),
        snapshot(symbol="PONSETH", price_quote="0.00022"),
        Decimal("0.0004"),
    )

    assert cost.rate == Decimal("1")
    assert cost.quote == Decimal("0.0004")
    assert cost.quote_coin == "ETH"
    # No extra pool lookup was needed.
    assert market.requested == []


async def test_gas_in_eth_is_converted_into_the_quote_currency():
    market = FakeMarket()
    cost = await convert_gas(
        market, resolve_pair("PONSUSDG"),
        snapshot(symbol="PONSUSDG", price_quote="0.55"),
        Decimal("0.0004"),
    )

    # ETH = $2500, USDG = $1, so 0.0004 ETH is $1.
    assert cost.rate == Decimal("2500")
    assert cost.quote == Decimal("1.000")
    assert cost.quote_coin == "USDG"
    assert cost.native == Decimal("0.0004")
    assert cost.coin == "ETH"


async def test_without_an_eth_pool_the_conversion_fails_loudly():
    with pytest.raises(PricingError) as exc:
        await convert_gas(
            FakeMarket(eth_pool=False), resolve_pair("PONSUSDG"),
            snapshot(symbol="PONSUSDG", price_quote="0.55"), Decimal("0.0004"),
        )
    assert "price gas against" in str(exc.value)
