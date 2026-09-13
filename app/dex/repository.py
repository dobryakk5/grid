"""Persistence for synthetic limit orders.

The one place that reads and writes ``dex_intents``. Both sides of the venue go
through it and neither talks to the other:

    grid engine -> RobinhoodClient -> DexIntentRepository
    DEX worker  ----------------------^

Nothing here commits. Transaction boundaries belong to the caller, because the
crash-safety property depends on them: reserving a nonce and recording the
intent that will use it must land in the *same* transaction, and that commit
must happen before the transaction is broadcast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DexIntent, DexWallet
from app.dex.intents import TERMINAL_STATUSES, IntentStatus, assert_transition

__all__ = ["DexIntentRepository"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DexIntentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- levels ----------------------------------------------------------

    async def create_level(
        self,
        *,
        symbol: str,
        side: str,
        limit_price: Decimal,
        amount_in: Decimal,
        amount_in_coin: str,
        order_link_id: str,
        profile_id: int | None = None,
        expires_at: datetime | None = None,
        parent_intent_id: int | None = None,
        retry_count: int = 0,
        ignore_liquidity_gate: bool = False,
    ) -> DexIntent:
        """Record a level that is now watching for its price."""
        intent = DexIntent(
            profile_id=profile_id,
            ignore_liquidity_gate=ignore_liquidity_gate,
            order_link_id=order_link_id,
            symbol=symbol,
            side=side,
            status=IntentStatus.WAITING,
            limit_price=limit_price,
            amount_in=amount_in,
            amount_in_coin=amount_in_coin,
            expires_at=expires_at
            or _now() + timedelta(hours=settings.dex_intent_ttl_hours),
            parent_intent_id=parent_intent_id,
            retry_count=retry_count,
        )
        self.session.add(intent)
        await self.session.flush()
        return intent

    async def by_id(self, intent_id: int) -> DexIntent | None:
        return await self.session.get(DexIntent, intent_id)

    async def by_link_id(self, order_link_id: str) -> DexIntent | None:
        return await self.session.scalar(
            select(DexIntent).where(DexIntent.order_link_id == order_link_id)
        )

    async def by_tx_hash(self, tx_hash: str) -> DexIntent | None:
        return await self.session.scalar(
            select(DexIntent).where(DexIntent.tx_hash == tx_hash)
        )

    async def open_intents(self, *, symbol: str | None = None) -> list[DexIntent]:
        """Everything still in flight, oldest first.

        Signed rows come out alongside unsigned ones on purpose: a restarted
        worker must look at them before it looks at any level, or it could arm a
        second attempt while the first is still confirming.
        """
        statement = (
            select(DexIntent)
            .where(DexIntent.status.notin_(tuple(TERMINAL_STATUSES)))
            .order_by(DexIntent.id)
        )
        if symbol is not None:
            statement = statement.where(DexIntent.symbol == symbol)
        return list((await self.session.execute(statement)).scalars())

    # ---- transitions -----------------------------------------------------

    async def transition(self, intent: DexIntent, target: str, **fields) -> DexIntent:
        """Move one intent, refusing any move the state machine forbids."""
        assert_transition(intent.status, target)
        intent.status = target
        for key, value in fields.items():
            setattr(intent, key, value)
        await self.session.flush()
        return intent

    async def block(self, intent: DexIntent, reason: str) -> DexIntent:
        return await self.transition(
            intent,
            IntentStatus.BLOCKED,
            blocked_reason=reason[:255],
            blocked_until=_now()
            + timedelta(seconds=settings.dex_blocked_retry_seconds),
        )

    async def fail(self, intent: DexIntent, reason: str) -> DexIntent:
        return await self.transition(
            intent, IntentStatus.FAILED, last_error=reason[:500]
        )

    async def retry_level(self, intent: DexIntent) -> DexIntent | None:
        """Re-arm a level whose attempt had to be abandoned.

        A new row rather than a reset one: an intent is one attempt under one
        nonce, and rewinding a signed row is exactly the mistake the state
        machine exists to prevent. The history stays linked through
        ``parent_intent_id``.
        """
        if intent.retry_count >= settings.dex_max_retries:
            return None
        return await self.create_level(
            symbol=intent.symbol,
            side=intent.side,
            limit_price=intent.limit_price,
            amount_in=intent.amount_in,
            amount_in_coin=intent.amount_in_coin,
            order_link_id=f"{intent.order_link_id}-r{intent.retry_count + 1}"[:36],
            profile_id=intent.profile_id,
            expires_at=intent.expires_at,
            parent_intent_id=intent.id,
            retry_count=intent.retry_count + 1,
        )

    # ---- nonces ----------------------------------------------------------

    async def reserve_nonce(self, *, wallet: str, chain_nonce: int) -> int:
        """Hand out the next nonce for ``wallet``, locking the row first.

        The database is the authority on what we have already used, and the
        chain's pending count is the floor -- that way a transaction sent from
        somewhere else (a manual approval, another tool) cannot make us reuse a
        nonce, and a database restored from backup cannot either.

        The caller must commit this together with the intent that uses it.
        """
        address = wallet.lower()
        await self.session.execute(
            insert(DexWallet)
            .values(address=address, next_nonce=chain_nonce)
            .on_conflict_do_nothing(index_elements=["address"])
        )
        row = await self.session.get(DexWallet, address, with_for_update=True)
        nonce = max(int(row.next_nonce), int(chain_nonce))
        row.next_nonce = nonce + 1
        await self.session.flush()
        return nonce

    async def release_nonce(self, *, wallet: str, nonce: int) -> None:
        """Give a nonce back when nothing was ever signed under it."""
        row = await self.session.get(DexWallet, wallet.lower(), with_for_update=True)
        if row is not None and int(row.next_nonce) == int(nonce) + 1:
            row.next_nonce = nonce
            await self.session.flush()
