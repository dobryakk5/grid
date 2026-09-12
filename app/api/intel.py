"""The card: what the top is trading, what it is, and what can go wrong.

Reads only. Everything it shows was collected by ``app.intel.refresh`` and is
dated, so a stale card looks stale instead of looking like news. The scoring
happens here, at read time, on purpose: the numbers are an opinion over stored
facts, and an opinion should be improvable without re-collecting anything.

This endpoint does not recommend a coin. It ranks by what is measurable and
shows, line by line, which facts produced each number and which were missing.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Query
from sqlalchemy import func, select, tuple_

from app.core.config import settings
from app.db.models import (
    FomoActivityLeg, FomoLeaderboardState, FomoThesis, FomoToken, ThesisEvent,
    TokenSecurity, TokenSnapshot,
)
from app.db.session import SessionLocal, database_target
from app.api.fomo_activity import cohort_identities, token_names
from app.intel import llm
from app.intel.market import MarketFacts
from app.intel.refresh import refresh as run_refresh
from app.intel.scoring import score

router = APIRouter(prefix="/api/intel")

HOUR_MS = 3600_000


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def flow_of(legs, identities) -> dict:
    """The cohort's own money in one coin: measured, not reported."""
    flow = {"buy_usd": Decimal(0), "sell_usd": Decimal(0), "buy_count": 0, "sell_count": 0,
            "unpriced": 0, "last_at_ms": 0, "traders": {}}
    for leg in legs:
        side = "buy" if leg.side == "BUY" else "sell"
        flow[side + "_count"] += 1
        if leg.value_usd is None:
            flow["unpriced"] += 1
        else:
            flow[side + "_usd"] += leg.value_usd
        flow["last_at_ms"] = max(flow["last_at_ms"], leg.occurred_at_ms)
        identity = identities.get(leg.user_id, {})
        person = flow["traders"].setdefault(leg.user_id, {
            "user_id": leg.user_id, "handle": identity.get("handle"),
            "display_name": identity.get("display_name"), "rank": identity.get("rank"),
            "buy_usd": Decimal(0), "sell_usd": Decimal(0),
        })
        person[side + "_usd"] += leg.value_usd or Decimal(0)
    buyers = [person for person in flow["traders"].values() if person["buy_usd"] > 0]
    flow["buyers"] = len(buyers)
    flow["sellers"] = sum(person["sell_usd"] > 0 for person in flow["traders"].values())
    ranks = [person["rank"] for person in buyers if person["rank"]]
    flow["rank_best"] = min(ranks) if ranks else None
    flow["traders"] = sorted(flow["traders"].values(), key=lambda person: person["rank"] or 10**9)
    return flow


def _facts_from_snapshot(row) -> MarketFacts:
    return MarketFacts(
        chain_id=row.chain_id, token_address=row.token_address, source=row.source,
        price_usd=row.price_usd, market_cap_usd=row.market_cap_usd, fdv_usd=row.fdv_usd,
        liquidity_usd=row.liquidity_usd, volume_h24_usd=row.volume_h24_usd,
        volume_h6_usd=row.volume_h6_usd, buys_h24=row.buys_h24, sells_h24=row.sells_h24,
        change_m5=row.change_m5, change_h1=row.change_h1, change_h6=row.change_h6,
        change_h24=row.change_h24, pair_created_at_ms=row.pair_created_at_ms, pools=row.pools,
    )


async def latest_snapshots(session, keys) -> dict:
    """The newest snapshot per coin, whenever it was taken.

    Deliberately not filtered by age: an old reading is shown with its date so
    the page can say "данные от вчера" rather than show an empty card.
    """
    if not keys:
        return {}
    newest = select(
        TokenSnapshot.chain_id, TokenSnapshot.token_address,
        func.max(TokenSnapshot.observed_at_ms).label("observed_at_ms"),
    ).where(
        tuple_(TokenSnapshot.chain_id, TokenSnapshot.token_address).in_(keys)
    ).group_by(TokenSnapshot.chain_id, TokenSnapshot.token_address).subquery()
    rows = (await session.execute(select(TokenSnapshot).join(newest, (
        TokenSnapshot.chain_id == newest.c.chain_id)
        & (TokenSnapshot.token_address == newest.c.token_address)
        & (TokenSnapshot.observed_at_ms == newest.c.observed_at_ms)))).scalars()
    return {(row.chain_id, row.token_address): row for row in rows}


def _security_facts(row) -> dict:
    """Back from storage: strings to the booleans and decimals scoring expects."""
    facts = {}
    for key, value in (row.facts or {}).items():
        if value in (None, "None"):
            continue
        if value in ("True", "False"):
            facts[key] = value == "True"
            continue
        try:
            facts[key] = Decimal(value)
        except Exception:
            facts[key] = value
    return facts


