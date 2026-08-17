from app.trading.market_data import (
    HOUR_MS,
    INITIAL_HISTORY_HOURS,
    closed_candles,
    current_hour_start_ms,
    fetch_limit,
)


def test_current_hour_start_ms_rounds_down_to_hour():
    assert current_hour_start_ms(10 * HOUR_MS + 12345) == 10 * HOUR_MS


def test_initial_sync_requests_one_year_and_forming_candle():
    assert fetch_limit(None, 20 * HOUR_MS) == INITIAL_HISTORY_HOURS + 1


def test_incremental_sync_requests_overlap_and_missing_hours():
    assert fetch_limit(17 * HOUR_MS, 20 * HOUR_MS) == 4
    assert fetch_limit(19 * HOUR_MS, 20 * HOUR_MS) == 3


def test_forming_candle_is_not_persisted():
    candles = [
        {"timestamp_ms": 18 * HOUR_MS},
        {"timestamp_ms": 19 * HOUR_MS},
        {"timestamp_ms": 20 * HOUR_MS},
    ]
    assert closed_candles(candles, 20 * HOUR_MS) == candles[:2]
