from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Protocol


class ExecutionLike(Protocol):
    exec_price: Decimal
    exec_qty: Decimal
    exec_value: Decimal
    exec_fee: Decimal
    fee_currency: str | None


@dataclass
class OrderExecutionSummary:
    qty: Decimal = Decimal("0")
    value: Decimal = Decimal("0")
    fees_quote: Decimal = Decimal("0")
    unconverted_fees: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class CyclePnl:
    turnover: Decimal
    gross_profit: Decimal
    fees_quote: Decimal
    net_profit: Decimal
    buy_qty: Decimal
    sell_qty: Decimal
    residual_qty: Decimal
    unconverted_fees: dict[str, Decimal]


def summarize_executions(
    executions: Iterable[ExecutionLike], *, base_coin: str, quote_coin: str
) -> OrderExecutionSummary:
    summary = OrderExecutionSummary()
    base_coin = base_coin.upper()
    quote_coin = quote_coin.upper()

    for execution in executions:
        price = Decimal(execution.exec_price)
        qty = Decimal(execution.exec_qty)
        value = Decimal(execution.exec_value)
        fee = Decimal(execution.exec_fee or 0)
        currency = (execution.fee_currency or "").upper()

        summary.qty += qty
        summary.value += value

        if fee == 0:
            continue
        if currency == quote_coin:
            summary.fees_quote += fee
        elif currency == base_coin:
            # Convert a base-coin fee at the actual price of that execution.
            summary.fees_quote += fee * price
        else:
            key = currency or "UNKNOWN"
            summary.unconverted_fees[key] = summary.unconverted_fees.get(
                key, Decimal("0")
            ) + fee

    return summary


def calculate_cycle_pnl(
    buy: OrderExecutionSummary, sell: OrderExecutionSummary
) -> CyclePnl | None:
    if buy.qty <= 0 or sell.qty <= 0:
        return None

    # The SELL quantity can be slightly below the preceding BUY quantity because
    # the bot keeps a fee buffer. Only the cost basis corresponding to the sold
    # base quantity is realised; any remainder stays as inventory/dust.
    realised_ratio = min(sell.qty / buy.qty, Decimal("1"))
    realised_buy_value = buy.value * realised_ratio

    turnover = realised_buy_value + sell.value
    gross_profit = sell.value - realised_buy_value
    fees_quote = buy.fees_quote + sell.fees_quote
    net_profit = gross_profit - fees_quote

    unknown = dict(buy.unconverted_fees)
    for currency, amount in sell.unconverted_fees.items():
        unknown[currency] = unknown.get(currency, Decimal("0")) + amount

    return CyclePnl(
        turnover=turnover,
        gross_profit=gross_profit,
        fees_quote=fees_quote,
        net_profit=net_profit,
        buy_qty=buy.qty,
        sell_qty=sell.qty,
        residual_qty=max(buy.qty - sell.qty, Decimal("0")),
        unconverted_fees=unknown,
    )


def grid_cell_statistics(
    profile, orders, *, base_coin: str, quote_coin: str,
    tick_size: Decimal | None = None,
) -> dict:
    """Aggregate realised PnL by grid cell from persisted exchange executions."""
    from app.trading.math import strategy_grid_cells

    by_exchange_id = {order.exchange_order_id: order for order in orders}
    grid_pairs = strategy_grid_cells(
        Decimal(profile.lower_price),
        Decimal(profile.upper_price),
        Decimal(profile.step_price),
        mode=getattr(profile, "grid_mode", "arithmetic"),
        step_percent=(
            Decimal(profile.step_percent)
            if getattr(profile, "step_percent", None) is not None else None
        ),
    )
    if tick_size is not None:
        from app.trading.math import floor_to_step
        grid_pairs = [
            (floor_to_step(buy, tick_size), floor_to_step(sell, tick_size))
            for buy, sell in grid_pairs
        ]
    levels = [buy for buy, _ in grid_pairs]
    sells = dict(grid_pairs)

    cells: dict[Decimal, dict] = {
        Decimal(level): {
            "buy_price": Decimal(level),
            "sell_price": sells[Decimal(level)],
            "cycles": 0,
            "turnover": Decimal("0"),
            "gross_profit": Decimal("0"),
            "fees_quote": Decimal("0"),
            "net_profit": Decimal("0"),
            "residual_qty": Decimal("0"),
            "unconverted_fees": {},
            "last_completed_at_ms": None,
            "state": "WAITING",
        }
        for level in levels
    }

    latest_by_cell = {}
    for order in orders:
        latest_by_cell[Decimal(order.grid_buy_price)] = order

    for level, latest in latest_by_cell.items():
        if level not in cells:
            continue
        if latest.status in {"New", "PartiallyFilled", "Untriggered", "Created"}:
            cells[level]["state"] = f"{latest.side.upper()} {latest.status}"
        elif latest.status == "Filled":
            cells[level]["state"] = f"{latest.side.upper()} FILLED"
        else:
            cells[level]["state"] = latest.status.upper()

    for sell in orders:
        if sell.side != "Sell" or sell.status != "Filled" or not sell.executions:
            continue

        parent_id = sell.replacement_for
        visited: set[str] = set()
        buy = None
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = by_exchange_id.get(parent_id)
            if parent is None:
                break
            if parent.side == "Buy" and parent.status == "Filled":
                buy = parent
                break
            parent_id = parent.replacement_for

        if buy is None or not buy.executions:
            continue

        level = Decimal(sell.grid_buy_price)
        if level not in cells:
            continue

        buy_summary = summarize_executions(
            buy.executions, base_coin=base_coin, quote_coin=quote_coin
        )
        sell_summary = summarize_executions(
            sell.executions, base_coin=base_coin, quote_coin=quote_coin
        )
        cycle = calculate_cycle_pnl(buy_summary, sell_summary)
        if cycle is None:
            continue

        cell = cells[level]
        cell["cycles"] += 1
        cell["turnover"] += cycle.turnover
        cell["gross_profit"] += cycle.gross_profit
        cell["fees_quote"] += cycle.fees_quote
        cell["net_profit"] += cycle.net_profit
        cell["residual_qty"] += cycle.residual_qty
        for currency, amount in cycle.unconverted_fees.items():
            cell["unconverted_fees"][currency] = (
                cell["unconverted_fees"].get(currency, Decimal("0")) + amount
            )
        completed = max(
            (e.exec_time_ms for e in sell.executions if e.exec_time_ms is not None),
            default=None,
        )
        if completed is not None:
            current = cell["last_completed_at_ms"]
            cell["last_completed_at_ms"] = max(current or 0, completed)

    total = {
        "cycles": 0,
        "turnover": Decimal("0"),
        "gross_profit": Decimal("0"),
        "fees_quote": Decimal("0"),
        "net_profit": Decimal("0"),
        "residual_qty": Decimal("0"),
        "unconverted_fees": {},
    }
    for cell in cells.values():
        for key in ("cycles", "turnover", "gross_profit", "fees_quote", "net_profit", "residual_qty"):
            total[key] += cell[key]
        for currency, amount in cell["unconverted_fees"].items():
            total["unconverted_fees"][currency] = (
                total["unconverted_fees"].get(currency, Decimal("0")) + amount
            )

    def encode(item: dict) -> dict:
        return {
            key: (
                str(value)
                if isinstance(value, Decimal)
                else {k: str(v) for k, v in value.items()}
                if key == "unconverted_fees"
                else value
            )
            for key, value in item.items()
        }

    return {
        "base_coin": base_coin,
        "quote_coin": quote_coin,
        "total": encode(total),
        "cells": [encode(cells[level]) for level in levels],
    }
