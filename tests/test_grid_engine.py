from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.exchanges.bybit import InstrumentInfo
from app.trading.grid import GridEngine, classify_regime


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
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def execute(self, _statement):
        return ScalarRows(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        pass


class FakeExchange:
    def __init__(self):
        self.placed = []

    async def instrument_info(self, _symbol):
        return INFO

    async def last_price(self, _symbol):
        return Decimal("65500")

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
    )


@pytest.mark.asyncio
async def test_only_nearest_buy_is_seeded():
    exchange = FakeExchange()
    await GridEngine(exchange).seed_missing_buy_orders(FakeSession([]), profile())
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
        FakeSession([existing_sell]), profile()
    )
    assert [item["price"] for item in exchange.placed] == [Decimal("64000")]


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
    )
    exchange = FakeExchange()
    await GridEngine(exchange)._create_replacement(FakeSession([]), p, order, INFO)
    assert order.replacement_created is True
    assert order.order_role == "below_accumulation"
    assert exchange.placed == []
