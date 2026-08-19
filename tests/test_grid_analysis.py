from dataclasses import dataclass, replace
from decimal import Decimal
from math import sin

from app.trading.backtest import run_grid_backtest
from app.trading.grid_analysis import analyze_grid, build_candidate_specs, candidate_score, percentile


@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("1")


def candles(count: int = 90 * 24) -> list[Candle]:
    result = []
    for index in range(count):
        price = Decimal(str(100 + 8 * sin(index / 8) + 2 * sin(index / 37)))
        result.append(Candle(index * 3_600_000, price, price + 1, price - 1, price))
    return result


def test_percentile_interpolates_without_float_round_trip():
    assert percentile([Decimal("1"), Decimal("2"), Decimal("3")], Decimal("25")) == Decimal("1.5")


def test_analysis_creates_at_most_eight_candidates_and_returns_top_two():
    result = analyze_grid(candles())
    assert result["candidate_counts"]["generated"] == 8
    assert len(result["candidates"]) <= 2
    assert len(result["candidates"]) + len(result["rejected_candidates"]) <= 8
    assert [item["rank"] for item in result["candidates"]] == list(range(1, len(result["candidates"]) + 1))


def test_test_period_changes_do_not_change_frozen_candidate_parameters():
    baseline_candles = candles()
    changed_candles = list(baseline_candles)
    for index in range(80 * 24, 90 * 24):
        item = changed_candles[index]
        changed = item.close * (Decimal("1.15") if index % 2 else Decimal("0.85"))
        changed_candles[index] = replace(item, open=changed, high=changed + 1, low=changed - 1, close=changed)
    baseline_train = [item.close for item in baseline_candles[60 * 24:80 * 24]]
    changed_train = [item.close for item in changed_candles[60 * 24:80 * 24]]
    baseline = build_candidate_specs(baseline_train)
    changed = build_candidate_specs(changed_train)
    keys = ("range_type", "range_low", "range_high", "step_pct", "step_price", "levels")
    baseline_configs = sorted(tuple(item[key] for key in keys) for item in baseline)
    changed_configs = sorted(tuple(item[key] for key in keys) for item in changed)
    assert baseline_configs == changed_configs


def test_score_rewards_test_profit_over_train_reputation():
    weak_test = {"net_profit_pct": "-1", "max_drawdown_pct": "8", "completed_cycles": 2, "range_break_up_count": 2, "range_break_down_count": 2}
    strong_test = {"net_profit_pct": "4", "max_drawdown_pct": "3", "completed_cycles": 5, "range_break_up_count": 0, "range_break_down_count": 1}
    assert candidate_score(strong_test) > candidate_score(weak_test)


def test_slippage_is_included_in_profit_and_reported():
    prices = [Decimal("100"), Decimal("98"), Decimal("100")]
    common = dict(lower=Decimal("96"), upper=Decimal("104"), step=Decimal("2"), quote_per_level=Decimal("100"), fee_rate=Decimal("0"))
    clean = run_grid_backtest(prices, **common)
    slipped = run_grid_backtest(prices, slippage_rate=Decimal("0.001"), **common)
    assert Decimal(slipped["total_pnl"]) < Decimal(clean["total_pnl"])
    assert Decimal(slipped["slippage_cost"]) > 0


def test_profile_capital_limit_rejects_oversized_grids():
    result = analyze_grid(candles(), quote_per_level=Decimal("100"), capital_limit=Decimal("1"))
    assert result["candidates"] == []
    assert any(item["reason"] == "CAPITAL_LIMIT" for item in result["rejected_candidates"])
