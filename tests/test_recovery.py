from decimal import Decimal
from types import SimpleNamespace

from app.trading.grid import GridEngine, confirmed_break_down, exponential_moving_average


def test_breakdown_requires_closes_below_low_and_ema():
    closes = [Decimal("100")] * 50 + [Decimal("95"), Decimal("94")]
    assert confirmed_break_down(closes, Decimal("96"), bars=2, ema_period=50)
    assert not confirmed_break_down(closes, Decimal("94"), bars=2, ema_period=50)


def test_ema_uses_only_closed_candle_values_passed_to_it():
    ema = exponential_moving_average([Decimal("10"), Decimal("10"), Decimal("20")], 2)
    assert ema == Decimal("16.66666666666666666666666667")


def test_tracking_trade_raises_trigger_from_the_latest_low():
    episode = SimpleNamespace(id=7, attempt_count=1)
    trade = GridEngine._new_tracking_trade(
        episode, source_range_id=3, price=Decimal("59000"), deviation_pct=Decimal("2"),
    )
    assert trade.status == "TRACKING"
    assert trade.lowest_price == Decimal("59000")
    assert trade.trigger_price == Decimal("60180")
