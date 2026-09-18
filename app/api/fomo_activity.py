from datetime import datetime, timezone
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.db.models import (
    ChainToken, FomoActivityLeg, FomoLeaderboardState, FomoThesis, FomoToken, FomoTrader,
)
from app.db.session import SessionLocal, database_target
from app.fomo.activity import aggregate_legs, normalize_swap
from app.fomo.theses import normalize_thesis
from app.fomo.tokens import CHAIN_SLUGS, resolve as resolve_names

router = APIRouter(prefix="/api/fomo/activity")


class TraderSwaps(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    handle: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    rank: int = Field(ge=1, le=500)
    has_more: bool | None = None
    # A full history is ~600 swaps per trader today; the cap is a ceiling on
    # one request body, and the collector stops at the same number.
    swaps: list[dict] = Field(default_factory=list, max_length=5000)


class TokenTheses(BaseModel):
    """Theses the collector read about one coin, exactly as the feed gave them.

    Grouped by coin rather than by author because that is how FOMO serves
    them: the request names a coin, and each row names its author.
    """

    chain_id: int = Field(ge=1, le=2**31 - 1)
    token_address: str = Field(min_length=1, max_length=128)
    items: list[dict] = Field(default_factory=list, max_length=2000)


class ActivityImport(BaseModel):
    period: Literal["24h", "7d", "30d"] = "30d"
    requested_limit: int = Field(default=30, ge=1, le=500)
    traders: list[TraderSwaps] = Field(min_length=1, max_length=500)
    # Optional: an older collector sends none, and an import that read swaps
    # but failed to read theses is still a good import.
    theses: list[TokenTheses] = Field(default_factory=list, max_length=1000)
    thesis_coverage: dict | None = None

    @field_validator("thesis_coverage")
    @classmethod
    def small_diagnostics(cls, value):
        """Counters and one message, not a place to park arbitrary JSON.

        This dict is stored verbatim in the cohort snapshot, so it is bounded
        here rather than trusted for being ours.
        """
        if value is None:
            return None
        if len(value) > 40:
            raise ValueError("thesis_coverage: слишком много полей")
        for key, item in value.items():
            if len(str(key)) > 40 or not isinstance(item, (int, float, str, bool, type(None))):
                raise ValueError("thesis_coverage: допустимы только числа, строки и флаги")
            if isinstance(item, str) and len(item) > 500:
                raise ValueError("thesis_coverage: слишком длинное значение")
        return value

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


def prepare_theses(payload):
    """Stored theses plus what was dropped and why.

    Only the cohort's own notes are kept: the feed for a coin carries everyone
    who wrote about it, and this table answers "what did the leaderboard say",
    not "what does the internet think of this coin". An unparseable row is
    counted, never guessed at.
    """
    cohort = {trader.user_id for trader in payload.traders}
    rows, outside, rejected = {}, 0, 0
    for group in payload.theses:
        for item in group.items:
            thesis = normalize_thesis(item, chain_id=group.chain_id,
                                      token_address=group.token_address)
            if thesis is None:
                rejected += 1
            elif thesis["user_id"] not in cohort:
                outside += 1
            else:
                rows[thesis["thesis_id"]] = thesis
    coverage = dict(payload.thesis_coverage or {})
    coverage.update({"stored": len(rows), "outside_cohort": outside, "rejected": rejected})
    return list(rows.values()), coverage


async def name_tokens(session, wanted) -> int:
    """Name the coins we have not asked about yet; return how many got a name.

    Two sources, because neither is complete on its own. Tokens on Robinhood
    Chain come from ``chain_tokens``, read from each contract's own ``symbol()``
    by the tape scanner -- back when DexScreener did not index that chain, this
    was the only thing keeping its busiest coins from staying bare addresses,
    and it still names what the screener has not listed. Everything else comes
    from DexScreener. The contract's own answer wins where both exist.

    Called after the legs are committed, in its own transaction: naming is
    cosmetic and talks to a third party, and an import that has just walked
    FOMO's whole history must not be rolled back because DexScreener was slow.
    """
    if not wanted:
        return 0
    known = set((await session.execute(select(FomoToken.chain_id, FomoToken.token_address).where(
        tuple_(FomoToken.chain_id, FomoToken.token_address).in_(wanted)
    ))).all())
    missing = wanted - known
    if not missing:
        return 0
    found = {}
    for token in (await session.execute(select(ChainToken).where(
        tuple_(ChainToken.chain_id, ChainToken.address).in_(missing)
    ))).scalars():
        if token.symbol:
            # chain_tokens carries a ticker and no long name; there is nothing
            # to invent here, so the name stays empty.
            found[(token.chain_id, token.address)] = (token.symbol[:64], None)
    listed = {key for key in missing - set(found) if key[0] in CHAIN_SLUGS}
    if listed:
        async with httpx.AsyncClient(timeout=20) as http:
            found.update(await resolve_names(http, listed))
    rows = [{"chain_id": chain_id, "token_address": address,
             "symbol": symbol, "name": name,
             "source": "chain" if chain_id not in CHAIN_SLUGS else "dexscreener"}
            for (chain_id, address), (symbol, name) in found.items()]
    for start in range(0, len(rows), 500):
        statement = insert(FomoToken).values(rows[start:start + 500])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[FomoToken.chain_id, FomoToken.token_address],
            set_={"symbol": statement.excluded.symbol, "name": statement.excluded.name,
                  "source": statement.excluded.source, "checked_at": datetime.now(timezone.utc)},
        ))
    await session.commit()
    return sum(1 for symbol, _ in found.values() if symbol)


