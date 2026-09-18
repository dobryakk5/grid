from datetime import datetime, timezone
from uuid import uuid4
from decimal import Decimal
import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, func, or_, select, update as sa_update

from app.core.auth import require_trading
from app.core.config import settings
from app.db.models import (
    ChainScanCursor,
    DexIntent,
    ChainSwap,
    FomoTrader,
    FomoTraderRank,
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
from app.exchanges import (
    SUPPORTED_EXCHANGES,
    BybitClient,
    ExchangeError,
    make_exchange,
    split_symbol,
)
from app.dex.dexscreener import DexScreenerClient, DexScreenerError
from app.dex.risk import RiskLimits, evaluate
from app.dex.tokens import (
    DexConfigError,
    dynamic_token_by_address,
    list_pairs,
    resolve_pair,
    resolve_token,
)
from app.exchanges.robinhood import RobinhoodClient
from app.dex.dynamic_tokens import load_dynamic_tokens
from app.dex.repository import DexIntentRepository
from app.fomo.client import FomoAuthError, FomoClient, FomoError, FomoRateLimited
from app.fomo.identity import candidate_claims, exclusive_addresses, wallets_per_entry
from app.fomo.session import (
    clear_session as clear_fomo_session,
    current_jwt as current_fomo_jwt,
    session_status as fomo_session_status,
    set_session as set_fomo_session,
)
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
from app.trading.math import (
    configured_grid_cells,
    grid_exposure,
    level_size_weights,
    strategy_grid_cells,
)
from sqlalchemy.orm import selectinload

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


class ProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    exchange: Literal["bybit", "mexc", "robinhood"] = "bybit"
    symbol: str = Field(default="BTCUSDT", min_length=3, max_length=32)
    lower_price: Decimal = Field(gt=0)
    upper_price: Decimal = Field(gt=0)
    step_price: Decimal = Field(gt=0)
    quote_per_level: Decimal = Field(gt=0)
    # 1 = every level the same size; above 1 turns the grid into a martingale
    # around the middle of the corridor (app/trading/math.level_size_weights).
    level_size_multiplier: Decimal = Field(default=Decimal("1"), ge=1, le=5)
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
        if self.exchange == "robinhood":
            # The venue has no market orders yet (app/exchanges/robinhood.py
            # place_market_order raises), and those paths -- classic's initial
            # buy, DCA, recovery -- would only fail at runtime, mid-cycle.
            if self.strategy != "accumulation":
                raise ValueError("robinhood supports only the accumulation strategy")
            if self.break_down_action == "trailing_buy":
                raise ValueError(
                    "robinhood does not support break_down_action=trailing_buy "
                    "(trailing buy places a market order)"
                )
            try:
                resolve_pair(self.symbol)
            except DexConfigError as exc:
                raise ValueError(str(exc)) from exc
            if self.quote_per_level < settings.dex_min_order_quote:
                raise ValueError(
                    f"quote_per_level must be at least {settings.dex_min_order_quote} "
                    "(DEX_MIN_ORDER_QUOTE) on robinhood"
                )
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
        # What the grid costs to hold, not what one level costs: with a
        # multiplier the edges are several times the middle cell, so the flat
        # product understates the funding requirement badly enough to strand
        # the ladder halfway down.
        required = grid_exposure(
            self.quote_per_level, len(cells), self.level_size_multiplier
        )
        if self.level_size_multiplier > 1:
            if self.buy_below_grid and self.below_grid_lower_price is not None:
                raise ValueError(
                    "level_size_multiplier and buying below the grid cannot be "
                    "combined: the martingale is measured from the middle of "
                    "the corridor, and an open-ended extension below it has no "
                    "middle to measure from"
                )
            if self.max_investment is None:
                raise ValueError(
                    "max_investment is required when level_size_multiplier is "
                    f"above 1: this grid commits {required} at full exposure"
                )
        if self.strategy == "dca" and self.max_investment is None:
            raise ValueError("max_investment is required for DCA Grid")
        if self.strategy != "dca" and self.max_investment is not None and self.max_investment < required:
            raise ValueError(
                f"max_investment must be at least {required} "
                f"(full exposure of {len(cells)} levels)"
            )
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
    level_size_multiplier: Decimal = Field(default=Decimal("1"), ge=1, le=5)
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


def profile_level_quotes(profile: GridProfile) -> list[Decimal]:
    """The per-cell BUY sizes this profile will actually place."""
    cells = configured_grid_cells(profile)
    base = Decimal(profile.quote_per_level)
    multiplier = Decimal(getattr(profile, "level_size_multiplier", 1) or 1)
    return [base * weight for weight in level_size_weights(len(cells), multiplier)]


def profile_exposure(profile: GridProfile) -> Decimal:
    """Quote committed with every cell long at once."""
    return sum(profile_level_quotes(profile), Decimal("0"))


def profile_dict(profile: GridProfile, *, current_range: GridRange | None = None, active_orders: int = 0, filled_buys: int = 0, filled_sells: int = 0) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "enabled": profile.enabled,
        "exchange": getattr(profile, "exchange", "bybit"),
        "symbol": profile.symbol,
        "lower_price": str(profile.lower_price),
        "upper_price": str(profile.upper_price),
        "step_price": str(profile.step_price),
        "quote_per_level": str(profile.quote_per_level),
        "level_size_multiplier": str(getattr(profile, "level_size_multiplier", 1) or 1),
        "exposure_quote": str(profile_exposure(profile)),
        "level_quotes": [str(quote) for quote in profile_level_quotes(profile)],
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
async def price(symbol: str, exchange: str = "bybit") -> dict:
    venue = exchange.strip().lower()
    if venue not in SUPPORTED_EXCHANGES:
        raise HTTPException(status_code=422, detail=f"unknown exchange {venue!r}")
    client = make_exchange(venue)
    try:
        last = await client.last_price(symbol.upper())
        return {"symbol": symbol.upper(), "exchange": venue, "last_price": str(last)}
    except ExchangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await client.close()


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


@router.get("/dex/pairs")
async def dex_pairs() -> dict:
    client = RobinhoodClient()
    try:
        info = (await client.api_key_info())["result"]
        return {"pairs": list(list_pairs()), **info}
    finally:
        await client.close()


@router.get("/dex/{symbol}/snapshot")
async def dex_snapshot(symbol: str) -> dict:
    """Pool price plus the health metrics a level would be risk-checked against."""
    client = RobinhoodClient()
    try:
        snapshot = await client.market_snapshot(symbol.upper())
        verdict = await client.risk_verdict(symbol.upper())
    except (DexConfigError, DexScreenerError, ExchangeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await client.close()
    return {
        "symbol": snapshot.symbol,
        "observed_at_ms": snapshot.observed_at_ms,
        "price_quote": str(snapshot.price_quote),
        "price_usd": str(snapshot.price_usd),
        "pair_address": snapshot.pair_address,
        "pair_liquidity_usd": str(snapshot.pair_liquidity_usd),
        "token_liquidity_usd": str(snapshot.token_liquidity_usd),
        "token_volume_h24_usd": str(snapshot.token_volume_h24),
        "pools_considered": snapshot.pools_considered,
        "risk": verdict.as_dict(),
        # An indicative pool mid, not a fillable price -- execution decisions
        # need a Uniswap quote for the actual size.
        "price_is_indicative": True,
    }


@router.get("/balance")
async def balance() -> dict:
    exchange = BybitClient()
    try:
        return await exchange.wallet_balance()
    except ExchangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await exchange.close()


@router.post("/demo/funds")
async def demo_funds(payload: DemoFundsRequest) -> dict:
    exchange = BybitClient()
    try:
        return await exchange.apply_demo_usdt(payload.usdt)
    except ExchangeError as exc:
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
            exchange=payload.exchange,
            symbol=payload.symbol.upper(),
            lower_price=payload.lower_price,
            upper_price=payload.upper_price,
            step_price=payload.step_price,
            quote_per_level=payload.quote_per_level,
            level_size_multiplier=payload.level_size_multiplier,
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
        profile.exchange = payload.exchange
        profile.symbol = payload.symbol.upper()
        profile.lower_price = payload.lower_price
        profile.upper_price = payload.upper_price
        profile.step_price = payload.step_price
        profile.quote_per_level = payload.quote_per_level
        profile.level_size_multiplier = payload.level_size_multiplier
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
    async with SessionLocal() as session:
        profile = await session.get(GridProfile, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        venue = getattr(profile, "exchange", "bybit")

    # Verify the venue's API key can place spot orders before enabling the
    # profile. On Bybit this is the demo account; MEXC is live.
    exchange = make_exchange(venue)
    try:
        info = await exchange.api_key_info()
        result = info["result"]
        if result.get("readOnly") == 1:
            raise HTTPException(status_code=409, detail=f"{venue} API key is read-only")
        if "SpotTrade" not in result.get("permissions", {}).get("Spot", []):
            raise HTTPException(status_code=409, detail=f"{venue} API key has no spot trading permission")
    except ExchangeError as exc:
        raise HTTPException(status_code=400, detail=f"{venue} authentication failed: {exc}") from exc
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
                price_client = make_exchange(getattr(profile, "exchange", "bybit"))
                try:
                    current = await price_client.last_price(profile.symbol)
                except ExchangeError as exc:
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
            level_size_multiplier=payload.level_size_multiplier,
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
            level_size_multiplier=(
                Decimal(getattr(profile, "level_size_multiplier", 1) or 1)
                if profile is not None else Decimal("1")
            ),
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
    exchange = None
    try:
        async with SessionLocal() as session:
            profile = await session.get(GridProfile, profile_id)
            if profile is None:
                raise HTTPException(status_code=404, detail="profile not found")
            exchange = make_exchange(getattr(profile, "exchange", "bybit"))
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
    except ExchangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if exchange is not None:
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
        order_profile = await session.get(GridProfile, profile_id)
        exchange = make_exchange(getattr(order_profile, "exchange", "bybit"))
        try:
            await exchange.cancel_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
        except ExchangeError as exc:
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

    exchange = make_exchange(getattr(profile, "exchange", "bybit"))
    try:
        info = await exchange.instrument_info(profile.symbol)
        market_price = await exchange.last_price(profile.symbol)
    except ExchangeError as exc:
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
                if (execution.fee_currency or "").upper() == split_symbol(profile.symbol)[0]:
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
    exchange = None
    async with SessionLocal() as session:
        recommendation = await claim_recommendation(session, recommendation_id)
        if recommendation is None:
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
                exchange = make_exchange(getattr(profile, "exchange", "bybit"))
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
            if exchange is not None:
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


# ---- FOMO smart-money page --------------------------------------------
#
# Everything below reads this app's own database -- the trader registry
# (`app.workers.fomo_registry`) and the on-chain trade tape
# (`app.workers.chain_tape`) -- rather than proxying FOMO live. The one
# exception is `/fomo/raw`, a development-only passthrough for inspecting the
# upstream schema, gated by `settings.fomo_debug_api`.

_FOMO_RAW_ENDPOINTS = {"leaderboard", "balances", "trades", "trade", "holders", "user"}


class FomoSessionPayload(BaseModel):
    jwt: str = Field(min_length=10)


def _resolve_fomo_token_address(token: str) -> str:
    text = token.strip()
    if text.lower().startswith("0x"):
        return text.lower()
    try:
        return resolve_token(text.upper()).address.lower()
    except DexConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/fomo/session")
async def set_fomo_session_endpoint(payload: FomoSessionPayload) -> dict:
    """Accept a pasted JWT; it is never returned to the browser again."""
    set_fomo_session(payload.jwt)
    return fomo_session_status()


@router.get("/fomo/session")
async def get_fomo_session_endpoint() -> dict:
    return fomo_session_status()


@router.delete("/fomo/session")
async def delete_fomo_session_endpoint() -> dict:
    clear_fomo_session()
    return fomo_session_status()


@router.get("/fomo/status")
async def fomo_status() -> dict:
    # Never raises -- the page needs a status even when the DB query itself
    # or the session is in a bad state, same convention as /api/bybit/status.
    result: dict = {**fomo_session_status()}
    try:
        async with SessionLocal() as session:
            newest_rank = await session.scalar(select(func.max(FomoTraderRank.captured_at)))
            traders_known = await session.scalar(select(func.count(FomoTrader.fomo_user_id)))
            traders_with_wallet = await session.scalar(
                select(func.count(FomoTrader.fomo_user_id)).where(FomoTrader.evm_address.is_not(None))
            )
            pending_backfill = await session.scalar(
                select(func.count(FomoTrader.fomo_user_id)).where(
                    FomoTrader.backfilled_from_block.is_(None),
                    FomoTrader.evm_address.is_not(None),
                )
            )
            # The only naming figure worth showing: how many of the wallets we
            # actually see trading carry a name. Counting registry rows instead
            # reads as success while the column on every table stays empty --
            # FOMO's own evmAddress never appears on this chain, so most rows
            # are named identities that trade nowhere we can see.
            trading = select(func.distinct(func.lower(ChainSwap.wallet_address))).subquery()
            trading_wallets = await session.scalar(select(func.count()).select_from(trading))
            named_trading = await session.scalar(
                select(func.count(func.distinct(func.lower(FomoTrader.evm_address)))).where(
                    func.lower(FomoTrader.evm_address).in_(select(trading.c[0])),
                    or_(FomoTrader.user_handle.is_not(None), FomoTrader.display_name.is_not(None)),
                )
            )
            cursor = await session.get(ChainScanCursor, (settings.rh_chain_id, "realtime"))
        result.update({
            "trading_wallets": trading_wallets or 0,
            "named_trading_wallets": named_trading or 0,
            "registry_last_updated": newest_rank.isoformat() if newest_rank else None,
            "traders_known": traders_known or 0,
            "traders_with_wallet": traders_with_wallet or 0,
            "wallets_pending_backfill": pending_backfill or 0,
            "chain_scan_last_block": cursor.last_block if cursor else None,
            "chain_id": settings.rh_chain_id,
            "chain_name": settings.rh_chain_name,
        })
    except Exception as exc:
        result["db_error"] = str(exc)
    return result


async def _identities(session, wallets: list[str]) -> dict[str, dict]:
    """``{lowercase wallet: identity}`` for whoever we can put a name to."""
    if not wallets:
        return {}
    latest_rank = (
        select(FomoTraderRank.fomo_user_id, FomoTraderRank.rank)
        .distinct(FomoTraderRank.fomo_user_id)
        .order_by(FomoTraderRank.fomo_user_id, FomoTraderRank.captured_at.desc())
        .subquery()
    )
    rows = (await session.execute(
        select(
            FomoTrader.fomo_user_id,
            FomoTrader.evm_address,
            FomoTrader.user_handle,
            FomoTrader.display_name,
            latest_rank.c.rank,
        )
        .outerjoin(latest_rank, latest_rank.c.fomo_user_id == FomoTrader.fomo_user_id)
        .where(func.lower(FomoTrader.evm_address).in_({wallet.lower() for wallet in wallets}))
    )).all()
    return {
        row.evm_address.lower(): {
            "fomo_user_id": row.fomo_user_id,
            "handle": row.user_handle,
            "display_name": row.display_name,
            "rank": row.rank,
        }
        for row in rows
    }


def _window_start_ms(hours: int) -> tuple[int, int]:
    hours = max(1, min(hours, 24 * 30))
    return hours, int(datetime.now(timezone.utc).timestamp() * 1000) - hours * 3_600_000


class FomoNamePayload(BaseModel):
    """Attach a human name to a wallet, from wherever the name came from."""

    wallet_address: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    handle: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)


@router.post("/fomo/names")
async def set_fomo_name(payload: FomoNamePayload) -> dict:
    """Record a wallet -> name match so it survives and shows everywhere.

    Names normally arrive from FOMO's registry, but that needs a live
    session. This lets a name be set from any other source (a profile page
    read by hand, a spreadsheet, an explorer label) and stored against the
    same ``fomo_traders`` row, so the tables stop showing a bare address
    without anything having to be re-derived later.
    """
    address = payload.wallet_address
    handle = (payload.handle or "").strip() or None
    display_name = (payload.display_name or "").strip() or None
    if handle is None and display_name is None:
        raise HTTPException(status_code=422, detail="handle or display_name is required")

    async with SessionLocal() as session:
        updated = await session.execute(
            sa_update(FomoTrader)
            .where(func.lower(FomoTrader.evm_address) == address.lower())
            .values(user_handle=handle, display_name=display_name)
        )
        if updated.rowcount == 0:
            # Naming a wallet we have not seen trade yet is legitimate --
            # keep it, and the tape will fill in its trades once it is
            # backfilled like any other newly known wallet.
            session.add(FomoTrader(
                fomo_user_id=f"manual:{address.lower()}",
                evm_address=address,
                user_handle=handle,
                display_name=display_name,
                source="manual",
            ))
        await session.commit()
    return {"wallet_address": address, "handle": handle, "display_name": display_name}


class FomoNamesBulkPayload(BaseModel):
    names: list[FomoNamePayload] = Field(min_length=1, max_length=1000)


@router.post("/fomo/names/bulk")
async def set_fomo_names_bulk(payload: FomoNamesBulkPayload) -> dict:
    """Import many wallet -> name matches at once.

    Fed by names collected in the browser, where a FOMO session actually
    works: server-side calls to their API are refused (HTTP 430) even with a
    valid-looking token, so the page that is already logged in is the one
    place the mapping can be read from. Once imported it lives in
    ``fomo_traders`` like any other identity and needs no session again.
    """
    applied, created = 0, 0
    async with SessionLocal() as session:
        for entry in payload.names:
            handle = (entry.handle or "").strip() or None
            display_name = (entry.display_name or "").strip() or None
            if handle is None and display_name is None:
                continue
            address = entry.wallet_address
            result = await session.execute(
                sa_update(FomoTrader)
                .where(func.lower(FomoTrader.evm_address) == address.lower())
                .values(user_handle=handle, display_name=display_name)
            )
            if result.rowcount == 0:
                session.add(FomoTrader(
                    fomo_user_id=f"manual:{address.lower()}",
                    evm_address=address,
                    user_handle=handle,
                    display_name=display_name,
                    source="manual",
                ))
                created += 1
            applied += 1
        await session.commit()

        named = await session.scalar(
            select(func.count(FomoTrader.fomo_user_id)).where(FomoTrader.user_handle.is_not(None))
        )
        total = await session.scalar(select(func.count(FomoTrader.fomo_user_id)))
    return {"applied": applied, "created": created, "named": named or 0, "tracked": total or 0}



class FomoCandidatePayload(BaseModel):
    """One FOMO identity plus every address seen anywhere in its trade JSON."""

    handle: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=120)
    candidates: list[str] = Field(default_factory=list, max_length=200)


class FomoCandidatesBulkPayload(BaseModel):
    entries: list[FomoCandidatePayload] = Field(min_length=1, max_length=500)


@router.post("/fomo/names/candidates")
async def resolve_fomo_names_from_candidates(payload: FomoCandidatesBulkPayload) -> dict:
    """Name a wallet by intersecting FOMO's addresses with wallets that trade.

    The address FOMO publishes as a user's ``evmAddress`` turns out never to
    appear on this chain at all -- not one of the 313 imported ones shows up
    in a single ``Transfer`` -- so naming by that field can only ever produce
    an empty column. A trade record, though, mentions several addresses
    (payer, receiver, router, pool) without saying which is the trader.

    So the caller sends every address it saw and this decides, using the one
    thing it knows independently: which addresses actually trade. Two filters
    make that safe -- an address must appear in ``chain_swaps``, and it must
    be claimed by exactly one identity, which drops the routers and pools
    that necessarily show up in everybody's trades.
    """
    entries = payload.entries
    claims = candidate_claims([entry.candidates for entry in entries])
    exclusive = exclusive_addresses(claims)
    shared = len(claims) - len(exclusive)

    named = matched = ambiguous = 0
    async with SessionLocal() as session:
        trading: set[str] = set()
        if exclusive:
            rows = await session.execute(
                select(func.distinct(func.lower(ChainSwap.wallet_address)))
                .where(func.lower(ChainSwap.wallet_address).in_(exclusive))
            )
            trading = {row[0] for row in rows}

        for entry, hits in zip(entries, wallets_per_entry(claims, trading, len(entries))):
            if not hits:
                continue
            matched += 1
            if len(hits) > 1:
                # Two trading wallets under one name is not a naming failure,
                # but guessing which one is meant would be.
                ambiguous += 1
                continue
            handle = (entry.handle or "").strip() or None
            display_name = (entry.display_name or "").strip() or None
            if handle is None and display_name is None:
                continue
            result = await session.execute(
                sa_update(FomoTrader)
                .where(func.lower(FomoTrader.evm_address) == hits[0])
                .values(user_handle=handle, display_name=display_name)
            )
            if result.rowcount == 0:
                session.add(FomoTrader(
                    fomo_user_id=f"manual:{hits[0]}",
                    evm_address=hits[0],
                    user_handle=handle,
                    display_name=display_name,
                    source="manual",
                ))
            named += 1
        await session.commit()

    return {
        "entries": len(payload.entries),
        "addresses": len(claims),
        "shared_addresses": shared,
        "matched": matched,
        "ambiguous": ambiguous,
        "named": named,
        "unmatched": len(payload.entries) - matched,
    }


@router.get("/fomo/coins")
async def fomo_coins(hours: int = 24, limit: int = 50) -> dict:
    """Which coins the tracked wallets are buying, biggest buy volume first."""
    hours, since_ms = _window_start_ms(hours)
    limit = max(1, min(limit, 200))

    # Unpriced trades carry a NULL value_usd; fold them to zero so a coin
    # with only unpriced activity sums to 0 rather than NULL -- otherwise it
    # sorts ahead of every real volume, since Postgres orders NULLs first.
    bought = func.coalesce(
        func.sum(case((ChainSwap.side == "BUY", func.coalesce(ChainSwap.value_usd, 0)), else_=0)), 0
    )
    sold = func.coalesce(
        func.sum(case((ChainSwap.side == "SELL", func.coalesce(ChainSwap.value_usd, 0)), else_=0)), 0
    )
    net_qty = func.sum(case((ChainSwap.side == "BUY", ChainSwap.token_amount), else_=-ChainSwap.token_amount))

    async with SessionLocal() as session:
        rows = (await session.execute(
            select(
                ChainSwap.token_address,
                func.max(ChainSwap.symbol).label("symbol"),
                bought.label("bought_usd"),
                sold.label("sold_usd"),
                net_qty.label("net_qty"),
                func.count().filter(ChainSwap.side == "BUY").label("buys"),
                func.count().filter(ChainSwap.side == "SELL").label("sells"),
                func.count(func.distinct(ChainSwap.wallet_address)).filter(ChainSwap.side == "BUY").label("buyers"),
                func.count(func.distinct(ChainSwap.wallet_address)).filter(ChainSwap.side == "SELL").label("sellers"),
                func.count().filter(ChainSwap.pricing_source == "UNPRICED").label("unpriced"),
                func.max(ChainSwap.block_time_ms).label("last_trade_ms"),
            )
            .where(ChainSwap.block_time_ms >= since_ms)
            .group_by(ChainSwap.token_address)
            .order_by(bought.desc(), func.count().desc())
            .limit(limit)
        )).all()

    coins = []
    for row in rows:
        bought_usd = Decimal(row.bought_usd or 0)
        sold_usd = Decimal(row.sold_usd or 0)
        coins.append({
            "token_address": row.token_address,
            "symbol": row.symbol,
            "bought_usd": str(bought_usd),
            "sold_usd": str(sold_usd),
            "net_flow_usd": str(bought_usd - sold_usd),
            "net_token_amount": str(row.net_qty),
            "buy_count": row.buys,
            "sell_count": row.sells,
            "buyers": row.buyers,
            "sellers": row.sellers,
            "unpriced_trades": row.unpriced,
            "last_trade_at_ms": row.last_trade_ms,
        })
    return {"hours": hours, "since_ms": since_ms, "coins": coins}


@router.get("/fomo/coins/{token_address}")
async def fomo_coin_detail(token_address: str, hours: int = 24) -> dict:
    """Who bought this coin and who sold it, with how much of each."""
    hours, since_ms = _window_start_ms(hours)
    address = _resolve_fomo_token_address(token_address)

    async with SessionLocal() as session:
        rows = (await session.execute(
            select(
                ChainSwap.wallet_address,
                ChainSwap.side,
                func.max(ChainSwap.symbol).label("symbol"),
                func.coalesce(func.sum(ChainSwap.value_usd), 0).label("usd"),
                func.sum(ChainSwap.token_amount).label("qty"),
                func.count().label("trades"),
                func.count().filter(ChainSwap.pricing_source == "UNPRICED").label("unpriced"),
                func.max(ChainSwap.block_time_ms).label("last_trade_ms"),
            )
            .where(func.lower(ChainSwap.token_address) == address.lower(), ChainSwap.block_time_ms >= since_ms)
            .group_by(ChainSwap.wallet_address, ChainSwap.side)
            .order_by(func.coalesce(func.sum(ChainSwap.value_usd), 0).desc())
        )).all()
        identities = await _identities(session, [row.wallet_address for row in rows])

    symbol = next((row.symbol for row in rows if row.symbol), None)
    buyers, sellers = [], []
    bought_total = sold_total = Decimal("0")
    for row in rows:
        identity = identities.get(row.wallet_address.lower(), {})
        entry = {
            "wallet_address": row.wallet_address,
            "fomo_user_id": identity.get("fomo_user_id"),
            "handle": identity.get("handle"),
            "display_name": identity.get("display_name"),
            "rank": identity.get("rank"),
            "usd": str(Decimal(row.usd or 0)),
            "token_amount": str(row.qty),
            "trades": row.trades,
            "unpriced_trades": row.unpriced,
            "last_trade_at_ms": row.last_trade_ms,
        }
        if row.side == "BUY":
            buyers.append(entry)
            bought_total += Decimal(row.usd or 0)
        else:
            sellers.append(entry)
            sold_total += Decimal(row.usd or 0)

    # Chain-verified tokens count as tradable, so the registry has to be
    # loaded before asking what pairs exist for this coin.
    await load_dynamic_tokens(SessionLocal)
    if symbol is None:
        # No trades recorded yet is not the same as an unknown coin: the tape
        # read its name off the contract the first time it saw it.
        meta = dynamic_token_by_address(address)
        if meta is not None:
            symbol = meta.symbol.split("-")[0]
    tradable = _tradable_symbols(address)
    async with SessionLocal() as session:
        levels = list((await session.execute(
            select(DexIntent)
            .where(DexIntent.symbol.in_(tradable), DexIntent.profile_id.is_(None))
            .where(DexIntent.status.notin_(("FILLED", "CANCELLED", "EXPIRED", "FAILED")))
            .order_by(DexIntent.id.desc())
            .limit(20)
        )).scalars()) if tradable else []

    return {
        "token_address": address,
        "symbol": symbol,
        "hours": hours,
        "since_ms": since_ms,
        "bought_usd": str(bought_total),
        "sold_usd": str(sold_total),
        "net_flow_usd": str(bought_total - sold_total),
        "buyers": buyers,
        "sellers": sellers,
        "trading": {
            # Empty when the coin is observed but has no market we can price.
            "symbols": tradable,
            # Gate verdicts up front: an armed level on a pair that fails
            # these sits BLOCKED instead of filling, and that is worth
            # knowing before placing it, not after.
            "pairs": await _pair_health(tradable),
            "dry_run": settings.dex_dry_run,
            "min_order_quote": str(settings.dex_min_order_quote),
            "open_levels": [
                {
                    "intent_id": level.id,
                    "symbol": level.symbol,
                    "side": level.side,
                    "status": level.status,
                    "limit_price": str(level.limit_price),
                    "amount_in": str(level.amount_in),
                    "amount_in_coin": level.amount_in_coin,
                    "blocked_reason": level.blocked_reason,
                }
                for level in levels
            ],
        },
    }


def _dynamic_candidates(token_address: str) -> list[str]:
    """Pair names for a discovered token, keyed by its address.

    Looked up by address rather than by symbol: this chain has several
    contracts per popular name, so a name alone does not say which one.
    """
    meta = dynamic_token_by_address(token_address)
    if meta is None:
        return []
    return [f"{meta.symbol}{quote}" for quote in ("USDG", "ETH")]


async def _pair_health(symbols: list[str]) -> list[dict]:
    """Risk verdict per pair, evaluated now rather than at execution.

    The gates are what decide whether an armed level ever fills, so finding
    out after placing one is finding out too late. One DexScreener client is
    shared across the symbols: its TTL cache is keyed by token address, so
    the USDG and ETH pairs of the same coin cost a single upstream call.
    """
    if not symbols:
        return []
    market = DexScreenerClient()
    limits = RiskLimits.from_settings()
    health = []
    try:
        for symbol in symbols:
            entry = {
                "symbol": symbol,
                "min_liquidity_usd": str(limits.min_liquidity_usd),
                "min_volume_h24_usd": str(limits.min_volume_h24_usd),
            }
            try:
                snapshot = await market.snapshot(resolve_pair(symbol))
            except (DexConfigError, DexScreenerError) as exc:
                # No pool for this quote, or the token is unknown upstream --
                # not a risk verdict, an absence of a market.
                health.append({**entry, "ok": False, "reasons": [str(exc)], "tradable": False})
                continue
            verdict = evaluate(snapshot, limits=limits)
            health.append({
                **entry,
                "tradable": True,
                "ok": verdict.ok,
                "reasons": list(verdict.reasons),
                "price_quote": str(snapshot.price_quote),
                "price_usd": str(snapshot.price_usd),
                "liquidity_usd": str(snapshot.token_liquidity_usd),
                "volume_h24_usd": str(snapshot.token_volume_h24),
                "pool_liquidity_usd": str(snapshot.pair_liquidity_usd),
                "pools_considered": snapshot.pools_considered,
            })
    finally:
        await market.close()
    return health


def _tradable_symbols(token_address: str) -> list[str]:
    """Pairs whose base token is this coin, hand-pinned or chain-verified.

    A coin only needs its ``decimals`` known for certain; the registry in
    ``app.dex.tokens`` pins some by hand, and the tape verifies the rest by
    calling the token's own ``decimals()``. Both are safe to price an order
    with -- so both are offered. Whether a trade can actually route and pass
    the risk gates is decided at execution, not hidden behind a whitelist.
    """
    wanted = token_address.lower()
    symbols: list[str] = []
    for symbol in list_pairs():
        try:
            pair = resolve_pair(symbol)
        except DexConfigError:
            continue
        if pair.base.address.lower() == wanted and symbol not in symbols:
            symbols.append(symbol)
    # A hand-pinned pair wins: same token, but with a tick size chosen for its
    # price range instead of the generic one a discovered pair gets.
    if symbols:
        return symbols
    for symbol in _dynamic_candidates(wanted):
        try:
            pair = resolve_pair(symbol)
        except DexConfigError:
            continue
        if pair.base.address.lower() == wanted and symbol not in symbols:
            symbols.append(symbol)
    return symbols


class LimitOrderPayload(BaseModel):
    symbol: str
    side: Literal["Buy", "Sell"] = "Buy"
    limit_price: Decimal = Field(gt=0)
    # What you hand over: quote coin for a buy, base coin for a sell. Naming
    # it "amount" rather than "amount_quote" keeps that honest.
    amount: Decimal = Field(gt=0)
    # Only meaningful for a buy; a sale is never gated on liquidity anyway.
    ignore_liquidity: bool = False


@router.post("/fomo/limit-order", dependencies=[Depends(require_trading)])
async def fomo_limit_order(payload: LimitOrderPayload) -> dict:
    """Arm a limit buy for a coin, as a synthetic level the DEX worker runs.

    This does not touch the chain: it writes a ``dex_intents`` row in
    ``WAITING``, exactly like a grid level, and ``app.workers.dex`` is what
    watches the price and executes it -- under the same risk gates, the same
    slippage cap and the same ``DEX_DRY_RUN`` switch. The row carries no
    ``profile_id``, which is what distinguishes a hand-placed level from one
    a grid produced.
    """
    symbol = payload.symbol.strip().upper()
    # Tokens the tape verified on chain are tradable too, not just the ones
    # pinned in app/dex/tokens.py.
    await load_dynamic_tokens(SessionLocal)
    try:
        pair = resolve_pair(symbol)
    except DexConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A sell hands over base tokens, a buy hands over quote.
    buying = payload.side == "Buy"
    amount_in_coin = pair.quote_coin if buying else pair.base_coin
    # The floor applies to opening a position, never to closing one: a holding
    # too small to be worth its gas is still a holding, and refusing to sell it
    # strands it for good. Buying into that same size is a choice, and the floor
    # is what stops it.
    if buying and payload.amount < settings.dex_min_order_quote:
        raise HTTPException(
            status_code=422,
            detail=(
                f"minimum order is {settings.dex_min_order_quote} {pair.quote_coin}; "
                f"this is worth about {payload.amount} {pair.quote_coin}"
            ),
        )

    async with SessionLocal() as session:
        repository = DexIntentRepository(session)
        intent = await repository.create_level(
            symbol=symbol,
            side=payload.side,
            limit_price=payload.limit_price,
            amount_in=payload.amount,
            amount_in_coin=amount_in_coin,
            order_link_id=str(uuid4()),
            ignore_liquidity_gate=payload.ignore_liquidity,
        )
        await session.commit()
        intent_id, link_id = intent.id, intent.order_link_id

    logger.info(
        "manual limit %s armed: %s %s %s at %s (intent %s)",
        payload.side, payload.amount, amount_in_coin, symbol, payload.limit_price, intent_id,
    )
    return {
        "intent_id": intent_id,
        "order_link_id": link_id,
        "symbol": symbol,
        "side": payload.side,
        "limit_price": str(payload.limit_price),
        "amount_in": str(payload.amount),
        "amount_in_coin": amount_in_coin,
        "ignore_liquidity": payload.ignore_liquidity,
        "status": "WAITING",
        "dry_run": settings.dex_dry_run,
        "note": (
            "DEX_DRY_RUN is on: the level is watched and quoted but nothing is signed"
            if settings.dex_dry_run
            else "DEX_DRY_RUN is off: the DEX worker will sign and broadcast when the price is met"
        ),
    }


class LimitOrderEdit(BaseModel):
    """A new price and/or size for a level that has not started executing.

    Deliberately cannot change ``symbol`` or ``side``: that is not an edit of
    this order but a different order, and pretending otherwise would keep the
    original's id, baseline snapshot and audit trail while trading something
    else entirely. Cancel and arm a new one for that.
    """

    limit_price: Decimal | None = Field(default=None, gt=0)
    amount: Decimal | None = Field(default=None, gt=0)
    # Tri-state on purpose: absent leaves the level's own waiver alone, so an
    # edit of the price cannot silently re-arm the floors on a level that was
    # placed without them.
    ignore_liquidity: bool | None = None


@router.patch("/fomo/limit-order/{intent_id}", dependencies=[Depends(require_trading)])
async def fomo_limit_order_edit(intent_id: int, payload: LimitOrderEdit) -> dict:
    """Move a waiting level's price or size without losing the level.

    The guards are the cancel endpoint's, for the same reasons: a grid's own
    level is not a button's to touch, and past the watching statuses a nonce
    may be reserved or a transaction already signed -- editing the row then
    would describe something different from what the chain is about to do.

    The minimum-order floor is re-checked against the *new* numbers, because
    the edit can walk an order under it just as easily as arming one can.
    """
    if payload.limit_price is None and payload.amount is None and payload.ignore_liquidity is None:
        raise HTTPException(status_code=422, detail="nothing to change")

    await load_dynamic_tokens(SessionLocal)
    async with SessionLocal() as session:
        intent = await session.get(DexIntent, intent_id)
        if intent is None:
            raise HTTPException(status_code=404, detail="level not found")
        if intent.profile_id is not None:
            raise HTTPException(status_code=409, detail="this level belongs to a grid profile")
        if intent.status not in ("WAITING", "BLOCKED", "MISSED"):
            raise HTTPException(
                status_code=409, detail=f"level is {intent.status}, too late to edit"
            )

        limit_price = payload.limit_price if payload.limit_price is not None else intent.limit_price
        amount = payload.amount if payload.amount is not None else intent.amount_in
        try:
            pair = resolve_pair(intent.symbol)
        except DexConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Same asymmetry as arming: an edit may walk a *buy* under the floor,
        # while a sell of any size stays allowed -- there is no size at which
        # closing a position becomes the wrong thing to let someone do.
        if intent.side == "Buy" and amount < settings.dex_min_order_quote:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"minimum order is {settings.dex_min_order_quote} {pair.quote_coin}; "
                    f"this is worth about {amount} {pair.quote_coin}"
                ),
            )

        intent.limit_price = limit_price
        intent.amount_in = amount
        if payload.ignore_liquidity is not None:
            intent.ignore_liquidity_gate = payload.ignore_liquidity
        symbol, side, status = intent.symbol, intent.side, intent.status
        waived = bool(intent.ignore_liquidity_gate)
        await session.commit()

    logger.info(
        "manual limit %s edited: %s %s at %s (intent %s)",
        side, amount, symbol, limit_price, intent_id,
    )
    return {
        "intent_id": intent_id,
        "symbol": symbol,
        "side": side,
        "limit_price": str(limit_price),
        "amount_in": str(amount),
        "ignore_liquidity": waived,
        "status": status,
    }


