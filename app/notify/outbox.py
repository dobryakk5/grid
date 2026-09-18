"""Queueing a message, and handing it to the notifier.

Nothing here commits, and nothing here talks to the network. ``enqueue`` is
synchronous on purpose: its callers are inside a trading transaction (sometimes
a synchronous one, as in :func:`app.trading.events.record_strategy_event`), and
the whole guarantee of this design is that the message and the operation share
one commit. An ``await`` there would be a place for the two to come apart.

The queue is a table rather than an in-process buffer because the process that
notices a fill is not the process that sends messages, and because a worker
restart must not lose the one message that mattered.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Notification
from app.notify.events import chat_ids, is_configured, is_enabled

__all__ = ["claim", "enqueue", "mark_failed", "mark_sent"]

logger = logging.getLogger(__name__)

PENDING = "PENDING"
SENT = "SENT"
FAILED = "FAILED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    """Decimals and datetimes as strings; JSONB refuses them, and a lost
    payload is a lost message."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def enqueue(session: AsyncSession | Session, kind: str, payload: dict) -> list[Notification]:
    """Queue ``kind`` for every configured chat, inside the caller's transaction.

    Returns the rows added, which is an empty list whenever notifications are
    off or the kind is not subscribed to -- filtering here rather than at send
    time keeps the table from filling with messages nobody asked for.
    """
    if not is_configured() or not is_enabled(kind):
        return []
    body = _jsonable(payload)
    rows = [
        Notification(kind=kind, chat_id=chat, payload=body, status=PENDING, scheduled_at=_now())
        for chat in chat_ids()
    ]
    for row in rows:
        session.add(row)
    return rows


async def claim(session: AsyncSession, *, limit: int | None = None) -> list[Notification]:
    """Take the next due messages, oldest first, locking them for this worker.

    ``skip_locked`` is not for the single notifier this project runs, but for
    the moment someone starts a second one: two senders sharing a queue must
    divide it, not duplicate it.
    """
    statement = (
        select(Notification)
        .where(Notification.status == PENDING, Notification.scheduled_at <= _now())
        .order_by(Notification.id)
        .limit(limit if limit is not None else settings.notify_batch)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(statement)).scalars())


def mark_sent(notification: Notification) -> None:
    notification.status = SENT
    notification.sent_at = _now()
    notification.last_error = None


def mark_failed(notification: Notification, error: str, *, retry_after: float | None = None) -> None:
    """Back off, or give up once ``NOTIFY_MAX_ATTEMPTS`` is spent.

    Giving up is the right end state: an undeliverable notification is not a
    reason to keep the queue -- and therefore every later message -- waiting.
    """
    # A row straight from the ORM has not been through an INSERT yet, so its
    # server-side default is not there to add to.
    notification.attempts = int(notification.attempts or 0) + 1
    notification.last_error = error[:500]
    if notification.attempts >= settings.notify_max_attempts:
        notification.status = FAILED
        return
    # 2, 4, 8 ... capped at a minute, unless Telegram named its own delay.
    backoff = min(2.0 ** notification.attempts, 60.0)
    if retry_after is not None:
        backoff = max(backoff, float(retry_after))
    notification.scheduled_at = _now() + timedelta(seconds=backoff)
