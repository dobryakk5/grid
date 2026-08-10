from decimal import Decimal

import pytest

from app.trading.math import (
    floor_to_step, grid_buy_levels, grid_lines, strategy_grid_cells,
    strategy_grid_lines,
)


def test_floor_to_step():
    assert floor_to_step(Decimal("123.456"), Decimal("0.1")) == Decimal("123.4")
    assert floor_to_step(Decimal("0.001234"), Decimal("0.00001")) == Decimal("0.00123")


def test_absolute_grid_profile():
    buys = grid_buy_levels(Decimal("62000"), Decimal("67000"), Decimal("1000"))
    assert buys == [
        Decimal("62000"),
        Decimal("63000"),
        Decimal("64000"),
        Decimal("65000"),
        Decimal("66000"),
    ]
    assert grid_lines(Decimal("62000"), Decimal("67000"), Decimal("1000"))[-1] == Decimal("67000")


def test_invalid_grid():
    with pytest.raises(ValueError):
        grid_buy_levels(Decimal("62000"), Decimal("62500"), Decimal("1000"))


def test_geometric_grid_uses_equal_percentage_until_upper_bound():
    lines = strategy_grid_lines(
        Decimal("100"), Decimal("125"), Decimal("1"),
        mode="geometric", step_percent=Decimal("10"),
    )
    assert lines == [Decimal("100"), Decimal("110"), Decimal("121"), Decimal("125")]
    assert strategy_grid_cells(
        Decimal("100"), Decimal("121"), Decimal("1"),
        mode="geometric", step_percent=Decimal("10"),
    ) == [(Decimal("100"), Decimal("110")), (Decimal("110"), Decimal("121"))]
