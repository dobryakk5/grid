from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

import pytest

from app.exchanges.base import ExchangeError, OrderNotCancellable
from app.exchanges.bybit import InstrumentInfo
from app.trading.grid import (
    CANCELLABLE_STATUSES,
    OPEN_STATUSES,
    GridEngine,
    SYNC_STATUSES,
    classify_regime,
)


INFO = InstrumentInfo(
    symbol="BTCUSDT",
    base_coin="BTC",
    quote_coin="USDT",
    tick_size=Decimal("1"),
    base_precision=Decimal("0.000001"),
    min_order_amt=Decimal("5"),
)


class ScalarRows:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return iter(self.rows)


class FakeSession:
    def __init__(self, rows, grid_range=None, scalar_result=None):
        self.rows = rows
        self.added = []
        self.grid_range = grid_range
        self.scalar_result = scalar_result

    async def execute(self, _statement):
        return ScalarRows(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def get(self, _model, _id):
        return self.grid_range

    async def scalar(self, _statement):
        return self.scalar_result

    async def commit(self):
        pass


class FakeExchange:
    def __init__(self, balance=Decimal("1000000")):
        self.placed = []
        self.balance = balance

    async def instrument_info(self, _symbol):
        return INFO

    async def last_price(self, _symbol):
        return Decimal("65500")

    async def available_balance(self, _coin):
        return self.balance

    async def place_limit_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"result": {"orderId": f"order-{len(self.placed)}"}}


def profile():
    return SimpleNamespace(
        id=1,
        symbol="BTCUSDT",
        strategy="accumulation",
        lower_price=Decimal("62000"),
        upper_price=Decimal("67000"),
        step_price=Decimal("1000"),
        quote_per_level=Decimal("25"),
        grid_mode="arithmetic",
        step_percent=None,
        current_range_id=5,
    )


def current_range():
    return SimpleNamespace(
        id=5, profile_id=1, lower_price=Decimal("62000"),
        upper_price=Decimal("67000"), step_price=Decimal("1000"),
        grid_mode="arithmetic", step_percent=None, status="ACTIVE",
    )


@pytest.mark.asyncio
async def test_only_nearest_buy_is_seeded():
    exchange = FakeExchange()
    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), profile()
    )
    assert [item["price"] for item in exchange.placed] == [Decimal("65000")]


@pytest.mark.asyncio
async def test_next_buy_arms_after_previous_cell_has_sell():
    existing_sell = SimpleNamespace(
        grid_buy_price=Decimal("65000"),
        side="Sell",
        status="New",
    )
    exchange = FakeExchange()
    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([existing_sell], current_range()), profile()
    )
    assert [item["price"] for item in exchange.placed] == [Decimal("64000")]


@pytest.mark.asyncio
async def test_paused_range_cannot_seed_new_grid_buy():
    paused = current_range()
    paused.status = "PAUSED"
    exchange = FakeExchange()
    await GridEngine(exchange).seed_missing_buy_orders(FakeSession([], paused), profile())
    assert exchange.placed == []


def test_cancel_requested_orders_remain_in_sync_set():
    assert "CancelRequestedBreakdown" in SYNC_STATUSES
    assert "CancelRequestedByUser" in SYNC_STATUSES


def test_breakout_requires_two_hourly_closes():
    lower, upper = Decimal("62000"), Decimal("67000")
    assert classify_regime([Decimal("61900")], lower, upper) is None
    assert classify_regime([Decimal("62100"), Decimal("61900")], lower, upper) is None
    assert classify_regime([Decimal("61900"), Decimal("61000")], lower, upper) == "BREAK_DOWN"
    assert classify_regime([Decimal("67100"), Decimal("68000")], lower, upper) == "BREAK_UP"
    assert classify_regime([Decimal("63000"), Decimal("64000")], lower, upper) == "RANGE"


@pytest.mark.asyncio
async def test_below_grid_buy_can_be_kept_without_sell_order():
    p = profile()
    p.below_grid_lower_price = Decimal("55000")
    p.buy_below_grid = True
    p.sell_below_grid = False
    order = SimpleNamespace(
        side="Buy", filled_qty=Decimal("0.001"), qty=Decimal("0.001"),
        grid_buy_price=Decimal("61000"), order_role="below_grid",
        replacement_created=False,
        range_id=5,
    )
    exchange = FakeExchange()
    await GridEngine(exchange)._create_replacement(
        FakeSession([], current_range()), p, order, INFO
    )
    assert order.replacement_created is True
    assert order.order_role == "below_accumulation"
    assert exchange.placed == []


