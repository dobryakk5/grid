"""Token intelligence worker: keep the cards current between page loads.

Slow on purpose. One pass reads DexScreener once per 30 coins, GoPlus once per
chain per batch, and the model only for notes the rules could not read -- at
the default period that is a handful of requests every ten minutes.

Runs without FOMO, without a key and without the optional model package: with
nothing collected yet the pass finds no candidates and says so, which is the
same state as a fresh install rather than an error.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app.core.config import settings
from app.db.init import init_db
from app.db.session import SessionLocal
from app.intel.refresh import refresh

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    await init_db()
    logger.info("token intelligence worker started (every %ss)", settings.intel_refresh_seconds)
    while True:
        try:
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            async with SessionLocal() as session:
                result = await refresh(session, now_ms=now_ms)
            logger.info(
                "монет: %s; снимков рынка: %s; проверено контрактов: %s; "
                "тезисов разобрано правилами: %s, моделью: %s",
                result["tokens"], result["market"], result["security"],
                result["theses"]["rules"], result["theses"]["llm"],
            )
        except Exception:
            logger.exception("intel pass failed")
        await asyncio.sleep(settings.intel_refresh_seconds)


if __name__ == "__main__":
    asyncio.run(main())
