import asyncio
import logging

from app.core.config import settings
from app.db.init import init_db
from app.db.session import SessionLocal
from app.exchanges.bybit import BybitClient
from app.trading.grid import GridEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    exchange = BybitClient()
    engine = GridEngine(exchange)
    logger.info("Grid worker started")

    try:
        while True:
            try:
                async with SessionLocal() as session:
                    await engine.tick(session)
            except Exception:
                logger.exception("Grid tick failed")
            await asyncio.sleep(settings.grid_poll_seconds)
    finally:
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(main())
