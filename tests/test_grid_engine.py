from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.exchanges.bybit import InstrumentInfo
from app.trading.grid import GridEngine


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
