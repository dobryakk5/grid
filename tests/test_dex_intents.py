import pytest

from app.dex.intents import (
    SIGNED_STATUSES,
    TRANSITIONS,
    DexStateError,
    IntentStatus,
    assert_transition,
    can_transition,
    is_terminal,
)


def test_happy_path_walks_every_stage():
    path = [
        IntentStatus.WAITING,
        IntentStatus.TRIGGERED,
        IntentStatus.QUOTED,
        IntentStatus.SIGNING,
        IntentStatus.SUBMITTING,
        IntentStatus.PENDING,
        IntentStatus.FILLED,
    ]
    for current, target in zip(path, path[1:]):
        assert_transition(current, target)


def test_a_signed_intent_can_never_go_back_to_waiting():
    # Past signing a nonce is burned: re-deciding would buy twice.
    for status in SIGNED_STATUSES:
        assert not can_transition(status, IntentStatus.WAITING)
        with pytest.raises(DexStateError):
            assert_transition(status, IntentStatus.WAITING)


def test_pending_may_be_rebroadcast_under_the_same_nonce():
    assert can_transition(IntentStatus.PENDING, IntentStatus.SUBMITTING)


def test_blocked_is_recoverable_but_failed_is_not():
    assert not is_terminal(IntentStatus.BLOCKED)
    assert can_transition(IntentStatus.BLOCKED, IntentStatus.WAITING)
    assert is_terminal(IntentStatus.FAILED)
    assert TRANSITIONS[IntentStatus.FAILED] == frozenset()


def test_price_may_walk_back_out_of_the_band():
    assert can_transition(IntentStatus.TRIGGERED, IntentStatus.WAITING)
    # A quote worse than the limit is not a failure either.
    assert can_transition(IntentStatus.QUOTED, IntentStatus.WAITING)


def test_terminal_states_accept_nothing():
    for status in (IntentStatus.FILLED, IntentStatus.CANCELLED, IntentStatus.EXPIRED):
        assert is_terminal(status)
        with pytest.raises(DexStateError):
            assert_transition(status, IntentStatus.WAITING)


def test_unknown_status_is_an_error_not_a_silent_no():
    with pytest.raises(DexStateError):
        assert_transition("PARTIALLY_FILLED", IntentStatus.FILLED)
