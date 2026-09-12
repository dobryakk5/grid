"""Work out which of a trader's many addresses is the one that trades.

FOMO publishes an ``evmAddress`` per user, and on Robinhood Chain that
address is inert: of 313 imported ones, not a single one appears in any
``Transfer`` on the whole chain. So it cannot be used to name the wallets our
tape sees trading.

A trade record is more promising but less direct -- it mentions the payer,
the receiver, a router, a pool, without saying which is the person. Rather
than guess a field name from an undocumented API that has already moved once,
the caller harvests every address it can see and these functions narrow the
set using two facts we hold independently of FOMO:

* the address must appear in our own ``chain_swaps``, and
* it must be claimed by exactly one identity -- routers and pools are
  necessarily mentioned in everybody's trades, and that is what gives them
  away.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

__all__ = ["ADDRESS_RE", "candidate_claims", "exclusive_addresses", "wallets_per_entry"]

ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def candidate_claims(entries: Sequence[Sequence[str]]) -> dict[str, frozenset[int]]:
    """Map each valid address to the indices of the entries that claim it."""
    claims: dict[str, set[int]] = {}
    for index, addresses in enumerate(entries):
        for raw in addresses:
            address = raw.strip().lower()
            if ADDRESS_RE.match(address):
                claims.setdefault(address, set()).add(index)
    return {address: frozenset(owners) for address, owners in claims.items()}


def exclusive_addresses(claims: dict[str, frozenset[int]]) -> list[str]:
    """Addresses only one identity claims -- the only ones worth looking up."""
    return sorted(address for address, owners in claims.items() if len(owners) == 1)


def wallets_per_entry(
    claims: dict[str, frozenset[int]], trading: set[str], count: int
) -> list[tuple[str, ...]]:
    """For each entry, the trading wallets that only it claims.

    An empty tuple means the identity could not be placed on chain; more than
    one means it could be placed in two places at once, which is a reason to
    leave it unnamed rather than to pick one.
    """
    found: list[list[str]] = [[] for _ in range(count)]
    for address, owners in claims.items():
        if len(owners) != 1 or address not in trading:
            continue
        (index,) = owners
        if 0 <= index < count:
            found[index].append(address)
    return [tuple(sorted(hits)) for hits in found]
