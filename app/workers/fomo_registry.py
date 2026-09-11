"""Trader registry worker: who is this wallet, and how good is it.

Polls FOMO's leaderboard and per-token holders on a slow cadence -- there is
no reason to hit this more than every few minutes -- and keeps two things up
to date: ``fomo_traders`` (FOMO identity -> EVM wallet) and
``fomo_trader_ranks`` (rank history, because rank #5 today says nothing about
rank #5 last month).

Degrades without FOMO: no session configured, or one that has expired, just
skips this pass and leaves the registry as it already is. ``chain_tape``
does not need this worker running at all to keep recording trades for
wallets already known.
"""

import asyncio
import logging

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.db.init import init_db
from app.db.models import FomoTrader, FomoTraderRank
from app.db.session import SessionLocal
from app.dex.tokens import DexConfigError, resolve_pair
from app.fomo.client import FomoAuthError, FomoClient, FomoError, FomoRateLimited
from app.fomo.schema import normalize_holders, normalize_leaderboard
from app.workers.dex_sampler import watched_symbols

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def _touch_trader(session, *, fomo_user_id: str, user_handle: str | None, display_name: str | None) -> None:
    """Insert or refresh identity fields only -- never touches ``evm_address``,
    ``source`` or ``backfilled_from_block``, so a repeated sighting can never
    undo a wallet link or reset the backfill queue."""
    statement = insert(FomoTrader).values(
        fomo_user_id=fomo_user_id, user_handle=user_handle, display_name=display_name,
    )
    # `last_seen_at` has an ORM-level `onupdate`, which only fires for ORM
    # UPDATE statements, not for a Core upsert -- set it explicitly instead.
    statement = statement.on_conflict_do_update(
        index_elements=[FomoTrader.fomo_user_id],
        set_={
            "user_handle": statement.excluded.user_handle,
            "display_name": statement.excluded.display_name,
            "last_seen_at": func.now(),
        },
    )
    await session.execute(statement)


async def _link_wallet(session, *, fomo_user_id: str, evm_address: str, source: str) -> bool:
    """Attach a wallet only if this trader didn't already have one.

    Leaving an existing link alone keeps ``backfilled_from_block`` meaningful:
    it must stay set once a wallet has actually been backfilled, not get
    reset to NULL every time the same wallet resurfaces in a later poll.
    """
    result = await session.execute(
        update(FomoTrader)
        .where(FomoTrader.fomo_user_id == fomo_user_id, FomoTrader.evm_address.is_(None))
        .values(evm_address=evm_address, source=source)
    )
    return result.rowcount > 0


async def sync_leaderboard(fomo: FomoClient, session) -> int:
    payload = await fomo.leaderboard()
    ranks = normalize_leaderboard(payload)
    for entry in ranks:
        if not entry.user_id:
            continue
        await _touch_trader(session, fomo_user_id=entry.user_id, user_handle=entry.handle, display_name=entry.display_name)
        if entry.evm_address:
            await _link_wallet(session, fomo_user_id=entry.user_id, evm_address=entry.evm_address, source="leaderboard")
        session.add(FomoTraderRank(fomo_user_id=entry.user_id, rank=entry.rank, stats={}))
    await session.commit()
    return len(ranks)


async def sync_holders(fomo: FomoClient, session, symbols: list[str]) -> int:
    linked = 0
    for symbol in symbols:
        try:
            pair = resolve_pair(symbol)
        except DexConfigError as exc:
            logger.warning("%s skipped: %s", symbol, exc)
            continue
        try:
            payload = await fomo.holders(pair.base.address, settings.rh_chain_id)
        except (FomoAuthError, FomoRateLimited, FomoError) as exc:
            logger.warning("%s holders lookup failed: %s", symbol, exc)
            continue
        for holder in normalize_holders(payload):
            if not holder.user_id:
                continue
            await _touch_trader(session, fomo_user_id=holder.user_id, user_handle=holder.handle, display_name=holder.display_name)
            if holder.evm_address and await _link_wallet(
                session, fomo_user_id=holder.user_id, evm_address=holder.evm_address, source="holders"
            ):
                linked += 1
    await session.commit()
    return linked


async def main() -> None:
    await init_db()
    fomo = FomoClient()
    logger.info("FOMO registry worker started (every %ss)", settings.fomo_registry_poll_seconds)
    try:
        while True:
            try:
                if not fomo.has_session:
                    logger.warning("FOMO session not configured; registry stays as last collected")
                else:
                    async with SessionLocal() as session:
                        ranked = await sync_leaderboard(fomo, session)
                    symbols = await watched_symbols()
                    if symbols:
                        async with SessionLocal() as session:
                            linked = await sync_holders(fomo, session, symbols)
                        logger.info("leaderboard: %s ranked, %s wallets newly linked", ranked, linked)
                    else:
                        logger.info("leaderboard: %s ranked, no watched symbols for holders", ranked)
            except FomoAuthError as exc:
                logger.warning("FOMO session expired: %s", exc)
            except FomoRateLimited as exc:
                logger.warning("FOMO rate limited: %s", exc)
            except Exception:
                logger.exception("FOMO registry pass failed")
            await asyncio.sleep(settings.fomo_registry_poll_seconds)
    finally:
        await fomo.close()


if __name__ == "__main__":
    asyncio.run(main())
