from decimal import Decimal
from types import SimpleNamespace

from app.intel.movement import movement

NOW = 1_789_000_000_000
HOUR = 3600_000


def snapshot(**overrides):
    return SimpleNamespace(**{
        "observed_at_ms": NOW, "price_usd": Decimal("0.005"),
        "liquidity_usd": Decimal("400000"), **overrides,
    })


def leg(**overrides):
    return SimpleNamespace(**{
        "side": "BUY", "value_usd": Decimal("5000"), "occurred_at_ms": NOW - 600_000,
        **overrides,
    })


def test_the_window_is_the_gap_between_two_measured_snapshots():
    change = movement(snapshot(), snapshot(observed_at_ms=NOW - 2 * HOUR,
                                           price_usd=Decimal("0.004"),
                                           liquidity_usd=Decimal("500000")))
    assert change["since_ms"] == NOW - 2 * HOUR and change["hours"] == 2.0
    assert change["price_change_pct"] == Decimal("25")
    assert change["liquidity_change_pct"] == Decimal("-20")


def test_only_trades_that_happened_after_the_previous_snapshot_count():
    previous = snapshot(observed_at_ms=NOW - HOUR)
    change = movement(snapshot(), previous, legs=[
        leg(),                                            # внутри окна
        leg(side="SELL", value_usd=Decimal("1000")),      # внутри окна
        leg(occurred_at_ms=NOW - 10 * HOUR, value_usd=Decimal("90000")),  # вчерашняя
    ])
    assert change["buy_usd"] == Decimal("5000") and change["sell_usd"] == Decimal("1000")
    assert change["net_usd"] == Decimal("4000") and change["trades"] == 2


def test_an_unpriced_trade_is_counted_but_not_summed():
    change = movement(snapshot(), snapshot(observed_at_ms=NOW - HOUR),
                      legs=[leg(value_usd=None)])
    assert change["trades"] == 1 and change["net_usd"] == Decimal(0)


def test_fresh_theses_are_counted_the_same_way():
    change = movement(snapshot(), snapshot(observed_at_ms=NOW - HOUR), notes=[
        {"created_at_ms": NOW - 600_000}, {"created_at_ms": NOW - 5 * HOUR}, {},
    ])
    assert change["theses"] == 1


def test_the_first_pass_has_nothing_to_compare_with():
    assert movement(snapshot(), None) is None
    assert movement(None, snapshot()) is None
    # Два снимка с одним временем — это один и тот же проход, а не изменение.
    assert movement(snapshot(), snapshot()) is None


def test_a_price_that_was_unknown_before_does_not_become_a_change():
    change = movement(snapshot(), snapshot(observed_at_ms=NOW - HOUR, price_usd=None,
                                           liquidity_usd=Decimal(0)))
    assert change["price_change_pct"] is None
    # Ноль в знаменателе — это не «рост на бесконечность».
    assert change["liquidity_change_pct"] is None
