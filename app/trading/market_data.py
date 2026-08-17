import time
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketCandle
from app.exchanges.bybit import BybitClient


MARKET_SYMBOLS = ("BTCUSDT", "ADAUSDT", "XRPUSDT", "SUIUSDT")
MARKET_INTERVAL = "60"
HOUR_MS = 60 * 60 * 1000
INITIAL_HISTORY_HOURS = 365 * 24
UPSERT_BATCH_SIZE = 500


@dataclass(frozen=True)
class CandleSyncResult:
    symbol: str
    fetched: int
    stored: int
    latest_timestamp_ms: int | None


def current_hour_start_ms(now_ms: int | None = None) -> int:
    value = now_ms if now_ms is not None else int(time.time() * 1000)
    return value - value % HOUR_MS


def fetch_limit(latest_timestamp_ms: int | None, hour_start_ms: int) -> int:
    if latest_timestamp_ms is None:
        return INITIAL_HISTORY_HOURS + 1
    missing_hours = max(0, (hour_start_ms - latest_timestamp_ms) // HOUR_MS)
    return max(3, missing_hours + 1)


def closed_candles(candles: list[dict], hour_start_ms: int) -> list[dict]:
    return [item for item in candles if item["timestamp_ms"] < hour_start_ms]


async def sync_symbol_candles(
    session: AsyncSession,
    exchange: BybitClient,
    symbol: str,
    *,
    hour_start_ms: int | None = None,
) -> CandleSyncResult:
    normalized_symbol = symbol.upper()
    boundary = (
        hour_start_ms if hour_start_ms is not None else current_hour_start_ms()
    )
    latest = await session.scalar(
        select(func.max(MarketCandle.timestamp_ms)).where(
            MarketCandle.symbol == normalized_symbol,
            MarketCandle.interval == MARKET_INTERVAL,
        )
    )
    candles = await exchange.klines(
        normalized_symbol,
        interval=MARKET_INTERVAL,
        limit=fetch_limit(latest, boundary),
    )
    rows = [
        {
            "symbol": normalized_symbol,
            "interval": MARKET_INTERVAL,
            "timestamp_ms": item["timestamp_ms"],
            "open": item["open"],
            "high": item["high"],
            "low": item["low"],
            "close": item["close"],
            "volume": item["volume"],
            "turnover": item["turnover"],
        }
        for item in closed_candles(candles, boundary)
    ]
    for offset in range(0, len(rows), UPSERT_BATCH_SIZE):
        statement = insert(MarketCandle).values(
            rows[offset:offset + UPSERT_BATCH_SIZE]
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_market_candle_series_time",
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
                "turnover": statement.excluded.turnover,
                "updated_at": func.now(),
            },
        )
        await session.execute(statement)
    return CandleSyncResult(
        symbol=normalized_symbol,
        fetched=len(candles),
        stored=len(rows),
        latest_timestamp_ms=max((item["timestamp_ms"] for item in rows), default=latest),
    )


async def sync_market_candles(
    session: AsyncSession,
    exchange: BybitClient,
    *,
    symbols: tuple[str, ...] = MARKET_SYMBOLS,
    hour_start_ms: int | None = None,
) -> list[CandleSyncResult]:
    boundary = (
        hour_start_ms if hour_start_ms is not None else current_hour_start_ms()
    )
    results = []
    for symbol in symbols:
        results.append(
            await sync_symbol_candles(
                session, exchange, symbol, hour_start_ms=boundary
            )
        )
    await session.commit()
    return results