async def remember_traders(session, payload):
    """Keep id -> name outside the cohort snapshot.

    ``fomo_leaderboard_state`` holds only the latest top N, so as soon as the
    leaderboard rotates, months of imported legs point at user ids nothing can
    name. ``fomo_traders`` is the table that survives that rotation.
    """
    rows = [{"fomo_user_id": t.user_id, "user_handle": t.handle,
             "display_name": t.display_name, "source": "leaderboard"}
            for t in payload.traders]
    statement = insert(FomoTrader).values(rows)
    await session.execute(statement.on_conflict_do_update(
        index_elements=[FomoTrader.fomo_user_id],
        # Never blank a name we already have with a null from a thinner payload.
        set_={"user_handle": func.coalesce(statement.excluded.user_handle, FomoTrader.user_handle),
              "display_name": func.coalesce(statement.excluded.display_name, FomoTrader.display_name),
              "last_seen_at": datetime.now(timezone.utc)},
    ))


@router.post("/import")
async def import_activity(payload: ActivityImport):
    legs, coverage = prepare_import(payload)
    theses, thesis_coverage = prepare_theses(payload)
    coverage["theses"] = thesis_coverage
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
        for start in range(0, len(theses), 500):
            statement = insert(FomoThesis).values(
                [{**row, "imported_at": datetime.now(timezone.utc)}
                 for row in theses[start:start + 500]])
            await session.execute(statement.on_conflict_do_update(
                index_elements=[FomoThesis.thesis_id],
                # A thesis can be edited and liked after it is written; the
                # coin and the author it was filed under cannot change.
                set_={key: getattr(statement.excluded, key) for key in (
                    "text", "likes", "replies", "usd_amount", "created_at_ms",
                    "trade_id", "imported_at",
                )},
            ))
        await remember_traders(session, payload)
        await session.commit()
        named = await name_tokens(
            session, {(leg["chain_id"], leg["token_address"]) for leg in legs})
    return {"traders": len(payload.traders), "swap_legs": len(legs),
            "theses": len(theses), "coverage": coverage,
            "database": database_target(), "named_tokens": named}


async def cohort_identities(session, cohort) -> dict:
    """``user_id -> {handle, display_name, rank}`` for the stored cohort.

    The cohort snapshot can carry a null handle; ``fomo_traders`` is where a
    name learned on any earlier import still lives.
    """
    identities = {t["user_id"]: dict(t) for t in cohort.traders}
    for trader in (await session.execute(select(FomoTrader).where(
        FomoTrader.fomo_user_id.in_(identities)
    ))).scalars():
        identity = identities[trader.fomo_user_id]
        identity["handle"] = identity.get("handle") or trader.user_handle
        identity["display_name"] = identity.get("display_name") or trader.display_name
    return identities


