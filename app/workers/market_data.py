import asyncio
import logging

from app.db.init import init_db
from app.db.session import SessionLocal
from app.exchanges.bybit import BybitClient
from app.trading.market_data import sync_market_candles


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    exchange = BybitClient()
    try:
        async with SessionLocal() as session:
            results = await sync_market_candles(session, exchange)
        for result in results:
            logger.info(
                "%s candles synced: fetched=%s stored=%s latest=%s",
                result.symbol,
                result.fetched,
                result.stored,
                result.latest_timestamp_ms,
            )
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
