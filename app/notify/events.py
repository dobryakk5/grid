"""The vocabulary of notifiable operations, and which of them are turned on.

One flat namespace covers both venues, because a phone does not care which
engine moved the money:

``dex.<status>``
    A synthetic limit order reached a status worth reporting. The statuses are
    the terminal (and near-terminal) ones from :mod:`app.dex.intents`; the
    machinery in between -- TRIGGERED, QUOTED, SIGNING, SUBMITTING, PENDING --
    is never notifiable, so a level on its way to a fill produces one message,
    not six.

``dex.opened``
    A new level started watching its price -- armed by hand or placed by a
    grid. A re-armed retry of an abandoned attempt is the same order, not a new
    one, and says nothing.

``grid.<event_type>``
    A strategy transition recorded through :func:`app.trading.events.record_strategy_event`,
    lowercased: ``ORDER_FILLED`` becomes ``grid.order_filled``.

``NOTIFY_EVENTS`` selects from this namespace with :mod:`fnmatch` patterns, so
``grid.recovery_*`` subscribes to the whole recovery lifecycle and ``*`` to
everything. Two exclusions are deliberate rather than merely default:

* ``dex.blocked`` re-fires every ``DEX_BLOCKED_RETRY_SECONDS`` while a risk
  gate stays shut -- a message every thirty seconds until someone looks. It is
  subscribable, but never by accident.
* ``grid.order_synced`` and ``grid.position_lot_*`` are bookkeeping that
  happens *because* of an operation already reported, not an operation.
"""

from __future__ import annotations

from fnmatch import fnmatch

from app.core.config import settings
from app.dex.intents import IntentStatus

__all__ = [
    "DEX_NOTIFIABLE",
    "DEX_OPENED",
    "chat_ids",
    "dex_kind",
    "grid_kind",
    "is_enabled",
    "is_configured",
]


#: Intent statuses that produce a message; everything else is machinery.
DEX_NOTIFIABLE = frozenset(
    {
        IntentStatus.FILLED,
        IntentStatus.FAILED,
        IntentStatus.MISSED,
        IntentStatus.BLOCKED,
        IntentStatus.EXPIRED,
        IntentStatus.CANCELLED,
    }
)


#: A level was armed. Not a status: WAITING is also where BLOCKED and MISSED
#: return to, and those returns are not new orders.
DEX_OPENED = "dex.opened"


def dex_kind(status: str) -> str:
    return f"dex.{status.strip().lower()}"


def grid_kind(event_type: str) -> str:
    return f"grid.{event_type.strip().lower()}"


def chat_ids() -> list[str]:
    """Chats a message is fanned out to. Empty means notifications are off."""
    raw = settings.telegram_chat_id or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def is_configured() -> bool:
    """A blank token or no chat is a working configuration, not a broken one."""
    return bool(settings.telegram_bot_token.strip()) and bool(chat_ids())


def is_enabled(kind: str) -> bool:
    """Does ``NOTIFY_EVENTS`` subscribe to this kind?"""
    name = kind.strip().lower()
    for pattern in (settings.notify_events or "").split(","):
        pattern = pattern.strip().lower()
        if pattern and fnmatch(name, pattern):
            return True
    return False
