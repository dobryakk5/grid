from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import FomoActivityLeg, FomoLeaderboardState
from app.db.session import SessionLocal
from app.fomo.activity import aggregate_legs, normalize_swap

router = APIRouter(prefix="/api/fomo/activity")


class TraderSwaps(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    handle: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    rank: int = Field(ge=1, le=500)
    has_more: bool | None = None
    swaps: list[dict] = Field(default_factory=list, max_length=2000)


class ActivityImport(BaseModel):
    period: Literal["24h", "7d", "30d"] = "30d"
    requested_limit: int = Field(default=30, ge=1, le=500)
    traders: list[TraderSwaps] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_cohort(self):
        ids = [t.user_id for t in self.traders]
        ranks = [t.rank for t in self.traders]
        if len(set(ids)) != len(ids) or len(set(ranks)) != len(ranks):
            raise ValueError("Duplicate user or rank in leaderboard")
        if len(ids) > self.requested_limit:
            raise ValueError("Cohort exceeds requested limit")
        return self


def prepare_import(payload):
    legs, rejected, source_rows = {}, 0, 0
    for trader in payload.traders:
        for row in trader.swaps:
            source_rows += 1
            parsed = normalize_swap(row)
            rejected += not bool(parsed)
            for leg in parsed:
                legs[(trader.user_id, leg["swap_id"], leg["side"])] = {
                    "period": payload.period, "user_id": trader.user_id, **leg,
                }
    return list(legs.values()), {
        "source_rows": source_rows, "rejected_rows": rejected,
        "traders_with_more": sum(t.has_more is True for t in payload.traders),
        "traders_unknown_coverage": sum(t.has_more is None for t in payload.traders),
        "cohort_shortfall": max(0, payload.requested_limit - len(payload.traders)),
        "history_complete": not rejected and len(payload.traders) == payload.requested_limit
        and all(t.has_more is False for t in payload.traders),
    }


@router.post("/import")
async def import_activity(payload: ActivityImport):
    legs, coverage = prepare_import(payload)
    if coverage["source_rows"] and not legs:
        raise HTTPException(status_code=422, detail="Ни один swap не распознан; прежний сбор сохранён")
    async with SessionLocal() as session:
        # Bound SQL parameter counts and keep the whole cohort atomic.
        for start in range(0, len(legs), 500):
            statement = insert(FomoActivityLeg).values(legs[start:start + 500])
            statement = statement.on_conflict_do_update(
                index_elements=[FomoActivityLeg.period, FomoActivityLeg.user_id,
                                FomoActivityLeg.swap_id, FomoActivityLeg.side],
                set_={key: getattr(statement.excluded, key) for key in (
                    "chain_id", "token_address", "symbol", "token_amount", "value_usd", "occurred_at_ms",
                )},
            )
            await session.execute(statement)
        values = {
            "period": payload.period, "requested_limit": payload.requested_limit,
            "captured_at": datetime.now(timezone.utc),
            "traders": [t.model_dump(exclude={"swaps", "has_more"}) for t in payload.traders],
            "coverage": coverage,
        }
        statement = insert(FomoLeaderboardState).values(**values)
        await session.execute(statement.on_conflict_do_update(
            index_elements=[FomoLeaderboardState.period],
            set_={k: v for k, v in values.items() if k != "period"},
        ))
        await session.commit()
    return {"traders": len(payload.traders), "swap_legs": len(legs), "coverage": coverage}


@router.get("")
async def activity(period: Literal["24h", "7d", "30d"] = "30d",
                   hours: int = Query(default=24, ge=1, le=24 * 30)):
    async with SessionLocal() as session:
        cohort = await session.get(FomoLeaderboardState, period)
        if cohort is None:
            return {"cohort": None, "coins": []}
        identities = {t["user_id"]: t for t in cohort.traders}
        cutoff = int(datetime.now(timezone.utc).timestamp() * 1000) - hours * 3600_000
        legs = list((await session.execute(select(FomoActivityLeg).where(
            FomoActivityLeg.period == period,
            FomoActivityLeg.user_id.in_(identities),
            FomoActivityLeg.occurred_at_ms >= cutoff,
        ))).scalars())
        return {
            "cohort": {"period": period, "requested_limit": cohort.requested_limit,
                       "traders": len(identities), "captured_at": cohort.captured_at,
                       "coverage": cohort.coverage},
            "coins": aggregate_legs(legs, identities),
        }
