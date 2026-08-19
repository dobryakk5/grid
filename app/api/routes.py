from datetime import datetime, timezone
from decimal import Decimal
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from app.db.models import (
    GridOrder,
    GridProfile,
    GridRange,
    MarketCandle,
    PositionLot,
    BreakdownEpisode,
    RecoveryTrade,
    StrategyRecommendation,
)
from app.db.session import SessionLocal
from app.exchanges.bybit import BybitClient, BybitError
from app.trading.grid import GridEngine, OPEN_STATUSES
from app.trading.events import record_strategy_event
from app.trading.recommendations import (
    accept_recommendation,
    claim_recommendation,
    list_recommendations,
    reject_recommendation,
)
from app.trading.backtest import run_grid_backtest
from app.trading.grid_analysis import analyze_grid
from app.trading.pnl import grid_cell_statistics
from app.trading.math import configured_grid_cells, strategy_grid_cells
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


class ProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=32)
    lower_price: Decimal = Field(gt=0)
    upper_price: Decimal = Field(gt=0)
    step_price: Decimal = Field(gt=0)
    quote_per_level: Decimal = Field(gt=0)
    break_down_action: Literal["continue", "stop", "trailing_buy", "recommend"] = "continue"
    breakout_confirm_bars: int = Field(default=2, ge=2, le=12)
    breakout_ema_period: int = Field(default=50, ge=5, le=300)
    trailing_buy_deviation_mode: Literal["fixed"] = "fixed"
    trailing_buy_deviation_pct: Decimal = Field(default=Decimal("2"), gt=0, le=20)
    trailing_buy_target_quote: Decimal = Field(default=Decimal("250"), gt=0)
    trailing_buy_max_attempts: int = Field(default=2, ge=1, le=10)
    trailing_buy_timeout_hours: int = Field(default=168, ge=1, le=24 * 30)
    recovery_initial_stop_pct: Decimal = Field(default=Decimal("2.5"), gt=0, le=30)
    recovery_trailing_activation_pct: Decimal = Field(default=Decimal("3"), gt=0, le=100)
    recovery_trailing_pct: Decimal = Field(default=Decimal("1.5"), gt=0, le=30)
    recovery_break_even_trigger_pct: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    recovery_cooldown_bars: int = Field(default=4, ge=1, le=168)
    break_up_action: Literal["continue", "stop"] = "stop"
    below_grid_lower_price: Decimal | None = Field(default=None, gt=0)
    buy_below_grid: bool = True
    sell_below_grid: bool = False
    strategy: Literal["accumulation", "classic", "dca"] = "accumulation"
    grid_mode: Literal["arithmetic", "geometric"] = "arithmetic"
    step_percent: Decimal | None = Field(default=None, gt=0, le=100)
    max_investment: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    initial_buy_percent: Decimal = Field(default=Decimal("20"), gt=0, lt=50)
    buy_ladder_mode: Literal["linear", "geometric"] = "linear"
    sell_ladder_mode: Literal["linear", "geometric"] = "linear"
    ladder_multiplier: Decimal = Field(default=Decimal("1.5"), gt=1, le=10)

    @model_validator(mode="after")
    def validate_range(self):
        cells = strategy_grid_cells(
            self.lower_price, self.upper_price, self.step_price,
            mode=self.grid_mode, step_percent=self.step_percent,
        )
        if (
            self.below_grid_lower_price is not None
            and self.below_grid_lower_price >= self.lower_price
        ):
            raise ValueError("below_grid_lower_price must be below lower_price")
        if self.buy_below_grid and self.below_grid_lower_price is not None:
            cells = strategy_grid_cells(
                self.below_grid_lower_price,
                self.lower_price,
                self.step_price,
                mode="arithmetic",
            ) + cells
        required = self.quote_per_level * len(cells)
        if self.strategy == "dca" and self.max_investment is None:
            raise ValueError("max_investment is required for DCA Grid")
        if self.strategy != "dca" and self.max_investment is not None and self.max_investment < required:
            raise ValueError(f"max_investment must be at least {required} USDT")
        if self.stop_loss is not None and self.stop_loss >= self.lower_price:
            raise ValueError("stop_loss must be below lower_price")
        if self.take_profit is not None and self.take_profit <= self.upper_price:
            raise ValueError("take_profit must be above upper_price")
        if self.recovery_trailing_activation_pct < self.recovery_break_even_trigger_pct:
            raise ValueError("recovery_trailing_activation_pct must not be below break-even trigger")
        return self


class DemoFundsRequest(BaseModel):
    usdt: Decimal = Field(default=Decimal("10000"), gt=0, le=Decimal("100000"))


class ProfileNamePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class BacktestPayload(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=32)
    lower_price: Decimal = Field(gt=0)
    upper_price: Decimal = Field(gt=0)
    steps: list[Decimal] = Field(default_factory=lambda: [
        Decimal("250"), Decimal("500"), Decimal("1000"), Decimal("1500")
    ], min_length=1, max_length=12)
    quote_per_level: Decimal = Field(default=Decimal("100"), gt=0)
    fee_rate: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.02"))
    days: int = Field(default=30, ge=2, le=365)
    below_grid_lower_price: Decimal | None = Field(default=None, gt=0)
    buy_below_grid: bool = True
    sell_below_grid: bool = False
    break_down_action: Literal["continue", "stop", "trailing_buy", "recommend"] = "continue"
    break_up_action: Literal["continue", "stop"] = "stop"

    @model_validator(mode="after")
    def validate_backtest(self):
        if self.upper_price <= self.lower_price:
            raise ValueError("upper_price must be greater than lower_price")
        if (
            self.below_grid_lower_price is not None
            and self.below_grid_lower_price >= self.lower_price
        ):
            raise ValueError("below_grid_lower_price must be below lower_price")
        for step in self.steps:
            strategy_grid_cells(self.lower_price, self.upper_price, step)
            if self.buy_below_grid and self.below_grid_lower_price is not None:
                strategy_grid_cells(
                    self.below_grid_lower_price, self.lower_price, step,
                    mode="arithmetic",
                )
        return self


class GridAnalysisPayload(BaseModel):
    symbol: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9]+$")
    profile_id: int | None = Field(default=None, ge=1)


async def create_current_range(session, profile: GridProfile, *, reason: str) -> GridRange:
    grid_range = GridRange(
        profile_id=profile.id,
        lower_price=profile.lower_price,
        upper_price=profile.upper_price,
        step_price=profile.step_price,
        grid_mode=profile.grid_mode,
        step_percent=profile.step_percent,
        status="ACTIVE",
    )
    session.add(grid_range)
    await session.flush()
    profile.current_range_id = grid_range.id
    record_strategy_event(
        session, profile_id=profile.id, event_type="GRID_RANGE_CREATED",
        to_state="ACTIVE", reason=reason, metadata={"range_id": grid_range.id},
    )
    record_strategy_event(
        session, profile_id=profile.id, event_type="GRID_RANGE_ACTIVATED",
        to_state="ACTIVE", reason=reason, metadata={"range_id": grid_range.id},
    )
    return grid_range


def range_dict(grid_range: GridRange | None) -> dict | None:
    if grid_range is None:
        return None
    return {
        "id": grid_range.id,
        "lower_price": str(grid_range.lower_price),
        "upper_price": str(grid_range.upper_price),
        "step_price": str(grid_range.step_price),
        "grid_mode": grid_range.grid_mode,
        "step_percent": str(grid_range.step_percent) if grid_range.step_percent is not None else None,
        "status": grid_range.status,
    }


def profile_dict(profile: GridProfile, *, current_range: GridRange | None = None, active_orders: int = 0, filled_buys: int = 0, filled_sells: int = 0) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "enabled": profile.enabled,
        "symbol": profile.symbol,
        "lower_price": str(profile.lower_price),
        "upper_price": str(profile.upper_price),
        "step_price": str(profile.step_price),
        "quote_per_level": str(profile.quote_per_level),
        "regime_state": getattr(profile, "regime_state", "RANGE"),
        "break_down_action": getattr(profile, "break_down_action", "continue"),
        "breakout_confirm_bars": profile.breakout_confirm_bars,
        "breakout_ema_period": profile.breakout_ema_period,
        "trailing_buy_deviation_mode": profile.trailing_buy_deviation_mode,
        "trailing_buy_deviation_pct": str(profile.trailing_buy_deviation_pct),
        "trailing_buy_target_quote": str(profile.trailing_buy_target_quote),
        "trailing_buy_max_attempts": profile.trailing_buy_max_attempts,
        "trailing_buy_timeout_hours": profile.trailing_buy_timeout_hours,
        "recovery_initial_stop_pct": str(profile.recovery_initial_stop_pct),
        "recovery_trailing_activation_pct": str(profile.recovery_trailing_activation_pct),
        "recovery_trailing_pct": str(profile.recovery_trailing_pct),
        "recovery_break_even_trigger_pct": str(profile.recovery_break_even_trigger_pct),
        "recovery_cooldown_bars": profile.recovery_cooldown_bars,
        "break_up_action": getattr(profile, "break_up_action", "stop"),
        "below_grid_lower_price": (
            str(profile.below_grid_lower_price)
            if getattr(profile, "below_grid_lower_price", None) is not None else None
        ),
        "buy_below_grid": getattr(profile, "buy_below_grid", True),
        "sell_below_grid": getattr(profile, "sell_below_grid", False),
        "strategy": profile.strategy,
        "grid_mode": profile.grid_mode,
        "step_percent": str(profile.step_percent) if profile.step_percent is not None else None,
        "max_investment": str(profile.max_investment) if profile.max_investment is not None else None,
        "stop_loss": str(profile.stop_loss) if profile.stop_loss is not None else None,
        "take_profit": str(profile.take_profit) if profile.take_profit is not None else None,
        "initial_buy_percent": str(profile.initial_buy_percent),
        "buy_ladder_mode": profile.buy_ladder_mode,
        "sell_ladder_mode": profile.sell_ladder_mode,
        "ladder_multiplier": str(profile.ladder_multiplier),
        "current_range_id": profile.current_range_id,
        "current_range": range_dict(current_range),
        "lines": [str(x) for x in (
            [buy for buy, _ in configured_grid_cells(profile)]
            + [configured_grid_cells(profile)[-1][1]]
        )],
        "active_orders": active_orders,
        "filled_buys": filled_buys,
        "filled_sells": filled_sells,
    }


