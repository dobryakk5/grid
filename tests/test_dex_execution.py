from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.dexscreener import MarketSnapshot
from app.dex.execution import execute_buy
from app.dex.intents import IntentStatus
from app.dex.tokens import resolve_pair
from app.dex.uniswap import QuoteResult


WALLET = "0x" + "99" * 20


class FakeMarket:
    def __init__(self, *, price="0.0002", liquidity="7000000", volume="6000000"):
        self.snapshot_value = MarketSnapshot(
            symbol="PONSETH",
            observed_at_ms=0,
            price_quote=Decimal(price),
            price_usd=Decimal("0.55"),
            pair_address="0xpair",
            pair_liquidity_usd=Decimal(liquidity),
            token_liquidity_usd=Decimal(liquidity),
            token_volume_h24=Decimal(volume),
            pools_considered=1,
        )

    async def snapshot(self, pair):
        return self.snapshot_value


class FakeChain:
    chain_id = 4663

    def __init__(self, *, balance="1"):
        self.balance = Decimal(balance)
        self.calls = []

    @property
    def wallet_address(self):
        return WALLET

    async def ensure_ready(self):
        self.calls.append("ensure_ready")

    async def verify_token(self, token):
        self.calls.append(f"verify:{token.symbol}")

    async def native_balance(self, address=None):
        return self.balance

    async def token_balance(self, token, address=None):
        return self.balance


class FakeUniswap:
    def __init__(self, *, amount_out="4500000000000000000"):
        self.amount_out = int(amount_out)
        self.calls = []

    async def quote_exact_in(self, *, pair, side, amount_in_wei, swapper, **kwargs):
        self.calls.append("quote")
        return QuoteResult(
            raw={},
            quote={"quoteId": "q-1"},
            routing="CLASSIC",
            amount_in=amount_in_wei,
            amount_out=self.amount_out,
            permit_data=None,
        )

    async def build_swap(self, quote, *, signature=None):
        self.calls.append("build_swap")
        return {"to": "0xrouter", "data": "0xdead", "value": "0x0"}


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))
    monkeypatch.setattr(settings, "dex_min_liquidity_usd", Decimal("5000000"))
    monkeypatch.setattr(settings, "dex_min_volume_h24_usd", Decimal("1000000"))
    monkeypatch.setattr(settings, "dex_quote_trigger_band_pct", Decimal("1"))
    monkeypatch.setattr(settings, "dex_dry_run", True)
    monkeypatch.setattr(settings, "rh_chain_id", 4663)


async def buy(*, chain=None, uniswap=None, market=None, limit="0.00025", amount="0.001",
              dry_run=None):
    return await execute_buy(
        None,
        symbol="PONSETH",
        amount_in=Decimal(amount),
        limit_price=Decimal(limit),
        chain=chain or FakeChain(),
        uniswap=uniswap or FakeUniswap(),
        market=market or FakeMarket(),
        dry_run=dry_run,
    )


async def test_a_dry_run_quotes_but_signs_nothing():
    uniswap = FakeUniswap()
    outcome = await buy(uniswap=uniswap)

    assert outcome.status == "DRY_RUN"
    assert outcome.amount_out == Decimal("4.5")
    assert uniswap.calls == ["quote"]


async def test_a_collapsing_pool_blocks_before_the_chain_is_touched():
    chain = FakeChain()
    uniswap = FakeUniswap()
    outcome = await buy(
        chain=chain, uniswap=uniswap, market=FakeMarket(liquidity="100000")
    )

    assert outcome.status == IntentStatus.BLOCKED
    assert "liquidity" in outcome.reason
    assert chain.calls == []
    assert uniswap.calls == []


async def test_a_price_far_from_the_level_does_not_spend_a_quote():
    uniswap = FakeUniswap()
    outcome = await buy(
        uniswap=uniswap, market=FakeMarket(price="0.0009"), limit="0.0002"
    )

    assert outcome.status == IntentStatus.WAITING
    assert uniswap.calls == []


async def test_a_price_just_above_the_level_still_gets_quoted():
    # Within the trigger band: worth asking what we could really fill at.
    uniswap = FakeUniswap()
    outcome = await buy(
        uniswap=uniswap, market=FakeMarket(price="0.000201"), limit="0.0002"
    )

    assert uniswap.calls == ["quote"]
    assert outcome.status in {"DRY_RUN", IntentStatus.WAITING}


async def test_a_quote_worse_than_the_limit_does_not_become_a_market_order():
    # 0.001 ETH for 4.0 PONS is 0.00025/PONS, worse than the 0.00022 limit.
    uniswap = FakeUniswap(amount_out="4000000000000000000")
    outcome = await buy(uniswap=uniswap, limit="0.00022", dry_run=False)

    assert outcome.status == IntentStatus.WAITING
    assert "worse than the limit" in outcome.reason
    assert uniswap.calls == ["quote"]


async def test_an_underfunded_wallet_blocks_before_quoting():
    uniswap = FakeUniswap()
    outcome = await buy(chain=FakeChain(balance="0.0001"), uniswap=uniswap)

    assert outcome.status == IntentStatus.BLOCKED
    assert "wallet holds" in outcome.reason
    assert uniswap.calls == []


async def test_token_decimals_are_verified_before_any_amount_is_computed():
    chain = FakeChain()
    await buy(chain=chain)

    assert "verify:PONS" in chain.calls


async def test_a_live_swap_without_a_session_is_refused():
    from app.dex.chain import ChainError

    with pytest.raises(ChainError):
        await buy(dry_run=False)
