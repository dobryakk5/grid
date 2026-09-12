"""Read one wallet's own trades off the chain into ``chain_swaps``.

The tape backfills wallets we *watch* (FOMO traders). Our own wallet is not
one of them, so a coin bought by hand in the Robinhood app leaves no trace in
any table -- and the positions page, which derives cost from our executed
``dex_intents``, honestly reports "средняя цена неизвестна" for it.

This closes that gap from the only source that cannot be argued with: the
transaction itself. A buy hands over USDG in the same transaction that hands
back the token, so the cost is measured, not estimated from a price feed --
which is just as well, since no feed covers these coins.

Scanning is deliberately patient. The public RPC rate-limits hard and refuses
wide ranges over cold history, so slices narrow on a timeout and back off on a
429 rather than giving up and leaving a half-imported wallet behind.

Both writes are idempotent (``chain_transactions`` by tx, ``chain_swaps`` by
tx+wallet+token), so re-running is safe and picks up whatever is new.

Usage::

    .venv/bin/python scripts/import-wallet-history.py 0xWALLET [--blocks N] [--apply]

Without ``--apply`` it only reports what it found, writing nothing.
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.chain.tape import fetch_wallet_transfers, group_by_tx  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.dex.chain import ChainClient  # noqa: E402
from app.workers.chain_tape import _rows_from, _store  # noqa: E402

# Wide enough that a normally active wallet is one or two slices, narrow
# enough that the node will actually serve it over recent history.
SLICE = 2_000_000
MIN_SLICE = 250_000


def _rate_limited(exc: Exception) -> bool:
    return "429" in str(exc) or "Too Many Requests" in str(exc)


def _too_wide(exc: Exception) -> bool:
    return "timed out" in str(exc)


async def _slice(client: ChainClient, wallet: str, start: int, end: int) -> list[dict]:
    """One ``eth_getLogs`` window, waiting out the limiter as long as it takes."""
    delay = 10.0
    while True:
        try:
            return await fetch_wallet_transfers(client, [wallet], start, end)
        except Exception as exc:
            if not _rate_limited(exc):
                raise
            print(f"    rate-limited, waiting {delay:.0f}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 90.0)


async def scan(client: ChainClient, wallet: str, blocks: int | None) -> list[dict]:
    head = await client.w3.eth.block_number
    floor = 0 if blocks is None else max(head - blocks, 0)
    found: list[dict] = []
    end, width = head, SLICE
    while end > floor:
        start = max(floor, end - width)
        try:
            got = await _slice(client, wallet, start, end)
        except Exception as exc:
            # Cold history is slower to serve than recent blocks; a narrower
            # window is the node's own remedy for its timeout.
            if _too_wide(exc) and width > MIN_SLICE:
                width //= 2
                continue
            raise
        found.extend(got)
        print(f"  blocks {start}-{end}: +{len(got)} (total {len(found)})", flush=True)
        if start == floor:
            break
        end = start - 1
        await asyncio.sleep(2)
    return found


def _when(block_time_ms: int) -> str:
    return datetime.fromtimestamp(block_time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def main(wallet: str, blocks: int | None, apply: bool) -> int:
    chain_id = settings.rh_chain_id
    client = ChainClient()
    try:
        transfers = await scan(client, wallet, blocks)
        if not transfers:
            print("no transfers touch this wallet in the scanned range")
            return 0
        grouped = group_by_tx(transfers)
        rows = await _rows_from(client, transfers, {wallet}, chain_id=chain_id)
    finally:
        await client.close()

    print(f"\n{len(transfers)} transfers in {len(grouped)} transactions -> {len(rows)} swaps\n")
    for row in sorted(rows, key=lambda row: row.block_time_ms):
        print(
            f"{_when(row.block_time_ms)} {row.side:4} {str(row.symbol):12} "
            f"{row.token_amount:>22} for {row.quote_amount} {row.quote_symbol} "
            f"@ {row.price} [{row.pricing_source}]"
        )

    if not apply:
        print("\nnothing written (pass --apply to store)")
        return 0

    async with SessionLocal() as session:
        await _store(session, grouped, rows, chain_id=chain_id)
        await session.commit()
    print(f"\nstored {len(grouped)} transactions and {len(rows)} swaps")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wallet", help="wallet address to import")
    parser.add_argument(
        "--blocks", type=int, default=4_000_000,
        help="how far back to scan; omit the flag's value cap with 0 for full history",
    )
    parser.add_argument("--apply", action="store_true", help="write to the database")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.wallet, args.blocks or None, args.apply)))