async def profile_stats(session, profile: GridProfile) -> dict:
    current_range = (
        await session.get(GridRange, profile.current_range_id)
        if profile.current_range_id is not None else None
    )
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
        current_range=current_range,
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


@router.get("/market-data/{symbol}/range")
async def cached_market_range(symbol: str, days: int = 30) -> dict:
    if not 2 <= days <= 365:
        raise HTTPException(status_code=422, detail="days must be between 2 and 365")
    normalized_symbol = symbol.upper()
    expected_candles = days * 24
    async with SessionLocal() as session:
        result = await session.execute(
            select(MarketCandle)
            .where(
                MarketCandle.symbol == normalized_symbol,
                MarketCandle.interval == "60",
            )
            .order_by(MarketCandle.timestamp_ms.desc())
            .limit(expected_candles)
        )
        candles = result.scalars().all()
    if len(candles) < expected_candles:
        raise HTTPException(
            status_code=409,
            detail=(
                f"not enough cached candles for {normalized_symbol}: "
                f"found {len(candles)}, need {expected_candles}; "
                "run the market-data collector first"
            ),
        )
    return {
        "symbol": normalized_symbol,
        "days": days,
        "candles": len(candles),
        "min_price": str(min(item.low for item in candles)),
        "max_price": str(max(item.high for item in candles)),
        "data_through_ms": max(item.timestamp_ms for item in candles),
        "source": "database",
    }


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
            regime_state="RANGE",
            break_down_action=payload.break_down_action,
            breakout_confirm_bars=payload.breakout_confirm_bars,
            breakout_ema_period=payload.breakout_ema_period,
            trailing_buy_deviation_mode=payload.trailing_buy_deviation_mode,
            trailing_buy_deviation_pct=payload.trailing_buy_deviation_pct,
            trailing_buy_target_quote=payload.trailing_buy_target_quote,
            trailing_buy_max_attempts=payload.trailing_buy_max_attempts,
            trailing_buy_timeout_hours=payload.trailing_buy_timeout_hours,
            recovery_initial_stop_pct=payload.recovery_initial_stop_pct,
            recovery_trailing_activation_pct=payload.recovery_trailing_activation_pct,
            recovery_trailing_pct=payload.recovery_trailing_pct,
            recovery_break_even_trigger_pct=payload.recovery_break_even_trigger_pct,
            recovery_cooldown_bars=payload.recovery_cooldown_bars,
            break_up_action=payload.break_up_action,
            below_grid_lower_price=payload.below_grid_lower_price,
            buy_below_grid=payload.buy_below_grid,
            sell_below_grid=payload.sell_below_grid,
            strategy=payload.strategy,
            grid_mode=payload.grid_mode,
            step_percent=payload.step_percent,
            max_investment=payload.max_investment,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
            initial_buy_percent=payload.initial_buy_percent,
            buy_ladder_mode=payload.buy_ladder_mode,
            sell_ladder_mode=payload.sell_ladder_mode,
            ladder_multiplier=payload.ladder_multiplier,
        )
        session.add(profile)
        await session.flush()
        await create_current_range(session, profile, reason="PROFILE_CREATED")
        await session.commit()
        await session.refresh(profile)
        return await profile_stats(session, profile)


