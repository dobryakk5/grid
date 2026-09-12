import json
import time
from unittest.mock import AsyncMock

import pytest

from app.api.fomo_activity import ActivityImport, prepare_theses
from app.fomo.browser import API_ORIGIN, BrowserCollector, BrowserSyncError
from app.fomo.theses import normalize_thesis, public_thesis, thesis_rows

NOW_MS = 1_757_670_000_000
HOUR = 3600_000


def item(**overrides):
    return {
        "id": "t1", "userId": "alice", "createdAt": NOW_MS - HOUR,
        "thesis": "  Беру на отскок  ", "tradeId": "trade-9",
        "likes": 4, "replyCount": 2, "usdAmount": "1200.5", **overrides,
    }


def normalized(**overrides):
    return normalize_thesis(item(**overrides), chain_id=8453, token_address="0xAbC")


def test_thesis_keeps_the_coin_that_was_asked_about_and_trims_the_note():
    row = normalized()
    assert (row["chain_id"], row["token_address"]) == (8453, "0xabc")
    assert row["text"] == "Беру на отскок"
    assert (row["user_id"], row["trade_id"], row["likes"], row["replies"]) == (
        "alice", "trade-9", 4, 2,
    )
    assert str(row["usd_amount"]) == "1200.5"
    assert row["created_at_ms"] == NOW_MS - HOUR


def test_solana_mint_case_survives_where_evm_case_does_not():
    row = normalize_thesis(item(), chain_id=1399811149, token_address="SoLaNaMiNt")
    assert row["token_address"] == "SoLaNaMiNt"


@pytest.mark.parametrize("key", ["text", "message", "content", "body", "comment"])
def test_the_note_is_read_whatever_fomo_currently_calls_it(key):
    row = normalized(thesis=None, **{key: "тезис"})
    assert row["text"] == "тезис"


def test_author_may_arrive_nested_instead_of_flat():
    row = normalize_thesis(
        {**item(), "userId": None, "user": {"id": "bob", "userHandle": "bob"}},
        chain_id=8453, token_address="0xabc")
    assert row["user_id"] == "bob"


@pytest.mark.parametrize("changes", [
    {"id": None}, {"userId": None}, {"createdAt": None}, {"createdAt": "вчера"},
    {"thesis": "   "}, {"thesis": None},
])
def test_a_row_that_is_not_a_thesis_is_dropped_not_guessed(changes):
    assert normalized(**changes) is None


def test_a_trade_without_a_note_is_not_a_thesis():
    # /feed/token carries trades too; only the ones carrying words are theses.
    assert normalize_thesis({"id": "x", "userId": "alice", "createdAt": NOW_MS,
                             "usdAmount": "10"}, chain_id=1, token_address="0x1") is None


def test_unknown_response_shape_is_an_error_not_an_empty_feed():
    assert thesis_rows({"responseObject": {"items": [1], "hasNextPage": True}}) == ([1], True)
    assert thesis_rows([{"id": "a"}]) == ([{"id": "a"}], None)
    with pytest.raises(ValueError):
        thesis_rows({"unexpected": 1})


def test_only_public_fields_leave_the_browser():
    exported = public_thesis({**item(), "viewerToken": "must-not-export",
                              "user": {"id": "alice", "email": "private@example.com"}})
    assert "must-not-export" not in json.dumps(exported)
    assert "private@example.com" not in json.dumps(exported)
    assert exported["user"] == {"id": "alice"}
    assert exported["thesis"] == item()["thesis"]


class Page:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def evaluate(self, script, args):
        self.calls.append(args)
        return self.responses.pop(0)


def trader(user_id="alice", **swap):
    return {"user_id": user_id, "handle": user_id, "display_name": None, "rank": 1,
            "has_more": False, "swaps": [{
                "id": "s1", "createdAt": NOW_MS - HOUR,
                "inTokenAddress": "0xUSDC", "outTokenAddress": "0xCoIn",
                "inNetworkId": 8453, "outNetworkId": 8453,
                "inHumanAmount": "100", "outHumanAmount": "2", **swap}]}


async def collector(responses):
    page = Page(responses)
    collector = BrowserCollector(page, sleep=AsyncMock(), log=lambda _: None)
    return collector, page


async def test_theses_are_read_for_the_coins_the_cohort_just_traded():
    reader, page = await collector([
        {"status": 200, "data": {"items": [item()], "hasNextPage": False}},
        {"status": 200, "data": {"items": [], "hasNextPage": False}},
    ])
    groups, coverage = await reader.theses([trader()], window_hours=24, now_ms=NOW_MS)
    asked = [call["url"] for call in page.calls]
    assert all(url.startswith(API_ORIGIN + "/feed/token/sortedThesis?") for url in asked)
    assert all("networkId=8453" in url for url in asked)
    assert all(f"afterTime={NOW_MS - 24 * HOUR}" in url and f"beforeTime={NOW_MS}" in url
               for url in asked)
    # Both legs are coins someone may have written about, quote asset included:
    # nothing in a swap says which side was the money.
    assert {"0xcoin", "0xusdc"} == {url.split("tokenAddress=")[1].split("&")[0] for url in asked}
    assert coverage["tokens_in_window"] == 2 and coverage["items"] == 1
    assert groups[0]["items"][0]["id"] == "t1"