# ---- USDG fee accounting (app/exchanges/base.py split_symbol) -------------


async def test_a_quote_coin_fee_beyond_usdt_usdc_still_counts_toward_fees_quote():
    # Before split_symbol, only {"USDT", "USDC"} read as a quote fee -- a DEX
    # gas fee paid in USDG silently vanished from cost_quote/fees_quote.
    order = SimpleNamespace(
        id=1, side="Buy", range_id=5, order_role="grid",
        profile_id=1, symbol="PONSUSDG",
    )
    execution = SimpleNamespace(
        id=10, exec_qty=Decimal("460"), exec_fee=Decimal("1"),
        exec_value=Decimal("250"), fee_currency="USDG",
    )
    lot = await GridEngine(FakeExchange()).ensure_position_lot_for_execution(
        FakeSession([]), order, execution
    )

    assert lot.fees_quote == Decimal("1")
    assert lot.cost_quote == Decimal("251")
    # The fee was paid in the quote coin, not PONS, so acquired qty is untouched.
    assert lot.acquired_qty == Decimal("460")


# ---- on-chain execution fields reach GridExecution -------------------------


class ExecutionsExchange(FakeExchange):
    def __init__(self, executions):
        super().__init__()
        self._executions = executions

    async def get_executions(self, *, order_id, symbol):
        return self._executions


async def test_sync_order_executions_carries_native_gas_onto_grid_execution():
    remote = [{
        "execId": "0x" + "ab" * 32,
        "execPrice": "0.5434",
        "execQty": "460",
        "execValue": "250",
        "execFee": "1",
        "feeCurrency": "USDG",
        "isMaker": False,
        "execTime": 1700000000000,
        "feeNativeAmount": "0.000022",
        "feeNativeCoin": "ETH",
        "txHash": "0x" + "ab" * 32,
    }]
    order = SimpleNamespace(
        id=1, side="Buy", range_id=None, order_role="grid",
        exchange_order_id="7", symbol="PONSUSDG",
    )
    session = FakeSession([])

    inserted = await GridEngine(ExecutionsExchange(remote)).sync_order_executions(
        session, order
    )

    assert inserted == 1
    execution = session.added[0]
    assert execution.fee_native_amount == Decimal("0.000022")
    assert execution.fee_native_coin == "ETH"
    assert execution.tx_hash == "0x" + "ab" * 32


async def test_sync_order_executions_leaves_native_gas_null_when_the_venue_omits_it():
    # Bybit and MEXC executions carry no feeNativeAmount/feeNativeCoin/txHash
    # keys at all; the on-chain-only fields must not be fabricated for them.
    remote = [{
        "execId": "ex-1", "execPrice": "65000", "execQty": "0.001",
        "execValue": "65", "execFee": "0.065", "feeCurrency": "USDT",
        "isMaker": True, "execTime": 1700000000000,
    }]
    order = SimpleNamespace(
        id=1, side="Buy", range_id=None, order_role="grid",
        exchange_order_id="7", symbol="BTCUSDT",
    )
    session = FakeSession([])

    await GridEngine(ExecutionsExchange(remote)).sync_order_executions(session, order)

    execution = session.added[0]
    assert execution.fee_native_amount is None
    assert execution.fee_native_coin is None
    assert execution.tx_hash is None


# ---- idempotency: a "Created" row with no exchange_order_id yet -----------


class LinkLookupExchange(FakeExchange):
    def __init__(self, remote_by_link=None, remote_by_id=None):
        super().__init__()
        self._by_link = remote_by_link or {}
        self._by_id = remote_by_id or {}

    async def get_order_by_link_id(self, *, order_link_id, symbol):
        return self._by_link.get(order_link_id)

    async def get_order(self, *, order_id, symbol):
        return self._by_id.get(order_id)


