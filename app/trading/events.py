from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StrategyEvent
from app.notify.events import grid_kind
from app.notify.outbox import enqueue


def record_strategy_event(
    session: AsyncSession,
    *,
    profile_id: int,
    event_type: str,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    market_price: Decimal | None = None,
    metadata: dict | None = None,
) -> StrategyEvent:
    """Append a meaningful strategy transition; never use this for worker heartbeats."""
    event = StrategyEvent(
        profile_id=profile_id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
        market_price=market_price,
        event_metadata=metadata or {},
    )
    session.add(event)
    # Queued in the caller's transaction, so a tick that rolls back takes the
    # message with the event it was about. `NOTIFY_EVENTS` decides which of
    # these are worth a phone buzzing; most of them are not.
    enqueue(session, grid_kind(event_type), {
        "profile_id": profile_id,
        "event_type": event_type,
        "from_state": from_state,
        "to_state": to_state,
        "reason": reason,
        "market_price": market_price,
        "metadata": metadata or {},
    })
    return event
