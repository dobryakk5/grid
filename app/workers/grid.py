import asyncio
import logging

from app.core.config import settings
from app.db.init import init_db
from app.db.session import SessionLocal
from app.trading.grid import GridEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
# httpx logs every request at INFO. This worker polls the venue several times
# a tick per open order, so that alone was two syslog lines per order per
# tick -- the bulk of what filled the disk. Failures still surface as WARNING.
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    await init_db()
    # No explicit client: the engine opens one per venue (Bybit demo, MEXC)
    # based on each profile's `exchange` column.
    engine = GridEngine()
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
        await engine.aclose()


if __name__ == "__main__":
    asyncio.run(main())
