from decimal import Decimal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from app.db.models import GridOrder, GridProfile
from app.db.session import SessionLocal
from app.exchanges.bybit import BybitClient, BybitError
from app.trading.grid import OPEN_STATUSES
from app.trading.math import grid_buy_levels, grid_lines

router = APIRouter(prefix="/api")


class ProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=32)
    lower_price: Decimal = Field(gt=0)
    upper_price: Decimal = Field(gt=0)
    step_price: Decimal = Field(gt=0)
    quote_per_level: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        grid_buy_levels(self.lower_price, self.upper_price, self.step_price)
        return self


class DemoFundsRequest(BaseModel):
    usdt: Decimal = Field(default=Decimal("10000"), gt=0, le=Decimal("100000"))


def profile_dict(profile: GridProfile, *, active_orders: int = 0, filled_buys: int = 0, filled_sells: int = 0) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "enabled": profile.enabled,
        "symbol": profile.symbol,
        "lower_price": str(profile.lower_price),
        "upper_price": str(profile.upper_price),
        "step_price": str(profile.step_price),
        "quote_per_level": str(profile.quote_per_level),
        "lines": [str(x) for x in grid_lines(
            Decimal(profile.lower_price), Decimal(profile.upper_price), Decimal(profile.step_price)
        )],
        "active_orders": active_orders,
        "filled_buys": filled_buys,
        "filled_sells": filled_sells,
    }


async def profile_stats(session, profile: GridProfile) -> dict:
    active = await session.scalar(
        select(func.count(GridOrder.id)).where(
            GridOrder.profile_id == profile.id,
            GridOrder.status.in_(OPEN_STATUSES),
        )
    )
    buys = await session.scalar(
        select(func.count(GridOrder.id)).where(
            GridOrder.profile_id == profile.id,
            GridOrder.side == "Buy",
            GridOrder.status == "Filled",
        )
    )
    sells = await session.scalar(
        select(func.count(GridOrder.id)).where(
            GridOrder.profile_id == profile.id,
            GridOrder.side == "Sell",
            GridOrder.status == "Filled",
        )
    )
    return profile_dict(
        profile,
        active_orders=active or 0,
        filled_buys=buys or 0,
        filled_sells=sells or 0,
    )


@router.get("/price/{symbol}")
async def price(symbol: str) -> dict:
    exchange = BybitClient()
    try:
        last = await exchange.last_price(symbol.upper())
        return {"symbol": symbol.upper(), "last_price": str(last)}
    finally:
        await exchange.close()


@router.get("/bybit/status")
async def bybit_status() -> dict:
    exchange = BybitClient()
    try:
        data = await exchange.api_key_info()
        result = data["result"]
        spot_permissions = result.get("permissions", {}).get("Spot", [])
        api_key = result.get("apiKey", "")
        return {
            "connected": True,
            "api_key": f"{api_key[:4]}…{api_key[-4:]}" if len(api_key) >= 8 else "configured",
            "read_only": result.get("readOnly") == 1,
            "spot_trade": "SpotTrade" in spot_permissions,
            "ips": result.get("ips", []),
            "uta": result.get("uta"),
            "note": result.get("note", ""),
        }
    except Exception as exc:
        # Deliberately return a normal JSON status so the dashboard can show the error.
        return {"connected": False, "error": str(exc)}
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


@router.get("/profiles")
async def list_profiles() -> list[dict]:
    async with SessionLocal() as session:
        result = await session.execute(select(GridProfile).order_by(GridProfile.id.desc()))
        return [await profile_stats(session, profile) for profile in result.scalars()]


@router.post("/profiles", status_code=201)
async def create_profile(payload: ProfilePayload) -> dict:
    async with SessionLocal() as session:
        profile = GridProfile(
            name=payload.name.strip(),
            enabled=False,
            symbol=payload.symbol.upper(),
            lower_price=payload.lower_price,
            upper_price=payload.upper_price,
            step_price=payload.step_price,
            quote_per_level=payload.quote_per_level,
        )
        session.add(profile)
        await session.commit()
        await session.refresh(profile)
        return await profile_stats(session, profile)


@router.put("/profiles/{profile_id}")
async def update_profile(profile_id: int, payload: ProfilePayload) -> dict:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        if profile.enabled:
            raise HTTPException(status_code=409, detail="stop profile before editing")

        active = await session.scalar(
            select(func.count(GridOrder.id)).where(
                GridOrder.profile_id == profile.id,
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        if active:
            raise HTTPException(status_code=409, detail="profile still has open orders; wait for worker to cancel them")

        history = await session.execute(
            select(GridOrder)
            .where(GridOrder.profile_id == profile.id)
            .order_by(GridOrder.id)
        )
        latest_by_cell = {}
        for order in history.scalars():
            latest_by_cell[Decimal(order.grid_buy_price)] = order
        inventory_cells = [
            order for order in latest_by_cell.values()
            if (order.side == "Sell" and order.status != "Filled")
            or (order.side == "Buy" and order.status == "Filled" and not order.replacement_created)
        ]
        if inventory_cells:
            raise HTTPException(
                status_code=409,
                detail="profile may still hold base asset from completed BUYs; restart the old profile and let its SELL orders resolve before editing",
            )

        profile.name = payload.name.strip()
        profile.symbol = payload.symbol.upper()
        profile.lower_price = payload.lower_price
        profile.upper_price = payload.upper_price
        profile.step_price = payload.step_price
        profile.quote_per_level = payload.quote_per_level
        await session.commit()
        return await profile_stats(session, profile)


@router.post("/profiles/{profile_id}/start")
async def start_profile(profile_id: int) -> dict:
    # Fail before enabling the profile if the configured key cannot trade Spot.
    exchange = BybitClient()
    try:
        info = await exchange.api_key_info()
        result = info["result"]
        if result.get("readOnly") == 1:
            raise HTTPException(status_code=409, detail="Bybit API key is read-only")
        if "SpotTrade" not in result.get("permissions", {}).get("Spot", []):
            raise HTTPException(status_code=409, detail="Bybit API key has no SpotTrade permission")
    except BybitError as exc:
        raise HTTPException(status_code=400, detail=f"Bybit authentication failed: {exc}") from exc
    finally:
        await exchange.close()

    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        profile.enabled = True
        await session.commit()
        return {"ok": True, "enabled": True}


@router.post("/profiles/{profile_id}/stop")
async def stop_profile(profile_id: int) -> dict:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        profile.enabled = False
        await session.commit()
        return {"ok": True, "enabled": False, "note": "worker cancels open orders on next tick"}


@router.get("/profiles/{profile_id}/orders")
async def profile_orders(profile_id: int, limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 500))
    async with SessionLocal() as session:
        if await session.get(GridProfile, profile_id) is None:
            raise HTTPException(status_code=404, detail="profile not found")
        result = await session.execute(
            select(GridOrder)
            .where(GridOrder.profile_id == profile_id)
            .order_by(GridOrder.id.desc())
            .limit(limit)
        )
        return [
            {
                "id": order.id,
                "side": order.side,
                "grid_buy_price": str(order.grid_buy_price),
                "price": str(order.price),
                "qty": str(order.qty),
                "status": order.status,
                "avg_price": str(order.avg_price) if order.avg_price else None,
                "filled_qty": str(order.filled_qty) if order.filled_qty else None,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            }
            for order in result.scalars()
        ]
