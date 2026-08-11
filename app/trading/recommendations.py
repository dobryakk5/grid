from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StrategyRecommendation
from app.trading.events import record_strategy_event


PENDING = "PENDING"
PROCESSING = "PROCESSING"
RECOMMENDATION_TYPES = {
    "START_SWING", "START_TRAILING_BUY", "APPLY_NEW_RANGE", "RETURN_TO_GRID",
}
RECOMMENDATION_STATUSES = {
    PENDING, PROCESSING, "ACCEPTED", "REJECTED", "EXPIRED", "SUPERSEDED", "BLOCKED", "FAILED",
}
TERMINAL_STATUSES = RECOMMENDATION_STATUSES - {PENDING, PROCESSING}


async def create_recommendation(
    session: AsyncSession,
    *,
    profile_id: int,
    type: str,
    payload: dict | None = None,
    market_price: Decimal | None = None,
    expires_at: datetime | None = None,
) -> StrategyRecommendation:
    if type not in RECOMMENDATION_TYPES:
        raise ValueError(f"unsupported recommendation type: {type}")
    recommendation = StrategyRecommendation(
        profile_id=profile_id,
        type=type,
        status=PENDING,
        payload=payload or {},
        market_price=market_price,
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=15),
    )
    session.add(recommendation)
    await session.flush()
    record_strategy_event(
        session, profile_id=profile_id, event_type="RECOMMENDATION_CREATED",
        metadata={"recommendation_id": recommendation.id, "type": type},
    )
    return recommendation


async def expire_recommendations(
    session: AsyncSession, profile_id: int | None = None,
) -> list[StrategyRecommendation]:
    statement = (
        update(StrategyRecommendation)
        .where(
            StrategyRecommendation.status == PENDING,
            StrategyRecommendation.expires_at <= datetime.now(timezone.utc),
        )
        .values(status="EXPIRED", resolved_at=datetime.now(timezone.utc))
        .returning(StrategyRecommendation)
    )
    if profile_id is not None:
        statement = statement.where(StrategyRecommendation.profile_id == profile_id)
    result = await session.execute(statement)
    return list(result.scalars())


async def claim_recommendation(
    session: AsyncSession, recommendation_id: int,
) -> StrategyRecommendation | None:
    """Atomically move a still-valid recommendation into PROCESSING."""
    result = await session.execute(
        update(StrategyRecommendation)
        .where(
            StrategyRecommendation.id == recommendation_id,
            StrategyRecommendation.status == PENDING,
            StrategyRecommendation.expires_at > datetime.now(timezone.utc),
        )
        .values(status=PROCESSING)
        .returning(StrategyRecommendation)
    )
    return result.scalar_one_or_none()


async def accept_recommendation(
    session: AsyncSession, recommendation: StrategyRecommendation,
) -> StrategyRecommendation:
    if recommendation.status != PROCESSING:
        raise ValueError("recommendation is not claimed")
    recommendation.status = "ACCEPTED"
    recommendation.resolved_at = datetime.now(timezone.utc)
    record_strategy_event(
        session, profile_id=recommendation.profile_id, event_type="RECOMMENDATION_ACCEPTED",
        metadata={"recommendation_id": recommendation.id, "type": recommendation.type},
    )
    return recommendation


async def reject_recommendation(
    session: AsyncSession, recommendation_id: int,
) -> StrategyRecommendation | None:
    """Reject a pending recommendation atomically, including an expiration check."""
    result = await session.execute(
        update(StrategyRecommendation)
        .where(
            StrategyRecommendation.id == recommendation_id,
            StrategyRecommendation.status == PENDING,
            StrategyRecommendation.expires_at > datetime.now(timezone.utc),
        )
        .values(status="REJECTED", resolved_at=datetime.now(timezone.utc))
        .returning(StrategyRecommendation)
    )
    recommendation = result.scalar_one_or_none()
    if recommendation is not None:
        record_strategy_event(
            session, profile_id=recommendation.profile_id, event_type="RECOMMENDATION_REJECTED",
            metadata={"recommendation_id": recommendation.id, "type": recommendation.type},
        )
    return recommendation


async def supersede_recommendations(
    session: AsyncSession, *, profile_id: int, types: set[str] | None = None,
) -> int:
    statement = (
        update(StrategyRecommendation)
        .where(
            StrategyRecommendation.profile_id == profile_id,
            StrategyRecommendation.status == PENDING,
        )
        .values(status="SUPERSEDED", resolved_at=datetime.now(timezone.utc))
    )
    if types:
        statement = statement.where(StrategyRecommendation.type.in_(types))
    result = await session.execute(statement)
    return result.rowcount or 0


async def list_recommendations(
    session: AsyncSession, profile_id: int,
) -> list[StrategyRecommendation]:
    await expire_recommendations(session, profile_id)
    result = await session.execute(
        select(StrategyRecommendation)
        .where(StrategyRecommendation.profile_id == profile_id)
        .order_by(StrategyRecommendation.id.desc())
    )
    return list(result.scalars())
