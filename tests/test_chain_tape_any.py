from decimal import Decimal

from app.chain.tape import classify_any
from app.chain.tokens import TokenMeta

PONS = TokenMeta(address="0x" + "11" * 20, symbol="PONS", decimals=18)
USDG = TokenMeta(address="0x" + "22" * 20, symbol="USDG", decimals=6)
WETH = TokenMeta(address="0x" + "44" * 20, symbol="WETH", decimals=18)
MEME = TokenMeta(address="0x" + "55" * 20, symbol="MEME", decimals=9)
NONAME = TokenMeta(address="0x" + "66" * 20, symbol=None, decimals=18)

META = {t.address.lower(): t for t in (PONS, USDG, WETH, MEME, NONAME)}
QUOTES = {USDG.address.lower(): "USDG", WETH.address.lower(): "WETH"}
USD_QUOTES = frozenset({"USDG"})

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
POOL = "0x" + "99" * 20


def transfer(token, from_address, to_address, human_amount, *, tx_hash="0xtx1", log_index=0):
    return {
        "tx_hash": tx_hash,
        "log_index": log_index,
        "token_address": token.address,
        "from_address": from_address,
        "to_address": to_address,
        "amount": int(Decimal(human_amount).scaleb(token.decimals)),
        "block_number": 100,
        "block_time_ms": 1_700_000_000_000,
    }


def run(transfers, wallets=frozenset({WALLET_A})):
    return classify_any(
        transfers, set(wallets),
        token_meta=META, quote_assets=QUOTES, chain_id=4663, usd_quote_symbols=USD_QUOTES,
    )


def test_any_token_against_usdg_is_classified_not_just_the_registered_pair():
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(USDG, WALLET_A, POOL, "250"),
    ])

    assert len(rows) == 1
    assert rows[0].symbol == "MEME"
    assert rows[0].side == "BUY"
    assert rows[0].token_amount == Decimal("1000")
    assert rows[0].quote_amount == Decimal("250")
    assert rows[0].value_usd == Decimal("250")
    assert rows[0].pricing_source == "QUOTE_LEG"


def test_sell_against_usdg():
    rows = run([
        transfer(PONS, WALLET_A, POOL, "40"),
        transfer(USDG, POOL, WALLET_A, "22"),
    ])
    assert rows[0].side == "SELL"
    assert rows[0].token_amount == Decimal("40")


def test_weth_quoted_swap_is_recorded_but_left_unpriced():
    # WETH is a quote asset, but it is not dollars -- inventing a USD figure
    # for it without an ETH price would be worse than leaving it blank.
    rows = run([
        transfer(PONS, POOL, WALLET_A, "100"),
        transfer(WETH, WALLET_A, POOL, "0.02"),
    ])

    assert rows[0].side == "BUY"
    assert rows[0].quote_symbol == "WETH"
    assert rows[0].pricing_source == "UNPRICED"
    assert rows[0].value_usd is None


def test_token_without_a_symbol_still_produces_a_row():
    rows = run([
        transfer(NONAME, POOL, WALLET_A, "5"),
        transfer(USDG, WALLET_A, POOL, "10"),
    ])
    assert rows[0].symbol is None
    assert rows[0].token_address == NONAME.address


def test_differing_decimals_are_honoured_per_token():
    # MEME is 9 decimals, USDG is 6 -- a shared assumption would be wrong by
    # orders of magnitude here.
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1.5"),
        transfer(USDG, WALLET_A, POOL, "3"),
    ])
    assert rows[0].token_amount == Decimal("1.5")
    assert rows[0].quote_amount == Decimal("3")
    assert rows[0].price == Decimal("2")


def test_two_tracked_wallets_in_one_transaction_yield_two_rows():
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(USDG, WALLET_A, POOL, "250"),
        transfer(PONS, WALLET_B, POOL, "10"),
        transfer(USDG, POOL, WALLET_B, "6"),
    ], wallets={WALLET_A, WALLET_B})

    by_wallet = {row.wallet_address.lower(): row for row in rows}
    assert set(by_wallet) == {WALLET_A, WALLET_B}
    assert by_wallet[WALLET_A].side == "BUY"
    assert by_wallet[WALLET_B].side == "SELL"


def test_token_to_token_swap_is_two_trades_both_unpriced():
    # Neither side is money, so no dollar figure can be honest -- but a
    # position was closed and another opened, and both are real trades.
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(PONS, WALLET_A, POOL, "50"),
    ])

    by_symbol = {row.symbol: row for row in rows}
    assert set(by_symbol) == {"MEME", "PONS"}
    assert by_symbol["MEME"].side == "BUY"
    assert by_symbol["MEME"].token_amount == Decimal("1000")
    assert by_symbol["MEME"].quote_symbol == "PONS"
    assert by_symbol["PONS"].side == "SELL"
    assert by_symbol["PONS"].token_amount == Decimal("50")
    assert by_symbol["PONS"].quote_symbol == "MEME"
    # No money leg anywhere in the transaction, so neither row claims a value.
    assert all(row.pricing_source == "UNPRICED" and row.value_usd is None for row in rows)


def test_token_to_token_rows_are_distinguishable_by_token_in_the_key():
    # Both rows share tx_hash and wallet, so the primary key has to include
    # the token or one of these two trades would overwrite the other.
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(PONS, WALLET_A, POOL, "50"),
    ])
    keys = {(row.tx_hash, row.wallet_address, row.token_address) for row in rows}
    assert len(keys) == len(rows) == 2


def test_two_tokens_moving_the_same_direction_is_not_a_swap():
    # Receiving two tokens at once is an airdrop or an LP exit, not a trade.
    rows = run([
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(PONS, POOL, WALLET_A, "50"),
    ])
    assert rows == []


def test_a_swap_landing_in_two_tokens_is_skipped_rather_than_split():
    rows = run([
        transfer(USDG, WALLET_A, POOL, "300"),
        transfer(MEME, POOL, WALLET_A, "1000"),
        transfer(PONS, POOL, WALLET_A, "20"),
    ])
    assert rows == []


def test_a_plain_transfer_is_not_a_swap():
    rows = run([transfer(PONS, WALLET_A, WALLET_B, "5")], wallets={WALLET_A, WALLET_B})
    assert rows == []


def test_untracked_wallet_activity_is_ignored():
    rows = run([
        transfer(MEME, POOL, "0x" + "cc" * 20, "1000"),
        transfer(USDG, "0x" + "cc" * 20, POOL, "250"),
    ])
    assert rows == []


def test_tokens_with_no_metadata_are_ignored_rather_than_guessed():
    unknown = TokenMeta(address="0x" + "77" * 20, symbol="???", decimals=18)
    rows = run([
        transfer(unknown, POOL, WALLET_A, "1000"),
        transfer(USDG, WALLET_A, POOL, "250"),
    ])
    # The unknown token is not in META, so only the USDG leg is visible and
    # there is no base side to report.
    assert rows == []
