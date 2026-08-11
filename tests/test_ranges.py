from decimal import Decimal
from types import SimpleNamespace

from app.exchanges.bybit import InstrumentInfo
from app.trading.grid import GridEngine


INFO = InstrumentInfo(
    symbol="BTCUSDT", base_coin="BTC", quote_coin="USDT",
    tick_size=Decimal("1"), base_precision=Decimal("0.000001"),
    min_order_amt=Decimal("5"),
)


def test_range_cells_read_prices_from_range_not_profile_cache():
    profile = SimpleNamespace(
        below_grid_lower_price=None, buy_below_grid=True,
        lower_price=Decimal("10"), upper_price=Decimal("20"),
    )
    grid_range = SimpleNamespace(
        lower_price=Decimal("100"), upper_price=Decimal("130"),
        step_price=Decimal("10"), grid_mode="arithmetic", step_percent=None,
    )

    assert GridEngine.range_cells(profile, grid_range, INFO) == [
        (Decimal("100"), Decimal("110")),
        (Decimal("110"), Decimal("120")),
        (Decimal("120"), Decimal("130")),
    ]
