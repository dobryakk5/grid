from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.db.models import GridExecution, PositionLot
from app.trading.grid import GridEngine


class ScalarResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return iter(self.rows)


class LotSession:
    def __init__(self, scalar_values=(), rows=()):
        self.scalar_values = iter(scalar_values)
        self.rows = rows
        self.added = []

    async def scalar(self, _statement):
        return next(self.scalar_values)

    async def execute(self, _statement):
        return ScalarResult(self.rows)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        pass


@pytest.mark.asyncio
async def test_buy_execution_creates_one_net_base_lot():
    order = SimpleNamespace(
        id=10, profile_id=1, range_id=5, side="Buy", symbol="BTCUSDT",
    )
    execution = GridExecution(
        id=11, order_id=10, exec_id="buy-exec", exec_price=Decimal("64000"),
        exec_qty=Decimal("0.001"), exec_value=Decimal("64"),
        exec_fee=Decimal("0.000001"), fee_currency="BTC",
    )
    session = LotSession([None])

    lot = await GridEngine(None).ensure_position_lot_for_execution(session, order, execution)

    assert lot is not None
    assert lot.acquired_qty == Decimal("0.000999")
    assert lot.remaining_qty == Decimal("0.000999")
    assert sum(isinstance(item, PositionLot) for item in session.added) == 1


@pytest.mark.asyncio
async def test_sell_execution_consumes_parent_buy_lot_once():
    buy = SimpleNamespace(id=10, side="Buy", replacement_for=None)
    lot = PositionLot(
        id=1, profile_id=1, source_execution_id=11, origin_type="GRID",
        owner_type="GRID", owner_id=5, acquired_qty=Decimal("0.001"),
        remaining_qty=Decimal("0.001"), cost_quote=Decimal("64"),
        fees_quote=Decimal("0"), status="OPEN",
    )
    sell = SimpleNamespace(id=12, profile_id=1, side="Sell", replacement_for="buy-order")
    execution = GridExecution(
        id=13, order_id=12, exec_id="sell-exec", exec_price=Decimal("65000"),
        exec_qty=Decimal("0.0005"), exec_value=Decimal("32.5"), exec_fee=Decimal("0"),
    )
    # find_origin_buy_order -> buy; total consumption and row lookup are both zero.
    session = LotSession([buy, Decimal("0"), None], rows=[lot])

    await GridEngine(None).apply_sell_execution_to_lots(session, sell, execution)

    assert lot.remaining_qty == Decimal("0.0005")
    assert lot.status == "OPEN"


@pytest.mark.asyncio
async def test_sell_execution_closes_lot_when_the_remaining_quantity_is_sold():
    buy = SimpleNamespace(id=10, side="Buy", replacement_for=None)
    lot = PositionLot(
        id=1, profile_id=1, source_execution_id=11, origin_type="GRID",
        owner_type="GRID", owner_id=5, acquired_qty=Decimal("0.001"),
        remaining_qty=Decimal("0.0005"), cost_quote=Decimal("64"),
        fees_quote=Decimal("0"), status="OPEN",
    )
    sell = SimpleNamespace(id=12, profile_id=1, side="Sell", replacement_for="buy-order")
    execution = GridExecution(
        id=14, order_id=12, exec_id="sell-rest", exec_price=Decimal("65000"),
        exec_qty=Decimal("0.0005"), exec_value=Decimal("32.5"), exec_fee=Decimal("0"),
    )
    session = LotSession([buy, Decimal("0"), None], rows=[lot])

    await GridEngine(None).apply_sell_execution_to_lots(session, sell, execution)

    assert lot.remaining_qty == Decimal("0")
    assert lot.status == "CLOSED"
    assert lot.closed_at is not None
