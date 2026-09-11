"""Candles for on-chain pairs, folded out of price samples.

A DEX has no kline endpoint, but ``GridEngine`` asks its exchange client for
candles directly (EMA, breakout confirmation, the market-range picker). So the
sampler writes observations, this module folds them into OHLC, and the
Robinhood client serves those rows back as ``klines``.

Volume stays ``NULL`` on every row we produce. DexScreener reports a rolling
24h figure, and slicing it per minute would put an invented number in the same
column as exchange-reported ones. Everything the engine needs today reads
closes; when real volume matters, the honest source is indexed on-chain Swap
events, not arithmetic on a rolling total.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DexPriceObservation, MarketCandle
from app.dex.dexscreener import MarketSnapshot

__all__ = [
    "CANDLE_SOURCE",
    "DEX_INTERVALS",
    "MINUTE_MS",
    "aggregate_minute_candles",
    "build_candles",
    "load_candles",
    "record_observation",
    "rollup",
]

MINUTE_MS = 60_000
CANDLE_SOURCE = "dex_sample"

# Minute counts the engine actually asks for, in the Bybit interval vocabulary.
DEX_INTERVALS: tuple[str, ...] = ("1", "15", "60")

# Observations older than this are not worth re-folding on every pass.
_BACKFILL_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


def _bucket(timestamp_ms: int, interval_ms: int) -> int:
    return timestamp_ms - timestamp_ms % interval_ms


def aggregate_minute_candles(
    observations: Sequence[tuple[int, Decimal]], *, closed_before_ms: int
) -> list[dict]:
    """Fold ``(timestamp_ms, price)`` samples into closed 1m OHLC rows.

    Samples must be chronological. A minute with no sample produces no row --
    a gap in sampling should be visible as a gap, not as a flat candle.
    """
    buckets: dict[int, dict] = {}
    for timestamp_ms, price in observations:
        start = _bucket(int(timestamp_ms), MINUTE_MS)
        if start + MINUTE_MS > closed_before_ms:
            continue
        price = Decimal(price)
        row = buckets.get(start)
        if row is None:
            buckets[start] = {
                "timestamp_ms": start,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": None,
                "turnover": None,
            }
            continue
        row["high"] = max(row["high"], price)
        row["low"] = min(row["low"], price)
        row["close"] = price
    return [buckets[key] for key in sorted(buckets)]


def rollup(candles: Iterable[dict], *, interval_ms: int, closed_before_ms: int) -> list[dict]:
    """Merge finer candles into ``interval_ms`` buckets, closed buckets only."""
    buckets: dict[int, dict] = {}
    for candle in candles:
        start = _bucket(int(candle["timestamp_ms"]), interval_ms)
        if start + interval_ms > closed_before_ms:
            continue
        row = buckets.get(start)
        if row is None:
            buckets[start] = {
                "timestamp_ms": start,
                "open": candle["open"],
                "high": candle["high"],
                "low": candle["low"],
                "close": candle["close"],
                "volume": None,
                "turnover": None,
            }
            continue
        row["high"] = max(row["high"], candle["high"])
        row["low"] = min(row["low"], candle["low"])
        row["close"] = candle["close"]
    return [buckets[key] for key in sorted(buckets)]


async def record_observation(session: AsyncSession, snapshot: MarketSnapshot) -> None:
    """Store one sample; a duplicate timestamp for a symbol is a no-op."""
    statement = insert(DexPriceObservation).values(
        symbol=snapshot.symbol,
        timestamp_ms=snapshot.observed_at_ms,
        price_quote=snapshot.price_quote,
        price_usd=snapshot.price_usd,
        liquidity_usd=snapshot.token_liquidity_usd,
        volume_h24_usd=snapshot.token_volume_h24,
        pair_address=snapshot.pair_address,
    )
    await session.execute(
        statement.on_conflict_do_nothing(constraint="uq_dex_observation_symbol_time")
    )


async def _store(
    session: AsyncSession, symbol: str, interval: str, rows: list[dict]
) -> int:
    if not rows:
        return 0
    values = [
        {
            "symbol": symbol,
            "interval": interval,
            "timestamp_ms": row["timestamp_ms"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "turnover": row["turnover"],
            "source": CANDLE_SOURCE,
        }
        for row in rows
    ]
    statement = insert(MarketCandle).values(values)
    statement = statement.on_conflict_do_update(
        constraint="uq_market_candle_series_time",
        set_={
            "open": statement.excluded.open,
            "high": statement.excluded.high,
            "low": statement.excluded.low,
            "close": statement.excluded.close,
            "source": statement.excluded.source,
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)
    return len(values)


async def build_candles(
    session: AsyncSession, symbol: str, *, now_ms: int | None = None
) -> dict[str, int]:
    """Re-fold recent observations into 1m candles and roll them up.

    Idempotent: re-running only rewrites rows it would have written anyway, so
    a restarted sampler never leaves a half-built series behind.
    """
    boundary = now_ms if now_ms is not None else int(time.time() * 1000)
    since = boundary - _BACKFILL_WINDOW_MS
    result = await session.execute(
        select(
            DexPriceObservation.timestamp_ms, DexPriceObservation.price_quote
        )
        .where(
            DexPriceObservation.symbol == symbol,
            DexPriceObservation.timestamp_ms >= since,
        )
        .order_by(DexPriceObservation.timestamp_ms)
    )
    observations = [(int(row[0]), Decimal(row[1])) for row in result.all()]
    minutes = aggregate_minute_candles(observations, closed_before_ms=boundary)

    stored = {"1": await _store(session, symbol, "1", minutes)}
    for interval in DEX_INTERVALS:
        if interval == "1":
            continue
        rows = rollup(
            minutes,
            interval_ms=int(interval) * MINUTE_MS,
            closed_before_ms=boundary,
        )
        stored[interval] = await _store(session, symbol, interval, rows)
    return stored


async def load_candles(
    session: AsyncSession, symbol: str, *, interval: str, limit: int
) -> list[dict]:
    """Oldest-first candles in the ``ExchangeClient.klines`` shape."""
    result = await session.execute(
        select(MarketCandle)
        .where(MarketCandle.symbol == symbol, MarketCandle.interval == interval)
        .order_by(MarketCandle.timestamp_ms.desc())
        .limit(limit)
    )
    rows = list(result.scalars())
    rows.reverse()
    return [
        {
            "timestamp_ms": row.timestamp_ms,
            "open": Decimal(row.open),
            "high": Decimal(row.high),
            "low": Decimal(row.low),
            "close": Decimal(row.close),
            "volume": None if row.volume is None else Decimal(row.volume),
            "turnover": None if row.turnover is None else Decimal(row.turnover),
        }
        for row in rows
    ]
