"""Lifecycle of a synthetic limit order on an on-chain venue.

A DEX has no resting orders, so ``RobinhoodClient.place_limit_order`` records an
*intent* in Postgres and the DEX worker is what eventually turns it into a swap.
This module owns the vocabulary and the legal moves between states; the worker
and the repository both go through :func:`assert_transition` so an illegal move
is a loud error and never a silently mis-stepped order.

    WAITING -> TRIGGERED -> QUOTED -> SIGNING -> SUBMITTING -> PENDING -> FILLED

Side states:

``BLOCKED``
    A risk gate said no. Not terminal -- the level goes back to watching and may
    re-arm once the pool recovers.
``CANCELLED`` / ``EXPIRED``
    Withdrawn by the engine or timed out, before anything was signed.
``FAILED``
    Unrecoverable: a reverted transaction, or a signing/build error we will not
    retry under the same nonce.

The one-way door is ``SIGNING -> SUBMITTING``: past it a nonce is burned and a
transaction hash exists, so recovery may only re-broadcast that same signed
transaction. It must never fall back to WAITING, which would buy twice.
"""

from __future__ import annotations

__all__ = [
    "DexStateError",
    "IntentStatus",
    "TRANSITIONS",
    "TERMINAL_STATUSES",
    "SIGNED_STATUSES",
    "assert_transition",
    "can_transition",
    "is_terminal",
]


class DexStateError(RuntimeError):
    """Raised on an illegal intent state transition."""


class IntentStatus:
    WAITING = "WAITING"
    TRIGGERED = "TRIGGERED"
    QUOTED = "QUOTED"
    SIGNING = "SIGNING"
    SUBMITTING = "SUBMITTING"
    PENDING = "PENDING"
    FILLED = "FILLED"
    BLOCKED = "BLOCKED"
    #: The price came and we could not take it -- not enough of what this swap
    #: spends. Distinct from BLOCKED on purpose: BLOCKED is the market's fault
    #: and clears itself, MISSED is ours and clears only when the wallet is
    #: topped up. Merging them would hide the one the operator can act on.
    MISSED = "MISSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


TERMINAL_STATUSES = frozenset(
    {
        IntentStatus.FILLED,
        IntentStatus.CANCELLED,
        IntentStatus.EXPIRED,
        IntentStatus.FAILED,
    }
)

# Past these a nonce is reserved and a signed payload exists: recovery
# re-broadcasts, it never re-decides.
SIGNED_STATUSES = frozenset(
    {IntentStatus.SUBMITTING, IntentStatus.PENDING}
)

TRANSITIONS: dict[str, frozenset[str]] = {
    IntentStatus.WAITING: frozenset(
        {
            IntentStatus.TRIGGERED,
            IntentStatus.BLOCKED,
            IntentStatus.MISSED,
            IntentStatus.CANCELLED,
            IntentStatus.EXPIRED,
        }
    ),
    # Price can walk back out of the band before we ever get a quote.
    IntentStatus.TRIGGERED: frozenset(
        {
            IntentStatus.QUOTED,
            IntentStatus.WAITING,
            IntentStatus.BLOCKED,
            IntentStatus.MISSED,
            IntentStatus.CANCELLED,
            IntentStatus.EXPIRED,
        }
    ),
    # A quote worse than the limit is not a failure: keep watching.
    IntentStatus.QUOTED: frozenset(
        {
            IntentStatus.SIGNING,
            IntentStatus.WAITING,
            IntentStatus.BLOCKED,
            IntentStatus.MISSED,
            IntentStatus.CANCELLED,
            IntentStatus.EXPIRED,
        }
    ),
    IntentStatus.SIGNING: frozenset(
        {IntentStatus.SUBMITTING, IntentStatus.BLOCKED, IntentStatus.FAILED}
    ),
    IntentStatus.SUBMITTING: frozenset({IntentStatus.PENDING, IntentStatus.FAILED}),
    # Back to SUBMITTING covers a re-broadcast or a gas bump on the same nonce.
    IntentStatus.PENDING: frozenset(
        {IntentStatus.FILLED, IntentStatus.SUBMITTING, IntentStatus.FAILED}
    ),
    IntentStatus.BLOCKED: frozenset(
        {
            IntentStatus.WAITING,
            IntentStatus.TRIGGERED,
            IntentStatus.MISSED,
            IntentStatus.CANCELLED,
            IntentStatus.EXPIRED,
        }
    ),
    # Still a live level: the wallet can be topped up and the price can come
    # back. It keeps the MISSED label meanwhile, which is the whole point --
    # WAITING said nothing had happened when something had.
    IntentStatus.MISSED: frozenset(
        {
            IntentStatus.TRIGGERED,
            IntentStatus.WAITING,
            IntentStatus.BLOCKED,
            IntentStatus.CANCELLED,
            IntentStatus.EXPIRED,
        }
    ),
    IntentStatus.FILLED: frozenset(),
    IntentStatus.CANCELLED: frozenset(),
    IntentStatus.EXPIRED: frozenset(),
    IntentStatus.FAILED: frozenset(),
}


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: str, target: str) -> None:
    if current not in TRANSITIONS:
        raise DexStateError(f"unknown intent status {current!r}")
    if target not in TRANSITIONS:
        raise DexStateError(f"unknown target status {target!r}")
    if not can_transition(current, target):
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "<terminal>"
        raise DexStateError(
            f"illegal intent transition {current} -> {target}; allowed: {allowed}"
        )
