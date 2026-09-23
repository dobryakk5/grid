"""The short list of contracts our own wallet has anything to do with.

The positions page used to ask ``chain_tokens`` -- the tape's metadata cache,
built from every ERC-20 that brushed past any tracked wallet. That table is
tens of thousands of rows and grows with other people's trading, so reading a
wallet inventory out of it meant dozens of Multicalls per page load and, at
the far end, a gateway timeout. The list of tokens *we* might hold is a few
dozen and grows only when we trade.

So the two are separated. ``chain_tokens`` stays the tape's; this is ours, and
it is filled from three directions:

* our own swaps on the tape, which is the history the page already reads for
  cost basis;
* the moment an order is armed or filled, so a coin bought thirty seconds ago
  is on the page rather than waiting for a sweep;
* a background sweep of ``chain_tokens``, which is the only way an airdrop or
  a purchase made outside the bot is ever noticed. It runs off the request
  path, where taking a minute costs nothing.

Rows are never deleted when a balance reaches zero. A position closed today is
one that may be opened again tomorrow, and re-finding it would cost the full
sweep this table exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.db.models import ChainSwap, ChainToken, DexWalletToken

__all__ = ["WalletToken", "ensure_seeded", "known_tokens", "remember", "seed_from_swaps"]


@dataclass(frozen=True)
class WalletToken:
    address: str
    symbol: str | None
    decimals: int


async def known_tokens(session_factory, *, chain_id: int | None = None) -> list[WalletToken]:
    """Every contract worth asking the wallet about, lowest address first.

    Sorted for the caller's benefit only: a stable order makes a Multicall
    batch reproducible, which matters when one of them misbehaves.
    """
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    async with session_factory() as session:
        rows = list((await session.execute(
            select(DexWalletToken)
            .where(DexWalletToken.chain_id == chain)
            .order_by(DexWalletToken.address)
        )).scalars())
    return [WalletToken(row.address, row.symbol, row.decimals) for row in rows]


async def remember(
    session_factory,
    tokens: list[WalletToken],
    *,
    source: str,
    chain_id: int | None = None,
    nonzero: bool = False,
) -> int:
    """Add these contracts to the wallet's list. Returns how many were given.

    Upsert, not insert: the same address arrives from a fill, from a sweep and
    from the tape, and the first arrival is the one that should keep its
    ``source`` and ``first_seen_at``. Metadata is refreshed because a symbol
    read later may be better than an empty one read early; ``last_nonzero_at``
    only ever moves forward, and only when the caller actually saw a balance.
    """
    if not tokens:
        return 0
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    now = datetime.now(timezone.utc)
    rows = [{
        "chain_id": chain,
        "address": token.address.lower(),
        "symbol": token.symbol,
        "decimals": token.decimals,
        "source": source,
        "last_nonzero_at": now if nonzero else None,
    } for token in tokens]

    async with session_factory() as session:
        statement = insert(DexWalletToken).values(rows)
        await session.execute(statement.on_conflict_do_update(
            index_elements=[DexWalletToken.chain_id, DexWalletToken.address],
            set_={
                # Never blank a name we already have with a null from a
                # thinner read -- the same reasoning as the trader import.
                "symbol": func.coalesce(statement.excluded.symbol, DexWalletToken.symbol),
                "decimals": statement.excluded.decimals,
                "last_nonzero_at": func.greatest(
                    DexWalletToken.last_nonzero_at, statement.excluded.last_nonzero_at
                ),
            },
        ))
        await session.commit()
    return len(rows)


async def ensure_seeded(session_factory, wallet: str, *, chain_id: int | None = None) -> int:
    """Fill the list from our own history if nothing has filled it yet.

    Called from both the page and the background sweep, because either may be
    the first to run after a deploy and an empty page is not an honest answer
    to "what do I hold". Costs one ``COUNT`` once the list is non-empty, which
    it is from the first trade onwards.
    """
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    async with session_factory() as session:
        existing = await session.scalar(
            select(func.count()).select_from(DexWalletToken)
            .where(DexWalletToken.chain_id == chain)
        )
    if existing:
        return 0
    return await seed_from_swaps(session_factory, wallet, chain_id=chain)


async def seed_from_swaps(session_factory, wallet: str, *, chain_id: int | None = None) -> int:
    """Take the wallet's own tape history as the starting list.

    A fresh deploy would otherwise show an empty page until the first
    background sweep finished. Everything we have ever swapped is already
    recorded -- against our own address, by the same tape -- so the list can
    start from there for the price of one query.
    """
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    async with session_factory() as session:
        addresses = [row.lower() for row in (await session.execute(
            select(func.distinct(func.lower(ChainSwap.token_address)))
            .where(func.lower(ChainSwap.wallet_address) == wallet.lower())
        )).scalars() if row]
        if not addresses:
            return 0
        # Metadata comes from the tape's cache rather than from the chain:
        # these are tokens it has already read, so there is nothing to ask a
        # node for. An address the cache somehow lacks still gets a row --
        # the balance read does not need a symbol, and the sweep will fill it.
        known = {row.address.lower(): row for row in (await session.execute(
            select(ChainToken).where(
                ChainToken.chain_id == chain, ChainToken.address.in_(addresses)
            )
        )).scalars()}

    tokens = [
        WalletToken(
            address=address,
            symbol=known[address].symbol if address in known else None,
            decimals=known[address].decimals if address in known else 18,
        )
        for address in addresses
    ]
    return await remember(session_factory, tokens, source="swap", chain_id=chain)
