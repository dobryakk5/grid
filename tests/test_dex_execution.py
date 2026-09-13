from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.dexscreener import MarketSnapshot
from app.dex.execution import execute_buy, execute_swap
from app.dex.intents import IntentStatus
from app.dex.tokens import resolve_pair
from app.dex.uniswap import QuoteResult, UniswapError


WALLET = "0x" + "99" * 20


class FakeMarket:
    def __init__(
        self, *, price="0.0002", liquidity="7000000", volume="6000000",
        symbol="PONSETH",
    ):
        self.snapshot_value = MarketSnapshot(
            symbol=symbol,
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

    def __init__(self, *, balance="1", allowance=0):
        self.balance = Decimal(balance)
        self.allowance = allowance
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

    async def token_allowance(self, token, spender, owner=None):
        self.calls.append(f"allowance:{token.symbol}")
        return self.allowance

    def sign_typed_data(self, *, domain, types, message):
        self.calls.append("sign_typed_data")
        return "0x" + "cd" * 65


class FakeUniswap:
    def __init__(self, *, amount_out="4500000000000000000", min_amount_out=None):
        self.amount_out = int(amount_out)
        # Default to no slippage, so tests that do not care about it read the
        # expected amount as the guaranteed one.
        self.min_amount_out = int(min_amount_out or amount_out)
        self.calls = []

    async def quote_exact_in(self, *, pair, side, amount_in_wei, swapper, **kwargs):
        self.calls.append("quote")
        return QuoteResult(
            raw={},
            quote={"quoteId": "q-1"},
            routing="CLASSIC",
            amount_in=amount_in_wei,
            amount_out=self.amount_out,
            min_amount_out=self.min_amount_out,
            permit_data=getattr(self, "permit_data", None),
        )

    async def build_swap(self, quote, *, signature=None):
        self.calls.append("build_swap")
        return {"to": "0xrouter", "data": "0xdead", "value": "0x0"}


class LiveSession:
    """Answers the storage preflight; never written to.

    The live-run tests here all stop before any row would be written -- they
    only need the preflight to pass so the decision behind it is reachable.
    """

    async def execute(self, *args, **kwargs):
        return None

    async def commit(self):
        pass


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
              dry_run=None, session=None, intent=None):
    return await execute_buy(
        session or (LiveSession() if dry_run is False else None),
        symbol="PONSETH",
        amount_in=Decimal(amount),
        limit_price=Decimal(limit),
        chain=chain or FakeChain(),
        uniswap=uniswap or FakeUniswap(),
        market=market or FakeMarket(),
        dry_run=dry_run,
        intent=intent,
    )


async def test_a_dry_run_quotes_but_signs_nothing():
    uniswap = FakeUniswap()
    outcome = await buy(uniswap=uniswap)

    assert outcome.status == "DRY_RUN"
    assert outcome.amount_out == Decimal("4.5")
    assert uniswap.calls == ["quote"]


async def test_a_level_armed_without_the_liquidity_gate_trades_a_thin_pool():
    """What the «без ликв» checkbox buys: the same pool, quoted instead of blocked."""
    uniswap = FakeUniswap()
    waived = type("Intent", (), {"ignore_liquidity_gate": True, "id": 1, "status": "WAITING"})()
    outcome = await buy(uniswap=uniswap, market=FakeMarket(liquidity="100000"), intent=waived)

    assert outcome.status != IntentStatus.BLOCKED, outcome.reason
    assert uniswap.calls == ["quote"], "гейт снят -- котировка должна быть запрошена"


async def test_a_level_without_the_waiver_still_blocks_on_the_same_pool():
    """The waiver is per level: an ordinary one keeps its floors."""
    ordinary = type("Intent", (), {"ignore_liquidity_gate": False, "id": 2, "status": "WAITING"})()
    outcome = await buy(market=FakeMarket(liquidity="100000"), intent=ordinary)

    assert outcome.status == IntentStatus.BLOCKED
    assert "liquidity" in outcome.reason


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


async def test_an_underfunded_wallet_blocks_a_live_swap_before_quoting():
    uniswap = FakeUniswap()
    outcome = await buy(
        chain=FakeChain(balance="0.0001"), uniswap=uniswap, dry_run=False
    )

    assert outcome.status == IntentStatus.BLOCKED
    assert "wallet holds" in outcome.reason
    assert uniswap.calls == []


async def test_token_decimals_are_verified_before_any_amount_is_computed():
    chain = FakeChain()
    await buy(chain=chain)

    assert "verify:PONS" in chain.calls


async def test_a_live_swap_without_somewhere_to_record_it_is_refused():
    from app.dex.execution import StorageUnavailable

    with pytest.raises(StorageUnavailable):
        await execute_buy(
            None,
            symbol="PONSETH",
            amount_in=Decimal("0.001"),
            limit_price=Decimal("0.00025"),
            chain=FakeChain(),
            uniswap=FakeUniswap(),
            market=FakeMarket(),
            dry_run=False,
        )