async def token_names(session, coins) -> dict:
    return {(row.chain_id, row.token_address): (row.symbol, row.name)
            for row in (await session.execute(select(FomoToken).where(
                tuple_(FomoToken.chain_id, FomoToken.token_address).in_(coins)
            ))).scalars()} if coins else {}


async def leg_symbols(session, coins) -> dict:
    """What the trade records themselves called each coin, where they said.

    The same precedence the coin table applies (``aggregate_legs``): a ticker
    that came with the trade beats a looked-up one, so the same coin is not
    labelled two different ways on one page.
    """
    if not coins:
        return {}
    rows = await session.execute(select(
        FomoActivityLeg.chain_id, FomoActivityLeg.token_address, FomoActivityLeg.symbol,
    ).where(
        tuple_(FomoActivityLeg.chain_id, FomoActivityLeg.token_address).in_(coins),
        FomoActivityLeg.symbol.is_not(None),
    ).distinct())
    return {(chain_id, address): symbol for chain_id, address, symbol in rows}


@router.get("/theses")
async def theses(period: Literal["24h", "7d", "30d"] = "30d",
                 hours: int = Query(default=24, ge=1, le=24 * 365),
                 limit: int = Query(default=200, ge=1, le=1000)):
    """What the current cohort wrote, newest first.

    Stored theses are not scoped to a leaderboard period -- the note is the
    same note whichever window brought its author into view -- so the period
    only selects whose notes to show.
    """
    async with SessionLocal() as session:
        cohort = await session.get(FomoLeaderboardState, period)
        if cohort is None:
            return {"cohort": None, "theses": [], "database": database_target()}
        identities = await cohort_identities(session, cohort)
        cutoff = int(datetime.now(timezone.utc).timestamp() * 1000) - hours * 3600_000
        rows = list((await session.execute(select(FomoThesis).where(
            FomoThesis.user_id.in_(identities),
            FomoThesis.created_at_ms >= cutoff,
        ).order_by(FomoThesis.created_at_ms.desc()).limit(limit))).scalars())
        coins = {(row.chain_id, row.token_address) for row in rows}
        names = await token_names(session, coins)
        traded = await leg_symbols(session, coins)
        return {
            "database": database_target(),
            "cohort": {"period": period, "captured_at": cohort.captured_at,
                       "coverage": (cohort.coverage or {}).get("theses")},
            "theses": [{
                "thesis_id": row.thesis_id,
                **identities[row.user_id],
                "chain_id": row.chain_id, "token_address": row.token_address,
                "symbol": traded.get((row.chain_id, row.token_address))
                or names.get((row.chain_id, row.token_address), (None, None))[0],
                "name": names.get((row.chain_id, row.token_address), (None, None))[1],
                "trade_id": row.trade_id, "text": row.text,
                "likes": row.likes, "replies": row.replies,
                "usd_amount": row.usd_amount, "created_at_ms": row.created_at_ms,
            } for row in rows],
        }


@router.get("")
async def activity(period: Literal["24h", "7d", "30d"] = "30d",
                   # The collector now walks a trader's whole history, which
                   # reaches back further than the leaderboard's own window.
                   hours: int = Query(default=24, ge=1, le=24 * 365)):
    async with SessionLocal() as session:
        cohort = await session.get(FomoLeaderboardState, period)
        if cohort is None:
            return {"cohort": None, "coins": [], "database": database_target()}
        identities = await cohort_identities(session, cohort)
        cutoff = int(datetime.now(timezone.utc).timestamp() * 1000) - hours * 3600_000
        legs = list((await session.execute(select(FomoActivityLeg).where(
            FomoActivityLeg.period == period,
            FomoActivityLeg.user_id.in_(identities),
            FomoActivityLeg.occurred_at_ms >= cutoff,
        ))).scalars())
        names = await token_names(session, {(leg.chain_id, leg.token_address) for leg in legs})
        return {
            "database": database_target(),
            "cohort": {"period": period, "requested_limit": cohort.requested_limit,
                       "traders": len(identities), "captured_at": cohort.captured_at,
                       "coverage": cohort.coverage},
            "coins": aggregate_legs(legs, identities, names),
        }