@router.patch("/profiles/{profile_id}/name")
async def rename_profile(profile_id: int, payload: ProfileNamePayload) -> dict:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="profile name cannot be empty")
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        profile.name = name
        await session.commit()
        return {"ok": True, "id": profile.id, "name": profile.name}


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
            latest_by_cell[
                (
                    Decimal(order.grid_buy_price),
                    order.side,
                    Decimal(order.price),
                    order.order_role,
                )
            ] = order
        inventory_cells = [
            order for order in latest_by_cell.values()
            if (order.side == "Sell" and order.status != "Filled")
            or (
                order.side == "Buy" and order.status == "Filled"
                and (
                    not order.replacement_created
                    or order.order_role == "below_accumulation"
                )
            )
        ]
        if inventory_cells:
            raise HTTPException(
                status_code=409,
                detail="profile may still hold base asset from completed BUYs; restart the old profile and let its SELL orders resolve before editing",
            )

        open_lots = await session.scalar(
            select(func.count(PositionLot.id)).where(
                PositionLot.profile_id == profile.id,
                PositionLot.remaining_qty > 0,
            )
        )
        if open_lots:
            raise HTTPException(
                status_code=409,
                detail="profile has open PositionLots; resolve inventory before changing settings",
            )

        range_changed = (
            Decimal(profile.lower_price) != payload.lower_price
            or Decimal(profile.upper_price) != payload.upper_price
            or Decimal(profile.step_price) != payload.step_price
            or profile.grid_mode != payload.grid_mode
            or (Decimal(profile.step_percent) if profile.step_percent is not None else None)
            != payload.step_percent
        )

        profile.name = payload.name.strip()
        profile.symbol = payload.symbol.upper()
        profile.lower_price = payload.lower_price
        profile.upper_price = payload.upper_price
        profile.step_price = payload.step_price
        profile.quote_per_level = payload.quote_per_level
        profile.regime_state = "RANGE"
        profile.break_down_action = payload.break_down_action
        profile.breakout_confirm_bars = payload.breakout_confirm_bars
        profile.breakout_ema_period = payload.breakout_ema_period
        profile.trailing_buy_deviation_mode = payload.trailing_buy_deviation_mode
        profile.trailing_buy_deviation_pct = payload.trailing_buy_deviation_pct
        profile.trailing_buy_target_quote = payload.trailing_buy_target_quote
        profile.trailing_buy_max_attempts = payload.trailing_buy_max_attempts
        profile.trailing_buy_timeout_hours = payload.trailing_buy_timeout_hours
        profile.recovery_initial_stop_pct = payload.recovery_initial_stop_pct
        profile.recovery_trailing_activation_pct = payload.recovery_trailing_activation_pct
        profile.recovery_trailing_pct = payload.recovery_trailing_pct
        profile.recovery_break_even_trigger_pct = payload.recovery_break_even_trigger_pct
        profile.recovery_cooldown_bars = payload.recovery_cooldown_bars
        profile.break_up_action = payload.break_up_action
        profile.below_grid_lower_price = payload.below_grid_lower_price
        profile.buy_below_grid = payload.buy_below_grid
        profile.sell_below_grid = payload.sell_below_grid
        profile.strategy = payload.strategy
        profile.grid_mode = payload.grid_mode
        profile.step_percent = payload.step_percent
        profile.max_investment = payload.max_investment
        profile.stop_loss = payload.stop_loss
        profile.take_profit = payload.take_profit
        profile.initial_buy_percent = payload.initial_buy_percent
        profile.buy_ladder_mode = payload.buy_ladder_mode
        profile.sell_ladder_mode = payload.sell_ladder_mode
        profile.ladder_multiplier = payload.ladder_multiplier
        if range_changed:
            previous = (
                await session.get(GridRange, profile.current_range_id)
                if profile.current_range_id is not None else None
            )
            if previous is not None:
                previous.status = "CLOSED"
                previous.close_reason = "MANUAL_REGRID"
                previous.ended_at = datetime.now(timezone.utc)
            await create_current_range(session, profile, reason="MANUAL_REGRID")
        await session.commit()
        return await profile_stats(session, profile)


@router.post("/profiles/{profile_id}/start")
async def start_profile(profile_id: int) -> dict:
    # Bybit Demo is the project's paper-trading environment. No live endpoint
    # is supported by this MVP.
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
        if profile.current_range_id is None:
            await create_current_range(session, profile, reason="START_BOOTSTRAP")
        if profile.strategy == "dca":
            has_initial = await session.scalar(
                select(func.count(GridOrder.id)).where(
                    GridOrder.profile_id == profile.id,
                    GridOrder.range_id == profile.current_range_id,
                    GridOrder.order_role == "dca_initial_buy",
                )
            )
            if not has_initial:
                price_client = BybitClient()
                try:
                    current = await price_client.last_price(profile.symbol)
                except BybitError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                finally:
                    await price_client.close()
                if not Decimal(profile.lower_price) < current < Decimal(profile.upper_price):
                    raise HTTPException(
                        status_code=409,
                        detail="DCA Grid можно запустить только когда цена находится внутри диапазона",
                    )
        profile.enabled = True
        known_states = {
            "RANGE", "BREAK_UP", "BREAK_DOWN", "TRAILING_BUY", "RECOVERY_ENTERING",
            "RECOVERY_LONG", "RECOVERY_EXITING", "RECOVERY_COOLDOWN", "WAIT_MANUAL",
            "WAIT_RANGE", "RECOMMENDATION_PENDING", "STOPPED",
        }
        if profile.regime_state == "STOPPED" or profile.regime_state not in known_states:
            profile.regime_state = "RANGE"
        await session.commit()
        return {"ok": True, "enabled": True, "regime_state": profile.regime_state}