async def test_the_storage_check_runs_before_anything_irreversible():
    # The database went down mid-run once, and the failure surfaced only after
    # an approval had been sent and a transaction signed.
    from app.dex.execution import StorageUnavailable

    class DeadSession:
        async def execute(self, *args, **kwargs):
            raise OSError("Connect call failed")

    chain, uniswap = FakeChain(), FakeUniswap()
    with pytest.raises(StorageUnavailable) as exc:
        await execute_swap(
            DeadSession(),
            symbol="PONSETH",
            side="Buy",
            amount_in=Decimal("0.001"),
            limit_price=Decimal("0.00025"),
            chain=chain,
            uniswap=uniswap,
            market=FakeMarket(),
            dry_run=False,
        )

    assert "not reachable" in str(exc.value)
    # No quote, no approval, no signature.
    assert uniswap.calls == []
    assert chain.calls == []


USDG_TOKENS = '{"USDG": {"address": "0x' + "ab" * 20 + '", "decimals": 6}}'


async def buy_usdg(*, chain=None, uniswap=None, market=None, limit="0.55",
                   amount="250", dry_run=None, session=None):
    return await execute_buy(
        session or (LiveSession() if dry_run is False else None),
        symbol="PONSUSDG",
        amount_in=Decimal(amount),
        limit_price=Decimal(limit),
        chain=chain or FakeChain(balance="1000"),
        # 250 USDG for 460 PONS is 0.5435 -- inside the 0.55 limit.
        uniswap=uniswap or FakeUniswap(amount_out="460000000000000000000"),
        market=market or FakeMarket(price="0.549", symbol="PONSUSDG"),
        dry_run=dry_run,
    )


