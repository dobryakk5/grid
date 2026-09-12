from decimal import Decimal
from types import SimpleNamespace

from app.intel.tape import aggregate_tape

NOW = 1_789_000_000_000
HOUR = 3600_000


def swap(**overrides):
    return SimpleNamespace(**{
        "token_address": "0xPons", "symbol": "PONS", "side": "BUY",
        "block_time_ms": NOW - HOUR, "token_amount": Decimal("1000"),
        "value_usd": Decimal("500"), **overrides,
    })


def test_the_tape_prices_a_coin_from_what_it_measured_not_from_the_quote_leg():
    facts = aggregate_tape([
        swap(block_time_ms=NOW - 20 * HOUR, value_usd=Decimal("400"), token_amount=Decimal("1000")),
        swap(block_time_ms=NOW - HOUR, value_usd=Decimal("600"), token_amount=Decimal("1000")),
    ], chain_id=4663, now_ms=NOW)[(4663, "0xpons")]
    assert facts.source == "tape"
    assert facts.price_usd == Decimal("0.6")
    assert facts.change_h24 == Decimal("50")
    assert facts.volume_h24_usd == Decimal("1000")
    # Never invented: the tape watches wallets, not pools or supply.
    assert facts.liquidity_usd is None and facts.market_cap_usd is None


def test_only_the_window_counts_and_six_hours_is_measured_separately():
    facts = aggregate_tape([
        swap(block_time_ms=NOW - 30 * HOUR, value_usd=Decimal("900")),
        swap(block_time_ms=NOW - 20 * HOUR, value_usd=Decimal("100")),
        swap(block_time_ms=NOW - 2 * HOUR, value_usd=Decimal("50")),
    ], chain_id=4663, now_ms=NOW)[(4663, "0xpons")]
    assert facts.volume_h24_usd == Decimal("150")
    assert facts.volume_h6_usd == Decimal("50")
    assert facts.buys_h24 == 2


def test_an_unpriced_swap_is_still_a_trade_but_not_volume():
    facts = aggregate_tape([
        swap(value_usd=None), swap(side="SELL", value_usd=Decimal("200")),
    ], chain_id=4663, now_ms=NOW)[(4663, "0xpons")]
    assert (facts.buys_h24, facts.sells_h24) == (1, 1)
    assert facts.volume_h24_usd == Decimal("200")


def test_one_priced_swap_cannot_produce_a_change():
    facts = aggregate_tape([swap()], chain_id=4663, now_ms=NOW)[(4663, "0xpons")]
    assert facts.price_usd == Decimal("0.5") and facts.change_h24 is None
