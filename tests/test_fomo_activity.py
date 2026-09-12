from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.fomo_activity import ActivityImport, prepare_import
from app.fomo.activity import aggregate_legs, normalize_swap, swap_rows, timestamp_ms


def swap(**overrides):
    return {
        "id": "swap-1", "createdAt": "2026-09-12T10:00:00Z",
        "inTokenAddress": "0xAbC", "outTokenAddress": "SoLaNaCaseSensitive",
        "inNetworkId": 8453, "outNetworkId": 1399811149,
        "inHumanAmount": "100", "outHumanAmount": "2.5",
        "humanUsdAmountIn": "100", "humanUsdAmountOut": "99.9",
        "inTokenSymbol": "USDC", "outTokenSymbol": "TOKEN", **overrides,
    }


def test_cross_chain_swap_has_two_different_networks_and_keeps_solana_case():
    sold, bought = normalize_swap(swap())
    assert (sold["side"], sold["chain_id"], sold["token_address"]) == ("SELL", 8453, "0xabc")
    assert (bought["side"], bought["chain_id"], bought["token_address"]) == (
        "BUY", 1399811149, "SoLaNaCaseSensitive",
    )
    assert bought["value_usd"] == Decimal("99.9")


@pytest.mark.parametrize("changes", [
    {"createdAt": None}, {"id": None}, {"inNetworkId": None},
    {"outHumanAmount": "NaN"}, {"inHumanAmount": "Infinity"},
    {"inHumanAmount": "-1"}, {"inHumanAmount": "1e50"},
    {"outTokenAddress": None},
])
def test_bad_swap_is_not_imported_as_a_partial_trade(changes):
    assert normalize_swap(swap(**changes)) == []


def test_missing_usd_is_not_zero_or_estimated_from_other_leg():
    sold, bought = normalize_swap(swap(humanUsdAmountOut=None))
    assert sold["value_usd"] == Decimal("100")
    assert bought["value_usd"] is None


def test_timestamp_seconds_milliseconds_and_iso_are_equivalent():
    expected = timestamp_ms("2026-09-12T10:00:00Z")
    assert timestamp_ms(expected) == expected
    assert timestamp_ms(expected / 1000) == expected
    assert timestamp_ms("2026-09-12T10:00:00") is None


def test_unrecognized_swaps_response_is_not_an_empty_history():
    with pytest.raises(ValueError):
        swap_rows({"responseObject": {"changedSchema": []}})
    assert swap_rows({"responseObject": {"swaps": [], "hasNextPage": False}}) == ([], False)
    assert swap_rows({"swaps": []}) == ([], None)


def test_import_deduplicates_repeated_swaps_and_reports_partial_history():
    payload = ActivityImport(traders=[{
        "user_id": "alice", "rank": 1, "handle": "alice", "has_more": True,
        "swaps": [swap(), swap(), {"id": "malformed"}],
    }])
    legs, coverage = prepare_import(payload)
    assert len(legs) == 2
    assert coverage["source_rows"] == 3
    assert coverage["rejected_rows"] == 1
    assert coverage["cohort_shortfall"] == 29
    assert coverage["traders_with_more"] == 1
    assert not coverage["history_complete"]


def test_aggregates_keep_networks_distinct_and_count_traders_not_swaps():
    raw = normalize_swap(swap(outTokenAddress="0xSame", outNetworkId=8453))
    raw += normalize_swap(swap(id="swap-2", outTokenAddress="0xSame", outNetworkId=8453,
                               humanUsdAmountOut=None))
    raw += normalize_swap(swap(id="swap-3", outTokenAddress="0xSame", outNetworkId=4663))
    legs = [SimpleNamespace(user_id="alice", **leg) for leg in raw]
    coins = aggregate_legs(legs, {"alice": {"user_id": "alice", "handle": "Alice", "rank": 1}})
    tokens = {c["chain_id"]: c for c in coins if c["token_address"] == "0xsame"}
    assert set(tokens) == {8453, 4663}
    base = tokens[8453]
    assert base["buyers"] == 1
    assert base["buy_usd"] == Decimal("99.9")
    assert base["unpriced"] == 1
    assert base["traders"][0]["buys"] == 2
    assert base["traders"][0]["buy_amount"] == Decimal("5")


def test_duplicate_identity_cannot_inflate_leaderboard_count():
    with pytest.raises(ValueError, match="Duplicate"):
        ActivityImport(traders=[{"user_id": "alice", "rank": 1}, {"user_id": "alice", "rank": 2}])
