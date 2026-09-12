"""What the tokens in the wallet cost, worked out from our own filled swaps.

Not from ``position_lots``: that table is keyed to a grid profile and a
``grid_executions`` row, so it only ever describes the CEX grid -- today it
holds XRPUSDT and BTCUSDT lots from bybit. Hand-placed DEX orders carry no
profile and never reach it. ``dex_intents`` is where a Robinhood Chain fill is
recorded, so that is what this reads.

The important part is what happens when the records do not add up. A wallet can
hold coins that were never bought through this system -- airdropped, sent in,
bought before any of this existed -- and for those there is no honest cost to
show. Rather than average them in at zero (which would invent a profit) or drop
them (which would invent a smaller position), the uncovered quantity is
reported separately and the page says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

__all__ = ["Fill", "Basis", "cost_basis"]


@dataclass(frozen=True)
class Fill:
    """One confirmed swap, in the pair's own terms."""

    side: str            # "Buy" or "Sell"
    base_qty: Decimal    # tokens received (buy) or handed over (sell)
    quote_qty: Decimal   # USDG spent (buy) or received (sell)
    gas_quote: Decimal = Decimal(0)


@dataclass(frozen=True)
class Basis:
    covered_qty: Decimal
    cost_quote: Decimal
    uncovered_qty: Decimal
    # Records say we should hold more than the wallet does: coins left without
    # a sale we know about. Shown rather than silently absorbed.
    unexplained_outflow: Decimal

    @property
    def average_price(self) -> Decimal | None:
        if self.covered_qty <= 0:
            return None
        return self.cost_quote / self.covered_qty

    @property
    def complete(self) -> bool:
        return self.uncovered_qty == 0 and self.unexplained_outflow == 0


def cost_basis(fills: list[Fill], held_qty: Decimal) -> Basis:
    """FIFO cost of ``held_qty``, from fills given oldest first.

    Gas on a *buy* is part of what the position cost and is carried into the
    lot. Gas on a sell is not: it belongs to the profit or loss of that sale,
    which has already happened and cannot change what the remaining coins cost.
    """
    lots: list[list[Decimal]] = []  # [qty, cost] per open lot, oldest first
    for fill in fills:
        if fill.base_qty <= 0:
            continue
        if fill.side == "Buy":
            lots.append([fill.base_qty, fill.quote_qty + fill.gas_quote])
            continue
        if fill.side != "Sell":
            raise ValueError(f"unknown side {fill.side!r}")
        remaining = fill.base_qty
        while remaining > 0 and lots:
            qty, cost = lots[0]
            taken = min(qty, remaining)
            unit = cost / qty if qty else Decimal(0)
            lots[0] = [qty - taken, cost - unit * taken]
            remaining -= taken
            if lots[0][0] <= 0:
                lots.pop(0)
        # A sale larger than anything we recorded buying just empties the book;
        # it cannot make the basis negative.

    on_paper = sum((lot[0] for lot in lots), Decimal(0))
    covered, cost = Decimal(0), Decimal(0)
    for qty, lot_cost in lots:
        if covered >= held_qty:
            break
        taken = min(qty, held_qty - covered)
        unit = lot_cost / qty if qty else Decimal(0)
        covered += taken
        cost += unit * taken
    return Basis(
        covered_qty=covered,
        cost_quote=cost,
        uncovered_qty=max(Decimal(0), held_qty - covered),
        unexplained_outflow=max(Decimal(0), on_paper - held_qty),
    )
