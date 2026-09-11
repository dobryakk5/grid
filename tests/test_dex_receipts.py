from decimal import Decimal

import pytest

from app.dex.receipts import (
    TRANSFER_TOPIC,
    ReceiptError,
    parse_swap_fill,
    wallet_deltas,
)
from app.dex.tokens import Token


WALLET = "0x" + "99" * 20
POOL_A = "0x" + "aa" * 20
POOL_B = "0x" + "bb" * 20

PONS = Token(symbol="PONS", address="0x" + "11" * 20, decimals=18)
USDG = Token(symbol="USDG", address="0x" + "22" * 20, decimals=6)
WETH = Token(symbol="WETH", address="0x" + "33" * 20, decimals=18)
ETH = Token(symbol="ETH", address="0x" + "00" * 20, decimals=18, native=True)


def topic(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


def transfer(token: Token, sender: str, recipient: str, amount: int) -> dict:
    return {
        "address": token.address,
        "topics": [TRANSFER_TOPIC, topic(sender), topic(recipient)],
        "data": hex(amount),
    }


def receipt(logs, *, status=1, gas_used=180_000, gas_price=1_500_000_000) -> dict:
    return {
        "status": status,
        "logs": logs,
        "gasUsed": gas_used,
        "effectiveGasPrice": gas_price,
        "transactionHash": "0x" + "ab" * 32,
        "blockNumber": 1234,
        "blockHash": "0x" + "cd" * 32,
    }


def test_intermediate_hops_do_not_count_as_our_fill():
    # USDG -> WETH -> PONS: only the first and last legs touch the wallet.
    logs = [
        transfer(USDG, WALLET, POOL_A, 250_000_000),
        transfer(WETH, POOL_A, POOL_B, 100_000_000_000_000_000),
        transfer(PONS, POOL_B, WALLET, 454_000_000_000_000_000_000),
    ]
    fill = parse_swap_fill(
        receipt(logs), wallet=WALLET, token_in=USDG, token_out=PONS
    )

    assert fill.amount_in(USDG) == Decimal("250")
    assert fill.amount_out(PONS) == Decimal("454")
    assert fill.price(token_in=USDG, token_out=PONS) == Decimal("250") / Decimal("454")


def test_price_comes_from_the_receipt_not_the_quote():
    # Quoted 454.5 PONS, actually received 450 -- the realised price is worse.
    logs = [
        transfer(USDG, WALLET, POOL_A, 250_000_000),
        transfer(PONS, POOL_A, WALLET, 450_000_000_000_000_000_000),
    ]
    fill = parse_swap_fill(receipt(logs), wallet=WALLET, token_in=USDG, token_out=PONS)

    assert fill.price(token_in=USDG, token_out=PONS) > Decimal("0.55")


def test_native_input_is_measured_by_the_transaction_value():
    # ETH movements emit no Transfer log at all.
    logs = [transfer(PONS, POOL_A, WALLET, 4_500_000_000_000_000_000)]
    fill = parse_swap_fill(
        receipt(logs),
        wallet=WALLET,
        token_in=ETH,
        token_out=PONS,
        sent_value_wei=1_000_000_000_000_000,
    )

    assert fill.amount_in(ETH) == Decimal("0.001")
    assert fill.amount_out(PONS) == Decimal("4.5")


def test_gas_is_reported_in_the_native_coin():
    logs = [transfer(PONS, POOL_A, WALLET, 4_500_000_000_000_000_000)]
    fill = parse_swap_fill(
        receipt(logs, gas_used=200_000, gas_price=2_000_000_000),
        wallet=WALLET, token_in=ETH, token_out=PONS, sent_value_wei=10**15,
    )

    assert fill.gas_native_wei == 400_000_000_000_000
    assert fill.gas_native == Decimal("0.0004")


def test_a_reverted_transaction_is_not_a_fill():
    with pytest.raises(ReceiptError):
        parse_swap_fill(
            receipt([], status=0), wallet=WALLET, token_in=ETH, token_out=PONS
        )


def test_a_receipt_without_our_token_arriving_is_refused():
    logs = [transfer(PONS, POOL_A, POOL_B, 4_500_000_000_000_000_000)]
    with pytest.raises(ReceiptError):
        parse_swap_fill(
            receipt(logs), wallet=WALLET, token_in=ETH, token_out=PONS,
            sent_value_wei=10**15,
        )


def test_hexbytes_style_logs_parse_the_same_as_strings():
    logs = [
        {
            "address": bytes.fromhex(PONS.address[2:]),
            "topics": [
                bytes.fromhex(TRANSFER_TOPIC[2:]),
                bytes.fromhex(topic(POOL_A)[2:]),
                bytes.fromhex(topic(WALLET)[2:]),
            ],
            "data": (4_500_000_000_000_000_000).to_bytes(32, "big"),
        }
    ]
    deltas = wallet_deltas(logs, WALLET)

    assert deltas[PONS.address.lower()] == 4_500_000_000_000_000_000


def test_a_wallet_that_both_sends_and_receives_a_token_nets_out():
    logs = [
        transfer(PONS, WALLET, POOL_A, 10),
        transfer(PONS, POOL_A, WALLET, 30),
    ]
    assert wallet_deltas(logs, WALLET)[PONS.address.lower()] == 20


def test_non_transfer_logs_are_ignored():
    logs = [
        {"address": WETH.address, "topics": ["0x" + "ee" * 32, topic(WALLET)], "data": "0x1"},
        transfer(PONS, POOL_A, WALLET, 5),
    ]
    assert wallet_deltas(logs, WALLET) == {PONS.address.lower(): 5}


def test_a_native_output_cannot_be_read_from_logs_alone():
    # Selling into ETH: the ETH arriving emits no event at all.
    logs = [transfer(PONS, WALLET, POOL_A, 100_000_000_000_000_000_000)]
    with pytest.raises(ReceiptError) as exc:
        parse_swap_fill(receipt(logs), wallet=WALLET, token_in=PONS, token_out=ETH)
    assert "measured separately" in str(exc.value)


def test_a_native_output_measured_by_the_caller_completes_the_fill():
    logs = [transfer(PONS, WALLET, POOL_A, 100_000_000_000_000_000_000)]
    fill = parse_swap_fill(
        receipt(logs),
        wallet=WALLET,
        token_in=PONS,
        token_out=ETH,
        native_out_wei=23_000_000_000_000_000,
    )

    assert fill.amount_in(PONS) == Decimal("100")
    assert fill.amount_out(ETH) == Decimal("0.023")