@router.post("/backtest")
async def backtest(payload: BacktestPayload) -> dict:
    symbol = payload.symbol.upper()
    async with SessionLocal() as session:
        result = await session.execute(
            select(MarketCandle)
            .where(
                MarketCandle.symbol == symbol,
                MarketCandle.interval == "60",
            )
            .order_by(MarketCandle.timestamp_ms.desc())
            .limit(payload.days * 24)
        )
        candles = list(reversed(result.scalars().all()))
    expected_candles = payload.days * 24
    if len(candles) < expected_candles:
        raise HTTPException(
            status_code=409,
            detail=(
                f"not enough cached candles for {symbol}: "
                f"found {len(candles)}, need {expected_candles}; "
                "run the market-data collector first"
            ),
        )
    closes = [item.close for item in candles]
    timestamps = [item.timestamp_ms for item in candles]
    results = [
        run_grid_backtest(
            closes,
            lower=payload.lower_price,
            upper=payload.upper_price,
            step=step,
            quote_per_level=payload.quote_per_level,
            fee_rate=payload.fee_rate,
            below_grid_lower_price=payload.below_grid_lower_price,
            buy_below_grid=payload.buy_below_grid,
            sell_below_grid=payload.sell_below_grid,
            timestamps_ms=timestamps,
            candle_minutes=60,
            break_down_action=payload.break_down_action,
            break_up_action=payload.break_up_action,
        )
        for step in payload.steps
    ]
    return {
        "symbol": symbol,
        "interval": "1h closes",
        "source": "database",
        "candles": len(candles),
        "data_from_ms": timestamps[0],
        "data_through_ms": timestamps[-1],
        "assumption": "fills are counted only when consecutive hourly closes cross a level",
        "results": results,
    }


@router.post("/grid-analysis")
async def grid_analysis(payload: GridAnalysisPayload) -> dict:
    symbol = payload.symbol.upper()
    logger.info("GRID_ANALYSIS_STARTED symbol=%s profile_id=%s", symbol, payload.profile_id)
    try:
        async with SessionLocal() as session:
            profile = await session.get(GridProfile, payload.profile_id) if payload.profile_id else None
            if payload.profile_id and profile is None:
                raise HTTPException(status_code=404, detail="profile not found")
            if profile is not None and profile.symbol.upper() != symbol:
                raise HTTPException(status_code=409, detail="profile symbol does not match analysis symbol")
            result = await session.execute(
                select(MarketCandle)
                .where(MarketCandle.symbol == symbol, MarketCandle.interval == "60")
                .order_by(MarketCandle.timestamp_ms.desc())
                .limit(90 * 24)
            )
            candles = list(reversed(result.scalars().all()))
        if len(candles) < 90 * 24:
            raise HTTPException(
                status_code=409,
                detail=f"not enough cached candles for {symbol}: found {len(candles)}, need {90 * 24}; run the market-data collector first",
            )
        logger.info("MARKET_REGIME_CALCULATED symbol=%s", symbol)
        analysis = analyze_grid(
            candles,
            quote_per_level=Decimal(profile.quote_per_level) if profile is not None else None,
            capital_limit=(Decimal(profile.max_investment) if profile is not None and profile.max_investment is not None else None),
        )
        logger.info("GRID_CANDIDATES_GENERATED symbol=%s count=%d", symbol, analysis["candidate_counts"]["generated"])
        for item in analysis["rejected_candidates"]:
            logger.info("GRID_CANDIDATE_REJECTED symbol=%s range=%s step=%s reason=%s", symbol, item["range_type"], item["step_pct"], item["reason"])
        logger.info("TRAIN_BACKTEST_COMPLETED symbol=%s", symbol)
        logger.info("TEST_BACKTEST_COMPLETED symbol=%s", symbol)
        logger.info("GRID_ANALYSIS_COMPLETED symbol=%s candidates=%d", symbol, len(analysis["candidates"]))
        return {"symbol": symbol, **analysis}
    except HTTPException:
        logger.exception("GRID_ANALYSIS_FAILED symbol=%s", symbol)
        raise
    except ValueError as exc:
        logger.exception("GRID_ANALYSIS_FAILED symbol=%s", symbol)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("GRID_ANALYSIS_FAILED symbol=%s", symbol)
        raise


