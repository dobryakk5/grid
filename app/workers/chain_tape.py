"""On-chain trade tape for tracked wallets -- every token they touch.

Scanning is wallet-centric, not token-centric: ``eth_getLogs`` is filtered by
the tracked wallets in the indexed ``from``/``to`` topics and **not** by token
address, so a wallet's whole trading activity is captured rather than only
the one pair this bot happens to trade. Cost does not grow with the number of
wallets -- a topic position accepts a set of values, so the whole roster is
two calls per block range.

Two jobs share one cadence:

1. **Backfill** -- for every ``fomo_traders`` row with
   ``backfilled_from_block IS NULL``, scan that wallet's last
   ``settings.fomo_new_wallet_backfill_blocks`` *before* the global cursor
   moves any further. Without this, a wallet discovered because it just
   bought something is exactly the wallet whose first purchase would never be
   recorded: it is noticed after the fact, by which point the realtime cursor
   has already passed that block.
2. **Realtime** -- advance the shared cursor forward from the chain head,
   staying ``chain_tape_confirmations`` blocks behind it.

``ChainTransaction`` rows and ``ChainSwap`` rows are written in the same
database transaction as the cursor move / backfill marker, so a crash between
them cannot silently lose data. Note that ``ChainTransaction.transfers``
holds the legs involving *tracked wallets*, which is everything
``classify_any`` needs to re-derive a fill -- not necessarily every leg of
the transaction.
"""

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.chain.tape import (
    AdaptiveBatchSize,
    ChainSwapRow,
    classify_any,
    fetch_wallet_transfers,
    group_by_tx,
    is_batch_too_large_error,
    price_swap,
)
from app.chain.tokens import resolve_token_meta
from app.core.config import settings
from app.db.init import init_db
from app.db.models import ChainScanCursor, ChainSwap, ChainTransaction, FomoTrader
from app.db.session import SessionLocal
from app.dex.chain import ChainClient
from app.dex.tokens import DexConfigError, resolve_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SCOPE_REALTIME = "realtime"

# Assets that count as the money side of a swap. Everything else a wallet
# trades against these is treated as the instrument being bought or sold.
_QUOTE_SYMBOLS = ("USDG", "WETH")


def quote_assets() -> dict[str, str]:
    assets: dict[str, str] = {}
    for symbol in _QUOTE_SYMBOLS:
        try:
            token = resolve_token(symbol)
        except DexConfigError:
            continue
        if token.address:
            assets[token.address.lower()] = token.symbol
    return assets


async def _tracked_wallets(session) -> set[str]:
    result = await session.execute(select(FomoTrader.evm_address).where(FomoTrader.evm_address.is_not(None)))
    return {row for row in result.scalars() if row}


async def _store(session, grouped: dict[str, list[dict]], rows: list[ChainSwapRow], *, chain_id: int) -> None:
    for tx_hash, transfers in grouped.items():
        if not transfers:
            continue
        statement = insert(ChainTransaction).values(
            chain_id=chain_id,
            tx_hash=tx_hash,
            block_number=transfers[0]["block_number"],
            block_time_ms=transfers[0]["block_time_ms"],
            transfers=transfers,
        )
        statement = statement.on_conflict_do_nothing(
            index_elements=[ChainTransaction.chain_id, ChainTransaction.tx_hash]
        )
        await session.execute(statement)

    for row in rows:
        priced = await price_swap(row, session)
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
            set_={"value_usd": statement.excluded.value_usd, "pricing_source": statement.excluded.pricing_source},
        )
        await session.execute(statement)


async def _rows_from(client, transfers: list[dict], wallets: set[str], *, chain_id: int) -> list[ChainSwapRow]:
    """Resolve token metadata for whatever was seen, then classify.

    Metadata resolution owns its own short sessions, so the RPC calls it may
    need happen before the write transaction is opened -- never inside it.
    """
    if not transfers:
        return []
    token_meta = await resolve_token_meta(
        client, SessionLocal, [item["token_address"] for item in transfers], chain_id=chain_id
    )
    assets = quote_assets()
    rows: list[ChainSwapRow] = []
    for tx_transfers in group_by_tx(transfers).values():
        rows.extend(classify_any(
            tx_transfers, wallets, token_meta=token_meta, quote_assets=assets, chain_id=chain_id
        ))
    return rows


async def _fetch_chunked(client, wallets: list[str], from_block: int, to_block: int) -> list[dict]:
    """Walk a wide range in RPC-sized pieces.

    A backfill window is an hour of a 15-blocks-per-second chain, which no
    node will serve as a single ``eth_getLogs``; the realtime batch size is
    the honest unit to ask for.
    """
    chunk = max(settings.chain_tape_block_batch_min, settings.chain_tape_block_batch_max)
    out: list[dict] = []
    start = from_block
    while start <= to_block:
        end = min(start + chunk - 1, to_block)
        out.extend(await fetch_wallet_transfers(client, wallets, start, end))
        start = end + 1
    return out