@router.post("/fomo/limit-order/{intent_id}/cancel", dependencies=[Depends(require_trading)])
async def fomo_limit_order_cancel(intent_id: int) -> dict:
    async with SessionLocal() as session:
        intent = await session.get(DexIntent, intent_id)
        if intent is None:
            raise HTTPException(status_code=404, detail="level not found")
        if intent.profile_id is not None:
            raise HTTPException(status_code=409, detail="this level belongs to a grid profile")
        if intent.status not in ("WAITING", "BLOCKED", "MISSED"):
            # Past WAITING a nonce may be reserved or a transaction signed;
            # cancelling there is the DEX worker's business, not a button's.
            raise HTTPException(status_code=409, detail=f"level is {intent.status}, too late to cancel")
        # Deleted, not marked CANCELLED. A level that never left WAITING or
        # BLOCKED touched no chain and produced no trade, so there is no history
        # in it to keep -- only a row that clutters the list it was removed
        # from. Every status that *did* reach the chain is refused above, so
        # nothing with a transaction behind it can be deleted here.
        await session.delete(intent)
        await session.commit()
    return {"intent_id": intent_id, "status": "DELETED"}


@router.get("/fomo/leaders")
async def fomo_leaders(hours: int = 24, limit: int = 50) -> dict:
    """Per-trader aggregates over a window: what each wallet bought and sold.

    The trader-centric counterpart to /fomo/token -- that one aggregates a
    token across every wallet, this one aggregates a wallet across every
    token, which is what "who is accumulating right now" actually asks.
    """
    hours, since_ms = _window_start_ms(hours)
    limit = max(1, min(limit, 200))

    # Unpriced trades carry a NULL value_usd; fold them to zero so a coin
    # with only unpriced activity sums to 0 rather than NULL -- otherwise it
    # sorts ahead of every real volume, since Postgres orders NULLs first.
    bought = func.coalesce(
        func.sum(case((ChainSwap.side == "BUY", func.coalesce(ChainSwap.value_usd, 0)), else_=0)), 0
    )
    sold = func.coalesce(
        func.sum(case((ChainSwap.side == "SELL", func.coalesce(ChainSwap.value_usd, 0)), else_=0)), 0
    )

    async with SessionLocal() as session:
        totals = (await session.execute(
            select(
                ChainSwap.wallet_address.label("wallet"),
                bought.label("bought_usd"),
                sold.label("sold_usd"),
                func.count().filter(ChainSwap.side == "BUY").label("buys"),
                func.count().filter(ChainSwap.side == "SELL").label("sells"),
                func.max(ChainSwap.block_time_ms).label("last_trade_ms"),
            )
            .where(ChainSwap.block_time_ms >= since_ms)
            .group_by(ChainSwap.wallet_address)
            .order_by(bought.desc())
            .limit(limit)
        )).all()

        if not totals:
            return {"hours": hours, "since_ms": since_ms, "leaders": []}

        wallets = [row.wallet for row in totals]

        # Per-token breakdown -- the "что купили" half of the answer.
        per_token = (await session.execute(
            select(
                ChainSwap.wallet_address.label("wallet"),
                ChainSwap.symbol,
                ChainSwap.token_address,
                func.sum(
                    case((ChainSwap.side == "BUY", ChainSwap.token_amount), else_=-ChainSwap.token_amount)
                ).label("net_qty"),
                bought.label("bought_usd"),
                sold.label("sold_usd"),
                func.count().label("trades"),
            )
            .where(ChainSwap.block_time_ms >= since_ms, ChainSwap.wallet_address.in_(wallets))
            .group_by(ChainSwap.wallet_address, ChainSwap.symbol, ChainSwap.token_address)
            .order_by(bought.desc())
        )).all()

        by_wallet = await _identities(session, wallets)

    tokens_by_wallet: dict[str, list[dict]] = {}
    for row in per_token:
        tokens_by_wallet.setdefault(row.wallet.lower(), []).append({
            "symbol": row.symbol,
            "token_address": row.token_address,
            "net_token_amount": str(row.net_qty),
            "bought_usd": str(row.bought_usd or 0),
            "sold_usd": str(row.sold_usd or 0),
            "trades": row.trades,
        })

    leaders = []
    for row in totals:
        key = row.wallet.lower()
        identity = by_wallet.get(key, {})
        bought_usd = Decimal(row.bought_usd or 0)
        sold_usd = Decimal(row.sold_usd or 0)
        leaders.append({
            "wallet_address": row.wallet,
            "fomo_user_id": identity.get("fomo_user_id"),
            "handle": identity.get("handle"),
            "display_name": identity.get("display_name"),
            "rank": identity.get("rank"),
            "bought_usd": str(bought_usd),
            "sold_usd": str(sold_usd),
            "net_flow_usd": str(bought_usd - sold_usd),
            "buy_count": row.buys,
            "sell_count": row.sells,
            "trade_count": row.buys + row.sells,
            "last_trade_at_ms": row.last_trade_ms,
            "tokens": tokens_by_wallet.get(key, []),
        })

    return {"hours": hours, "since_ms": since_ms, "leaders": leaders}