def _created_order(**overrides):
    fields = dict(
        id=1, profile_id=1, symbol="PONSUSDG",
        exchange_order_id=None, order_link_id="g1-abc123",
        status="Created", filled_qty=None, avg_price=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_sync_open_orders_adopts_a_row_the_venue_did_receive_before_a_restart():
    # 7A: place_limit_order committed the DexIntent and returned an id, but the
    # process died before that id reached this row.
    order = _created_order()
    remote = {"orderId": "9", "orderStatus": "New", "cumExecQty": "0", "avgPrice": ""}
    exchange = LinkLookupExchange(remote_by_link={"g1-abc123": remote})

    await GridEngine(exchange).sync_open_orders(FakeSession([order]), profile())

    assert order.exchange_order_id == "9"
    assert order.status == "New"


async def test_sync_open_orders_frees_the_cell_when_the_venue_never_saw_it():
    # 7B: the row was committed but the process died before place_limit_order
    # was even called -- the venue has no record of this link id at all.
    order = _created_order()
    exchange = LinkLookupExchange()

    await GridEngine(exchange).sync_open_orders(FakeSession([order]), profile())

    assert order.status == "Rejected"
    assert order.exchange_order_id is None


# ---- STOP semantics for an already-broadcast order -------------------------


class RefusingExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.cancel_calls = 0

    async def cancel_order(self, *, order_id, symbol):
        self.cancel_calls += 1
        raise OrderNotCancellable(f"level {order_id} already broadcast")


def test_cancel_refused_orders_stay_open_but_are_never_asked_to_cancel_again():
    assert "CancelRefused" in OPEN_STATUSES
    assert "CancelRefused" in SYNC_STATUSES
    assert "CancelRefused" not in CANCELLABLE_STATUSES


async def test_a_refused_cancel_is_recorded_once_not_raised():
    order = SimpleNamespace(
        id=1, profile_id=1, symbol="PONSUSDG",
        exchange_order_id="42", status="SUBMITTING",
    )
    exchange = RefusingExchange()

    await GridEngine(exchange).cancel_orders(FakeSession([order]), profile_id=1)

    assert order.status == "CancelRefused"
    assert exchange.cancel_calls == 1


async def test_sync_open_orders_keeps_a_refused_cancel_while_the_swap_is_in_flight():
    order = SimpleNamespace(
        id=1, profile_id=1, symbol="PONSUSDG",
        exchange_order_id="42", order_link_id="g1-abc",
        status="CancelRefused", filled_qty=None, avg_price=None,
    )
    remote = {"orderId": "42", "orderStatus": "New", "cumExecQty": "0", "avgPrice": ""}
    exchange = LinkLookupExchange(remote_by_id={"42": remote})

    await GridEngine(exchange).sync_open_orders(FakeSession([order]), profile())

    # Still in flight ("New"): the refusal stands, it must not bounce back to
    # New and become eligible for another cancel attempt next tick.
    assert order.status == "CancelRefused"


class CancellingExchange(FakeExchange):
    def __init__(self):
        super().__init__()
        self.cancelled = []

    async def cancel_order(self, *, order_id, symbol):
        self.cancelled.append(order_id)


async def test_enforce_single_open_buy_keeps_the_signed_order_over_the_freshest():
    # A signed BUY cannot be superseded (cancel is refused); re-enabling the
    # profile while it is still in flight must not seed a second BUY above it.
    signed = SimpleNamespace(
        id=1, side="Buy", order_role="grid", status="CancelRefused",
        price=Decimal("64000"), exchange_order_id="1", symbol="BTCUSDT",
    )
    fresh = SimpleNamespace(
        id=2, side="Buy", order_role="grid", status="New",
        price=Decimal("65000"), exchange_order_id="2", symbol="BTCUSDT",
    )
    exchange = CancellingExchange()

    await GridEngine(exchange).enforce_single_open_buy(
        FakeSession([signed, fresh], current_range()), profile()
    )

    assert exchange.cancelled == ["2"]
    assert fresh.status == "CancelledSuperseded"
    assert signed.status == "CancelRefused"


async def test_seed_missing_buy_orders_does_not_seed_while_a_signed_buy_is_pending():
    # Without this guard, seeding a fresh cell above the signed BUY would get
    # cancelled by enforce_single_open_buy next tick, land in retry_statuses,
    # and reseed here -- forever.
    signed = SimpleNamespace(
        id=1, side="Buy", grid_buy_price=Decimal("65000"), status="CancelRefused",
    )
    exchange = FakeExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([signed], current_range()), profile()
    )

    assert exchange.placed == []


@pytest.mark.asyncio
async def test_a_martingale_grid_sizes_a_cell_by_its_distance_from_the_middle():
    # 62000..67000 in five cells: the middle cell buys at 64000, and 65000 is
    # one step above it, so it commits one multiplier's worth more.
    martingale = profile()
    martingale.level_size_multiplier = Decimal("1.2")
    exchange = FakeExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), martingale
    )

    placed = exchange.placed[0]
    assert placed["price"] == Decimal("65000")
    expected = (Decimal("25") * Decimal("1.2") / Decimal("65000")).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    assert placed["qty"] == expected


@pytest.mark.asyncio
async def test_without_a_multiplier_every_cell_still_buys_quote_per_level():
    exchange = FakeExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), profile()
    )

    expected = (Decimal("25") / Decimal("65000")).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    assert exchange.placed[0]["qty"] == expected


