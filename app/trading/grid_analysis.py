"""Single-symbol, train/test Grid analysis without look-ahead bias."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from math import sqrt
from statistics import mean, pstdev
from typing import Sequence

from app.trading.backtest import run_grid_backtest


@dataclass(frozen=True)
class GridAnalysisConfig:
    step_rates: tuple[Decimal, ...] = (
        Decimal("0.008"), Decimal("0.012"), Decimal("0.016"), Decimal("0.020")
    )
    ranges: tuple[tuple[str, Decimal, Decimal], ...] = (
        ("P10_P90", Decimal("10"), Decimal("90")),
        ("P15_P85", Decimal("15"), Decimal("85")),
    )
    min_coverage_pct: Decimal = Decimal("70")
    min_levels: int = 5
    max_levels: int = 30
    hard_max_drawdown_pct: Decimal = Decimal("35")
    fee_rate: Decimal = Decimal("0.001")
    slippage_rate: Decimal = Decimal("0.0005")
    normalized_capital: Decimal = Decimal("1000")
    profit_weight: Decimal = Decimal("8")
    drawdown_weight: Decimal = Decimal("3")
    cycles_weight: Decimal = Decimal("1.5")
    range_break_weight: Decimal = Decimal("2")
    warning_suitability: int = 50


DEFAULT_CONFIG = GridAnalysisConfig()


def percentile(values: Sequence[Decimal], pct: Decimal) -> Decimal:
    ordered = sorted(Decimal(value) for value in values)
    if not ordered:
        raise ValueError("percentile requires values")
    position = Decimal(len(ordered) - 1) * pct / Decimal("100")
    lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 0.0
    true_ranges, plus_dm, minus_dm = [], [], []
    for index in range(1, len(closes)):
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    dx = []
    for index in range(period - 1, len(true_ranges)):
        atr = mean(true_ranges[index - period + 1:index + 1])
        if atr == 0:
            dx.append(0.0)
            continue
        plus = 100 * mean(plus_dm[index - period + 1:index + 1]) / atr
        minus = 100 * mean(minus_dm[index - period + 1:index + 1]) / atr
        dx.append(100 * abs(plus - minus) / (plus + minus) if plus + minus else 0.0)
    return mean(dx[-period:]) if dx else 0.0


def market_regime(
    candles: Sequence, config: GridAnalysisConfig = DEFAULT_CONFIG
) -> dict:
    closes = [float(item.close) for item in candles]
    highs = [float(item.high) for item in candles]
    lows = [float(item.low) for item in candles]
    # Regime indicators use daily aggregation; optimization below stays on 1h.
    daily_closes = [closes[index + 23] for index in range(0, len(closes) - 23, 24)]
    daily_highs = [max(highs[index:index + 24]) for index in range(0, len(highs) - 23, 24)]
    daily_lows = [min(lows[index:index + 24]) for index in range(0, len(lows) - 23, 24)]
    ema20, ema50, ema100 = (_ema(daily_closes, period) for period in (20, 50, 100))
    adx = _adx(daily_highs, daily_lows, daily_closes)
    true_ranges = [max(daily_highs[i] - daily_lows[i], abs(daily_highs[i] - daily_closes[i - 1]), abs(daily_lows[i] - daily_closes[i - 1])) for i in range(1, len(daily_closes))]
    atr = mean(true_ranges[-14:]) if true_ranges else 0.0
    atr_pct = atr / closes[-1] * 100 if closes[-1] else 0.0
    returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes)) if closes[i - 1]]
    volatility = (pstdev(returns) * sqrt(24 * 365) * 100) if len(returns) > 1 else 0.0
    changes = {days: (closes[-1] / closes[max(0, len(closes) - days * 24)] - 1) * 100 for days in (30, 60, 90)}
    directional = abs(changes[30])
    if atr_pct >= 3.0 or volatility >= 130:
        regime = "HIGH_VOLATILITY"
    elif adx >= 25 and ema20 > ema50 > ema100:
        regime = "TREND_UP"
    elif adx >= 25 and ema20 < ema50 < ema100:
        regime = "TREND_DOWN"
    else:
        regime = "SIDEWAYS"

    # Mean-return frequency and long one-way streaks make the score explainable.
    center = _ema(closes, 20 * 24)
    mean_crosses = sum((closes[i - 1] - center) * (closes[i] - center) <= 0 for i in range(1, len(closes)))
    longest_streak = streak = 0
    last_sign = 0
    for value in returns:
        sign = 1 if value > 0 else -1 if value < 0 else 0
        streak = streak + 1 if sign and sign == last_sign else (1 if sign else 0)
        longest_streak = max(longest_streak, streak)
        last_sign = sign
    suitability = 80
    suitability -= min(35, max(0, int((adx - 18) * 1.5)))
    suitability -= min(25, int(directional * 1.2))
    suitability -= 15 if regime == "HIGH_VOLATILITY" else 0
    suitability -= min(15, max(0, longest_streak - 6))
    suitability += min(10, mean_crosses // 4)
    suitability = max(0, min(100, suitability))
    reasons = []
    if adx >= 25:
        reasons.append("Высокий ADX: выраженное направленное движение")
    else:
        reasons.append("Слабый направленный тренд")
    if regime == "TREND_DOWN":
        reasons.append("Сильный нисходящий тренд")
    elif regime == "TREND_UP":
        reasons.append("Восходящий тренд повышает риск выхода вверх")
    if atr_pct >= 3:
        reasons.append("Повышенная внутридневная волатильность")
    else:
        reasons.append("Умеренная внутридиапазонная волатильность")
    reasons.append("Цена регулярно возвращается к средней" if mean_crosses >= 4 else "Мало возвратов цены к средней")
    if longest_streak >= 12:
        reasons.append("Частые продолжительные однонаправленные движения")
    return {
        "regime": regime, "suitability": suitability,
        "warning": suitability < config.warning_suitability,
        "reasons": reasons,
        "indicators": {
            "ema20": ema20, "ema50": ema50, "ema100": ema100, "adx": adx,
            "atr": atr, "atr_pct": atr_pct, "realized_volatility": volatility,
            "price_change_30d": changes[30], "price_change_60d": changes[60],
            "price_change_90d": changes[90],
        },
    }


def candidate_score(metrics: dict, config: GridAnalysisConfig = DEFAULT_CONFIG) -> Decimal:
    profit = Decimal(metrics["net_profit_pct"])
    drawdown = Decimal(metrics["max_drawdown_pct"])
    cycles = Decimal(metrics["completed_cycles"])
    breaks = Decimal(metrics["range_break_up_count"] + metrics["range_break_down_count"])
    raw = Decimal("50") + profit * config.profit_weight - drawdown * config.drawdown_weight + min(Decimal("20"), cycles * config.cycles_weight) - breaks * config.range_break_weight
    return max(Decimal("0"), min(Decimal("100"), raw))


def _metrics(result: dict) -> dict:
    initial = Decimal(result["initial_quote"])
    profit_pct = Decimal(result["total_pnl"]) / initial * Decimal("100") if initial else Decimal("0")
    data = {
        "net_profit": result["total_pnl"], "net_profit_pct": str(profit_pct),
        "max_drawdown_pct": result["max_drawdown_pct"],
        "buy_count": result["buy_count"], "sell_count": result["sell_count"],
        "completed_cycles": result["completed_cycles"], "fees": result["fees"],
        "slippage_cost": result["slippage_cost"],
        "range_break_up_count": result["range_break_up_count"],
        "range_break_down_count": result["range_break_down_count"],
        "capital_used_avg": result["capital_used_avg"], "capital_used_max": result["capital_used_max"],
    }
    data["score"] = str(candidate_score(data).quantize(Decimal("0.01")))
    return data


def _stability(train: dict, test: dict) -> str:
    profit_gap = abs(Decimal(train["net_profit_pct"]) - Decimal(test["net_profit_pct"]))
    score_gap = Decimal(train["score"]) - Decimal(test["score"])
    if Decimal(test["net_profit_pct"]) < 0 <= Decimal(train["net_profit_pct"]) or score_gap > 25 or (train["completed_cycles"] and not test["completed_cycles"]):
        return "LOW"
    if profit_gap > 5 or score_gap > 12:
        return "MEDIUM"
    return "GOOD"


def build_candidate_specs(
    train_closes: Sequence[Decimal], config: GridAnalysisConfig = DEFAULT_CONFIG
) -> list[dict]:
    """Build every candidate from TRAIN only; TEST data is intentionally absent."""
    specs = []
    for range_type, low_pct, high_pct in config.ranges:
        low = percentile(train_closes, low_pct)
        high = percentile(train_closes, high_pct)
        coverage = Decimal(sum(low <= value <= high for value in train_closes)) / Decimal(len(train_closes)) * Decimal("100")
        for step_rate in config.step_rates:
            step = low * step_rate
            levels = int(((high - low) / step).to_integral_value(rounding=ROUND_FLOOR))
            specs.append({
                "range_type": range_type, "range_low": str(low),
                "range_high": str(high), "step_pct": str(step_rate * 100),
                "step_price": str(step), "levels": levels,
                "coverage_pct": str(coverage),
            })
    return specs


def analyze_grid(candles: Sequence, *, quote_per_level: Decimal | None = None, capital_limit: Decimal | None = None, config: GridAnalysisConfig = DEFAULT_CONFIG) -> dict:
    if len(candles) < 90 * 24:
        raise ValueError(f"need {90 * 24} hourly candles, found {len(candles)}")
    regime_candles = candles[-90 * 24:]
    optimization = candles[-30 * 24:]
    train, test = optimization[:20 * 24], optimization[20 * 24:]
    train_closes = [Decimal(item.close) for item in train]
    rejected, candidates = [], []
    for base in build_candidate_specs(train_closes, config):
        low, high, step = (Decimal(base[key]) for key in ("range_low", "range_high", "step_price"))
        levels, coverage = base["levels"], Decimal(base["coverage_pct"])
        reason = None
        if coverage < config.min_coverage_pct:
            reason = "COVERAGE_LIMIT"
        elif levels < config.min_levels or levels > config.max_levels:
            reason = "LEVELS_LIMIT"
        if reason:
            rejected.append({**base, "reason": reason})
            continue
        per_level = quote_per_level or (config.normalized_capital / Decimal(levels))
        required_capital = per_level * Decimal(levels)
        if capital_limit is not None and required_capital > capital_limit:
            reason = "CAPITAL_LIMIT"
        if reason:
            rejected.append({**base, "reason": reason})
            continue
        kwargs = dict(lower=low, upper=high, step=step, quote_per_level=per_level, fee_rate=config.fee_rate, slippage_rate=config.slippage_rate, break_down_action="continue", break_up_action="continue")
        train_result = _metrics(run_grid_backtest([item.close for item in train], timestamps_ms=[item.timestamp_ms for item in train], **kwargs))
        if not train_result["completed_cycles"] or Decimal(train_result["max_drawdown_pct"]) > config.hard_max_drawdown_pct:
            rejected.append({**base, "reason": "NO_CYCLES" if not train_result["completed_cycles"] else "HARD_DRAWDOWN_LIMIT"})
            continue
        test_result = _metrics(run_grid_backtest([item.close for item in test], timestamps_ms=[item.timestamp_ms for item in test], **kwargs))
        candidates.append({**base, "required_capital": str(required_capital), "train": train_result, "test": test_result, "score_degradation": str(Decimal(train_result["score"]) - Decimal(test_result["score"])), "stability": _stability(train_result, test_result)})
    stability_order = {"GOOD": 2, "MEDIUM": 1, "LOW": 0}
    candidates.sort(key=lambda item: (Decimal(item["test"]["score"]), stability_order[item["stability"]]), reverse=True)
    tested_count = len(candidates)
    for rank, item in enumerate(candidates[:2], 1):
        item["rank"] = rank
    return {
        "market": market_regime(regime_candles, config),
        "analysis": {"regime_window_days": 90, "train_days": 20, "test_days": 10, "timeframe": "1h", "capital_mode": "profile" if quote_per_level is not None else "normalized", "initial_capital": str(config.normalized_capital) if quote_per_level is None else None},
        "candidates": candidates[:2], "rejected_candidates": rejected,
        "candidate_counts": {"generated": len(config.ranges) * len(config.step_rates), "tested": tested_count, "rejected": len(rejected)},
        "failure_reason": "NO_VALID_GRID_CONFIGURATION" if not candidates else None,
    }
