"""Optional PostgreSQL integration test for the token-intelligence card.

FOMO_TEST_DATABASE_URL=postgresql+asyncpg://... pytest -q tests/test_intel_api_db.py
"""
import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import intel as intel_api
from app.db.models import (
    Base, ChainSwap, FomoActivityLeg, FomoLeaderboardState, FomoThesis, FomoToken,
    FomoTrader, ThesisEvent, TokenSecurity, TokenSnapshot,
)
from app.intel.refresh import refresh

TABLES = [FomoActivityLeg, FomoLeaderboardState, FomoThesis, FomoToken, FomoTrader,
          ThesisEvent, TokenSecurity, TokenSnapshot, ChainSwap]

BRETT = "0xbrett"
PONS = "0xpons"

DEX_PAIR = {
    "chainId": "base", "pairAddress": "0xpair", "dexId": "uniswap",
    "baseToken": {"address": BRETT, "symbol": "BRETT", "name": "Brett"},
    "quoteToken": {"address": "0xusdc", "symbol": "USDC"},
    "priceUsd": "0.0049", "liquidity": {"usd": 2_300_000},
    "volume": {"h24": 870_000, "h6": 300_000},
    "txns": {"h24": {"buys": 692, "sells": 838}},
    "priceChange": {"h1": -0.6, "h6": -1.2, "h24": 1.0},
    "marketCap": 48_800_000, "fdv": 48_800_000, "pairCreatedAt": 1_700_000_000_000,
}

GOPLUS = {
    "is_open_source": "1", "is_mintable": "1", "is_honeypot": "0", "buy_tax": "",
    "sell_tax": "0", "holder_count": "903280", "token_symbol": "BRETT",
    "holders": [{"address": "0xwhale", "percent": "0.34", "is_locked": 0, "tag": ""}],
}


def handle(request):
    url = str(request.url)
    if "dexscreener" in url:
        return httpx.Response(200, json={"pairs": [DEX_PAIR]})
    if "gopluslabs" in url:
        return httpx.Response(200, json={"result": {BRETT: GOPLUS}})
    return httpx.Response(404)


@pytest.mark.skipif(not os.environ.get("FOMO_TEST_DATABASE_URL"), reason="requires test PostgreSQL")
async def test_the_card_joins_flow_market_contract_and_what_was_written(monkeypatch):
    schema = "intel_test_" + uuid4().hex
    engine = create_async_engine(os.environ["FOMO_TEST_DATABASE_URL"], connect_args={
        "server_settings": {"search_path": schema},
    })
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(
            sync, tables=[model.__table__ for model in TABLES]))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(intel_api, "SessionLocal", Session)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    hour = 3600_000
    try:
        async with Session() as session:
            session.add(FomoLeaderboardState(
                period="30d", requested_limit=2, coverage={},
                traders=[{"user_id": "u-alice", "handle": "alice", "display_name": "Alice", "rank": 1},
                         {"user_id": "u-bob", "handle": "bob", "display_name": "Bob", "rank": 2}]))
            for user, side, address, chain, usd, ago in [
                ("u-alice", "BUY", BRETT, 8453, "6000", 2), ("u-bob", "BUY", BRETT, 8453, "11000", 1),
                ("u-alice", "SELL", BRETT, 8453, "1000", 1), ("u-bob", "BUY", PONS, 4663, "4000", 3),
            ]:
                session.add(FomoActivityLeg(
                    period="30d", user_id=user, swap_id=f"{user}-{address}-{side}", side=side,
                    chain_id=chain, token_address=address, symbol=None,
                    token_amount=Decimal("100"), value_usd=Decimal(usd),
                    occurred_at_ms=now_ms - ago * hour))
            session.add(FomoThesis(
                thesis_id="th-1", user_id="u-alice", chain_id=8453, token_address=BRETT,
                text="First buyback executed: выкупили на $15,000 из казны",
                created_at_ms=now_ms - hour))
            session.add(FomoThesis(
                thesis_id="th-2", user_id="u-bob", chain_id=4663, token_address=PONS,
                text="wen", created_at_ms=now_ms - hour))
            # The tape is the only market data Robinhood Chain has.
            session.add(ChainSwap(
                tx_hash="0xtx", wallet_address="0xw", token_address=PONS, chain_id=4663,
                block_number=1, block_time_ms=now_ms - 2 * hour, symbol="PONS", side="BUY",
                token_amount=Decimal("1000"), value_usd=Decimal("600"),
                pricing_source="QUOTE_LEG"))
            await session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with Session() as session:
                # The model reader is not configured in tests; rules do the work.
                result = await refresh(session, now_ms=now_ms, http=http, use_llm=False)
        assert result["tokens"] == 2
        assert result["market"] == 2          # DexScreener for Base, our tape for 4663
        assert result["security"] == 1        # GoPlus answered about one of them
        assert result["theses"]["rules"] == 2
        # A ticker learned from the market lands in the app's one naming
        # registry: the FOMO swap feed never carries one.
        assert result["named"] == 2

        async with Session() as session:
            snapshots = {row.token_address: row for row in
                         (await session.execute(select(TokenSnapshot))).scalars()}
            assert snapshots[BRETT].source == "dexscreener"
            assert snapshots[PONS].source == "tape" and snapshots[PONS].liquidity_usd is None
            reading = await session.get(ThesisEvent, ("th-1", "rules"))
            assert reading.kinds == ["BUYBACK"] and reading.numbers["usd_max"] == "15000"
            # A note the rules could not read is stored as read-with-nothing, so
            # the next pass offers it to the model instead of re-reading it.
            assert (await session.get(ThesisEvent, ("th-2", "rules"))).kinds == []

        app = FastAPI()
        app.include_router(intel_api.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            data = (await client.get("/api/intel/candidates?hours=24")).json()
        cards = {coin["token_address"]: coin for coin in data["coins"]}
        assert set(cards) == {BRETT, PONS}
        brett = cards[BRETT]
        assert brett["symbol"] == "BRETT"
        assert brett["flow"]["buyers"] == 2 and brett["flow"]["sellers"] == 1
        assert float(brett["flow"]["buy_usd"]) == 17000
        assert brett["scores"]["quality"] and brett["scores"]["signal"] == "набирают"
        # The open mint authority is a finding with a cost, not a footnote.
        risky = [line for line in brett["scores"]["reasons"]["risk"] if line["points"] > 0]
        assert any("выпуск новых монет" in line["text"] for line in risky)
        assert brett["catalysts"][0]["kinds"] == ["BUYBACK"]
        assert brett["catalysts"][0]["importance"] == "HIGH"

        pons = cards[PONS]
        assert pons["market"]["source"] == "tape"
        # Unchecked is its own penalty and says so, rather than reading as safe.
        assert any("контракт не проверен" == line["text"] for line in pons["scores"]["reasons"]["risk"])
        assert pons["scores"]["quality"] is None
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