@router.get("/fomo/traders/{fomo_user_id}")
async def fomo_trader_detail(fomo_user_id: str) -> dict:
    async with SessionLocal() as session:
        trader = await session.get(FomoTrader, fomo_user_id)
        if trader is None:
            raise HTTPException(status_code=404, detail="unknown trader")
        ranks = list((await session.execute(
            select(FomoTraderRank)
            .where(FomoTraderRank.fomo_user_id == fomo_user_id)
            .order_by(FomoTraderRank.captured_at.desc())
            .limit(50)
        )).scalars())
        swaps = []
        if trader.evm_address:
            swaps = list((await session.execute(
                select(ChainSwap)
                .where(func.lower(ChainSwap.wallet_address) == trader.evm_address.lower())
                .order_by(ChainSwap.block_time_ms.desc())
                .limit(100)
            )).scalars())
    return {
        "fomo_user_id": trader.fomo_user_id,
        "evm_address": trader.evm_address,
        "handle": trader.user_handle,
        "display_name": trader.display_name,
        "backfilled_from_block": trader.backfilled_from_block,
        "rank_history": [{"captured_at": r.captured_at.isoformat(), "rank": r.rank} for r in ranks],
        "trades": [
            {
                "tx_hash": s.tx_hash,
                "symbol": s.symbol,
                "side": s.side,
                "token_amount": str(s.token_amount),
                "value_usd": str(s.value_usd) if s.value_usd is not None else None,
                "pricing_source": s.pricing_source,
                "block_time_ms": s.block_time_ms,
            }
            for s in swaps
        ],
    }


