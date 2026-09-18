"""Суточная строка: дельта только там, где есть вторая точка."""
from dataclasses import dataclass
from decimal import Decimal

from app.intel.daily import render, row, table_rows

HOUR = 3600_000
NOW = 1_700_000_000_000
EMBER = "5dvXTZ5qwgafnHtwu3Ls3QrWx1U4LQsFeCuJgkk4QEC6"
SOLANA = 1399811149


@dataclass
class Snapshot:
    observed_at_ms: int
    market_cap_usd: Decimal | None = None
    fdv_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_h24_usd: Decimal | None = None
    price_usd: Decimal | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None
    change_h24: Decimal | None = None
    pools: int | None = None
    pools_capped: bool | None = None
    source: str = "dexscreener"


@dataclass
class Sample:
    observed_at_ms: int
    holder_count: int | None
    top10_percent: Decimal | None = None
    top10_percent_free: Decimal | None = None


def snapshot(hours_ago: int, cap, liquidity, volume, **extra) -> Snapshot:
    return Snapshot(
        observed_at_ms=NOW - hours_ago * HOUR,
        market_cap_usd=Decimal(cap), fdv_usd=Decimal(cap),
        liquidity_usd=Decimal(liquidity), volume_h24_usd=Decimal(volume), **extra)


def ember(latest, baseline=None, holders=()) -> dict:
    return row(chain_id=SOLANA, token_address=EMBER, symbol="EMBER",
               latest=latest, baseline=baseline, holders=holders)


def test_growth_is_measured_against_the_stored_baseline():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000, pools=21),
                 snapshot(24, 11_000_000, 1_500_000, 4_000_000, pools=18))
    assert round(line["market_cap_change_pct"], 1) == 18.2
    assert round(line["liquidity_change_pct"], 1) == 13.3
    assert round(line["volume_change_pct"], 1) == 60.0
    assert line["baseline_hours"] == 24.0
    assert line["pools"] == 21


def test_without_a_baseline_the_deltas_are_absent_not_zero():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000))
    assert line["market_cap_usd"] == Decimal(13_000_000)
    assert line["market_cap_change_pct"] is None
    assert line["liquidity_change_pct"] is None
    assert line["baseline_hours"] is None


def test_a_baseline_off_the_window_edge_travels_with_its_real_interval():
    # «+18% за 31 час» и «+18% за сутки» -- разные новости, и подпись под
    # таблицей обязана показывать настоящий интервал.
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000),
                 snapshot(31, 11_000_000, 1_500_000, 4_000_000))
    assert line["baseline_hours"] == 31.0


def test_turnover_puts_volume_next_to_the_size_it_belongs_to():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000))
    assert round(line["turnover_pct"], 1) == 49.2


def test_a_fully_circulating_coin_reads_as_a_hundred_percent():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000))
    assert float(line["circulating_pct"]) == 100.0


def test_a_cap_above_its_own_fdv_is_dropped_rather_than_shown():
    # Тот же отбор, что на карточке: расхождение двух чисел -- не факт о
    # предложении, и «в обращении 130%» показывать нельзя.
    latest = snapshot(0, 13_000_000, 1_700_000, 6_400_000)
    latest.fdv_usd = Decimal(9_000_000)
    assert ember(latest)["circulating_pct"] is None


def test_the_buy_sell_column_is_a_count_ratio_and_says_nothing_when_a_side_is_empty():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000, buys_h24=23703, sells_h24=22942))
    assert round(line["buy_sell_ratio"], 3) == 1.033
    assert ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000))["buy_sell_ratio"] is None


def test_holders_arrive_with_their_own_window():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000),
                 holders=[Sample(NOW - 26 * HOUR, 1200, None, Decimal("18")),
                          Sample(NOW, 1290, None, Decimal("17"))])
    assert line["holders"] == 1290 and line["holders_change"] == 90
    assert line["holders_hours"] == 26.0
    assert float(line["top10_free_pct"]) == 17.0


def test_a_single_holder_sample_answers_how_many_and_nothing_about_change():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000),
                 holders=[Sample(NOW, 1290)])
    assert line["holders"] == 1290 and line["holders_change"] is None


def test_a_watched_coin_with_no_snapshot_yet_is_an_empty_row_not_a_missing_one():
    rows = table_rows([(SOLANA, EMBER)], names={}, snapshots={}, baselines={}, holders={})
    assert len(rows) == 1 and rows[0]["observed_at_ms"] is None
    assert "снимков ещё нет" in render(rows)


def test_the_rendered_table_marks_what_is_unknown_instead_of_printing_a_zero():
    rows = table_rows([(SOLANA, EMBER)],
                      names={(SOLANA, EMBER): ("EMBER", "EmberCurve")},
                      snapshots={(SOLANA, EMBER): snapshot(0, 13_000_000, 1_700_000, 6_400_000)},
                      baselines={}, holders={})
    text = render(rows)
    assert "EMBER" in text and "$13.00M" in text and "—" in text
    assert "рынка старше 24 ч нет" in text


def test_the_window_moves_both_deltas_at_once():
    # --hours 6 не должен давать «Δкап за 6 часов» рядом с «Δдерж за сутки»:
    # под одним заголовком это читается как одно окно.
    holders = [Sample(NOW - 7 * HOUR, 1200), Sample(NOW - 2 * HOUR, 1250), Sample(NOW, 1290)]
    line = row(chain_id=SOLANA, token_address=EMBER, symbol="EMBER",
               latest=snapshot(0, 13_000_000, 1_700_000, 6_400_000),
               baseline=snapshot(6, 12_000_000, 1_600_000, 5_000_000),
               holders=holders, hours=6)
    assert line["baseline_hours"] == 6.0
    assert line["holders_change"] == 90 and line["holders_hours"] == 7.0


def test_a_truncated_answer_reads_as_a_floor_not_a_total():
    # Источник отдаёт не больше 30 пулов за ответ и не говорит, что обрезал.
    # «21 пул» и «пулов не меньше 21» -- разные утверждения.
    latest = snapshot(0, 13_000_000, 1_700_000, 6_400_000, pools=21)
    latest.pools_capped = True
    rows = [ember(latest)]
    text = render(rows)
    assert rows[0]["pools_capped"] is True
    assert "21+" in text and "уперся в свой потолок" in text


def test_an_untruncated_answer_says_nothing_about_a_ceiling():
    latest = snapshot(0, 13_000_000, 1_700_000, 6_400_000, pools=3)
    latest.pools_capped = False
    text = render([ember(latest)])
    assert "3" in text and "потолок" not in text


def test_depth_puts_liquidity_next_to_the_size_it_has_to_move():
    line = ember(snapshot(0, 13_000_000, 1_700_000, 6_400_000))
    assert round(line["depth_pct"], 1) == 13.1
    # Без капитализации глубина не существует, а не равна нулю.
    thin = Snapshot(observed_at_ms=NOW, liquidity_usd=Decimal(1_700_000))
    assert ember(thin)["depth_pct"] is None
