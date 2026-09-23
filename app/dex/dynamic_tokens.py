"""Make chain-discovered tokens tradable, without editing the registry.

``app.dex.tokens`` pins a handful of instruments by hand because a wrong
``decimals`` misprices a real order. That reasoning applies to a *guessed*
value -- not to one read from the token's own ``decimals()`` call, which is
what ``app.chain.tokens`` stores in ``chain_tokens`` while the tape runs.

So this module bridges the two: every token the tape has verified on chain is
registered as tradable, which is what lets a limit order be placed on a coin
nobody typed into a Python file.

Both the API (which arms a level) and the DEX worker (which executes it) must
load this, or a symbol that resolved when the order was placed would fail at
execution time.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.models import ChainToken, DexWalletToken
from app.dex.tokens import DexConfigError, register_dynamic_token

__all__ = ["load_dynamic_tokens"]

logger = logging.getLogger(__name__)

# Re-reading a small table on every worker tick buys nothing; tokens appear at
# the pace a wallet first touches one.
_TTL_SECONDS = 60.0
_last_load = 0.0
# Addresses already reported as unregisterable. The registry is re-read every
# minute and the same impostor tokens fail every time. These collisions are
# expected input filtering, not an operational warning; retain them at DEBUG
# for diagnosis without polluting a production WARNING journal.
_reported: set[str] = set()


async def load_dynamic_tokens(session_factory, *, chain_id: int | None = None, force: bool = False) -> int:
    """Register every chain-verified token. Returns how many are known."""
    global _last_load
    now = time.monotonic()
    if not force and _last_load and now - _last_load < _TTL_SECONDS:
        return 0
    _last_load = now

    chain = chain_id if chain_id is not None else settings.rh_chain_id
    async with session_factory() as session:
        rows = list((await session.execute(
            select(ChainToken).where(ChainToken.chain_id == chain, ChainToken.symbol.is_not(None))
        )).scalars())
        # Our own list too, and not for symmetry: an address bought by hand is
        # read off its contract into ``dex_wallet_tokens`` and may never
        # appear in the tape's cache at all. Without this the API would arm a
        # level the worker -- a separate process, with its own registry --
        # could not resolve a pair for, and the order would sit unexecutable.
        rows += list((await session.execute(
            select(DexWalletToken).where(
                DexWalletToken.chain_id == chain, DexWalletToken.symbol.is_not(None)
            )
        )).scalars())

    registered = 0
    for row in rows:
        try:
            register_dynamic_token(row.symbol, row.address, row.decimals)
            registered += 1
        except DexConfigError as exc:
            # A token claiming a name the registry already pins to another
            # address -- on this chain, three separate contracts call
            # themselves USDG. Worth knowing once, not on every reload.
            if row.address not in _reported:
                _reported.add(row.address)
                logger.debug("skipped dynamic token: %s", exc)
    return registered
