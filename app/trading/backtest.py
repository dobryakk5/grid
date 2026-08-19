from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from app.trading.math import grid_buy_levels


@dataclass
class Lot:
    qty: Decimal
    cost: Decimal
    buy_price: Decimal
    buy_fee: Decimal
    bought_at_ms: int | None


def run_grid_backtest(
    closes: Iterable[Decimal], *, lower: Decimal, upper: Decimal,
    step: Decimal, quote_per_level: Decimal, fee_rate: Decimal,
    below_grid_lower_price: Decimal | None = None,
    buy_below_grid: bool = True, sell_below_grid: bool = False,
    timestamps_ms: Iterable[int] | None = None, candle_minutes: int = 60,
    break_down_action: str = "continue", break_up_action: str = "stop",
    slippage_rate: Decimal = Decimal("0"),
) -> dict:
    """Backtest a simple long-only grid on close-to-close level crossings.

    Close-only execution is intentionally conservative: it does not claim fills
    for an intrahour wick whose ordering cannot be known from OHLC data.
    """
    prices = [Decimal(price) for price in closes]
    if len(prices) < 2:
        raise ValueError("backtest requires at least two closes")
    timestamps = list(timestamps_ms) if timestamps_ms is not None else None
    if timestamps is not None and len(timestamps) != len(prices):
        raise ValueError("timestamps and closes must have equal length")
    main_levels = grid_buy_levels(lower, upper, step)
    below_levels = (
        grid_buy_levels(below_grid_lower_price, lower, step)
        if buy_below_grid and below_grid_lower_price is not None else []
    )
    levels = below_levels + main_levels
    sell_by_buy = {buy: buy + step for buy in levels}
    lots: dict[Decimal, Lot] = {}
    initial_quote = quote_per_level * Decimal(len(levels))
    quote = initial_quote
    realized = Decimal("0")
    fees = Decimal("0")
    slippage_cost = Decimal("0")
    buy_count = 0
    sell_count = 0
    cycles = 0
    trades: list[dict] = []
    outside = 0
    peak_equity = initial_quote
    max_drawdown = Decimal("0")
    capital_used_total = Decimal("0")
    capital_used_max = Decimal("0")
    range_break_up_count = 0
    range_break_down_count = 0
    first_is_hour_close = (
        timestamps is None
        or ((timestamps[0] // 60_000) + candle_minutes) % 60 == 0
    )
    hourly_closes: list[Decimal] = [prices[0]] if first_is_hour_close else []
    trading_active = True
    stop_reason: str | None = None
    stopped_at_ms: int | None = None

    previous = prices[0]
    for index, current in enumerate(prices[1:], start=1):
        if current > upper and previous <= upper:
            range_break_up_count += 1
        if current < lower and previous >= lower:
            range_break_down_count += 1
        if current < lower or current > upper:
            outside += 1

        if trading_active and current < previous:
            crossed = [level for level in reversed(levels) if current <= level < previous]
            for buy in crossed:
                if buy in lots or quote < quote_per_level:
                    continue
                execution_price = buy * (Decimal("1") + slippage_rate)
                fee = quote_per_level * fee_rate
                qty = (quote_per_level - fee) / execution_price
                slippage_cost += qty * (execution_price - buy)
                bought_at_ms = (
                    timestamps[index] + candle_minutes * 60_000
                    if timestamps is not None else None
                )
                lots[buy] = Lot(
                    qty=qty,
                    cost=quote_per_level,
                    buy_price=execution_price,
                    buy_fee=fee,
                    bought_at_ms=bought_at_ms,
                )
                quote -= quote_per_level
                fees += fee
                buy_count += 1
        elif trading_active and current > previous:
            crossed = [level for level in levels if previous < sell_by_buy[level] <= current]
            for buy in crossed:
                if buy < lower and not sell_below_grid:
                    continue
                lot = lots.pop(buy, None)
                if lot is None:
                    continue
                target_price = sell_by_buy[buy]
                execution_price = target_price * (Decimal("1") - slippage_rate)
                proceeds_before_fee = lot.qty * execution_price
                slippage_cost += lot.qty * (target_price - execution_price)
                fee = proceeds_before_fee * fee_rate
                proceeds = proceeds_before_fee - fee
                quote += proceeds
                trade_pnl = proceeds - lot.cost
                realized += trade_pnl
                fees += fee
                cycles += 1
                sell_count += 1
                trades.append({
                    "status": "CLOSED",
                    "entry_at_ms": lot.bought_at_ms,
                    "entry_price": lot.buy_price,
                    "exit_at_ms": (
                        timestamps[index] + candle_minutes * 60_000
                        if timestamps is not None else None
                    ),
                    "exit_price": execution_price,
                    "qty": lot.qty,
                    "pnl": trade_pnl,
                    "fees": lot.buy_fee + fee,
                })

        is_hour_close = (
            timestamps is None
            or ((timestamps[index] // 60_000) + candle_minutes) % 60 == 0
        )
        if trading_active and is_hour_close:
            hourly_closes.append(current)
            if len(hourly_closes) >= 2:
                recent = hourly_closes[-2:]
                if all(price < lower for price in recent) and break_down_action == "stop":
                    trading_active = False
                    stop_reason = "BREAK_DOWN"
                elif all(price > upper for price in recent) and break_up_action == "stop":
                    trading_active = False
                    stop_reason = "BREAK_UP"
                if not trading_active and timestamps is not None:
                    stopped_at_ms = timestamps[index] + candle_minutes * 60_000

        inventory = sum((lot.qty for lot in lots.values()), Decimal("0"))
        equity = quote + inventory * current
        capital_used = sum((lot.cost for lot in lots.values()), Decimal("0"))
        capital_used_total += capital_used
        capital_used_max = max(capital_used_max, capital_used)
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)
        previous = current

    last = prices[-1]
    inventory = sum((lot.qty for lot in lots.values()), Decimal("0"))
    inventory_cost = sum((lot.cost for lot in lots.values()), Decimal("0"))
    inventory_value = inventory * last
    unrealized = inventory_value - inventory_cost
    total = realized + unrealized

    def text(value: Decimal) -> str:
        return format(value.quantize(Decimal("0.00000001")), "f")

    for lot in lots.values():
        trades.append({
            "status": "OPEN",
            "entry_at_ms": lot.bought_at_ms,
            "entry_price": lot.buy_price,
            "exit_at_ms": None,
            "exit_price": None,
            "qty": lot.qty,
            "pnl": lot.qty * last - lot.cost,
            "fees": lot.buy_fee,
        })
    serialized_trades = [
        {
            "status": item["status"],
            "entry_at_ms": item["entry_at_ms"],
            "entry_price": text(item["entry_price"]),
            "exit_at_ms": item["exit_at_ms"],
            "exit_price": (
                text(item["exit_price"]) if item["exit_price"] is not None else None
            ),
            "qty": text(item["qty"]),
            "pnl": text(item["pnl"]),
            "fees": text(item["fees"]),
        }
        for item in sorted(
            trades,
            key=lambda item: (
                item["entry_at_ms"] is None,
                item["entry_at_ms"] or 0,
                item["entry_price"],
            ),
        )
    ]

    return {
        "step": text(step),
        "start_price": text(prices[0]),
        "end_price": text(last),
        "realized_pnl": text(realized),
        "unrealized_pnl": text(unrealized),
        "total_pnl": text(total),
        "cycles": cycles,
        "completed_cycles": cycles,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "fees": text(fees),
        "slippage_cost": text(slippage_cost),
        "max_drawdown_pct": text(max_drawdown * Decimal("100")),
        "outside_candles": outside,
        "outside_time_pct": text(Decimal(outside) / Decimal(len(prices) - 1) * Decimal("100")),
        "range_break_up_count": range_break_up_count,
        "range_break_down_count": range_break_down_count,
        "capital_used_avg": text(capital_used_total / Decimal(len(prices) - 1)),
        "capital_used_max": text(capital_used_max),
        "base_inventory": text(inventory),
        "inventory_value": text(inventory_value),
        "initial_quote": text(initial_quote),
        "final_equity": text(quote + inventory_value),
        "status": "COMPLETED" if trading_active else "STOPPED",
        "stop_reason": stop_reason,
        "stopped_at_ms": stopped_at_ms,
        "trades": serialized_trades,
    }
