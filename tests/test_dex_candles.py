from decimal import Decimal

from app.dex.candles import MINUTE_MS, aggregate_minute_candles, rollup


def samples(*pairs):
    return [(ts, Decimal(price)) for ts, price in pairs]


def test_samples_fold_into_ohlc_per_minute():
    observations = samples(
        (0, "0.50"), (15_000, "0.56"), (30_000, "0.48"), (45_000, "0.52"),
    )
    candles = aggregate_minute_candles(observations, closed_before_ms=MINUTE_MS)

    assert len(candles) == 1
    candle = candles[0]
    assert candle["timestamp_ms"] == 0
    assert candle["open"] == Decimal("0.50")
    assert candle["high"] == Decimal("0.56")
    assert candle["low"] == Decimal("0.48")
    assert candle["close"] == Decimal("0.52")


def test_volume_is_left_empty_rather_than_invented():
    candles = aggregate_minute_candles(samples((0, "0.5")), closed_before_ms=MINUTE_MS)
    assert candles[0]["volume"] is None
    assert candles[0]["turnover"] is None


def test_the_forming_minute_is_not_emitted():
    observations = samples((0, "0.50"), (MINUTE_MS + 1_000, "0.60"))
    candles = aggregate_minute_candles(observations, closed_before_ms=MINUTE_MS + 30_000)

    assert [candle["timestamp_ms"] for candle in candles] == [0]


def test_a_sampling_gap_stays_a_gap():
    observations = samples((0, "0.50"), (3 * MINUTE_MS, "0.60"))
    candles = aggregate_minute_candles(observations, closed_before_ms=10 * MINUTE_MS)

    # No flat filler candles for minutes 1 and 2.
    assert [candle["timestamp_ms"] for candle in candles] == [0, 3 * MINUTE_MS]


def test_rollup_merges_minutes_into_the_larger_bucket():
    minutes = aggregate_minute_candles(
        samples(
            (0, "0.50"), (MINUTE_MS, "0.62"), (2 * MINUTE_MS, "0.45"),
            (3 * MINUTE_MS, "0.51"),
        ),
        closed_before_ms=15 * MINUTE_MS,
    )
    quarters = rollup(
        minutes, interval_ms=15 * MINUTE_MS, closed_before_ms=15 * MINUTE_MS
    )

    assert len(quarters) == 1
    assert quarters[0]["open"] == Decimal("0.50")
    assert quarters[0]["high"] == Decimal("0.62")
    assert quarters[0]["low"] == Decimal("0.45")
    assert quarters[0]["close"] == Decimal("0.51")


def test_rollup_withholds_a_bucket_that_is_still_forming():
    minutes = aggregate_minute_candles(
        samples((0, "0.50"), (MINUTE_MS, "0.62")), closed_before_ms=5 * MINUTE_MS
    )
    assert rollup(minutes, interval_ms=15 * MINUTE_MS, closed_before_ms=5 * MINUTE_MS) == []
