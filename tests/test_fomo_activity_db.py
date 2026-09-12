"""Optional PostgreSQL integration test; uses a unique disposable schema.

FOMO_TEST_DATABASE_URL=postgresql+asyncpg://... pytest -q tests/test_fomo_activity_db.py
"""
import os
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import fomo_activity
from app.db.models import (
    Base, ChainToken, FomoActivityLeg, FomoLeaderboardState, FomoToken, FomoTrader,
)


@pytest.mark.skipif(not os.environ.get("FOMO_TEST_DATABASE_URL"), reason="requires test PostgreSQL")
async def test_reimport_does_not_double_totals_and_latest_cohort_controls_names(monkeypatch):
    schema = "fomo_test_" + uuid4().hex
    engine = create_async_engine(os.environ["FOMO_TEST_DATABASE_URL"], connect_args={
        "server_settings": {"search_path": schema},
    })
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[FomoActivityLeg.__table__, FomoLeaderboardState.__table__,
                          FomoToken.__table__, FomoTrader.__table__, ChainToken.__table__],
        ))
    monkeypatch.setattr(fomo_activity, "SessionLocal", async_sessionmaker(engine, expire_on_commit=False))

    # Names come from a third party; this test is about what the import stores.
    async def named(http, wanted):
        return {key: ("WIF", "dogwifhat") for key in wanted}

    monkeypatch.setattr(fomo_activity, "resolve_names", named)
    app = FastAPI()
    app.include_router(fomo_activity.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/api/fomo/activity")).json()["cohort"] is None
            swap = {
                "id": "same-swap", "createdAt": int(datetime.now(timezone.utc).timestamp() * 1000),
                "inTokenAddress": "0xUSDC", "outTokenAddress": "SoLaNa",
                "inNetworkId": 8453, "outNetworkId": 1399811149,
                "inHumanAmount": "10", "outHumanAmount": "2", "humanUsdAmountIn": "10",
                "humanUsdAmountOut": "9.9", "outTokenSymbol": "TOKEN",
            }
            # Robinhood Chain: DexScreener does not index it, but the tape
            # scanner has already read this contract's own symbol().
            async with engine.begin() as conn:
                await conn.execute(ChainToken.__table__.insert().values(
                    chain_id=4663, address="0xpons", symbol="PONS", decimals=18))
            on_chain = {**swap, "id": "rh-swap", "inTokenAddress": "0xUSDG",
                        "outTokenAddress": "0xpons", "inNetworkId": 4663,
                        "outNetworkId": 4663, "outTokenSymbol": None}
            payload = {"requested_limit": 1, "traders": [{
                "user_id": "alice", "handle": "alice", "rank": 1, "has_more": True,
                "swaps": [swap, on_chain],
            }]}
            for _ in range(2):
                response = await http.post("/api/fomo/activity/import", json=payload)
                assert response.status_code == 200, response.text
            result = (await http.get("/api/fomo/activity")).json()
            token = next(c for c in result["coins"] if c["chain_id"] == 1399811149)
            assert float(token["buy_usd"]) == 9.9
            assert token["traders"][0]["buys"] == 1
            assert token["traders"][0]["handle"] == "alice"
            # The swap named this one itself; the lookup must not overwrite it.
            assert token["symbol"] == "TOKEN"
            # The one it did not name gets the looked-up name instead.
            assert next(c for c in result["coins"] if c["chain_id"] == 8453)["name"] == "dogwifhat"
            # The chain's own answer, not the stubbed DexScreener one.
            pons = next(c for c in result["coins"] if c["token_address"] == "0xpons")
            assert (pons["symbol"], pons["name"]) == ("PONS", None)
            assert not result["cohort"]["coverage"]["history_complete"]

            # Malformed responses preserve the old cohort and stored events.
            payload["traders"][0]["swaps"] = [{"id": "bad"}]
            assert (await http.post("/api/fomo/activity/import", json=payload)).status_code == 422
            assert len((await http.get("/api/fomo/activity")).json()["coins"]) == 4

            # Same event enriched with a revised name/price updates in place.
            payload["traders"][0].update(handle="alice-new", swaps=[{**swap, "humanUsdAmountOut": "9.8"}])
            assert (await http.post("/api/fomo/activity/import", json=payload)).status_code == 200
            result = (await http.get("/api/fomo/activity")).json()
            token = next(c for c in result["coins"] if c["chain_id"] == 1399811149)
            assert float(token["buy_usd"]) == 9.8
            assert token["traders"][0]["handle"] == "alice-new"

            # Outgoing members' historical swaps cannot leak into the new top.
            payload["traders"] = [{"user_id": "bob", "rank": 1, "has_more": False, "swaps": []}]
            assert (await http.post("/api/fomo/activity/import", json=payload)).status_code == 200
            assert (await http.get("/api/fomo/activity")).json()["coins"] == []
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
