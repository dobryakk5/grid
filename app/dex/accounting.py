"""Turning a confirmed swap into the row the PnL code already understands.

``GridExecution`` is the project's unit of "this actually traded": lots, cycles
and every PnL figure are built from it. A swap has to arrive in exactly that
shape, with two differences handled here rather than pushed into the PnL code:

* the fee is gas, converted into the quote currency, with the native figure kept
  beside it;
* there is no exchange trade id, so the transaction hash is the identity.

Pure on purpose -- it takes a parsed fill and returns a dict, so the numbers can
be tested without a chain or a database.
"""

from __future__ import annotations

from decimal import Decimal

from app.dex.pricing import GasCost
from app.dex.receipts import FillReport
from app.dex.tokens import DexPair

__all__ = ["execution_values", "realised_price"]


def realised_price(fill: FillReport, pair: DexPair, side: str) -> Decimal:
    """Quote per base, whichever token was spent."""
    if side.strip().lower() == "sell":
        base = fill.amount_in(pair.base)
        quote = fill.amount_out(pair.quote)
    else:
        base = fill.amount_out(pair.base)
        quote = fill.amount_in(pair.quote)
    if base <= 0:
        raise ValueError("fill moved no base token")
    return quote / base


def execution_values(
    *,
    pair: DexPair,
    side: str,
    fill: FillReport,
    gas: GasCost | None = None,
    exec_time_ms: int | None = None,
) -> dict:
    """Fields for a ``GridExecution`` row describing this swap.

    ``exec_fee`` is quote-denominated because that is what PnL sums; the native
    gas figure travels alongside it instead of being thrown away. A swap has no
    maker side, so ``is_maker`` is false.
    """
    selling = side.strip().lower() == "sell"
    base_qty = fill.amount_in(pair.base) if selling else fill.amount_out(pair.base)
    quote_value = fill.amount_out(pair.quote) if selling else fill.amount_in(pair.quote)

    return {
        # A transaction is one atomic fill, so its hash is the execution id.
        "exec_id": fill.tx_hash,
        "exec_price": realised_price(fill, pair, side),
        "exec_qty": base_qty,
        "exec_value": quote_value,
        "exec_fee": gas.quote if gas is not None else Decimal("0"),
        "fee_currency": gas.quote_coin if gas is not None else pair.quote_coin,
        "fee_rate": None,
        "is_maker": False,
        "exec_time_ms": exec_time_ms,
        "fee_native_amount": gas.native if gas is not None else fill.gas_native,
        "fee_native_coin": gas.coin if gas is not None else "ETH",
        "tx_hash": fill.tx_hash,
    }
