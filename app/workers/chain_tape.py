"""On-chain trade tape for tracked wallets -- every token they touch.

Scanning is wallet-centric, not token-centric: ``eth_getLogs`` is filtered by
the tracked wallets in the indexed ``from``/``to`` topics and **not** by token
address, so a wallet's whole trading activity is captured rather than only
the one pair this bot happens to trade. Cost does not grow with the number of
wallets -- a topic position accepts a set of values, so the whole roster is
two calls per block range.

Three jobs share one loop:

0. **Discovery** -- once an hour, look at the watched market and add wallets
   that have started trading since the roster was last built. Without this
   the tape only ever sees wallets someone added by hand, and goes stale the
   moment a new trader shows up.
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
import time

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.chain.tape import (
    AdaptiveBatchSize,
    ChainSwapRow,
    classify_any,
    fetch_wallet_transfers,
    group_by_tx,
    is_batch_too_large_error,
    is_rate_limited_error,
    price_swap,
    RateLimitBackoff,
)
from app.chain.discovery import discover_wallets
from app.chain.tokens import resolve_token_meta
from app.core.config import settings
from app.db.init import init_db
from app.db.models import ChainScanCursor, ChainSwap, ChainTransaction, FomoTrader
from app.db.session import SessionLocal
from app.dex.chain import ChainClient
from app.dex.tokens import DexConfigError, DexPair, resolve_pair, resolve_token
from app.workers.dex_sampler import watched_symbols

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


async def _resolve_pairs(symbols: list[str]) -> list[DexPair]:
    """Markets discovery looks at. The tape itself is wallet-filtered and
    needs no pair -- this is only about where to go looking for new traders."""
    pairs = []
    for symbol in symbols:
        try:
            pairs.append(resolve_pair(symbol))
        except DexConfigError as exc:
            logger.warning("%s skipped: %s", symbol, exc)
    return pairs


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


# ---- discovery ---------------------------------------------------------


async def run_discovery_pass(client: ChainClient, *, chain_id: int) -> int:
    """Add wallets that have started trading since the roster was last built.

    New rows land with ``backfilled_from_block`` NULL, which is the backfill
    queue -- so a wallet discovered here has its recent history scanned on
    the same tick, and the trade that surfaced it is not the one trade that
    goes missing.
    """
    pairs = await _resolve_pairs(await watched_symbols())
    if not pairs:
        return 0

    candidates: list = []
    for pair in pairs:
        candidates.extend(await discover_wallets(
            client, pair,
            blocks=settings.wallet_discovery_blocks,
            top=settings.wallet_discovery_top,
            chain_id=chain_id,
        ))

    async with SessionLocal() as session:
        known = {wallet.lower() for wallet in await _tracked_wallets(session)}
        fresh = []
        for candidate in candidates:
            key = candidate.address.lower()
            if key in known:
                continue
            known.add(key)  # a wallet can rank in two pairs at once
            fresh.append(candidate)

        for candidate in fresh:
            statement = insert(FomoTrader).values(
                # Keyed by address, like the seed file's synthetic ids, so a
                # wallet later named by hand or by FOMO updates this row
                # instead of becoming a second identity for one address.
                fomo_user_id=f"chain:{candidate.address.lower()}",
                evm_address=candidate.address,
                source="discovered",
            )
            await session.execute(
                statement.on_conflict_do_nothing(index_elements=[FomoTrader.fomo_user_id])
            )
        await session.commit()

    if fresh:
        logger.info(
            "discovery: %s new wallet(s), top volume $%s",
            len(fresh), f"{max(c.volume_usd for c in fresh):,.0f}",
        )
    return len(fresh)


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

    # Backfill the queue in groups, not one wallet at a time. A topic
    # position accepts a set of values, so scanning fifty wallets over the
    # window costs exactly what scanning one costs -- and the per-wallet
    # version starved the realtime cursor for hours the first time a name
    # import queued three hundred wallets at once.
    batch = pending[: max(1, settings.chain_tape_backfill_wallets)]
    wallets = [trader.evm_address for trader in batch]

    try:
        transfers = await _fetch_chunked(client, wallets, from_block, current_block)
    except Exception:
        logger.exception("backfill failed for %s wallet(s); will retry next pass", len(wallets))
        return 0

    # Classify against every tracked wallet, not just this batch: the scan
    # returns whole transactions, so a counterparty we already track gets its
    # row for free.
    rows = await _rows_from(client, transfers, all_wallets, chain_id=chain_id)
    async with SessionLocal() as session:
        await _store(session, group_by_tx(transfers), rows, chain_id=chain_id)
        await session.execute(
            update(FomoTrader)
            .where(FomoTrader.fomo_user_id.in_([trader.fomo_user_id for trader in batch]))
            .values(backfilled_from_block=from_block)
        )
        await session.commit()

    logger.info(
        "backfilled %s wallet(s), %s left in queue: %s transfers, %s swaps",
        len(batch), len(pending) - len(batch), len(transfers), len(rows),
    )
    return len(batch)


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
    client = ChainClient(rpc_url=settings.chain_tape_rpc_url or None)
    await client.ensure_ready()
    chain_id = settings.rh_chain_id
    batch = AdaptiveBatchSize(
        current=settings.chain_tape_block_batch_max,
        minimum=settings.chain_tape_block_batch_min,
        maximum=settings.chain_tape_block_batch_max,
    )
    backoff = RateLimitBackoff(
        base=settings.chain_tape_backoff_seconds,
        maximum=settings.chain_tape_backoff_max_seconds,
    )
    logger.info(
        "chain tape worker started (every %ss, discovery every %ss)",
        settings.chain_tape_poll_seconds,
        settings.wallet_discovery_interval_seconds if settings.wallet_discovery_enabled else "off",
    )
    # Run discovery on the first tick so a restart picks up whoever started
    # trading while the worker was down.
    last_discovery = 0.0
    try:
        while True:
            try:
                now = time.monotonic()
                if settings.wallet_discovery_enabled and (
                    now - last_discovery >= settings.wallet_discovery_interval_seconds
                ):
                    last_discovery = now
                    await run_discovery_pass(client, chain_id=chain_id)

                backfilled = await run_backfill_pass(client, chain_id=chain_id)
                if backfilled:
                    logger.info("backfilled %s newly discovered wallets", backfilled)
                written = await run_realtime_pass(client, batch, chain_id=chain_id)
                if written:
                    logger.info("recorded %s swap rows", written)
                backoff.record_success()
            except Exception as exc:
                if is_rate_limited_error(exc):
                    # Not worth a traceback: it is one line, it is expected on
                    # a shared endpoint, and it says nothing about the tape.
                    logger.warning(
                        "node is rate-limiting the tape; waiting %.0fs",
                        backoff.record_refusal(),
                    )
                else:
                    logger.exception("chain tape pass failed")
            await asyncio.sleep(max(settings.chain_tape_poll_seconds, backoff.current))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
