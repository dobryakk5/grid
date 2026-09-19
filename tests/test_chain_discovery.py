from decimal import Decimal

from app.chain.discovery import Candidate, aggregate_candidates
from app.chain.tape import ChainSwapRow

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
WALLET_C = "0x" + "cc" * 20


def swap(wallet, side, usd, *, tx="0xtx"):
    return ChainSwapRow(
        tx_hash=tx,
        wallet_address=wallet,
        chain_id=4663,
        block_number=1,
        block_time_ms=1,
        token_address="0x" + "11" * 20,
        symbol="PONS",
        side=side,
        token_amount=Decimal("1"),
        quote_address="0x" + "22" * 20,
        quote_symbol="USDG",
        quote_amount=Decimal(str(usd)) if usd is not None else Decimal("0"),
        price=Decimal("1"),
        value_usd=None if usd is None else Decimal(str(usd)),
        pricing_source="UNPRICED" if usd is None else "QUOTE_LEG",
    )


def test_candidates_are_ranked_by_traded_volume():
    rows = [
        swap(WALLET_A, "BUY", 100),
        swap(WALLET_B, "BUY", 900),
        swap(WALLET_C, "SELL", 500),
    ]

    ranked = aggregate_candidates(rows)

    assert [c.address for c in ranked] == [WALLET_B, WALLET_C, WALLET_A]


def test_volume_counts_both_sides_not_just_buys():
    rows = [
        swap(WALLET_A, "BUY", 400),
        swap(WALLET_A, "SELL", 400),   # volume 800
        swap(WALLET_B, "BUY", 700),    # volume 700
    ]

    ranked = aggregate_candidates(rows)

    assert ranked[0].address == WALLET_A
    assert ranked[0].volume_usd == Decimal("800")
    assert ranked[0].bought_usd == Decimal("400")
    assert ranked[0].sold_usd == Decimal("400")


def test_many_dust_trades_do_not_outrank_real_size():
    dust = [swap(WALLET_A, "BUY", 1, tx=f"0x{i}") for i in range(100)]
    whale = [swap(WALLET_B, "BUY", 5_000)]

    ranked = aggregate_candidates(dust + whale)

    assert ranked[0].address == WALLET_B
    assert ranked[1].trades == 100


def test_buy_and_sell_counts_are_kept_separately():
    rows = [swap(WALLET_A, "BUY", 10), swap(WALLET_A, "BUY", 10), swap(WALLET_A, "SELL", 10)]

    candidate = aggregate_candidates(rows)[0]

    assert (candidate.buys, candidate.sells) == (2, 1)
    assert candidate.trades == 3


def test_unpriced_trades_still_produce_a_candidate_with_zero_volume():
    # A wallet trading only token-for-token has no dollar figure, but it is
    # still a wallet that traded -- it just cannot outrank priced volume.
    ranked = aggregate_candidates([swap(WALLET_A, "BUY", None), swap(WALLET_B, "BUY", 1)])

    assert [c.address for c in ranked] == [WALLET_B, WALLET_A]
    assert ranked[1].volume_usd == Decimal("0")
    assert ranked[1].trades == 1


def test_ranking_is_deterministic_for_equal_volume():
    ranked = aggregate_candidates([swap(WALLET_B, "BUY", 5), swap(WALLET_A, "BUY", 5)])
    assert [c.address for c in ranked] == sorted([WALLET_A, WALLET_B])


def test_no_rows_means_no_candidates():
    assert aggregate_candidates([]) == []


def test_candidate_volume_is_the_sum_of_both_sides():
    candidate = Candidate(
        address=WALLET_A, buys=1, sells=1, bought_usd=Decimal("10"), sold_usd=Decimal("2.5")
    )
    assert candidate.volume_usd == Decimal("12.5")
    assert candidate.trades == 2


# ---- a symbol is whatever the contract felt like returning ----------------


def test_a_symbol_too_long_for_the_column_is_cut_to_fit():
    """The bug this exists for: a token on chain answers `symbol()` with
    several hundred digits of pi. The INSERT failed, the scan pass failed with
    it, and the tape sat on that block re-reading the same token forever."""
    from app.chain.tokens import _SYMBOL_MAX, _clean_symbol

    pi = "3.14159265358979323846264338327950288419716939937510582097494459"

    symbol = _clean_symbol(pi)

    assert len(symbol) == _SYMBOL_MAX
    assert symbol == pi[:_SYMBOL_MAX]


def test_an_ordinary_symbol_is_left_alone():
    from app.chain.tokens import _clean_symbol

    assert _clean_symbol("PONS") == "PONS"
    assert _clean_symbol("  USDG  ") == "USDG"


def test_a_symbol_of_nothing_printable_is_no_symbol():
    """An unnamed token is still tradable; the address stands in for the name."""
    from app.chain.tokens import _clean_symbol

    assert _clean_symbol("") is None
    assert _clean_symbol("   ") is None
    assert _clean_symbol("\x00\x07\x1b") is None
