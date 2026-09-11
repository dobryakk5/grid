"""Replay stored ``chain_transactions`` through the current classify logic.

No RPC calls -- that is the entire point. If ``app.chain.tape.classify_any``
turns out to have mishandled some router path, fix it and re-run this script:
``chain_swaps`` is recomputed from raw data already on disk, not from a fresh
read of the chain. Token metadata comes from the ``chain_tokens`` cache for
the same reason; a token that was never resolved is skipped rather than
fetched.

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

from app.chain.tape import ChainSwapRow, classify_any, price_swap  # noqa: E402
from app.chain.tokens import TokenMeta  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db.models import ChainSwap, ChainToken, ChainTransaction, FomoTrader  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.workers.chain_tape import quote_assets  # noqa: E402


async def main(from_block: int | None) -> int:
    chain_id = settings.rh_chain_id
    async with SessionLocal() as session:
        wallets = {
            row for row in (await session.execute(
                select(FomoTrader.evm_address).where(FomoTrader.evm_address.is_not(None))
            )).scalars()
            if row
        }
        if not wallets:
            print("no tracked wallets in the registry; nothing to rebuild", file=sys.stderr)
            return 1

        token_meta = {
            row.address.lower(): TokenMeta(row.address, row.symbol, row.decimals)
            for row in (await session.execute(
                select(ChainToken).where(ChainToken.chain_id == chain_id)
            )).scalars()
        }
        assets = quote_assets()

        query = select(ChainTransaction).where(ChainTransaction.chain_id == chain_id)
        if from_block is not None:
            query = query.where(ChainTransaction.block_number >= from_block)
        transactions = list(
            (await session.execute(query.order_by(ChainTransaction.block_number))).scalars()
        )

        update_columns = [
            column.name for column in ChainSwap.__table__.columns
            if column.name not in ("tx_hash", "wallet_address", "token_address")
        ]
        rewritten = 0
        for tx in transactions:
            rows = classify_any(
                tx.transfers, wallets,
                token_meta=token_meta, quote_assets=assets, chain_id=chain_id,
            )
            for row in rows:
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
                    index_elements=[ChainSwap.tx_hash, ChainSwap.wallet_address, ChainSwap.token_address],
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
