from decimal import Decimal

from app.trading.math import buy_level, floor_to_step, one_step_down, one_step_up


def test_floor_to_step():
    assert floor_to_step(Decimal("123.456"), Decimal("0.1")) == Decimal("123.4")
    assert floor_to_step(Decimal("0.001234"), Decimal("0.00001")) == Decimal("0.00123")


def test_grid_round_trip():
    price = Decimal("100")
    step = Decimal("0.01")
    sell = one_step_up(price, step)
    buy = one_step_down(sell, step)
    assert sell == Decimal("101")
    assert buy == Decimal("100")


def test_buy_level():
    assert buy_level(Decimal("100"), Decimal("0.01"), 3) == Decimal("97")
