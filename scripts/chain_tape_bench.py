"""Acceptance bench for the token-filtered realtime scan.

Measures, over a real window of blocks on the configured RPC: how many
``Transfer`` logs come back, how large the payload is, how long
``eth_getLogs`` takes, and what fraction of the returned transactions
actually touch a tracked wallet.

The realtime scan (``app.workers.chain_tape``) stays token-filtered only as
long as these numbers look reasonable for the chain in question. If the
useful fraction is tiny, switch it to the wallet-topic-filtered + receipt
path already used for wallet backfill -- see
``app.chain.tape.discover_wallet_tx_hashes`` /
``app.chain.tape.fetch_transaction_transfers``. Run this *before* relying on
the realtime path in production, not after.

Usage::

    .venv/bin/python scripts/chain_tape_bench.py --blocks 10000
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.chain.tape import fetch_transfers, group_by_tx, is_batch_too_large_error  # noqa: E402
from app.db.models import FomoTrader  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.dex.chain import ChainClient  # noqa: E402
from app.dex.tokens import DexConfigError, resolve_pair  # noqa: E402
from app.workers.dex_sampler import watched_symbols  # noqa: E402


async def main(blocks: int) -> int:
    client = ChainClient()
    try:
        await client.ensure_ready()

        pairs = []
        for symbol in await watched_symbols():
            try:
                pairs.append(resolve_pair(symbol))
            except DexConfigError as exc:
                print(f"{symbol} skipped: {exc}", file=sys.stderr)
        if not pairs:
            print("no watched DEX pairs configured", file=sys.stderr)
            return 1
        token_addresses = sorted({pair.base.address for pair in pairs} | {pair.quote.address for pair in pairs})

        async with SessionLocal() as session:
            wallets = {
                row for row in (await session.execute(
                    select(FomoTrader.evm_address).where(FomoTrader.evm_address.is_not(None))
                )).scalars()
                if row
            }

        head = await client.w3.eth.block_number
        from_block = max(head - blocks, 0)

        started = time.monotonic()
        try:
            transfers = await fetch_transfers(client, token_addresses, from_block, head)
        except Exception as exc:
            if is_batch_too_large_error(exc):
                print(json.dumps({
                    "blocks_requested": head - from_block,
                    "result": "RPC refused the range (too many results) -- "
                              "this is itself the answer: the realtime scan needs a "
                              "smaller adaptive batch, or the wallet-filtered path.",
                    "error": str(exc)[:300],
                }, indent=2))
                return 0
            raise
        elapsed = time.monotonic() - started

        grouped = group_by_tx(transfers)
        tracked_lower = {wallet.lower() for wallet in wallets}
        interesting = sum(
            1 for tx_transfers in grouped.values()
            if any(
                t["from_address"].lower() in tracked_lower or t["to_address"].lower() in tracked_lower
                for t in tx_transfers
            )
        )
        payload_bytes = len(json.dumps(transfers, default=str))

        report = {
            "blocks_scanned": head - from_block,
            "transfer_logs": len(transfers),
            "transactions": len(grouped),
            "tracked_wallets": len(wallets),
            "interesting_transactions": interesting,
            "useful_fraction": round(interesting / len(grouped), 4) if grouped else None,
            "eth_getLogs_seconds": round(elapsed, 2),
            "payload_json_bytes": payload_bytes,
        }
        print(json.dumps(report, indent=2))
        if report["useful_fraction"] is not None and report["useful_fraction"] < 0.01:
            print(
                "\nUseful fraction is very low -- consider the wallet-topic-filtered path "
                "instead of token-filtered for the realtime scan.",
                file=sys.stderr,
            )
        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=10_000)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.blocks)))
