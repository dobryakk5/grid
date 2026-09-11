from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.dex.intents import IntentStatus
from app.dex.scheduling import Action, IntentView, plan_intent


NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_rebroadcast_after_seconds", 60)
    monkeypatch.setattr(settings, "dex_stuck_after_seconds", 300)


def view(status, **fields) -> IntentView:
    return IntentView(status=status, **fields)


def test_a_watching_level_is_evaluated():
    assert plan_intent(view(IntentStatus.WAITING), now=NOW) == Action.EVALUATE


def test_a_level_past_its_deadline_expires():
    stale = view(IntentStatus.WAITING, expires_at=NOW - timedelta(minutes=1))
    assert plan_intent(stale, now=NOW) == Action.EXPIRE


def test_a_blocked_level_waits_out_its_cooldown_then_returns():
    cooling = view(IntentStatus.BLOCKED, blocked_until=NOW + timedelta(seconds=20))
    recovered = view(IntentStatus.BLOCKED, blocked_until=NOW - timedelta(seconds=1))

    assert plan_intent(cooling, now=NOW) == Action.IGNORE
    assert plan_intent(recovered, now=NOW) == Action.UNBLOCK


def test_a_signed_intent_is_never_expired_out_from_under_its_transaction():
    # The deadline passed, but a transaction that may confirm at any moment is
    # not something a clock gets to overrule.
    stale = view(
        IntentStatus.PENDING,
        tx_hash="0xabc",
        submitted_at=NOW - timedelta(seconds=10),
        expires_at=NOW - timedelta(hours=5),
    )
    assert plan_intent(stale, now=NOW) == Action.CHECK_RECEIPT


def test_a_fresh_broadcast_is_only_checked():
    fresh = view(
        IntentStatus.SUBMITTING, tx_hash="0xabc",
        submitted_at=NOW - timedelta(seconds=5),
    )
    assert plan_intent(fresh, now=NOW) == Action.CHECK_RECEIPT


def test_a_submitting_row_that_went_quiet_is_re_sent():
    # Committed before broadcast: the send may never have happened.
    quiet = view(
        IntentStatus.SUBMITTING, tx_hash="0xabc",
        created_at=NOW - timedelta(seconds=120),
    )
    assert plan_intent(quiet, now=NOW) == Action.REBROADCAST


def test_a_transaction_unmined_for_too_long_is_replaced():
    stuck = view(
        IntentStatus.PENDING, tx_hash="0xabc",
        submitted_at=NOW - timedelta(seconds=600),
    )
    assert plan_intent(stuck, now=NOW) == Action.REPLACE


def test_a_signed_row_without_a_hash_never_reached_the_network():
    orphan = view(IntentStatus.SUBMITTING, tx_hash=None)
    assert plan_intent(orphan, now=NOW) == Action.ABANDON


def test_a_row_stuck_at_signing_reserved_a_nonce_and_sent_nothing():
    assert plan_intent(view(IntentStatus.SIGNING), now=NOW) == Action.ABANDON


@pytest.mark.parametrize(
    "status",
    [IntentStatus.FILLED, IntentStatus.CANCELLED, IntentStatus.EXPIRED, IntentStatus.FAILED],
)
def test_finished_intents_are_left_alone(status):
    assert plan_intent(view(status), now=NOW) == Action.IGNORE


def test_naive_timestamps_are_read_as_utc():
    # Postgres can hand back a naive datetime depending on the driver path.
    naive = view(
        IntentStatus.PENDING,
        tx_hash="0xabc",
        submitted_at=(NOW - timedelta(seconds=600)).replace(tzinfo=None),
    )
    assert plan_intent(naive, now=NOW) == Action.REPLACE
