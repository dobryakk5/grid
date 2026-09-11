"""Seed the trader registry from a hand-curated wallet file.

The path that needs no FOMO session at all: addresses gathered from public
profiles and the public chain go into ``config/fomo_wallets.json``, and the
chain tape starts watching them (backfilling their recent history first).

Usage::

    .venv/bin/python scripts/seed_fomo_wallets.py [--file config/fomo_wallets.json]
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db.init import init_db  # noqa: E402
from app.db.models import FomoTrader  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.fomo.seed import SeedError, apply_seed, load_seed_file  # noqa: E402

DEFAULT_FILE = ROOT / "config" / "fomo_wallets.json"


async def main(path: Path) -> int:
    try:
        wallets = load_seed_file(path)
    except SeedError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    if not wallets:
        print("seed file has no wallets; nothing to do")
        return 0

    await init_db()
    async with SessionLocal() as session:
        counts = await apply_seed(session, wallets)
        await session.commit()
        pending = await session.scalar(
            select(func.count(FomoTrader.fomo_user_id)).where(
                FomoTrader.backfilled_from_block.is_(None),
                FomoTrader.evm_address.is_not(None),
            )
        )

    print(f"seeded {counts['seeded']} wallets from {path.relative_to(ROOT)}")
    print(f"{pending} wallet(s) now queued for backfill -- run `make chain-tape` to scan their history")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.file)))