# ---- backfill ---------------------------------------------------------


async def run_backfill_pass(client: ChainClient, *, chain_id: int) -> int:
    async with SessionLocal() as session:
        pending = list((await session.execute(
            select(FomoTrader).where(
                FomoTrader.backfilled_from_block.is_(None),
                FomoTrader.evm_address.is_not(None),
            )
        )).scalars())
        all_wallets = await _tracked_wallets(session)
    if not pending:
        return 0

    current_block = await client.w3.eth.block_number
    from_block = max(current_block - settings.fomo_new_wallet_backfill_blocks, 0)
    done = 0

    for trader in pending:
        wallet = trader.evm_address
        try:
            transfers = await _fetch_chunked(client, [wallet], from_block, current_block)
        except Exception:
            logger.exception("backfill failed for wallet %s; will retry next pass", wallet)
            continue

        # Classify against every tracked wallet, not just this one: the scan
        # is wallet-filtered, so if another tracked wallet was the
        # counterparty its legs are already in hand and its row is free.
        rows = await _rows_from(client, transfers, all_wallets, chain_id=chain_id)
        async with SessionLocal() as session:
            await _store(session, group_by_tx(transfers), rows, chain_id=chain_id)
            await session.execute(
                update(FomoTrader)
                .where(FomoTrader.fomo_user_id == trader.fomo_user_id)
                .values(backfilled_from_block=from_block)
            )
            await session.commit()
        done += 1
        logger.info("backfilled %s: %s transfers, %s swaps", wallet, len(transfers), len(rows))
    return done


# ---- realtime -----------------------------------------------------------


async def run_realtime_pass(client: ChainClient, batch: AdaptiveBatchSize, *, chain_id: int) -> int:
    async with SessionLocal() as session:
        cursor = await session.get(ChainScanCursor, (chain_id, SCOPE_REALTIME))
        wallets = await _tracked_wallets(session)
    if not wallets:
        return 0

    head = await client.w3.eth.block_number
    safe_head = head - settings.chain_tape_confirmations

    if cursor is None:
        start = safe_head if settings.chain_tape_start_block == 0 else settings.chain_tape_start_block
        async with SessionLocal() as session:
            statement = insert(ChainScanCursor).values(chain_id=chain_id, scope=SCOPE_REALTIME, last_block=start)
            statement = statement.on_conflict_do_nothing(
                index_elements=[ChainScanCursor.chain_id, ChainScanCursor.scope]
            )
            await session.execute(statement)
            await session.commit()
        return 0

    if cursor.last_block >= safe_head:
        return 0

    from_block = cursor.last_block + 1
    to_block = min(from_block + batch.current - 1, safe_head)

    try:
        transfers = await fetch_wallet_transfers(client, sorted(wallets), from_block, to_block)
    except Exception as exc:
        if is_batch_too_large_error(exc):
            new_size = batch.shrink()
            logger.warning("block range too large for %s-%s; shrinking batch to %s blocks", from_block, to_block, new_size)
            return 0
        raise
    batch.record_success()

    rows = await _rows_from(client, transfers, wallets, chain_id=chain_id)
    async with SessionLocal() as session:
        await _store(session, group_by_tx(transfers), rows, chain_id=chain_id)
        await session.execute(
            update(ChainScanCursor)
            .where(ChainScanCursor.chain_id == chain_id, ChainScanCursor.scope == SCOPE_REALTIME)
            .values(last_block=to_block)
        )
        await session.commit()
    return len(rows)


async def main() -> None:
    await init_db()
    client = ChainClient()
    await client.ensure_ready()
    chain_id = settings.rh_chain_id
    batch = AdaptiveBatchSize(
        current=settings.chain_tape_block_batch_max,
        minimum=settings.chain_tape_block_batch_min,
        maximum=settings.chain_tape_block_batch_max,
    )
    logger.info("chain tape worker started (every %ss)", settings.chain_tape_poll_seconds)
    try:
        while True:
            try:
                backfilled = await run_backfill_pass(client, chain_id=chain_id)
                if backfilled:
                    logger.info("backfilled %s newly discovered wallets", backfilled)
                written = await run_realtime_pass(client, batch, chain_id=chain_id)
                if written:
                    logger.info("recorded %s swap rows", written)
            except Exception:
                logger.exception("chain tape pass failed")
            await asyncio.sleep(settings.chain_tape_poll_seconds)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
