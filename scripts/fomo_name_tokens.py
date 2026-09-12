#!/usr/bin/env python3
"""Name the coins already imported, without re-reading anything from FOMO.

The import names each coin it meets for the first time, so this is only needed
when the naming itself changes -- a new source, a fixed slug -- or to fill in
tokens a DexScreener outage skipped. Tokens already in ``fomo_tokens`` are left
alone, including the ones recorded as "asked, nobody knows".
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.api.fomo_activity import name_tokens  # noqa: E402
from app.db.models import FomoActivityLeg  # noqa: E402
from app.db.session import SessionLocal, database_target  # noqa: E402


async def main() -> int:
    async with SessionLocal() as session:
        wanted = set((await session.execute(select(
            FomoActivityLeg.chain_id, FomoActivityLeg.token_address
        ).distinct())).all())
        print(f"БД: {database_target()}; монет в сделках: {len(wanted)}")
        named = await name_tokens(session, wanted)
    print(f"Названий добавлено: {named}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
