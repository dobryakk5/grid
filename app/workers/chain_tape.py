"""On-chain trade tape for tracked FOMO wallets.

Two jobs share one cadence:

1. **Backfill** -- for every ``fomo_traders`` row with
   ``backfilled_from_block IS NULL``, scan that one wallet's last
   ``settings.fomo_new_wallet_backfill_blocks`` for trades *before* the
   global cursor is allowed to move any further. Without this, a wallet
   discovered because it just bought something is exactly the wallet whose
   first purchase would never be recorded: the registry only learns about it
   after the fact, by which point the realtime cursor has already moved past
   that block.
2. **Realtime** -- advance the shared cursor forward from the chain head,
   staying ``chain_tape_confirmations`` blocks behind it, filtered by token
   address (see ``app.chain.tape`` for why token-filtered rather than
   wallet-filtered is the right axis here).

``ChainTransaction`` rows (raw ``Transfer`` legs) and ``ChainSwap`` rows
(reconstructed fills) are written in the same database transaction as the
cursor move / backfill marker -- a crash between them must not lose data
silently.
"""

import asyncio
import logging

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.chain.tape import (
    AdaptiveBatchSize,
    ChainSwapRow,
    classify,
    discover_wallet_tx_hashes,
    fetch_transaction_transfers,
    fetch_transfers,
    group_by_tx,
    is_batch_too_large_error,
    price_swap,
)
from app.core.config import settings
from app.db.init import init_db
from app.db.models import ChainScanCursor, ChainSwap, ChainTransaction, FomoTrader
from app.db.session import SessionLocal
from app.dex.chain import ChainClient
from app.dex.tokens import DexConfigError, DexPair, resolve_pair
from app.workers.dex_sampler import watched_symbols

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SCOPE_REALTIME = "realtime"


async def _resolve_pairs(symbols: list[str]) -> list[DexPair]:
    pairs = []
    for symbol in symbols:
        try:
            pairs.append(resolve_pair(symbol))
        except DexConfigError as exc:
            logger.warning("%s skipped: %s", symbol, exc)
    return pairs


def _token_addresses(pairs: list[DexPair]) -> list[str]:
    addresses = {pair.base.address for pair in pairs} | {pair.quote.address for pair in pairs}
    return sorted(addresses)


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
            index_elements=[ChainSwap.tx_hash, ChainSwap.wallet_address],
            set_={"value_usd": statement.excluded.value_usd, "pricing_source": statement.excluded.pricing_source},
        )
        await session.execute(statement)


# ---- backfill ---------------------------------------------------------


async def run_backfill_pass(client: ChainClient, pairs: list[DexPair], token_addresses: list[str], *, chain_id: int) -> int:
    async with SessionLocal() as session:
        pending = list((await session.execute(
            select(FomoTrader).where(
                FomoTrader.backfilled_from_block.is_(None),
                FomoTrader.evm_address.is_not(None),
            )
        )).scalars())
    if not pending:
        return 0

    current_block = await client.w3.eth.block_number
    from_block = max(current_block - settings.fomo_new_wallet_backfill_blocks, 0)
    done = 0
    # Shared across every wallet in this pass: trades cluster in the same
    # blocks, and each repeat lookup is a round trip we do not need.
    block_times: dict[int, int] = {}

    async with SessionLocal() as session:
        all_wallets = await _tracked_wallets(session)

    for trader in pending:
        wallet = trader.evm_address
        try:
            tx_hashes = await discover_wallet_tx_hashes(client, wallet, token_addresses, from_block, current_block)
            grouped: dict[str, list[dict]] = {}
            rows: list[ChainSwapRow] = []
            for tx_hash in tx_hashes:
                transfers = await fetch_transaction_transfers(
                    client, tx_hash, token_addresses, block_time_cache=block_times
                )
                if not transfers:
                    continue
                grouped[tx_hash] = transfers
                for pair in pairs:
                    # Classify against *every* tracked wallet, not just the one
                    # being backfilled: we fetched the whole receipt, so if this
                    # transaction also moved another tracked wallet's tokens,
                    # that row is free to record here. Scoping to one wallet
                    # would leave it to that wallet's own backfill window, which
                    # may not reach back this far. The composite PK makes the
                    # overlap idempotent.
                    rows.extend(classify(transfers, all_wallets, pair, chain_id=chain_id))
        except Exception:
            logger.exception("backfill failed for wallet %s; will retry next pass", wallet)
            continue

        async with SessionLocal() as session:
            await _store(session, grouped, rows, chain_id=chain_id)
            await session.execute(
                update(FomoTrader)
                .where(FomoTrader.fomo_user_id == trader.fomo_user_id)
                .values(backfilled_from_block=from_block)
            )
            await session.commit()
        done += 1
        logger.info("backfilled %s: %s tx scanned, %s swaps found", wallet, len(tx_hashes), len(rows))
    return done


# ---- realtime -----------------------------------------------------------


async def run_realtime_pass(
    client: ChainClient, pairs: list[DexPair], token_addresses: list[str], batch: AdaptiveBatchSize, *, chain_id: int
) -> int:
    async with SessionLocal() as session:
        cursor = await session.get(ChainScanCursor, (chain_id, SCOPE_REALTIME))

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
        transfers = await fetch_transfers(client, token_addresses, from_block, to_block)
    except Exception as exc:
        if is_batch_too_large_error(exc):
            new_size = batch.shrink()
            logger.warning("block range too large for %s-%s; shrinking batch to %s blocks", from_block, to_block, new_size)
            return 0
        raise
    batch.record_success()

    grouped = group_by_tx(transfers)

    async with SessionLocal() as session:
        wallets = await _tracked_wallets(session)
        rows: list[ChainSwapRow] = []
        for tx_transfers in grouped.values():
            for pair in pairs:
                rows.extend(classify(tx_transfers, wallets, pair, chain_id=chain_id))
        await _store(session, grouped, rows, chain_id=chain_id)
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
                symbols = await watched_symbols()
                pairs = await _resolve_pairs(symbols)
                if not pairs:
                    logger.info("no DEX pairs to watch; set DEX_WATCH_SYMBOLS")
                else:
                    token_addresses = _token_addresses(pairs)
                    backfilled = await run_backfill_pass(client, pairs, token_addresses, chain_id=chain_id)
                    if backfilled:
                        logger.info("backfilled %s newly discovered wallets", backfilled)
                    written = await run_realtime_pass(client, pairs, token_addresses, batch, chain_id=chain_id)
                    if written:
                        logger.info("recorded %s swap rows", written)
            except Exception:
                logger.exception("chain tape pass failed")
            await asyncio.sleep(settings.chain_tape_poll_seconds)
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
