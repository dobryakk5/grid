from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db.models import BotState, GridOrder
from app.db.session import SessionLocal
from app.exchanges.bybit import BybitClient, BybitError

router = APIRouter(prefix="/api")


class StartBotRequest(BaseModel):
    symbol: str = "BTCUSDT"
    levels: int = Field(default=3, ge=1, le=10)
    step_pct: Decimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.10"))
    quote_per_level: Decimal = Field(default=Decimal("25"), gt=0)


class DemoFundsRequest(BaseModel):
    usdt: Decimal = Field(default=Decimal("10000"), gt=0, le=Decimal("100000"))


@router.get("/price/{symbol}")
async def price(symbol: str) -> dict:
    exchange = BybitClient()
    try:
        last = await exchange.last_price(symbol.upper())
        return {"symbol": symbol.upper(), "last_price": str(last)}
    finally:
        await exchange.close()


@router.get("/balance")
async def balance() -> dict:
    exchange = BybitClient()
    try:
        return await exchange.wallet_balance()
    except BybitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await exchange.close()


@router.post("/demo/funds")
async def demo_funds(payload: DemoFundsRequest) -> dict:
    exchange = BybitClient()
    try:
        return await exchange.apply_demo_usdt(payload.usdt)
    except BybitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await exchange.close()


@router.post("/bot/start")
async def start_bot(payload: StartBotRequest) -> dict:
    async with SessionLocal() as session:
        state = await session.get(BotState, 1)
        if state is None:
            raise HTTPException(status_code=500, detail="bot_state is missing")
        state.symbol = payload.symbol.upper()
        state.levels = payload.levels
        state.step_pct = payload.step_pct
        state.quote_per_level = payload.quote_per_level
        state.anchor_price = None
        state.enabled = True
        await session.commit()
        return {"ok": True, "enabled": True, "symbol": state.symbol}


@router.post("/bot/stop")
async def stop_bot() -> dict:
    async with SessionLocal() as session:
        state = await session.get(BotState, 1)
        if state is None:
            raise HTTPException(status_code=500, detail="bot_state is missing")
        state.enabled = False
        await session.commit()
        return {
            "ok": True,
            "enabled": False,
            "note": "worker will cancel tracked open orders on its next tick",
        }


@router.get("/bot/status")
async def bot_status() -> dict:
    async with SessionLocal() as session:
        state = await session.get(BotState, 1)
        if state is None:
            raise HTTPException(status_code=500, detail="bot_state is missing")

        active = await session.scalar(
            select(func.count(GridOrder.id)).where(
                GridOrder.symbol == state.symbol,
                GridOrder.status.in_(["New", "PartiallyFilled", "Untriggered", "Created"]),
            )
        )
        result = await session.execute(
            select(GridOrder)
            .where(GridOrder.symbol == state.symbol)
            .order_by(GridOrder.id.desc())
            .limit(20)
        )
        orders = list(result.scalars())
        return {
            "enabled": state.enabled,
            "symbol": state.symbol,
            "levels": state.levels,
            "step_pct": str(state.step_pct),
            "quote_per_level": str(state.quote_per_level),
            "anchor_price": str(state.anchor_price) if state.anchor_price else None,
            "active_orders": active or 0,
            "recent_orders": [
                {
                    "id": o.id,
                    "exchange_order_id": o.exchange_order_id,
                    "side": o.side,
                    "price": str(o.price),
                    "qty": str(o.qty),
                    "status": o.status,
                    "replacement_created": o.replacement_created,
                }
                for o in orders
            ],
        }
