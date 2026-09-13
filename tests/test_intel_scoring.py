from decimal import Decimal

from app.intel.market import MarketFacts
from app.intel.scoring import score


def risk_lines(card):
    """Risk reasons carry their cost; most assertions only care about the text."""
    return [line["text"] for line in card["reasons"]["risk"]]

NOW = 1_789_000_000_000
DAY = 86_400_000


def market(**overrides):
    return MarketFacts(**{
        "chain_id": 8453, "token_address": "0xcoin", "source": "dexscreener",
        "price_usd": Decimal("0.004"), "market_cap_usd": Decimal("4100000"),
        "fdv_usd": Decimal("5000000"), "liquidity_usd": Decimal("380000"),
        "volume_h24_usd": Decimal("1200000"), "volume_h6_usd": Decimal("400000"),
        "buys_h24": 900, "sells_h24": 600, "change_h6": Decimal("8"),
        "change_h24": Decimal("12"), "pair_created_at_ms": NOW - 45 * DAY, "pools": 3,
        **overrides,
    })


SAFE = {"holder_count": 6481, "top10_percent_free": Decimal("21"), "honeypot": False,
        "mintable": False}
BUYING = {"buy_usd": Decimal("26000"), "sell_usd": Decimal("2000"), "buyers": 4,
          "sellers": 1, "rank_best": 3}


def test_a_honeypot_outranks_every_other_risk_line():
    trap = score(market=market(), security={**SAFE, "honeypot": True}, flow=BUYING, now_ms=NOW)
    clean = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW)
    assert trap["risk"] >= 40 > clean["risk"]
    assert "не даёт продавать (honeypot)" in risk_lines(trap)
    # And the line says what it cost, so a 40 is never unexplained.
    assert next(line["points"] for line in trap["reasons"]["risk"]
                if line["text"].startswith("не даёт")) == 40
    assert all(line["points"] == 0 for line in clean["reasons"]["risk"])
    # And it does not quietly drag momentum down: the three answer different
    # questions, which is the whole reason there are three.
    assert trap["momentum"] == clean["momentum"]


def test_an_unchecked_contract_is_riskier_than_a_clean_one_and_says_so():
    unchecked = score(market=market(), security=None, flow=BUYING, now_ms=NOW)
    clean = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW)
    assert unchecked["risk"] > clean["risk"]
    assert "контракт не проверен" in risk_lines(unchecked)
    assert unchecked["unknown_facts"] > 0


def test_a_missing_fact_is_not_scored_as_a_bad_one():
    # No market data at all: quality is unknown rather than zero, because
    # "nobody has measured this" is not the same as "this measured badly".
    blind = score(market=None, security=None, flow={}, now_ms=NOW)
    assert blind["quality"] is None and blind["momentum"] is None
    assert blind["risk"] > 0


def test_selling_by_the_cohort_shows_up_in_both_momentum_and_risk():
    dumping = score(market=market(), security=SAFE, now_ms=NOW, flow={
        "buy_usd": Decimal("1000"), "sell_usd": Decimal("40000"), "buyers": 1, "sellers": 5,
    })
    assert dumping["signal"] == "разгружают"
    assert dumping["momentum"] < score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW)["momentum"]
    assert any("продаёт больше" in line for line in risk_lines(dumping))


def test_a_market_younger_than_a_day_is_flagged_even_when_everything_else_is_good():
    fresh = score(market=market(pair_created_at_ms=NOW - 3600_000), security=SAFE,
                  flow=BUYING, now_ms=NOW)
    assert "рынок моложе суток" in risk_lines(fresh)


def test_a_thin_pool_makes_the_same_dollar_flow_look_bigger_not_smaller():
    deep = score(market=market(liquidity_usd=Decimal("20000000")), security=SAFE,
                 flow=BUYING, now_ms=NOW)
    thin = score(market=market(liquidity_usd=Decimal("200000")), security=SAFE,
                 flow=BUYING, now_ms=NOW)
    # $24k net into a $200k pool is an event; into a $20M pool it is noise.
    assert thin["momentum"] > deep["momentum"]


def test_a_coin_that_already_tripled_today_is_not_ranked_higher_for_it():
    calm = score(market=market(change_h6=Decimal("25")), security=SAFE, flow=BUYING, now_ms=NOW)
    parabolic = score(market=market(change_h6=Decimal("300"), change_h24=Decimal("400")),
                      security=SAFE, flow=BUYING, now_ms=NOW)
    assert parabolic["momentum"] <= calm["momentum"]
    assert any("вход после движения" in line for line in risk_lines(parabolic))