async def test_an_erc20_input_checks_its_permit2_allowance(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", USDG_TOKENS)
    chain = FakeChain(balance="1000", allowance=0)

    outcome = await buy_usdg(chain=chain)

    assert outcome.status == "DRY_RUN"
    assert "allowance:USDG" in chain.calls
    assert "would first approve USDG" in outcome.reason


async def test_a_standing_allowance_needs_no_approval(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", USDG_TOKENS)
    chain = FakeChain(balance="1000", allowance=10**30)

    outcome = await buy_usdg(chain=chain)

    assert "would first approve" not in outcome.reason


async def test_a_native_input_never_asks_about_allowances():
    chain = FakeChain()
    await buy(chain=chain)

    assert not any(call.startswith("allowance") for call in chain.calls)


async def test_a_permit_requiring_quote_is_announced_in_a_dry_run(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", USDG_TOKENS)
    uniswap = FakeUniswap(amount_out="460000000000000000000")
    uniswap.permit_data = {"domain": {}, "types": {"X": []}, "values": {"a": 1}}

    outcome = await buy_usdg(uniswap=uniswap)

    assert "Permit2 signature" in outcome.reason


async def sell(*, chain=None, uniswap=None, market=None, limit="0.00022",
               amount="100", dry_run=None, session=None):
    from app.dex.execution import execute_sell

    return await execute_sell(
        session or (LiveSession() if dry_run is False else None),
        symbol="PONSETH",
        amount_in=Decimal(amount),
        limit_price=Decimal(limit),
        chain=chain or FakeChain(balance="1000"),
        # 100 PONS for 0.023 ETH is 0.00023 -- better than the 0.00022 floor.
        uniswap=uniswap or FakeUniswap(amount_out="23000000000000000"),
        market=market or FakeMarket(price="0.00023"),
        dry_run=dry_run,
    )


async def test_a_sale_is_never_blocked_by_a_drained_pool():
    """The exit has no gate. Nothing else in this file matters more.

    The floors exist to stop us entering a market too thin to leave. Firing
    them on the way out locks the position in at exactly the moment the price
    reached our level *because* the coin is dying -- the case they were
    written for, turned against us.
    """
    uniswap = FakeUniswap(amount_out="23000000000000000")
    outcome = await sell(uniswap=uniswap,
                         market=FakeMarket(price="0.00023", liquidity="900", volume="120"))

    assert outcome.status != IntentStatus.BLOCKED, outcome.reason
    assert uniswap.calls == ["quote"], "продажа обязана быть котирована, а не отброшена"


async def test_a_sale_is_still_refused_by_a_pool_that_cannot_price_itself():
    """Not a thin market -- no market. There is nothing to execute against."""
    outcome = await sell(market=FakeMarket(price="0", liquidity="900", volume="120"))

    assert outcome.status == IntentStatus.BLOCKED
    assert "non-positive price" in outcome.reason


async def test_a_sell_quotes_when_the_price_rises_to_the_level():
    outcome = await sell()

    assert outcome.status == "DRY_RUN"
    assert outcome.side == "Sell"
    assert outcome.quoted_price == Decimal("0.00023")
    # Spent in PONS, received in ETH.
    assert outcome.amount_in == Decimal("100")
    assert outcome.amount_out == Decimal("0.023")


async def test_a_sell_below_its_floor_does_not_spend_a_quote():
    uniswap = FakeUniswap()
    outcome = await sell(uniswap=uniswap, market=FakeMarket(price="0.0001"))

    assert outcome.status == IntentStatus.WAITING
    assert uniswap.calls == []


async def test_a_sell_quote_below_the_floor_is_declined():
    # 100 PONS for 0.021 ETH is 0.00021, under the 0.00022 floor.
    outcome = await sell(uniswap=FakeUniswap(amount_out="21000000000000000"))

    assert outcome.status == IntentStatus.WAITING
    assert "worse than the limit" in outcome.reason


async def test_a_sell_approves_the_token_it_spends_not_the_one_it_receives():
    chain = FakeChain(balance="1000", allowance=0)
    outcome = await sell(chain=chain)

    assert "allowance:PONS" in chain.calls
    assert "would first approve PONS" in outcome.reason


async def test_calldata_aimed_at_an_unknown_contract_is_not_signed(monkeypatch):
    from app.dex.execution import _check_router

    monkeypatch.setattr(
        settings, "rh_universal_router_address",
        "0x8876789976decbfcbbbe364623c63652db8c0904",
    )
    # The router itself, in any casing, is fine.
    _check_router("0x8876789976DECBFCBBBE364623C63652DB8C0904")

    with pytest.raises(UniswapError) as exc:
        _check_router("0x" + "ee" * 20)
    assert "refusing to sign" in str(exc.value)


async def test_a_dry_run_quotes_even_when_the_wallet_is_empty():
    # Reporting what the swap would cost is the whole point of a dry run;
    # an unfunded wallet is a note on the result, not a reason to hide it.
    uniswap = FakeUniswap()
    outcome = await buy(chain=FakeChain(balance="0"), uniswap=uniswap)

    assert outcome.status == "DRY_RUN"
    assert uniswap.calls == ["quote"]
    assert "fund it before a live run" in outcome.reason
    assert outcome.quoted_price is not None


async def test_a_live_swap_still_stops_at_an_empty_wallet():
    uniswap = FakeUniswap()
    outcome = await buy(chain=FakeChain(balance="0"), uniswap=uniswap, dry_run=False)

    assert outcome.status == IntentStatus.BLOCKED
    assert uniswap.calls == []


async def test_the_limit_is_judged_on_the_worst_fill_not_the_expected_one():
    # Expected 4.5 PONS for 0.001 ETH is 0.000222, inside a 0.000225 limit; but
    # slippage allows as little as 4.4 PONS, which is 0.000227 -- a breach.
    uniswap = FakeUniswap(
        amount_out="4500000000000000000", min_amount_out="4400000000000000000"
    )
    outcome = await buy(uniswap=uniswap, limit="0.000225")

    assert outcome.status == IntentStatus.WAITING
    assert "worst-case fill" in outcome.reason
    # The expected price is still reported, so the log shows both.
    assert outcome.quoted_price == Decimal("0.001") / Decimal("4.5")


async def test_a_quote_whose_worst_case_still_meets_the_limit_proceeds():
    uniswap = FakeUniswap(
        amount_out="4500000000000000000", min_amount_out="4400000000000000000"
    )
    outcome = await buy(uniswap=uniswap, limit="0.00023")

    assert outcome.status == "DRY_RUN"


async def test_an_unfunded_wallet_is_reported_even_when_the_limit_declines():
    # Otherwise the funding problem is only discovered after retuning the limit
    # and running again.
    outcome = await buy(
        chain=FakeChain(balance="0"),
        uniswap=FakeUniswap(amount_out="4000000000000000000"),
        limit="0.00022",
    )

    assert outcome.status == IntentStatus.WAITING
    assert outcome.funding_note is not None
    assert "needs" in outcome.funding_note


async def test_slippage_can_be_set_per_trade():
    class RecordingUniswap(FakeUniswap):
        async def quote_exact_in(self, **kwargs):
            self.slippage = kwargs.get("slippage_pct")
            return await super().quote_exact_in(**kwargs)

    uniswap = RecordingUniswap()
    await execute_swap(
        None,
        symbol="PONSETH",
        side="Buy",
        amount_in=Decimal("0.001"),
        limit_price=Decimal("0.00025"),
        chain=FakeChain(),
        uniswap=uniswap,
        market=FakeMarket(),
        dry_run=True,
        slippage_pct=Decimal("0.1"),
    )

    assert uniswap.slippage == Decimal("0.1")


async def test_a_database_without_the_schema_fails_the_preflight_too():
    # A reachable server with no tables in it fails just as late and just as
    # expensively as an unreachable one, so the probe reads the actual table.
    from app.dex.execution import StorageUnavailable

    class EmptySchemaSession:
        def __init__(self):
            self.rolled_back = False

        async def execute(self, *args, **kwargs):
            raise RuntimeError('relation "dex_intents" does not exist')

        async def rollback(self):
            self.rolled_back = True

    session = EmptySchemaSession()
    chain, uniswap = FakeChain(), FakeUniswap()
    with pytest.raises(StorageUnavailable) as exc:
        await buy(session=session, chain=chain, uniswap=uniswap, dry_run=False)

    assert "make db-init" in str(exc.value)
    assert session.rolled_back
    assert uniswap.calls == []
    assert chain.calls == []