async def test_a_swap_older_than_the_window_asks_about_nothing():
    reader, page = await collector([])
    groups, coverage = await reader.theses(
        [trader(createdAt=NOW_MS - 48 * HOUR)], window_hours=24, now_ms=NOW_MS)
    assert (groups, page.calls) == ([], [])
    assert coverage["tokens_in_window"] == 0


async def test_one_coin_that_refuses_costs_that_coin_not_the_whole_import():
    reader, _ = await collector([
        {"status": 404},
        {"status": 200, "data": {"items": [item()], "hasNextPage": True}},
    ])
    groups, coverage = await reader.theses([trader()], window_hours=24, now_ms=NOW_MS)
    assert len(groups) == 1
    assert (coverage["tokens_failed"], coverage["tokens_with_more"]) == (1, 1)


@pytest.mark.parametrize("status", [401, 403, 429, 430, 431])
async def test_a_dead_session_stops_the_pass_instead_of_reporting_silence(status):
    reader, _ = await collector([{"status": status}, {"status": status}])
    with pytest.raises(BrowserSyncError):
        await reader.theses([trader()], window_hours=24, now_ms=NOW_MS)


async def test_the_coin_cap_keeps_the_most_recently_traded():
    reader, page = await collector([{"status": 200, "data": {"items": []}}] * 2)
    people = [trader(inTokenAddress="0xQuoteA", outTokenAddress="0xOld",
                     createdAt=NOW_MS - 5 * HOUR),
              trader(user_id="bob", inTokenAddress="0xQuoteB", outTokenAddress="0xNew",
                     createdAt=NOW_MS - HOUR)]
    _, coverage = await reader.theses(people, window_hours=24, max_tokens=2, now_ms=NOW_MS)
    asked = {call["url"].split("tokenAddress=")[1].split("&")[0] for call in page.calls}
    assert asked == {"0xnew", "0xquoteb"}
    assert (coverage["tokens_read"], coverage["tokens_skipped"]) == (2, 2)


def payload(theses, cohort=("alice",)):
    return ActivityImport.model_validate({
        "requested_limit": len(cohort),
        "traders": [{"user_id": user, "rank": rank, "swaps": []}
                    for rank, user in enumerate(cohort, 1)],
        "theses": [{"chain_id": 8453, "token_address": "0xCoIn", "items": theses}],
    })


def test_only_the_cohorts_own_notes_are_stored():
    rows, coverage = prepare_theses(payload([
        item(), item(id="t2", userId="stranger"), {"id": "t3", "userId": "alice"},
    ]))
    assert [row["thesis_id"] for row in rows] == ["t1"]
    assert (coverage["stored"], coverage["outside_cohort"], coverage["rejected"]) == (1, 1, 1)


def test_the_same_thesis_seen_twice_is_stored_once():
    rows, coverage = prepare_theses(payload([item(), item(text="edited")]))
    assert len(rows) == 1 and coverage["stored"] == 1


def test_collector_diagnostics_survive_the_trip_to_the_server():
    parsed = ActivityImport.model_validate({
        "requested_limit": 1, "traders": [{"user_id": "alice", "rank": 1, "swaps": []}],
        "theses": [], "thesis_coverage": {"window_hours": 24, "tokens_failed": 2},
    })
    _, coverage = prepare_theses(parsed)
    assert coverage["window_hours"] == 24 and coverage["tokens_failed"] == 2
    assert coverage["stored"] == 0


def test_an_old_collector_that_sends_no_theses_still_imports():
    parsed = ActivityImport.model_validate({
        "requested_limit": 1, "traders": [{"user_id": "alice", "rank": 1, "swaps": []}]})
    rows, coverage = prepare_theses(parsed)
    assert rows == [] and coverage == {"stored": 0, "outside_cohort": 0, "rejected": 0}


async def test_a_dead_thesis_feed_does_not_throw_away_the_swap_history():
    # Leaderboard, then one page of swaps, then the thesis feed refusing.
    just_now = int(time.time() * 1000) - 60_000
    reader, _ = await collector([
        {"status": 200, "data": {"leaderboard": [{"id": "alice", "rank": 1, "userHandle": "alice"}]}},
        {"status": 200, "data": {"swaps": [{**trader()["swaps"][0], "createdAt": just_now}],
                                 "hasNextPage": False}},
        {"status": 401}, {"status": 401},
    ])
    payload = await reader.collect(limit=1)
    assert len(payload["traders"][0]["swaps"]) == 1
    assert payload["theses"] == []
    assert "stopped" in payload["thesis_coverage"]


def test_oversized_diagnostics_are_refused_instead_of_stored():
    with pytest.raises(Exception):
        ActivityImport.model_validate({
            "requested_limit": 1, "traders": [{"user_id": "alice", "rank": 1, "swaps": []}],
            "thesis_coverage": {"note": "x" * 501}})
    with pytest.raises(Exception):
        ActivityImport.model_validate({
            "requested_limit": 1, "traders": [{"user_id": "alice", "rank": 1, "swaps": []}],
            "thesis_coverage": {"nested": {"not": "allowed"}}})
