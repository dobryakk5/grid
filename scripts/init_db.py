#!/usr/bin/env python3
"""Create or update the database schema.

The API and the workers do this on startup, so it normally never has to be run
by hand. The manual swap script does not -- it refuses to trade rather than
mutate a database as a side effect of buying something -- so this is how a fresh
database gets its tables.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.init import init_db  # noqa: E402


async def main() -> int:
    # Host only: the URL carries a password.
    target = settings.database_url.rsplit("@", 1)[-1]
    print(f"Applying schema to {target}")
    await init_db()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
