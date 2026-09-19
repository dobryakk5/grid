from decimal import Decimal

from app.chain.tape import classify
from app.dex.tokens import DexPair, Token

BASE = Token(symbol="PONS", address="0x" + "11" * 20, decimals=18)
QUOTE = Token(symbol="USDG", address="0x" + "22" * 20, decimals=6)
PAIR = DexPair(symbol="PONSUSDG", base=BASE, quote=QUOTE, chain="robinhood",
                tick_size=Decimal("0.0001"), min_order_quote=Decimal("10"))

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
WALLET_C = "0x" + "cc" * 20  # untracked
POOL = "0x" + "99" * 20  # counterparty; classify() must not care what this is

USD_QUOTES = frozenset({"USDG"})


def transfer(token, from_address, to_address, human_amount, *, tx_hash="0xtx1", block_number=100,
             block_time_ms=1_700_000_000_000, log_index=0):
    return {
        "tx_hash": tx_hash,
        "log_index": log_index,
        "token_address": token.address,
        "from_address": from_address,
        "to_address": to_address,
        "amount": token.to_wei(Decimal(human_amount)),
        "block_number": block_number,
        "block_time_ms": block_time_ms,
    }


def test_buy_is_base_in_quote_out():
    transfers = [
        transfer(BASE, POOL, WALLET_A, "100"),
        transfer(QUOTE, WALLET_A, POOL, "55"),
    ]

    rows = classify(transfers, {WALLET_A}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert len(rows) == 1
    row = rows[0]
    assert row.side == "BUY"
    assert row.wallet_address.lower() == WALLET_A
    assert row.token_amount == Decimal("100")
    assert row.quote_amount == Decimal("55")
    assert row.price == Decimal("0.55")
    assert row.pricing_source == "QUOTE_LEG"
    assert row.value_usd == Decimal("55")


def test_sell_is_base_out_quote_in():
    transfers = [
        transfer(BASE, WALLET_A, POOL, "40"),
        transfer(QUOTE, POOL, WALLET_A, "22"),
    ]

    rows = classify(transfers, {WALLET_A}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert len(rows) == 1
    assert rows[0].side == "SELL"
    assert rows[0].token_amount == Decimal("40")
    assert rows[0].quote_amount == Decimal("22")


def test_one_transaction_with_two_tracked_wallets_yields_two_rows():
    transfers = [
        transfer(BASE, POOL, WALLET_A, "100"),
        transfer(QUOTE, WALLET_A, POOL, "55"),
        transfer(BASE, WALLET_B, POOL, "10"),
        transfer(QUOTE, POOL, WALLET_B, "6"),
    ]

    rows = classify(transfers, {WALLET_A, WALLET_B}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    by_wallet = {row.wallet_address.lower(): row for row in rows}
    assert set(by_wallet) == {WALLET_A, WALLET_B}
    assert by_wallet[WALLET_A].side == "BUY"
    assert by_wallet[WALLET_B].side == "SELL"


def test_untracked_wallets_swap_is_ignored():
    transfers = [
        transfer(BASE, POOL, WALLET_C, "100"),
        transfer(QUOTE, WALLET_C, POOL, "55"),
    ]

    rows = classify(transfers, {WALLET_A, WALLET_B}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert rows == []


def test_a_plain_transfer_is_not_reported_as_a_swap():
    # Only the base leg moves; no offsetting quote leg -- not a trade.
    transfers = [transfer(BASE, WALLET_A, WALLET_B, "5")]

    rows = classify(transfers, {WALLET_A, WALLET_B}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert rows == []


def test_multi_hop_within_one_transaction_nets_to_a_single_row():
    transfers = [
        transfer(QUOTE, WALLET_A, POOL, "55", log_index=0),
        transfer(BASE, POOL, "0x" + "33" * 20, "100", log_index=1),
        transfer(BASE, "0x" + "33" * 20, WALLET_A, "100", log_index=2),
    ]

    rows = classify(transfers, {WALLET_A}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert len(rows) == 1
    assert rows[0].side == "BUY"
    assert rows[0].token_amount == Decimal("100")
    assert rows[0].quote_amount == Decimal("55")


def test_decimals_mismatch_between_base_and_quote_does_not_break_arithmetic():
    # BASE has 18 decimals, QUOTE has 6 -- exercised by every test above via
    # Token.to_wei/from_wei, but assert it explicitly for a non-round amount.
    transfers = [
        transfer(BASE, POOL, WALLET_A, "0.000000000000000001"),
        transfer(QUOTE, WALLET_A, POOL, "0.000001"),
    ]

    rows = classify(transfers, {WALLET_A}, PAIR, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert rows[0].token_amount == Decimal("0.000000000000000001")
    assert rows[0].quote_amount == Decimal("0.000001")


def test_non_usd_quote_symbol_leaves_the_row_unpriced_for_now():
    weth = Token(symbol="WETH", address="0x" + "44" * 20, decimals=18)
    eth_pair = DexPair(symbol="PONSETH", base=BASE, quote=weth, chain="robinhood",
                        tick_size=Decimal("0.0000000001"), min_order_quote=Decimal("10"))
    transfers = [
        transfer(BASE, POOL, WALLET_A, "100"),
        transfer(weth, WALLET_A, POOL, "0.02"),
    ]

    rows = classify(transfers, {WALLET_A}, eth_pair, chain_id=4663, usd_quote_symbols=USD_QUOTES)

    assert rows[0].pricing_source == "UNPRICED"
    assert rows[0].value_usd is None


def test_empty_transfers_list_yields_no_rows():
    assert classify([], {WALLET_A}, PAIR, chain_id=4663) == []


# ---- backing off a node that is refusing us ------------------------------


def test_the_wait_doubles_while_the_node_keeps_refusing():
    """Retrying a 429 at the normal cadence keeps the limit tripped: the
    refused request still spends the budget that would have served the next."""
    from app.chain.tape import RateLimitBackoff

    backoff = RateLimitBackoff(base=5.0, maximum=120.0)

    waits = [backoff.record_refusal() for _ in range(7)]

    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0]


def test_one_pass_getting_through_drops_the_wait_entirely():
    """The tape wants the next block range as soon as it is allowed to have
    it -- it is days behind, and a cautious ramp back costs hours."""
    from app.chain.tape import RateLimitBackoff

    backoff = RateLimitBackoff(base=5.0, maximum=120.0)
    backoff.record_refusal()
    backoff.record_refusal()

    backoff.record_success()

    assert backoff.current == 0.0


def test_a_rate_limit_is_told_apart_from_every_other_failure():
    """They are answered differently: one waits, the other is a bug to see."""
    from app.chain.tape import is_rate_limited_error

    class Refused(Exception):
        status = 429

    assert is_rate_limited_error(Refused())
    assert is_rate_limited_error(Exception("429, message='Too Many Requests'"))
    assert is_rate_limited_error(Exception("rate limit exceeded"))
    assert not is_rate_limited_error(
        Exception("value too long for type character varying(32)")
    )