async def catalysts_for(session, *, identities, cutoff_ms: int) -> dict:
    """Theses by cohort members in the window, with whatever reading exists.

    Both readings travel with the note. The rules' reading is the default; the
    model's is shown as a second opinion rather than replacing it, because one
    of them is reproducible and the other is not.
    """
    notes = list((await session.execute(select(FomoThesis).where(
        FomoThesis.user_id.in_(identities), FomoThesis.created_at_ms >= cutoff_ms,
    ).order_by(FomoThesis.created_at_ms.desc()))).scalars())
    if not notes:
        return {}
    readings = {}
    for row in (await session.execute(select(ThesisEvent).where(
        ThesisEvent.thesis_id.in_([note.thesis_id for note in notes])
    ))).scalars():
        readings.setdefault(row.thesis_id, {})[row.source] = {
            "kinds": row.kinds, "stance": row.stance, "importance": row.importance,
            "numbers": row.numbers, "confidence": row.confidence,
        }
    grouped: dict[tuple[int, str], list] = {}
    for note in notes:
        seen = readings.get(note.thesis_id, {})
        best = seen.get("rules") or {}
        if not best.get("kinds") and seen.get("llm"):
            best = seen["llm"]
        identity = identities.get(note.user_id, {})
        grouped.setdefault((note.chain_id, note.token_address), []).append({
            "thesis_id": note.thesis_id, "user_id": note.user_id,
            "handle": identity.get("handle"), "display_name": identity.get("display_name"),
            "rank": identity.get("rank"), "text": note.text,
            "created_at_ms": note.created_at_ms, "likes": note.likes,
            "usd_amount": note.usd_amount,
            "kinds": best.get("kinds") or [], "stance": best.get("stance"),
            "importance": best.get("importance") or "LOW",
            "numbers": best.get("numbers") or {},
            "read_by": sorted(seen),
        })
    return grouped


@router.post("/refresh")
async def refresh_now(period: Literal["24h", "7d", "30d"] = "30d",
                      hours: int = Query(default=24, ge=1, le=24 * 30)):
    """Collect market, contract and thesis data for the current candidates."""
    now_ms = _now_ms()
    async with SessionLocal() as session:
        result = await run_refresh(session, period=period, hours=hours, now_ms=now_ms)
    return {**result, "database": database_target(), "observed_at_ms": now_ms}


@router.get("/candidates")
async def candidates(period: Literal["24h", "7d", "30d"] = "30d",
                     hours: int = Query(default=24, ge=1, le=24 * 365),
                     limit: int = Query(default=60, ge=1, le=300)):
    """Every coin the cohort traded in the window, scored on what is known."""
    now_ms = _now_ms()
    cutoff = now_ms - hours * HOUR_MS
    async with SessionLocal() as session:
        cohort = await session.get(FomoLeaderboardState, period)
        if cohort is None:
            return {"cohort": None, "coins": [], "database": database_target()}
        identities = await cohort_identities(session, cohort)
        legs = list((await session.execute(select(FomoActivityLeg).where(
            FomoActivityLeg.period == period,
            FomoActivityLeg.user_id.in_(identities),
            FomoActivityLeg.occurred_at_ms >= cutoff,
        ))).scalars())
        by_coin: dict[tuple[int, str], list] = {}
        for leg in legs:
            by_coin.setdefault((leg.chain_id, leg.token_address), []).append(leg)
        keys = list(by_coin)
        names = await token_names(session, set(keys))
        snapshots = await latest_snapshots(session, keys)
        security = {
            (row.chain_id, row.token_address): row
            for row in (await session.execute(select(TokenSecurity).where(
                tuple_(TokenSecurity.chain_id, TokenSecurity.token_address).in_(keys)
            ))).scalars()
        } if keys else {}
        catalysts = await catalysts_for(session, identities=identities, cutoff_ms=cutoff)

    coins = []
    for key, coin_legs in by_coin.items():
        snapshot = snapshots.get(key)
        market = _facts_from_snapshot(snapshot) if snapshot is not None else None
        checked = security.get(key)
        facts = _security_facts(checked) if checked is not None else None
        flow = flow_of(coin_legs, identities)
        notes = catalysts.get(key, [])
        symbol = next((leg.symbol for leg in coin_legs if leg.symbol), None) \
            or names.get(key, (None, None))[0]
        coins.append({
            "chain_id": key[0], "token_address": key[1],
            "symbol": symbol, "name": names.get(key, (None, None))[1],
            "market": {
                "source": snapshot.source, "observed_at_ms": snapshot.observed_at_ms,
                "price_usd": snapshot.price_usd, "market_cap_usd": snapshot.market_cap_usd,
                "fdv_usd": snapshot.fdv_usd, "liquidity_usd": snapshot.liquidity_usd,
                "volume_h24_usd": snapshot.volume_h24_usd, "volume_h6_usd": snapshot.volume_h6_usd,
                "buys_h24": snapshot.buys_h24, "sells_h24": snapshot.sells_h24,
                "change_h1": snapshot.change_h1, "change_h6": snapshot.change_h6,
                "change_h24": snapshot.change_h24,
                "pair_created_at_ms": snapshot.pair_created_at_ms,
            } if snapshot is not None else None,
            "security": {
                "checked_at": checked.checked_at, "source": checked.source, "facts": checked.facts,
            } if checked is not None else None,
            "flow": {key_: value for key_, value in flow.items() if key_ != "traders"},
            "traders": flow["traders"],
            "catalysts": notes,
            "scores": score(market=market, security=facts, flow=flow,
                            catalysts=notes, now_ms=now_ms),
        })
    # Momentum first, because the list answers "что происходит сейчас"; risk and
    # quality travel with every row so the sort never hides them.
    coins.sort(key=lambda coin: (coin["scores"]["momentum"] or -1), reverse=True)
    freshest = max((coin["market"]["observed_at_ms"] for coin in coins if coin["market"]),
                   default=None)
    return {
        "database": database_target(),
        "cohort": {"period": period, "traders": len(identities),
                   "captured_at": cohort.captured_at, "hours": hours},
        # Whether the model pass is actually usable, not merely configured:
        # a provider named in .env with no key behind it reads as off.
        "collected": {"market_observed_at_ms": freshest,
                      "stale_after_seconds": settings.intel_market_ttl_seconds,
                      "llm_reader": f"{settings.intel_llm_provider} · {settings.intel_llm_model}"
                      if llm.available() else None},
        "coins": coins[:limit],
    }
