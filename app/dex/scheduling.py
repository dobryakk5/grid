"""What the DEX worker should do with an intent on this pass.

Kept pure and separate from the worker so the decisions can be tested without a
chain or a database: the worker reads rows, asks this module what each one
needs, and carries it out.

The important asymmetry is between rows that have a signed transaction and rows
that do not. An unsigned level is free to wait, re-check, or expire. A signed one
has burned a nonce, so the only questions left are "did it land?" and, if it has
been stuck too long, "replace it at the same nonce".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.dex.intents import SIGNED_STATUSES, IntentStatus, is_terminal

__all__ = ["Action", "IntentView", "plan_intent"]


class Action:
    IGNORE = "IGNORE"
    #: Unsigned level: look at the market and maybe trade.
    EVALUATE = "EVALUATE"
    #: Risk gate cooldown has passed; put it back in the watching pool.
    UNBLOCK = "UNBLOCK"
    #: Waited for its price long enough.
    EXPIRE = "EXPIRE"
    #: Signed and broadcast: ask the chain whether it landed.
    CHECK_RECEIPT = "CHECK_RECEIPT"
    #: Broadcast never took, or we crashed before it: send the same payload.
    REBROADCAST = "REBROADCAST"
    #: Mined nowhere for too long: free the nonce and re-arm the level.
    REPLACE = "REPLACE"
    #: Reserved a nonce but never signed; nothing was sent.
    ABANDON = "ABANDON"


@dataclass(frozen=True)
class IntentView:
    """The only fields a scheduling decision depends on."""

    status: str
    created_at: datetime | None = None
    submitted_at: datetime | None = None
    expires_at: datetime | None = None
    blocked_until: datetime | None = None
    tx_hash: str | None = None
    raw_tx: str | None = None

    @classmethod
    def of(cls, intent) -> "IntentView":
        return cls(
            status=intent.status,
            created_at=intent.created_at,
            submitted_at=intent.submitted_at,
            expires_at=intent.expires_at,
            blocked_until=intent.blocked_until,
            tx_hash=intent.tx_hash,
            raw_tx=intent.raw_tx,
        )


def _elapsed(moment: datetime | None, now: datetime) -> timedelta | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return now - moment


def plan_intent(view: IntentView, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)

    if is_terminal(view.status):
        return Action.IGNORE

    if view.status in SIGNED_STATUSES:
        # Never expire or abandon a signed row: the transaction it names may
        # confirm at any time, and re-deciding would trade twice.
        if view.tx_hash is None:
            return Action.ABANDON
        age = _elapsed(view.submitted_at, moment)
        if view.status == IntentStatus.SUBMITTING:
            # Committed but the broadcast may never have happened.
            waited = age or _elapsed(view.created_at, moment)
            if waited is None or waited.total_seconds() >= settings.dex_rebroadcast_after_seconds:
                return Action.REBROADCAST
            return Action.CHECK_RECEIPT
        if age is not None and age.total_seconds() >= settings.dex_stuck_after_seconds:
            return Action.REPLACE
        return Action.CHECK_RECEIPT

    if view.status == IntentStatus.SIGNING:
        # Nothing was broadcast under this row; the nonce goes back to the pool.
        return Action.ABANDON

    expired = _elapsed(view.expires_at, moment)
    if expired is not None and expired.total_seconds() >= 0:
        return Action.EXPIRE

    if view.status == IntentStatus.BLOCKED:
        waited = _elapsed(view.blocked_until, moment)
        if waited is None or waited.total_seconds() >= 0:
            return Action.UNBLOCK
        return Action.IGNORE

    return Action.EVALUATE
