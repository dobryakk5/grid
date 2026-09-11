"""Worker that turns synthetic limit orders into swaps.

Runs separately from the grid worker: on-chain work is paced by blocks, gas and
API rate limits, not by the few-seconds tick a CEX venue wants.

Every pass settles signed intents *before* it looks at any watching level. A
restarted worker that armed a new attempt while an old transaction was still
confirming would be the one bug this whole design exists to avoid.
"""

import asyncio
import logging

from app.core.config import settings
from app.db.init import init_db
from app.db.session import SessionLocal
from app.dex.chain import ChainClient, ChainError
from app.dex.dexscreener import DexScreenerClient, DexScreenerError
from app.dex.execution import execute_swap
from app.dex.intents import SIGNED_STATUSES, IntentStatus
from app.dex.recovery import rebroadcast, replace_stuck, settle
from app.dex.repository import DexIntentRepository
from app.dex.scheduling import Action, IntentView, plan_intent
from app.dex.tokens import DexConfigError
from app.dex.uniswap import UniswapClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class DexWorker:
    def __init__(self) -> None:
        self.chain = ChainClient()
        self.uniswap = UniswapClient()
        self.market = DexScreenerClient()

    async def aclose(self) -> None:
        for client in (self.chain, self.uniswap, self.market):
            try:
                await client.close()
            except Exception:  # pragma: no cover - best-effort shutdown
                logger.exception("Failed to close a DEX client")

    async def tick(self, session) -> None:
        repository = DexIntentRepository(session)
        intents = await repository.open_intents()
        # Signed rows first, always.
        ordered = sorted(intents, key=lambda row: row.status not in SIGNED_STATUSES)

        for intent in ordered:
            action = plan_intent(IntentView.of(intent))
            try:
                await self._apply(session, repository, intent, action)
            except (ChainError, DexScreenerError, DexConfigError) as exc:
                logger.warning("intent %s (%s): %s", intent.id, action, exc)
                await session.rollback()
            except Exception:
                logger.exception("intent %s failed on %s", intent.id, action)
                await session.rollback()

    async def _apply(self, session, repository, intent, action: str) -> None:
        if action == Action.IGNORE:
            return

        if action == Action.EXPIRE:
            await repository.transition(intent, IntentStatus.EXPIRED)
            await session.commit()
            logger.info("level %s expired without filling", intent.order_link_id)
            return

        if action == Action.ABANDON:
            # A nonce was reserved but nothing was ever signed under it.
            await repository.fail(intent, "reserved a nonce but never signed")
            if intent.wallet_address and intent.nonce is not None:
                await repository.release_nonce(
                    wallet=intent.wallet_address, nonce=int(intent.nonce)
                )
            await session.commit()
            return

        if action == Action.UNBLOCK:
            await repository.transition(
                intent, IntentStatus.WAITING, blocked_reason=None, blocked_until=None
            )
            await session.commit()
            return

        if action == Action.CHECK_RECEIPT:
            await settle(
                session, repository, intent, chain=self.chain, market=self.market
            )
            return

        if action == Action.REBROADCAST:
            outcome = await settle(
                session, repository, intent, chain=self.chain, market=self.market
            )
            if outcome is None:
                await rebroadcast(self.chain, intent)
                logger.info("re-sent %s for intent %s", intent.tx_hash, intent.id)
            return

        if action == Action.REPLACE:
            if await settle(
                session, repository, intent, chain=self.chain, market=self.market
            ):
                return
            retry = await replace_stuck(
                session, repository, intent, chain=self.chain
            )
            if retry is None:
                logger.warning(
                    "level %s gave up after %s attempts",
                    intent.order_link_id, intent.retry_count,
                )
            return

        if action == Action.EVALUATE:
            outcome = await execute_swap(
                session,
                symbol=intent.symbol,
                side=intent.side,
                amount_in=intent.amount_in,
                limit_price=intent.limit_price,
                chain=self.chain,
                uniswap=self.uniswap,
                market=self.market,
                repository=repository,
                intent=intent,
                profile_id=intent.profile_id,
            )
            await session.commit()
            if outcome.status not in {IntentStatus.WAITING, "DRY_RUN"}:
                logger.info(
                    "level %s -> %s%s",
                    intent.order_link_id, outcome.status,
                    f": {outcome.reason}" if outcome.reason else "",
                )


async def main() -> None:
    await init_db()
    worker = DexWorker()
    if settings.dex_dry_run:
        logger.warning(
            "DEX_DRY_RUN is on: levels will be evaluated but never signed or sent"
        )
    logger.info("DEX worker started (every %ss)", settings.dex_poll_seconds)

    try:
        while True:
            try:
                async with SessionLocal() as session:
                    await worker.tick(session)
            except Exception:
                logger.exception("DEX tick failed")
            await asyncio.sleep(settings.dex_poll_seconds)
    finally:
        await worker.aclose()


if __name__ == "__main__":
    asyncio.run(main())
