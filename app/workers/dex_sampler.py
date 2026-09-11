"""Price sampler for on-chain pairs.

DexScreener has no kline endpoint, so candles have to be accumulated: this
worker samples the pool price on a fixed cadence, stores each sample, and folds
closed minutes into ``market_candles`` (also rolled up to 15m and 60m) for the
grid engine to read back through ``RobinhoodClient.klines``.

Runs on its own cadence rather than inside the grid tick: the grid polls every
few seconds per profile, and an upstream API call at that rate buys nothing.
"""

import asyncio
import logging
import time

from sqlalchemy import select

from app.core.config import settings
from app.db.init import init_db
from app.db.models import GridProfile
from app.db.session import SessionLocal
from app.dex.candles import MINUTE_MS, build_candles, record_observation
from app.dex.dexscreener import DexScreenerClient, DexScreenerError
from app.dex.risk import evaluate
from app.dex.tokens import DexConfigError, resolve_pair

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def watched_symbols() -> list[str]:
    """Configured pairs plus whatever on-chain profiles actually trade."""
    symbols = {
        item.strip().upper()
        for item in (settings.dex_watch_symbols or "").split(",")
        if item.strip()
    }
    async with SessionLocal() as session:
        rows = await session.execute(
            select(GridProfile.symbol).where(GridProfile.exchange == "robinhood")
        )
        symbols.update(symbol.upper() for symbol in rows.scalars())
    return sorted(symbols)


async def sample_once(market: DexScreenerClient, symbols: list[str]) -> None:
    for symbol in symbols:
        try:
            pair = resolve_pair(symbol)
        except DexConfigError as exc:
            logger.warning("%s skipped: %s", symbol, exc)
            continue
        try:
            snapshot = await market.snapshot(pair)
        except DexScreenerError as exc:
            logger.warning("%s snapshot failed: %s", symbol, exc)
            continue

        verdict = evaluate(snapshot)
        async with SessionLocal() as session:
            await record_observation(session, snapshot)
            await session.commit()
        logger.info(
            "%s price=%s %s liquidity=$%s volume24h=$%s pools=%s%s",
            symbol,
            snapshot.price_quote,
            pair.quote_coin,
            f"{snapshot.token_liquidity_usd:,.0f}",
            f"{snapshot.token_volume_h24:,.0f}",
            snapshot.pools_considered,
            "" if verdict.ok else f" BLOCKED: {'; '.join(verdict.reasons)}",
        )


async def fold_candles(symbols: list[str]) -> None:
    for symbol in symbols:
        async with SessionLocal() as session:
            try:
                stored = await build_candles(session, symbol)
            except Exception:
                await session.rollback()
                logger.exception("%s candle build failed", symbol)
                continue
            await session.commit()
        if stored.get("1"):
            logger.info(
                "%s candles: %s",
                symbol,
                " ".join(f"{interval}m={count}" for interval, count in stored.items()),
            )


async def main() -> None:
    await init_db()
    market = DexScreenerClient()
    logger.info("DEX sampler started (every %ss)", settings.dex_sample_seconds)
    last_fold_minute = -1

    try:
        while True:
            try:
                symbols = await watched_symbols()
                if symbols:
                    await sample_once(market, symbols)
                    # Candles only change on a minute boundary.
                    minute = int(time.time() * 1000) // MINUTE_MS
                    if minute != last_fold_minute:
                        await fold_candles(symbols)
                        last_fold_minute = minute
                else:
                    logger.info("No DEX pairs to watch; set DEX_WATCH_SYMBOLS")
            except Exception:
                logger.exception("DEX sampler pass failed")
            await asyncio.sleep(settings.dex_sample_seconds)
    finally:
        await market.close()


if __name__ == "__main__":
    asyncio.run(main())
