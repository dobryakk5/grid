"""Replay stored ``chain_transactions`` through the current classify() logic.

No RPC calls -- that is the entire point. If ``app.chain.tape.classify`` turns
out to have mishandled some Universal Router path, fix it and re-run this
script: ``chain_swaps`` is recomputed from raw data already on disk, not from
a fresh read of the chain.

Usage::

    .venv/bin/python scripts/rebuild-chain-swaps.py [--from-block N]
"""

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from app.chain.tape import ChainSwapRow, classify, price_swap  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.models import ChainSwap, ChainTransaction, FomoTrader  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.dex.tokens import DexConfigError, resolve_pair  # noqa: E402
from app.workers.dex_sampler import watched_symbols  # noqa: E402


async def _pairs():
    pairs = []
    for symbol in await watched_symbols():
        try:
            pairs.append(resolve_pair(symbol))
        except DexConfigError as exc:
            print(f"{symbol} skipped: {exc}", file=sys.stderr)
    return pairs


async def main(from_block: int | None) -> int:
    chain_id = settings.rh_chain_id
    async with SessionLocal() as session:
        wallets = {
            row for row in (await session.execute(
                select(FomoTrader.evm_address).where(FomoTrader.evm_address.is_not(None))
            )).scalars()
            if row
        }
        pairs = await _pairs()
        if not pairs:
            print("no watched DEX pairs configured", file=sys.stderr)
            return 1

        query = select(ChainTransaction).where(ChainTransaction.chain_id == chain_id)
        if from_block is not None:
            query = query.where(ChainTransaction.block_number >= from_block)
        transactions = list(
            (await session.execute(query.order_by(ChainTransaction.block_number))).scalars()
        )

        update_columns = [
            column.name for column in ChainSwap.__table__.columns
            if column.name not in ("tx_hash", "wallet_address")
        ]
        rewritten = 0
        for tx in transactions:
            for pair in pairs:
                for row in classify(tx.transfers, wallets, pair, chain_id=chain_id):
                    priced: ChainSwapRow = await price_swap(row, session)
                    statement = insert(ChainSwap).values(
                        tx_hash=priced.tx_hash,
                        wallet_address=priced.wallet_address,
                        chain_id=priced.chain_id,
                        block_number=priced.block_number,
                        block_time_ms=priced.block_time_ms,
                        token_address=priced.token_address,
                        symbol=priced.symbol,
                        side=priced.side,
                        token_amount=priced.token_amount,
                        quote_address=priced.quote_address,
                        quote_symbol=priced.quote_symbol,
                        quote_amount=priced.quote_amount,
                        price=priced.price,
                        value_usd=priced.value_usd,
                        pricing_source=priced.pricing_source,
                    )
                    statement = statement.on_conflict_do_update(
                        index_elements=[ChainSwap.tx_hash, ChainSwap.wallet_address],
                        set_={name: getattr(statement.excluded, name) for name in update_columns},
                    )
                    await session.execute(statement)
                    rewritten += 1
        await session.commit()

    print(f"replayed {len(transactions)} transactions, wrote {rewritten} swap rows")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-block", type=int, default=None, help="only replay transactions at or after this block")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.from_block)))
