"""Drop the rows the tape collected before it was told what to collect.

Until the roster and the token cache were bounded, every ERC-20 that brushed
past any of 698 tracked wallets got a ``chain_tokens`` row -- whether it was
a trade or an airdrop, an intermediate hop or a receipt token. Two thirds of
the table never appeared in a single classified swap. The tape no longer
writes those rows; this removes the ones already written.

Run it **after** the bounded code is deployed, never before. The old
``_known_token`` answered 404 for any address missing from ``chain_tokens``,
so pruning first would make coins briefly unbuyable; the new one falls back
to reading the contract, which makes the table a cache again rather than a
permission list.

Dry by default -- it prints what it would remove and changes nothing. Pass
``--apply`` to actually delete.

Usage::

    .venv/bin/python scripts/prune-chain-tokens.py
    .venv/bin/python scripts/prune-chain-tokens.py --apply
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

# A token is junk when nothing ever traded it and we have no interest of our
# own in it. Both legs of a swap count: a quote asset appears in
# ``quote_address`` and would otherwise look untraded. ``dex_wallet_tokens``
# is checked too -- an airdrop sitting in our wallet has no swap anywhere and
# is the last thing to forget the decimals of.
_JUNK = """
    FROM chain_tokens AS t
   WHERE t.chain_id = :chain_id
     AND NOT EXISTS (
         SELECT 1 FROM chain_swaps s
          WHERE lower(s.token_address) = lower(t.address)
             OR lower(s.quote_address) = lower(t.address)
     )
     AND NOT EXISTS (
         SELECT 1 FROM dex_wallet_tokens w
          WHERE w.chain_id = t.chain_id AND lower(w.address) = lower(t.address)
     )
"""


async def main(apply: bool) -> int:
    chain_id = settings.rh_chain_id
    async with SessionLocal() as session:
        # The ordering guard, made mechanical. ``dex_wallet_tokens`` arrives
        # with the same change that taught ``_known_token`` to read a missing
        # contract off the chain; if it is not here, neither is that fallback,
        # and pruning would take coins out of reach until the deploy lands.
        deployed = await session.scalar(text("SELECT to_regclass('dex_wallet_tokens')"))
        if deployed is None:
            print(
                "dex_wallet_tokens does not exist: the bounded-tape change is not "
                "deployed here yet.\nDeploy and let init_db run first -- pruning "
                "before it would make coins briefly unbuyable."
            )
            return 0

        total = await session.scalar(
            text("SELECT count(*) FROM chain_tokens WHERE chain_id = :chain_id"),
            {"chain_id": chain_id},
        )
        junk = await session.scalar(text("SELECT count(*) " + _JUNK), {"chain_id": chain_id})

        print(f"chain_tokens (chain {chain_id}): {total}")
        print(f"  never a leg of any swap, and not ours: {junk}")
        print(f"  kept: {total - junk}")

        if not junk:
            return 0
        if not apply:
            sample = (await session.execute(
                text("SELECT t.address, t.symbol " + _JUNK + " ORDER BY t.created_at DESC LIMIT 5"),
                {"chain_id": chain_id},
            )).all()
            print("\n  a few of them:")
            for address, symbol in sample:
                print(f"    {address}  {symbol or '(no symbol)'}")
            print("\nDry run. Re-run with --apply to delete.")
            return 0

        result = await session.execute(text("DELETE " + _JUNK), {"chain_id": chain_id})
        await session.commit()
        print(f"\ndeleted {result.rowcount} row(s)")
        return result.rowcount or 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually delete; default is a dry run")
    asyncio.run(main(parser.parse_args().apply))
