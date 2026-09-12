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