@router.post("/profiles/{profile_id}/stop")
async def stop_profile(profile_id: int) -> dict:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        active_recovery = await session.scalar(
            select(RecoveryTrade.id)
            .join(BreakdownEpisode)
            .where(
                BreakdownEpisode.profile_id == profile_id,
                RecoveryTrade.status.in_({"ENTERING", "OPEN", "EXITING"}),
            )
            .limit(1)
        )
        if active_recovery is not None:
            raise HTTPException(
                status_code=409,
                detail="Close Recovery first via POST /api/profiles/{id}/recovery/close",
            )
        profile.enabled = False
        await session.commit()
        return {"ok": True, "enabled": False, "note": "worker cancels open orders on next tick"}


@router.post("/profiles/{profile_id}/recovery/close")
async def close_recovery(profile_id: int) -> dict:
    exchange = BybitClient()
    try:
        async with SessionLocal() as session:
            profile = await session.get(GridProfile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="profile not found")
            trade = await session.scalar(
                select(RecoveryTrade)
                .join(BreakdownEpisode)
                .where(
                    BreakdownEpisode.profile_id == profile_id,
                    RecoveryTrade.status.in_({"ENTERING", "OPEN", "EXITING"}),
                )
                .order_by(RecoveryTrade.id.desc()).limit(1)
            )
            if trade is None:
                raise HTTPException(status_code=409, detail="no active recovery trade")
            if trade.status == "ENTERING":
                raise HTTPException(status_code=409, detail="recovery entry is still pending")
            if trade.status == "EXITING":
                return {"ok": True, "status": "RECOVERY_EXITING"}
            episode = await session.get(BreakdownEpisode, trade.breakdown_episode_id)
            market_price = await exchange.last_price(profile.symbol)
            await GridEngine(exchange)._begin_recovery_exit(
                session, profile, episode, trade, "MANUAL_CLOSE", market_price
            )
            return {"ok": True, "status": "RECOVERY_EXITING"}
    except BybitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await exchange.close()


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
                "range_id": order.range_id,
                "side": order.side,
                "grid_buy_price": str(order.grid_buy_price),
                "price": str(order.price),
                "qty": str(order.qty),
                "status": order.status,
                "avg_price": str(order.avg_price) if order.avg_price else None,
                "order_role": order.order_role,
                "filled_qty": str(order.filled_qty) if order.filled_qty else None,
                "created_at": order.created_at.isoformat() if order.created_at else None,
                "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            }
            for order in result.scalars()
        ]


@router.post("/profiles/{profile_id}/orders/{order_id}/cancel")
async def cancel_profile_order(profile_id: int, order_id: int) -> dict:
    async with SessionLocal() as session:
        order = await session.get(GridOrder, order_id)
        if order is None or order.profile_id != profile_id:
            raise HTTPException(status_code=404, detail="order not found")
        if order.status not in {"New", "Untriggered", "Created"}:
            raise HTTPException(
                status_code=409,
                detail="Можно отменить только активную неисполненную заявку",
            )
        exchange = BybitClient()
        try:
            await exchange.cancel_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
        except BybitError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await exchange.close()
        order.status = "CancelRequestedByUser"
        await session.commit()
        return {"ok": True, "status": order.status}


@router.get("/profiles/{profile_id}/pnl")
async def profile_pnl(profile_id: int) -> dict:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")

        result = await session.execute(
            select(GridOrder)
            .options(selectinload(GridOrder.executions))
            .where(GridOrder.profile_id == profile_id)
            .order_by(GridOrder.id)
        )
        orders = list(result.scalars())

    exchange = BybitClient()
    try:
        info = await exchange.instrument_info(profile.symbol)
        market_price = await exchange.last_price(profile.symbol)
    except BybitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await exchange.close()

    stats = grid_cell_statistics(
        profile,
        orders,
        base_coin=info.base_coin,
        quote_coin=info.quote_coin,
        tick_size=info.tick_size,
    )
    inventory = Decimal("0")
    cash_flow = Decimal("0")
    for order in orders:
        for execution in order.executions:
            qty = Decimal(execution.exec_qty)
            value = Decimal(execution.exec_value)
            fee = Decimal(execution.exec_fee or 0)
            currency = (execution.fee_currency or "").upper()
            if order.side == "Buy":
                inventory += qty
                cash_flow -= value
            else:
                inventory -= qty
                cash_flow += value
            if currency == info.base_coin.upper():
                inventory -= fee
            elif currency == info.quote_coin.upper():
                cash_flow -= fee
    total_pnl = cash_flow + inventory * market_price
    realized = Decimal(stats["total"]["net_profit"])
    stats["total"].update({
        "realized_pnl": str(realized),
        "unrealized_pnl": str(total_pnl - realized),
        "total_pnl": str(total_pnl),
        "base_inventory": str(inventory),
        "inventory_value": str(inventory * market_price),
        "market_price": str(market_price),
    })
    async with SessionLocal() as session:
        lot_inventory = await session.scalar(
            select(func.coalesce(func.sum(PositionLot.remaining_qty), 0)).where(
                PositionLot.profile_id == profile_id,
                PositionLot.status == "OPEN",
            )
        )
    lot_inventory = Decimal(lot_inventory or 0)
    stats["lot_reconciliation"] = {
        "base_inventory_legacy": str(inventory),
        "base_inventory_lots": str(lot_inventory),
        "difference": str(inventory - lot_inventory),
    }
    return stats


