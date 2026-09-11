from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.accounting import execution_values, realised_price
from app.dex.pricing import GasCost
from app.dex.receipts import FillReport
from app.dex.tokens import resolve_pair


USDG = "0x" + "ab" * 20


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(
        settings, "dex_tokens", f'{{"USDG": {{"address": "{USDG}", "decimals": 6}}}}'
    )
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


def fill(*, amount_in_wei, amount_out_wei) -> FillReport:
    return FillReport(
        amount_in_wei=amount_in_wei,
        amount_out_wei=amount_out_wei,
        gas_used=200_000,
        effective_gas_price_wei=2_000_000_000,
        tx_hash="0x" + "ab" * 32,
        block_number=1234,
        block_hash="0x" + "cd" * 32,
    )


GAS = GasCost(
    native=Decimal("0.0004"), coin="ETH",
    quote=Decimal("1"), quote_coin="USDG", rate=Decimal("2500"),
)

# 250 USDG in, 500 PONS out -- a buy at 0.50.
BUY = fill(amount_in_wei=250_000_000, amount_out_wei=500 * 10**18)
# 500 PONS in, 300 USDG out -- a sell at 0.60.
SELL = fill(amount_in_wei=500 * 10**18, amount_out_wei=300_000_000)


def test_both_directions_price_in_quote_per_base():
    pair = resolve_pair("PONSUSDG")

    assert realised_price(BUY, pair, "Buy") == Decimal("0.5")
    assert realised_price(SELL, pair, "Sell") == Decimal("0.6")


def test_a_buy_records_base_quantity_and_quote_value():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Buy", fill=BUY, gas=GAS)

    assert values["exec_qty"] == Decimal("500")
    assert values["exec_value"] == Decimal("250")
    assert values["exec_price"] == Decimal("0.5")


def test_a_sell_records_the_same_way_round():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Sell", fill=SELL, gas=GAS)

    assert values["exec_qty"] == Decimal("500")
    assert values["exec_value"] == Decimal("300")
    assert values["exec_price"] == Decimal("0.6")


def test_gas_lands_in_the_fee_column_pnl_actually_sums():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Buy", fill=BUY, gas=GAS)

    # PnL only converts fees paid in the base or quote coin; anything else is
    # dropped into unconverted_fees, so gas has to arrive already converted.
    assert values["exec_fee"] == Decimal("1")
    assert values["fee_currency"] == "USDG"


def test_the_native_gas_figure_is_kept_beside_the_converted_one():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Buy", fill=BUY, gas=GAS)

    assert values["fee_native_amount"] == Decimal("0.0004")
    assert values["fee_native_coin"] == "ETH"


def test_an_unconverted_fill_still_records_the_native_gas():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Buy", fill=BUY, gas=None)

    assert values["exec_fee"] == Decimal("0")
    assert values["fee_native_amount"] == Decimal("0.0004")


def test_the_transaction_hash_is_the_execution_identity():
    values = execution_values(pair=resolve_pair("PONSUSDG"), side="Buy", fill=BUY, gas=GAS)

    assert values["exec_id"] == BUY.tx_hash
    assert values["tx_hash"] == BUY.tx_hash
    assert values["is_maker"] is False


def test_a_fill_that_moved_no_base_token_is_not_a_price():
    pair = resolve_pair("PONSUSDG")
    with pytest.raises(ValueError):
        realised_price(fill(amount_in_wei=1, amount_out_wei=0), pair, "Buy")