@pytest.mark.asyncio
async def test_a_level_the_wallet_cannot_fund_is_not_placed():
    exchange = FakeExchange(balance=Decimal("10"))

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), profile()
    )

    assert exchange.placed == []


@pytest.mark.asyncio
async def test_a_declared_budget_counts_what_the_range_already_holds():
    # One cell is long for 25 quote and the ceiling is 40, so the next level's
    # 25 no longer fits even though the wallet itself is full.
    long_cell = SimpleNamespace(
        grid_buy_price=Decimal("66000"), side="Buy", status="Filled",
        qty=Decimal("0.000378"), filled_qty=Decimal("0.000378"),
        price=Decimal("66000"), order_role="grid", replacement_created=True,
    )
    capped = profile()
    capped.max_investment = Decimal("40")
    exchange = FakeExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([long_cell], current_range()), capped
    )

    assert exchange.placed == []


@pytest.mark.asyncio
async def test_a_declared_budget_with_room_left_still_seeds():
    capped = profile()
    capped.max_investment = Decimal("500")
    exchange = FakeExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), capped
    )

    assert [item["price"] for item in exchange.placed] == [Decimal("65000")]


@pytest.mark.asyncio
async def test_an_unreadable_balance_leaves_the_cell_unarmed():
    class BlindExchange(FakeExchange):
        async def available_balance(self, _coin):
            raise ExchangeError("rpc is rate limited")

    exchange = BlindExchange()

    await GridEngine(exchange).seed_missing_buy_orders(
        FakeSession([], current_range()), profile()
    )

    assert exchange.placed == []


# ---- an order the venue has forgotten -------------------------------------


def _missing_order(status, age_days):
    from datetime import datetime, timedelta, timezone

    return SimpleNamespace(
        id=45, profile_id=1, symbol="XRPUSDT",
        exchange_order_id="2285715138592115968", order_link_id="g3-x",
        status=status, filled_qty=None, avg_price=None,
        updated_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


async def test_a_long_requested_cancel_the_venue_forgot_is_settled():
    """The disk-filler. Three orders we asked Bybit demo to cancel in August
    were no longer in its history, so every tick asked about them again and
    warned again -- 300k lines in five days. A cancel we requested, that the
    venue no longer knows at all, is done."""
    order = _missing_order("CancelRequestedByUser", age_days=30)

    await GridEngine(LinkLookupExchange()).sync_open_orders(FakeSession([order]), profile())

    assert order.status == "CancelledByUser"


async def test_a_fresh_cancel_is_not_settled_on_one_missing_answer():
    # Minutes after the request, "not found" may just be the venue catching up.
    order = _missing_order("CancelRequested", age_days=0)

    await GridEngine(LinkLookupExchange()).sync_open_orders(FakeSession([order]), profile())

    assert order.status == "CancelRequested"


async def test_a_live_order_that_goes_missing_is_never_closed_by_guesswork():
    # Unlike a cancel we asked for, a live order that vanishes may have
    # filled. Closing it would lose the fill; it stays for a human.
    order = _missing_order("New", age_days=30)

    await GridEngine(LinkLookupExchange()).sync_open_orders(FakeSession([order]), profile())

    assert order.status == "New"


async def test_a_disabled_profile_keeps_what_the_sync_learned(monkeypatch):
    """Nothing after the sync commits for a disabled profile with nothing to
    cancel, and the worker's session rolls back on close. So its sync -- a
    settled cancel, or a fill -- was thrown away and redone every tick."""
    from app.trading import grid as grid_module

    calls = []
    disabled = SimpleNamespace(id=4, name="xrp", enabled=False, regime_state="RANGE", exchange="bybit")

    class Session(FakeSession):
        async def commit(self):
            calls.append("commit")

    engine = GridEngine(FakeExchange())

    async def noop(*_args, **_kwargs):
        return None

    async def sync(_session, _profile):
        calls.append("sync")

    async def cancel(_session, _profile_id):
        calls.append("cancel")

    async def no_expired(*_args, **_kwargs):
        return []

    monkeypatch.setattr(engine, "_select_exchange_for", lambda _p: None)
    monkeypatch.setattr(engine, "backfill_filled_executions", noop)
    monkeypatch.setattr(engine, "ensure_current_range", noop)
    monkeypatch.setattr(engine, "sync_open_orders", sync)
    monkeypatch.setattr(engine, "cancel_open_orders", cancel)
    monkeypatch.setattr(grid_module, "expire_recommendations", no_expired)

    await engine.tick(Session([disabled]))

    assert calls[:2] == ["sync", "commit"], calls
