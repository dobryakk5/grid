"""Cost basis, including the cases where the records do not add up."""

from decimal import Decimal

import pytest

from app.dex.costbasis import Basis, Fill, cost_basis

D = Decimal


def test_one_buy_is_its_own_basis_including_its_gas():
    basis = cost_basis([Fill("Buy", D(8), D(40), D("0.5"))], D(8))
    assert basis.cost_quote == D("40.5")
    assert basis.average_price == D("40.5") / D(8)
    assert basis.complete


def test_two_buys_average_by_weight_not_by_count():
    # 2 @ 10 and 8 @ 5 is 60 for 10 -> 6, not the 7.5 a naive mean would give.
    basis = cost_basis([Fill("Buy", D(2), D(20)), Fill("Buy", D(8), D(40))], D(10))
    assert basis.average_price == D(6)


def test_a_sale_consumes_the_oldest_lot_first():
    # Buy 5 @ 2, then 5 @ 10; sell 5. FIFO leaves the expensive lot behind.
    fills = [Fill("Buy", D(5), D(10)), Fill("Buy", D(5), D(50)), Fill("Sell", D(5), D(30))]
    basis = cost_basis(fills, D(5))
    assert basis.cost_quote == D(50) and basis.average_price == D(10)


def test_a_partial_sale_leaves_a_proportional_cost_behind():
    basis = cost_basis([Fill("Buy", D(10), D(100)), Fill("Sell", D(4), D(50))], D(6))
    assert basis.cost_quote == D(60) and basis.average_price == D(10)


def test_sell_gas_does_not_change_what_the_rest_cost():
    # Gas on a sale belongs to that sale's PnL, not to the remaining coins.
    with_gas = cost_basis([Fill("Buy", D(10), D(100)), Fill("Sell", D(4), D(50), D(9))], D(6))
    assert with_gas.cost_quote == D(60)


def test_coins_we_never_bought_are_reported_not_averaged_in():
    # 8 held, nothing recorded: averaging them in at zero would invent profit.
    basis = cost_basis([], D(8))
    assert basis == Basis(D(0), D(0), D(8), D(0))
    assert basis.average_price is None and not basis.complete


def test_a_partly_explained_holding_splits_into_covered_and_not():
    basis = cost_basis([Fill("Buy", D(3), D(30))], D(8))
    assert basis.covered_qty == D(3) and basis.cost_quote == D(30)
    assert basis.uncovered_qty == D(5) and not basis.complete


def test_coins_that_left_without_a_recorded_sale_are_flagged():
    # Records say 10, wallet holds 4: six went somewhere we did not see.
    basis = cost_basis([Fill("Buy", D(10), D(100))], D(4))
    assert basis.covered_qty == D(4) and basis.cost_quote == D(40)
    assert basis.unexplained_outflow == D(6) and not basis.complete


def test_selling_more_than_was_ever_bought_cannot_go_negative():
    basis = cost_basis([Fill("Buy", D(2), D(20)), Fill("Sell", D(5), D(60))], D(0))
    assert basis.cost_quote == D(0) and basis.covered_qty == D(0)


def test_an_unknown_side_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        cost_basis([Fill("Short", D(1), D(1))], D(1))