@router.get("/fomo/raw")
async def fomo_raw(
    endpoint: str,
    user_id: str | None = None,
    trade_id: str | None = None,
    token: str | None = None,
    network_id: int | None = None,
    handle: str | None = None,
    limit: int = 25,
) -> object:
    """Unparsed FOMO response, for sanity-checking `app.fomo.schema` against
    the live API. Development only -- off unless FOMO_DEBUG_API=true."""
    if not settings.fomo_debug_api:
        raise HTTPException(status_code=404, detail="not found")
    if endpoint not in _FOMO_RAW_ENDPOINTS:
        raise HTTPException(
            status_code=422, detail=f"unknown endpoint; expected one of {sorted(_FOMO_RAW_ENDPOINTS)}"
        )

    fomo = FomoClient(jwt=current_fomo_jwt())
    try:
        if endpoint == "leaderboard":
            return await fomo.leaderboard(limit=limit)
        if endpoint == "balances":
            if not user_id:
                raise HTTPException(status_code=422, detail="user_id is required")
            return await fomo.balances(user_id)
        if endpoint == "trades":
            if not user_id:
                raise HTTPException(status_code=422, detail="user_id is required")
            return await fomo.trades(user_id, limit=limit)
        if endpoint == "trade":
            if not trade_id:
                raise HTTPException(status_code=422, detail="trade_id is required")
            return await fomo.trade(trade_id)
        if endpoint == "holders":
            if not token:
                raise HTTPException(status_code=422, detail="token is required")
            return await fomo.holders(_resolve_fomo_token_address(token), network_id or settings.rh_chain_id)
        if endpoint == "user":
            if not handle:
                raise HTTPException(status_code=422, detail="handle is required")
            return await fomo.user_by_handle(handle)
        raise HTTPException(status_code=422, detail="unhandled endpoint")
    except FomoAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except FomoRateLimited as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except FomoError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await fomo.close()