def recommendation_dict(item: StrategyRecommendation) -> dict:
    return {
        "id": item.id,
        "profile_id": item.profile_id,
        "type": item.type,
        "status": item.status,
        "payload": item.payload,
        "market_price": str(item.market_price) if item.market_price is not None else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "resolved_at": item.resolved_at.isoformat() if item.resolved_at else None,
    }


@router.get("/profiles/{profile_id}/diagnostics")
async def profile_diagnostics(profile_id: int) -> dict:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        open_orders = await session.scalar(select(func.count(GridOrder.id)).where(
            GridOrder.profile_id == profile_id, GridOrder.status.in_(OPEN_STATUSES),
        ))
        open_lots = await session.scalar(select(func.count(PositionLot.id)).where(
            PositionLot.profile_id == profile_id, PositionLot.remaining_qty > 0,
        ))
        lot_inventory = await session.scalar(select(func.coalesce(func.sum(PositionLot.remaining_qty), 0)).where(
            PositionLot.profile_id == profile_id, PositionLot.remaining_qty > 0,
        ))
        pending = await session.scalar(select(func.count(StrategyRecommendation.id)).where(
            StrategyRecommendation.profile_id == profile_id,
            StrategyRecommendation.status == "PENDING",
        ))
        recovery = await session.scalar(
            select(RecoveryTrade)
            .join(BreakdownEpisode)
            .where(
                BreakdownEpisode.profile_id == profile_id,
                RecoveryTrade.status.in_({"TRACKING", "TRIGGERED", "ENTERING", "OPEN", "EXITING"}),
            )
            .order_by(RecoveryTrade.id.desc())
            .limit(1)
        )
        orders = list((await session.execute(
            select(GridOrder).options(selectinload(GridOrder.executions)).where(
                GridOrder.profile_id == profile_id,
            )
        )).scalars())
        legacy_inventory = Decimal("0")
        for order in orders:
            for execution in order.executions:
                qty = Decimal(execution.exec_qty)
                fee = Decimal(execution.exec_fee or 0)
                if order.side == "Buy":
                    legacy_inventory += qty
                else:
                    legacy_inventory -= qty
                # The exchange uses the base coin as fee currency for the common
                # spot pairs supported by this MVP.
                if (execution.fee_currency or "").upper() == profile.symbol[:-4].upper():
                    legacy_inventory -= fee
        lot_inventory = Decimal(lot_inventory or 0)
        current_range = await session.get(GridRange, profile.current_range_id) if profile.current_range_id else None
        return {
            "current_range": range_dict(current_range),
            "open_orders": open_orders or 0,
            "open_lots": open_lots or 0,
            "legacy_inventory": str(legacy_inventory),
            "lot_inventory": str(lot_inventory),
            "inventory_difference": str(legacy_inventory - lot_inventory),
            "pending_recommendations": pending or 0,
            "recovery": (
                {
                    "id": recovery.id,
                    "status": recovery.status,
                    "attempt": recovery.attempt_number,
                    "lowest_price": str(recovery.lowest_price),
                    "trigger_price": str(recovery.trigger_price),
                    "effective_stop_price": str(recovery.effective_stop_price) if recovery.effective_stop_price is not None else None,
                }
                if recovery is not None else None
            ),
        }


@router.get("/profiles/{profile_id}/recommendations")
async def profile_recommendations(profile_id: int) -> list[dict]:
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        items = await list_recommendations(session, profile_id)
        if profile.regime_state == "RECOMMENDATION_PENDING" and not any(
            item.status == "PENDING" and item.type == "START_TRAILING_BUY" for item in items
        ):
            profile.regime_state = "WAIT_MANUAL"
            record_strategy_event(
                session, profile_id=profile.id, event_type="RECOMMENDATION_EXPIRED",
                from_state="RECOMMENDATION_PENDING", to_state="WAIT_MANUAL",
            )
        await session.commit()
        return [recommendation_dict(item) for item in items]


