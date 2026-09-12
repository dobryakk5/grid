"""One pass: read the market, check the contracts, read what was written.

Shared by the worker and by the page's refresh button so there is exactly one
description of what a pass does. Everything it writes is a fact with a time on
it -- snapshots accumulate, security answers are replaced when stale, thesis
readings are inserted once per reader.

Nothing here scores anything or decides anything; it only collects. The card
is assembled at read time in ``app.api.intel``, so a better score can be shipped
without re-collecting a month of data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.db.models import (
    FomoActivityLeg, FomoLeaderboardState, FomoThesis, FomoToken, ThesisEvent,
    TokenSecurity, TokenSnapshot,
)
from app.intel import llm
from app.intel.events import classify
from app.intel.market import snapshot_tokens
from app.intel.security import check_tokens
from app.intel.tape import tape_facts
from app.fomo.tokens import CHAIN_SLUGS

__all__ = ["candidate_keys", "refresh"]

logger = logging.getLogger(__name__)


async def candidate_keys(session, *, period: str, hours: int, now_ms: int, limit: int) -> list:
    """The coins the cohort traded inside the window, freshest first.

    This is the whole watchlist: no separate list to maintain, and nothing in
    it that the people we actually follow have not touched.
    """
    cohort = await session.get(FomoLeaderboardState, period)
    if cohort is None:
        return []
    users = [trader["user_id"] for trader in cohort.traders]
    cutoff = now_ms - hours * 3600_000
    rows = (await session.execute(select(
        FomoActivityLeg.chain_id, FomoActivityLeg.token_address, FomoActivityLeg.occurred_at_ms,
    ).where(
        FomoActivityLeg.period == period,
        FomoActivityLeg.user_id.in_(users),
        FomoActivityLeg.occurred_at_ms >= cutoff,
    ))).all()
    latest: dict[tuple[int, str], int] = {}
    for chain_id, address, at_ms in rows:
        key = (chain_id, address)
        latest[key] = max(latest.get(key, 0), at_ms)
    return sorted(latest, key=lambda key: (-latest[key], key[0], key[1]))[:limit]


async def store_market(session, facts, *, now_ms: int) -> int:
    if not facts:
        return 0
    rows = [fact.row(now_ms) for fact in facts.values()]
    for start in range(0, len(rows), 500):
        await session.execute(insert(TokenSnapshot).values(rows[start:start + 500]))
    await session.commit()
    return len(rows)


async def store_names(session, facts) -> int:
    """Names learned while reading the market go to the one naming registry.

    ``fomo_tokens`` already answers "what is this address called" for the whole
    app, and the market lookup meets tickers the FOMO import never learned --
    its swap feed carries no symbol at all. Writing them here means the intel
    page and the activity page never disagree about a coin's name.

    Never blanks a name we already have: a lookup that came back without one
    must not erase a good answer from an earlier pass.
    """
    rows = [{"chain_id": fact.chain_id, "token_address": fact.token_address,
             "symbol": fact.symbol[:64], "name": (fact.name or None) and fact.name[:160],
             "source": "chain" if fact.source == "tape" else "dexscreener"}
            for fact in facts.values() if fact.symbol]
    if not rows:
        return 0
    for start in range(0, len(rows), 500):
        statement = insert(FomoToken).values(rows[start:start + 500])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[FomoToken.chain_id, FomoToken.token_address],
            set_={"symbol": func.coalesce(statement.excluded.symbol, FomoToken.symbol),
                  "name": func.coalesce(statement.excluded.name, FomoToken.name),
                  "source": statement.excluded.source,
                  "checked_at": datetime.now(timezone.utc)},
        ))
    await session.commit()
    return len(rows)


async def stale_security(session, keys) -> list:
    """Coins whose contract answer is missing or older than the TTL."""
    if not keys:
        return []
    fresh_after = datetime.now(timezone.utc) - timedelta(hours=settings.intel_security_ttl_hours)
    known = {
        (row.chain_id, row.token_address): row.checked_at
        for row in (await session.execute(select(TokenSecurity).where(
            tuple_(TokenSecurity.chain_id, TokenSecurity.token_address).in_(keys)
        ))).scalars()
    }
    return [key for key in keys
            if known.get(key) is None or known[key] < fresh_after]


async def store_security(session, results) -> int:
    if not results:
        return 0
    now = datetime.now(timezone.utc)
    rows = [{"chain_id": chain_id, "token_address": address, "source": "goplus",
             "checked_at": now, "facts": {k: str(v) if v is not None else None
                                          for k, v in facts.items()},
             "raw": raw}
            for (chain_id, address), (facts, raw) in results.items()]
    for start in range(0, len(rows), 100):
        statement = insert(TokenSecurity).values(rows[start:start + 100])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[TokenSecurity.chain_id, TokenSecurity.token_address],
            set_={key: getattr(statement.excluded, key)
                  for key in ("source", "checked_at", "facts", "raw")},
        ))
    await session.commit()
    return len(rows)


async def read_theses(session, *, hours: int, now_ms: int, use_llm: bool = True) -> dict:
    """Classify the notes nobody has read yet: rules first, model only after.

    Readings are keyed by ``(thesis_id, source)``, so a pass never re-reads a
    note the same reader has already read, and the two readers never overwrite
    each other. The model is offered only the notes the rules came back empty
    on -- a note about a buyback needs no second opinion.
    """
    cutoff = now_ms - hours * 3600_000
    notes = list((await session.execute(select(FomoThesis).where(
        FomoThesis.created_at_ms >= cutoff
    ))).scalars())
    if not notes:
        return {"rules": 0, "llm": 0, "unread": 0}
    existing = {
        (row.thesis_id, row.source): row
        for row in (await session.execute(select(ThesisEvent).where(
            ThesisEvent.thesis_id.in_([note.thesis_id for note in notes])
        ))).scalars()
    }

    rows, unclear = [], {}
    for note in notes:
        stored = existing.get((note.thesis_id, "rules"))
        if stored is None:
            reading = classify(note.text)
            rows.append({"thesis_id": note.thesis_id, "source": "rules", **_event_row(reading)})
            kinds = reading["kinds"]
        else:
            kinds = stored.kinds or []
        if not kinds and (note.thesis_id, "llm") not in existing:
            unclear[note.thesis_id] = note.text
    if rows:
        await _store_events(session, rows)

    readings = {}
    if use_llm and unclear and llm.available():
        readings = await llm.classify_theses(list(unclear.items()))
        if readings:
            await _store_events(session, [
                {"thesis_id": thesis_id, "source": "llm", **_event_row(reading)}
                for thesis_id, reading in readings.items()
            ])
    return {"rules": len(rows), "llm": len(readings), "unread": len(unclear)}


def _event_row(reading: dict) -> dict:
    return {
        "kinds": reading["kinds"],
        "stance": reading["stance"],
        "importance": reading["importance"],
        "numbers": reading["numbers"],
        "confidence": reading["confidence"],
    }


async def _store_events(session, rows) -> None:
    for start in range(0, len(rows), 500):
        statement = insert(ThesisEvent).values(rows[start:start + 500])
        await session.execute(statement.on_conflict_do_update(
            index_elements=[ThesisEvent.thesis_id, ThesisEvent.source],
            set_={key: getattr(statement.excluded, key)
                  for key in ("kinds", "stance", "importance", "numbers", "confidence")},
        ))
    await session.commit()


async def refresh(session, *, period: str = "30d", hours: int = 24, now_ms: int,
                  http=None, use_llm: bool = True) -> dict:
    """Collect everything the card needs for the current candidate set."""
    keys = await candidate_keys(session, period=period, hours=hours, now_ms=now_ms,
                                limit=settings.intel_max_tokens)
    result = {"tokens": len(keys), "market": 0, "named": 0, "security": 0,
              "theses": {"rules": 0, "llm": 0, "unread": 0}}
    if not keys:
        return result

    owns_http = http is None
    http = http or httpx.AsyncClient(timeout=20.0)
    try:
        listed = [key for key in keys if key[0] in CHAIN_SLUGS]
        facts = await snapshot_tokens(http, listed) if listed else {}
        # Chains no screener indexes -- Robinhood Chain today -- are read from
        # our own tape instead of left blank.
        for chain_id in {key[0] for key in keys} - set(CHAIN_SLUGS):
            own = {key for key in keys if key[0] == chain_id}
            facts.update(await tape_facts(session, own, now_ms=now_ms, hours=hours))
        result["market"] = await store_market(session, facts, now_ms=now_ms)
        result["named"] = await store_names(session, facts)

        wanted = await stale_security(session, keys)
        if wanted:
            result["security"] = await store_security(session, await check_tokens(http, wanted))
    except Exception:
        # A pass is a refresh, not a transaction: whatever was collected before
        # the failure is already committed and useful.
        logger.exception("intel: сбор рыночных данных не завершился")
    finally:
        if owns_http:
            await http.aclose()

    result["theses"] = await read_theses(session, hours=hours, now_ms=now_ms, use_llm=use_llm)
    return result