def test_every_number_comes_with_the_facts_that_produced_it():
    card = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW, catalysts=[
        {"created_at_ms": NOW - 3600_000, "importance": "HIGH"},
    ])
    assert card["reasons"]["quality"] and card["reasons"]["momentum"] and card["reasons"]["risk"]
    assert any("тезисов за сутки 1" in line for line in card["reasons"]["momentum"])
    assert card["market_source"] == "dexscreener"


def test_the_tape_is_labelled_so_a_quiet_tape_is_not_read_as_a_quiet_market():
    tape = score(now_ms=NOW, security=SAFE, flow=BUYING, market=MarketFacts(
        chain_id=4663, token_address="0xpons", source="tape",
        price_usd=Decimal("0.5"), volume_h24_usd=Decimal("5000"), buys_h24=3, sells_h24=1,
    ))
    assert tape["market_source"] == "tape"
    # Liquidity is unknown there, and unknown is counted, not assumed.
    assert "ликвидность неизвестна" in risk_lines(tape)


def test_a_circulating_share_above_the_full_supply_is_dropped_not_shown():
    # DexScreener computes FDV per pool and market cap per token, so the pair
    # can disagree; "в обращении 652% от FDV" is a screener artefact.
    card = score(market=market(market_cap_usd=Decimal("60900000000"),
                               fdv_usd=Decimal("9300000000")),
                 security=SAFE, flow=BUYING, now_ms=NOW)
    assert not any("от FDV" in line for line in card["reasons"]["quality"])


def holders(**windows):
    """Готовый ``holder_trend`` с указанными окнами; остальные — без базы."""
    return {"holder_count": 392, "samples": 9, "observed_at_ms": NOW, "first_at_ms": NOW - DAY,
            "top10_percent": Decimal("34"), "top10_percent_free": Decimal("21"),
            "windows": {key: windows.get(key) for key in ("1", "6", "24")}}


def window(change, pct, *, hours, top10_free_change=None):
    return {"hours": hours, "actual_hours": float(hours), "from_ms": NOW - hours * 3600_000,
            "from_count": 392 - change, "change": change, "change_pct": pct,
            "top10_free_change": top10_free_change}


def test_new_holders_lift_momentum_because_volume_cannot_fake_them():
    quiet = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                  holders=holders(**{"6": window(1, 0.3, hours=6)}))
    growing = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                    holders=holders(**{"6": window(22, 6.0, hours=6)}))
    assert growing["momentum"] > quiet["momentum"]
    assert any("держателей" in line for line in growing["reasons"]["momentum"])


def test_holders_nobody_has_measured_twice_change_no_score_at_all():
    blind = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW)
    empty = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW, holders=holders())
    # Окно без второй точки снимает свой вес со знаменателя, а не приносит ноль:
    # «мы не мерили» не должно ранжироваться ниже, чем «мерили, и не растёт».
    assert empty["momentum"] == blind["momentum"]
    assert not any("держателей" in line for line in empty["reasons"]["momentum"])


def test_six_hours_speaks_for_momentum_before_the_day_does():
    card = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                 holders=holders(**{"6": window(22, 6.0, hours=6),
                                    "24": window(-9, -2.2, hours=24)}))
    # Импульс отвечает на «что сейчас», поэтому берёт шесть часов; суточный
    # отток при этом не теряется — его забирает риск.
    assert any("за 6 ч +22" in line for line in card["reasons"]["momentum"])
    assert any("за 24 ч -9" in line["text"] for line in card["reasons"]["risk"])


def test_holders_leaving_on_a_rising_price_is_counted_as_risk():
    steady = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                   holders=holders(**{"24": window(4, 1.0, hours=24)}))
    draining = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                     holders=holders(**{"24": window(-40, -9.3, hours=24)}))
    assert draining["risk"] > steady["risk"]


def test_a_top10_that_grew_while_holders_did_is_its_own_finding():
    spread = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                   holders=holders(**{"24": window(34, 9.5, hours=24, top10_free_change=0.4)}))
    concentrating = score(market=market(), security=SAFE, flow=BUYING, now_ms=NOW,
                          holders=holders(**{"24": window(34, 9.5, hours=24,
                                                          top10_free_change=7.0)}))
    # Тот же приток адресов, но доля топ-10 выросла на 7 п.п. -- это не то же
    # самое накопление, и одинаковый риск у этих двух случаев был бы враньём.
    assert concentrating["risk"] > spread["risk"]
    assert any("топ-10" in line["text"] for line in concentrating["reasons"]["risk"])