@router.post("/recommendations/{recommendation_id}/accept")
async def accept_profile_recommendation(recommendation_id: int) -> dict:
    exchange = BybitClient()
    async with SessionLocal() as session:
        recommendation = await claim_recommendation(session, recommendation_id)
        if recommendation is None:
            await exchange.close()
            raise HTTPException(status_code=409, detail="recommendation is no longer pending or has expired")
        try:
            if recommendation.type == "START_TRAILING_BUY":
                profile = await session.get(GridProfile, recommendation.profile_id)
                if profile is None:
                    raise HTTPException(status_code=404, detail="profile not found")
                source_range_id = recommendation.payload.get("source_range_id")
                grid_range = (
                    await session.get(GridRange, profile.current_range_id)
                    if profile.current_range_id is not None else None
                )
                active_recovery = await session.scalar(
                    select(RecoveryTrade.id)
                    .join(BreakdownEpisode)
                    .where(
                        BreakdownEpisode.profile_id == profile.id,
                        RecoveryTrade.status.in_({"TRACKING", "TRIGGERED", "ENTERING", "OPEN", "EXITING"}),
                    )
                    .limit(1)
                )
                if (
                    str(source_range_id) != str(profile.current_range_id)
                    or grid_range is None
                    or grid_range.status != "PAUSED"
                    or profile.regime_state != "RECOMMENDATION_PENDING"
                    or active_recovery is not None
                ):
                    raise HTTPException(status_code=409, detail="recommendation no longer matches the current paused range")
                await GridEngine(exchange).start_trailing_buy(session, profile)
            await accept_recommendation(session, recommendation)
            await session.commit()
            return recommendation_dict(recommendation)
        except Exception as exc:
            recommendation.status = "FAILED"
            recommendation.resolved_at = datetime.now(timezone.utc)
            failed_profile = await session.get(GridProfile, recommendation.profile_id)
            if (
                failed_profile is not None
                and recommendation.type == "START_TRAILING_BUY"
                and failed_profile.regime_state == "RECOMMENDATION_PENDING"
            ):
                failed_profile.regime_state = "WAIT_MANUAL"
                record_strategy_event(
                    session, profile_id=failed_profile.id, event_type="RECOMMENDATION_REJECTED",
                    from_state="RECOMMENDATION_PENDING", to_state="WAIT_MANUAL",
                    reason="ACCEPT_VALIDATION_FAILED",
                    metadata={"recommendation_id": recommendation.id},
                )
            await session.commit()
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await exchange.close()


@router.post("/recommendations/{recommendation_id}/reject")
async def reject_profile_recommendation(recommendation_id: int) -> dict:
    async with SessionLocal() as session:
        recommendation = await reject_recommendation(session, recommendation_id)
        if recommendation is None:
            raise HTTPException(status_code=409, detail="recommendation is no longer pending or has expired")
        profile = await session.get(GridProfile, recommendation.profile_id)
        if (
            profile is not None
            and recommendation.type == "START_TRAILING_BUY"
            and profile.regime_state == "RECOMMENDATION_PENDING"
        ):
            profile.regime_state = "WAIT_MANUAL"
            record_strategy_event(
                session, profile_id=profile.id, event_type="RECOMMENDATION_REJECTED",
                from_state="RECOMMENDATION_PENDING", to_state="WAIT_MANUAL",
                metadata={"recommendation_id": recommendation.id},
            )
        await session.commit()
        return recommendation_dict(recommendation)


@router.post("/recommendations/{recommendation_id}/continue-grid")
async def continue_grid_recommendation(recommendation_id: int) -> dict:
    async with SessionLocal() as session:
        recommendation = await reject_recommendation(session, recommendation_id)
        if recommendation is None or recommendation.type != "START_TRAILING_BUY":
            raise HTTPException(status_code=409, detail="trailing-buy recommendation is no longer pending")
        profile = await session.get(GridProfile, recommendation.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        grid_range = await session.get(GridRange, profile.current_range_id) if profile.current_range_id else None
        if grid_range is not None:
            grid_range.status = "ACTIVE"
        profile.break_down_action = "continue"
        profile.regime_state = "RANGE"
        record_strategy_event(
            session, profile_id=profile.id, event_type="GRID_RANGE_ACTIVATED",
            from_state="PAUSED", to_state="ACTIVE", reason="RECOMMENDATION_CONTINUE_GRID",
            metadata={"recommendation_id": recommendation.id, "range_id": profile.current_range_id},
        )
        await session.commit()
        return {"ok": True, "enabled": profile.enabled, "regime_state": profile.regime_state}


@router.post("/recommendations/{recommendation_id}/stop")
async def stop_recommendation(recommendation_id: int) -> dict:
    async with SessionLocal() as session:
        recommendation = await reject_recommendation(session, recommendation_id)
        if recommendation is None or recommendation.type != "START_TRAILING_BUY":
            raise HTTPException(status_code=409, detail="trailing-buy recommendation is no longer pending")
        profile = await session.get(GridProfile, recommendation.profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        profile.enabled = False
        await session.commit()
        return {"ok": True, "enabled": False, "note": "worker cancels open orders on next tick"}
