"""Окна и ряды: что считается, а что честно остаётся без ответа."""
from dataclasses import dataclass
from decimal import Decimal

from app.intel.history import flow_series, flow_windows, holder_trend

HOUR = 3600_000
NOW = 1_700_000_000_000


@dataclass
class Sample:
    observed_at_ms: int
    holder_count: int | None
    top10_percent: Decimal | None = None
    top10_percent_free: Decimal | None = None


@dataclass
class Leg:
    side: str
    value_usd: Decimal | None
    occurred_at_ms: int
    user_id: str = "u-alice"


def samples(*points):
    return [Sample(NOW - hours * HOUR, count, None, Decimal(str(free)) if free is not None else None)
            for hours, count, free in points]


def test_holder_growth_is_measured_against_the_newest_sample_before_the_window():
    trend = holder_trend(samples((0, 392, 21), (1, 388, 21), (6, 370, 20), (24, 358, 18)))
    assert trend["holder_count"] == 392
    assert trend["windows"]["1"]["change"] == 4
    assert trend["windows"]["6"]["change"] == 22
    day = trend["windows"]["24"]
    assert day["change"] == 34 and round(day["change_pct"], 1) == 9.5
    # Концентрация едет рядом: +34 адреса при выросшей свободной доле топ-10 --
    # не то же самое, что +34 при упавшей.
    assert round(day["top10_free_change"], 1) == 3.0


def test_a_window_with_nothing_old_enough_behind_it_is_not_answered():
    trend = holder_trend(samples((0, 392, None), (1, 388, None)))
    assert trend["windows"]["1"]["change"] == 4
    # Двадцать часов истории не превращаются в суточную дельту -- ни нулём, ни
    # «примерно сутками».
    assert trend["windows"]["6"] is None and trend["windows"]["24"] is None


def test_a_base_older_than_its_window_travels_with_the_real_interval():
    trend = holder_trend(samples((0, 400, None), (30, 358, None)))
    day = trend["windows"]["24"]
    assert day["change"] == 42 and day["actual_hours"] == 30.0


def test_one_sample_answers_how_many_and_nothing_about_change():
    trend = holder_trend(samples((0, 392, None)))
    assert trend["holder_count"] == 392 and trend["samples"] == 1
    assert all(window is None for window in trend["windows"].values())


def test_nothing_at_all_is_nothing_not_zero():
    assert holder_trend([]) is None
    assert holder_trend(samples((0, None, None))) is None


def test_flow_windows_nest_so_the_day_contains_the_hour():
    legs = [
        Leg("BUY", Decimal("31000"), NOW - 2 * HOUR),
        Leg("SELL", Decimal("18000"), NOW - 3 * HOUR, user_id="u-bob"),
        Leg("BUY", Decimal("9000"), NOW - 30 * HOUR),
        Leg("BUY", Decimal("500"), NOW - 30 * 24 * HOUR),
    ]
    windows = flow_windows(legs, now_ms=NOW)
    assert windows["1"]["trades"] == 0
    assert windows["6"]["net_usd"] == Decimal("13000")
    assert windows["6"]["buyers"] == 1 and windows["6"]["sellers"] == 1
    # Сутки включают шесть часов, а не стоят рядом с ними.
    assert windows["24"]["net_usd"] == Decimal("13000")
    assert windows["72"]["net_usd"] == Decimal("22000") and windows["72"]["trades"] == 3


def test_an_unpriced_leg_counts_as_a_trade_and_not_as_money():
    windows = flow_windows([Leg("BUY", None, NOW - HOUR // 2)], now_ms=NOW)
    hour = windows["1"]
    assert hour["trades"] == 1 and hour["unpriced"] == 1
    assert hour["buy_usd"] == Decimal(0) and hour["buyers"] == 0


def test_the_series_keeps_empty_buckets_so_a_gap_stays_visible():
    series = flow_series([Leg("BUY", Decimal("13000"), NOW - 2 * HOUR)],
                         now_ms=NOW, hours=72, bucket_hours=6)
    assert len(series) == 12
    assert [bucket["trades"] for bucket in series] == [0] * 11 + [1]
    assert series[-1]["net_usd"] == Decimal("13000")
    assert series[0]["from_ms"] == NOW - 72 * HOUR


def test_the_same_total_reads_differently_when_it_arrived_all_at_once():
    steady = [Leg("BUY", Decimal("3000"), NOW - hours * HOUR) for hours in (2, 8, 14, 20)]
    burst = [Leg("BUY", Decimal("12000"), NOW - 2 * HOUR)]
    assert flow_windows(steady, now_ms=NOW)["24"]["net_usd"] == \
        flow_windows(burst, now_ms=NOW)["24"]["net_usd"]
    # Одинаковая сумма, разный ряд -- ровно то, ради чего ряд и существует.
    assert sum(b["trades"] > 0 for b in flow_series(steady, now_ms=NOW)) == 4
    assert sum(b["trades"] > 0 for b in flow_series(burst, now_ms=NOW)) == 1
