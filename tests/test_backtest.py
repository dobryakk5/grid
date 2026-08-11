from decimal import Decimal

from app.trading.backtest import run_grid_backtest


def test_backtest_completes_grid_cycle_and_counts_fees():
    result = run_grid_backtest(
        [Decimal("64"), Decimal("63"), Decimal("64")],
        lower=Decimal("62"), upper=Decimal("67"), step=Decimal("1"),
        quote_per_level=Decimal("100"), fee_rate=Decimal("0.001"),
    )
    assert result["cycles"] == 1
    assert Decimal(result["realized_pnl"]) > 0
    assert Decimal(result["fees"]) > 0
    assert result["base_inventory"] == "0.00000000"


def test_backtest_marks_open_inventory_and_time_outside_range():
    result = run_grid_backtest(
        [Decimal("64"), Decimal("63"), Decimal("62"), Decimal("60")],
        lower=Decimal("62"), upper=Decimal("67"), step=Decimal("1"),
        quote_per_level=Decimal("100"), fee_rate=Decimal("0"),
    )
    assert Decimal(result["base_inventory"]) > 0
    assert Decimal(result["unrealized_pnl"]) < 0
    assert result["outside_candles"] == 1


def test_below_grid_lot_is_accumulated_unless_selling_is_enabled():
    common = dict(
        lower=Decimal("62"), upper=Decimal("67"), step=Decimal("1"),
        quote_per_level=Decimal("100"), fee_rate=Decimal("0"),
        below_grid_lower_price=Decimal("60"), buy_below_grid=True,
    )
    accumulation = run_grid_backtest(
        [Decimal("63"), Decimal("61"), Decimal("62")],
        sell_below_grid=False, **common,
    )
    grid = run_grid_backtest(
        [Decimal("63"), Decimal("61"), Decimal("62")],
        sell_below_grid=True, **common,
    )
    assert accumulation["cycles"] == 0
    assert grid["cycles"] == 1
    assert Decimal(accumulation["base_inventory"]) > Decimal(grid["base_inventory"])


def test_backtest_stops_after_two_hourly_closes_above_grid():
    result = run_grid_backtest(
        [Decimal("66"), Decimal("68"), Decimal("69"), Decimal("65")],
        lower=Decimal("62"), upper=Decimal("67"), step=Decimal("1"),
        quote_per_level=Decimal("100"), fee_rate=Decimal("0"),
        break_up_action="stop",
    )
    assert result["status"] == "STOPPED"
    assert result["stop_reason"] == "BREAK_UP"


def test_backtest_can_continue_after_breakdown():
    result = run_grid_backtest(
        [Decimal("63"), Decimal("61"), Decimal("60"), Decimal("64")],
        lower=Decimal("62"), upper=Decimal("67"), step=Decimal("1"),
        quote_per_level=Decimal("100"), fee_rate=Decimal("0"),
        break_down_action="continue",
    )
    assert result["status"] == "COMPLETED"
