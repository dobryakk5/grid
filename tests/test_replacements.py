from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.exchanges.bybit import InstrumentInfo
from app.trading.grid import GridEngine


INFO = InstrumentInfo(
    symbol="BTCUSDT", base_coin="BTC", quote_coin="USDT",
    tick_size=Decimal("1"), base_precision=Decimal("0.000001"),
    min_order_amt=Decimal("5"),
)


class Session:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def get(self, _model, _id):
        return None


class Exchange:
    async def place_limit_order(self, **_kwargs):
        raise AssertionError("stale range must not place an order")


@pytest.mark.asyncio
async def test_old_range_cannot_create_a_replacement():
    session = Session()
    profile = SimpleNamespace(id=1, current_range_id=5)
    order = SimpleNamespace(
        id=42, order_role="grid", range_id=4, side="Buy",
        filled_qty=Decimal("0.001"), qty=Decimal("0.001"),
        grid_buy_price=Decimal("64000"),
    )

    await GridEngine(Exchange())._create_replacement(session, profile, order, INFO)

    assert not getattr(order, "replacement_created", False)
    assert session.added[0].reason == "REPLACEMENT_BLOCKED_STALE_RANGE"
