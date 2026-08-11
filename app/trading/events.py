from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StrategyEvent


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
    return event
