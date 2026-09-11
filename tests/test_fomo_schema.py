from decimal import Decimal

from app.fomo.schema import (
    normalize_balances,
    normalize_holders,
    normalize_leaderboard,
    normalize_trades,
    trade_matches_token,
)


def test_leaderboard_extracts_identity_rank_and_inline_wallet():
    payload = {"leaderboard": [
        {"id": "u1", "userHandle": "alice", "displayName": "Alice", "rank": 1, "evmAddress": "0xAAA"},
        {"user": {"id": "u2", "userHandle": "bob"}, "position": 2},
    ]}

    result = normalize_leaderboard(payload)

    assert result[0].user_id == "u1"
    assert result[0].evm_address == "0xAAA"
    assert result[0].rank == 1
    assert result[1].user_id == "u2"
    assert result[1].rank == 2
    assert result[1].evm_address is None


def test_leaderboard_falls_back_to_position_index_when_rank_is_absent():
    result = normalize_leaderboard([{"id": "u1"}, {"id": "u2"}])
    assert [row.rank for row in result] == [1, 2]


def test_leaderboard_rows_without_an_id_are_skipped():
    result = normalize_leaderboard([{"userHandle": "no-id"}, {"id": "u1"}])
    assert [row.user_id for row in result] == ["u1"]


def test_holders_reads_topHolders_and_nested_user_evm_address():
    payload = [{
        "topHolders": [
            {
                "value": "1000.5", "humanAmount": "250", "pnl": "42",
                "costBasis": "900", "firstBuyTime": 1700000000000,
                "user": {"id": "u1", "userHandle": "alice", "evmAddress": "0xAAA"},
            },
            {"humanAmount": "10", "user": {"id": "u2"}},
        ],
    }]

    result = normalize_holders(payload)

    assert result[0].user_id == "u1"
    assert result[0].evm_address == "0xAAA"
    assert result[0].value_usd == Decimal("1000.5")
    assert result[0].cost_usd == Decimal("900")
    assert result[0].first_buy_time_ms == 1700000000000
    assert result[1].evm_address is None


def test_holders_handles_a_bare_dict_result_without_the_list_wrapper():
    payload = {"topHolders": [{"user": {"id": "u1", "evmAddress": "0xAAA"}, "humanAmount": "5"}]}
    result = normalize_holders(payload)
    assert len(result) == 1
    assert result[0].evm_address == "0xAAA"


def test_holders_rows_without_a_user_id_are_skipped():
    result = normalize_holders([{"topHolders": [{"humanAmount": "5"}]}])
    assert result == []


def test_balances_reads_nested_balance_and_token_filter_result():
    payload = {"balances": [
        {
            "balance": {
                "tokenAddress": "0xPONS", "networkId": 4663, "symbol": "PONS",
                "shiftedBalance": "150", "value": "82.5", "pnl": "-3",
            },
            "tokenFilterResult": {"priceUSD": "0.55"},
        },
    ]}

    result = normalize_balances(payload)

    assert result[0].token_address == "0xPONS"
    assert result[0].network_id == 4663
    assert result[0].amount == Decimal("150")
    assert result[0].value_usd == Decimal("82.5")
    assert result[0].pnl_usd == Decimal("-3")
    assert result[0].price_usd == Decimal("0.55")


def test_balances_price_prefers_the_balances_own_field_over_token_filter_result():
    payload = [{"balance": {"tokenAddress": "0xPONS", "price": "0.6"}, "tokenFilterResult": {"priceUSD": "0.9"}}]
    result = normalize_balances(payload)
    assert result[0].price_usd == Decimal("0.6")


def test_trades_bare_list_shape():
    payload = [{"id": "t1", "tokenAddress": "0xPONS", "status": "OPEN", "humanTokenAmount": "10", "createdAt": 1000}]

    result = normalize_trades(payload)

    assert len(result) == 1
    assert result[0].trade_id == "t1"
    assert result[0].token_address == "0xPONS"
    assert result[0].token_amount == Decimal("10")
    assert result[0].created_at_ms == 1000


def test_trades_items_shape_unwraps_the_nested_trade_and_falls_back_to_swap_address():
    payload = {"items": [
        {
            "trade": {"id": "t1", "tokenAddress": "0xPONS", "realizedPnlUsd": "12.5"},
            "swaps": [{"address": "0xWALLET"}],
        },
    ]}

    result = normalize_trades(payload)

    assert result[0].trade_id == "t1"
    assert result[0].realized_pnl_usd == Decimal("12.5")
    assert result[0].user_address == "0xWALLET"


def test_trades_active_and_closed_shape_merges_both_lists():
    payload = {
        "activeTrades": [{"id": "t1", "status": "OPEN"}],
        "closedTrades": [{"id": "t2", "status": "CLOSED", "closedAt": 2000}],
    }

    result = normalize_trades(payload)

    assert {row.trade_id for row in result} == {"t1", "t2"}
    closed = next(row for row in result if row.trade_id == "t2")
    assert closed.closed_at_ms == 2000


def test_trades_unrecognized_shape_is_an_empty_list_not_an_error():
    assert normalize_trades({"somethingElse": []}) == []
    assert normalize_trades(None) == []
    assert normalize_trades("not even a dict") == []


def test_trade_matches_token_is_case_insensitive():
    trade = normalize_trades([{"id": "t1", "tokenAddress": "0xPONS"}])[0]
    assert trade_matches_token(trade, "0xpons")
    assert not trade_matches_token(trade, "0xOTHER")
