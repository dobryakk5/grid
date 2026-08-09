from decimal import Decimal
from types import SimpleNamespace

from app.trading.pnl import (
    calculate_cycle_pnl,
    grid_cell_statistics,
    summarize_executions,
)


def ex(*, price, qty, value, fee="0", currency=None, time_ms=1):
    return SimpleNamespace(
        exec_price=Decimal(price),
        exec_qty=Decimal(qty),
        exec_value=Decimal(value),
        exec_fee=Decimal(fee),
        fee_currency=currency,
        exec_time_ms=time_ms,
    )


def test_cycle_pnl_uses_actual_executions_and_fees():
    buy = summarize_executions(
        [ex(price="64000", qty="0.001", value="64", fee="0.000001", currency="BTC")],
        base_coin="BTC",
        quote_coin="USDT",
    )
    sell = summarize_executions(
        [ex(price="65000", qty="0.000998", value="64.87", fee="0.06487", currency="USDT")],
        base_coin="BTC",
        quote_coin="USDT",
    )
    pnl = calculate_cycle_pnl(buy, sell)

    assert pnl is not None
    assert pnl.gross_profit == Decimal("0.998")
    assert pnl.fees_quote == Decimal("0.128870")
    assert pnl.net_profit == Decimal("0.869130")
    assert pnl.turnover == Decimal("128.742")
    assert pnl.residual_qty == Decimal("0.000002")


def test_cell_stats_follow_replacement_chain_through_cancelled_sell():
    profile = SimpleNamespace(
        lower_price=Decimal("62000"),
        upper_price=Decimal("67000"),
        step_price=Decimal("1000"),
    )
    buy = SimpleNamespace(
        exchange_order_id="buy-1",
        replacement_for=None,
        side="Buy",
        status="Filled",
        grid_buy_price=Decimal("64000"),
        executions=[ex(price="64000", qty="0.001", value="64", fee="0", currency="BTC")],
    )
    cancelled_sell = SimpleNamespace(
        exchange_order_id="sell-cancelled",
        replacement_for="buy-1",
        side="Sell",
        status="Cancelled",
        grid_buy_price=Decimal("64000"),
        executions=[],
    )
    filled_sell = SimpleNamespace(
        exchange_order_id="sell-2",
        replacement_for="sell-cancelled",
        side="Sell",
        status="Filled",
        grid_buy_price=Decimal("64000"),
        executions=[ex(price="65000", qty="0.001", value="65", fee="0", currency="USDT", time_ms=99)],
    )

    stats = grid_cell_statistics(
        profile,
        [buy, cancelled_sell, filled_sell],
        base_coin="BTC",
        quote_coin="USDT",
    )
    cell = next(c for c in stats["cells"] if c["buy_price"] == "64000")

    assert cell["cycles"] == 1
    assert cell["gross_profit"] == "1"
    assert cell["net_profit"] == "1"
    assert cell["last_completed_at_ms"] == 99
    assert stats["total"]["cycles"] == 1
