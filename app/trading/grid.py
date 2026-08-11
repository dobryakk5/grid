import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import (
    GridExecution,
    GridOrder,
    GridProfile,
    GridRange,
    LotConsumption,
    PositionLot,
    BreakdownEpisode,
    RecoveryTrade,
    StrategyRecommendation,
)
from app.exchanges.bybit import BybitClient, InstrumentInfo
from app.trading.events import record_strategy_event
from app.trading.math import (
    dca_initial_percent,
    floor_to_step,
    ladder_allocations,
    strategy_grid_cells,
)
from app.trading.recommendations import create_recommendation, expire_recommendations

logger = logging.getLogger(__name__)

OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Created"}
SYNC_STATUSES = OPEN_STATUSES | {
    "CancelRequested", "CancelRequestedBreakdown", "CancelRequestedByUser",
}
GRID_ORDER_ROLES = {"grid", "below_grid", "below_accumulation", "dca_initial_buy", "dca_grid"}


def classify_regime(
    closes: list[Decimal], lower: Decimal, upper: Decimal,
) -> str | None:
    """Classify only confirmed breakouts; None means no state change."""
    if len(closes) < 2:
        return None
    recent = closes[-2:]
    if all(close < lower for close in recent):
        return "BREAK_DOWN"
    if all(close > upper for close in recent):
        return "BREAK_UP"
    if all(lower <= close <= upper for close in recent):
        return "RANGE"
    return None


def exponential_moving_average(values: list[Decimal], period: int) -> Decimal | None:
    if period <= 0 or len(values) < period:
        return None
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = sum(values[:period], Decimal("0")) / Decimal(period)
    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
    return ema


def confirmed_break_down(
    closes: list[Decimal], lower: Decimal, *, bars: int, ema_period: int,
) -> bool:
    if len(closes) < max(bars, ema_period):
        return False
    ema = exponential_moving_average(closes, ema_period)
    return (
        ema is not None
        and all(close < lower for close in closes[-bars:])
        and closes[-1] < ema
    )


class GridEngine:
    def __init__(self, exchange: BybitClient) -> None:
        self.exchange = exchange
        self._regime_cache: dict[str, tuple[float, list[Decimal]]] = {}

    async def tick(self, session: AsyncSession) -> None:
        result = await session.execute(select(GridProfile).order_by(GridProfile.id))
        profiles = list(result.scalars())

        for profile in profiles:
            try:
                # Backfill actual fills for databases upgraded from an older
                # version and for any fill that was not persisted on a prior tick.
                await self.backfill_filled_executions(session, profile)
                await self.ensure_current_range(session, profile)
                expired = await expire_recommendations(session, profile.id)
                if expired and profile.regime_state == "RECOMMENDATION_PENDING":
                    profile.regime_state = "WAIT_MANUAL"
                    record_strategy_event(
                        session, profile_id=profile.id, event_type="RECOMMENDATION_EXPIRED",
                        from_state="RECOMMENDATION_PENDING", to_state="WAIT_MANUAL",
                        metadata={"recommendation_ids": [item.id for item in expired]},
                    )
                    await session.commit()

                await self.sync_open_orders(session, profile)
                if not profile.enabled:
                    await self.cancel_open_orders(session, profile.id)
                    continue
                state = await self.update_regime(session, profile)
                if not profile.enabled:
                    await self.cancel_open_orders(session, profile.id)
                    continue
                if await self.apply_price_guards(session, profile):
                    continue
                if state == "BREAK_DOWN":
                    if profile.break_down_action == "trailing_buy":
                        await self.start_trailing_buy(session, profile)
                        continue
                    if profile.break_down_action == "recommend":
                        await self.recommend_trailing_buy(session, profile)
                        continue
                if profile.regime_state in {
                    "TRAILING_BUY", "RECOVERY_ENTERING", "RECOVERY_LONG",
                    "RECOVERY_EXITING", "RECOVERY_COOLDOWN", "WAIT_MANUAL",
                    "RECOMMENDATION_PENDING", "WAIT_RANGE",
                }:
                    await self.manage_recovery(session, profile)
                    continue
                await self.process_filled_orders(session, profile)
                await self.enforce_single_open_buy(session, profile)
                if profile.strategy == "dca":
                    await self.seed_dca_orders(session, profile)
                else:
                    await self.seed_missing_buy_orders(session, profile)
            except Exception:
                logger.exception("Profile %s (%s) tick failed", profile.id, profile.name)

    async def enforce_single_open_buy(
        self, session: AsyncSession, profile: GridProfile
    ) -> None:
        grid_range = await self.get_current_range(session, profile)
        if grid_range is None or grid_range.status != "ACTIVE":
            return
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.range_id == profile.current_range_id,
                GridOrder.side == "Buy",
                GridOrder.order_role.in_(GRID_ORDER_ROLES),
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        buys = list(result.scalars())
        if len(buys) <= 1:
            return
        # Keep only the closest/highest pending BUY. Older versions seeded every
        # lower level at once; this collapses such profiles to sequential mode.
        keep = max(buys, key=lambda item: Decimal(item.price))
        for order in buys:
            if order.id == keep.id:
                continue
            await self.exchange.cancel_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
            order.status = "CancelledSuperseded"
        await session.commit()

    async def get_current_range(
        self, session: AsyncSession, profile: GridProfile,
    ) -> GridRange | None:
        if profile.current_range_id is None:
            return None
        grid_range = await session.get(GridRange, profile.current_range_id)
        if grid_range is None or grid_range.profile_id != profile.id:
            return None
        # Legacy profile fields remain the UI/cache representation of the active
        # range, never an independent source of grid geometry.
        profile.lower_price = grid_range.lower_price
        profile.upper_price = grid_range.upper_price
        profile.step_price = grid_range.step_price
        profile.grid_mode = grid_range.grid_mode
        profile.step_percent = grid_range.step_percent
        return grid_range

    async def ensure_current_range(
        self, session: AsyncSession, profile: GridProfile,
    ) -> GridRange:
        current = await self.get_current_range(session, profile)
        if current is not None:
            return current
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
            to_state="ACTIVE", reason="ENGINE_ENSURE_CURRENT_RANGE",
            metadata={"range_id": grid_range.id},
        )
        record_strategy_event(
            session, profile_id=profile.id, event_type="GRID_RANGE_ACTIVATED",
            to_state="ACTIVE", reason="ENGINE_ENSURE_CURRENT_RANGE",
            metadata={"range_id": grid_range.id},
        )
        await session.commit()
        return grid_range

    async def update_regime(self, session: AsyncSession, profile: GridProfile) -> str:
        """Classify range breakouts; recovery states are managed separately."""
        if profile.regime_state not in {"RANGE", "BREAK_DOWN", "BREAK_UP"}:
            return profile.regime_state
        now = time.monotonic()
        cached = self._regime_cache.get(profile.symbol)
        if cached is None or now - cached[0] >= 300:
            limit = max(profile.breakout_ema_period + 2, profile.breakout_confirm_bars + 2)
            candles = await self.exchange.klines(profile.symbol, interval="60", limit=limit)
            closes = [item["close"] for item in candles[:-1]]
            self._regime_cache[profile.symbol] = (now, closes)
        else:
            closes = cached[1]
        lower = Decimal(profile.lower_price)
        upper = Decimal(profile.upper_price)
        state = (
            "BREAK_DOWN"
            if confirmed_break_down(
                closes, lower,
                bars=profile.breakout_confirm_bars,
                ema_period=profile.breakout_ema_period,
            )
            else classify_regime(closes[-2:], lower, upper) or profile.regime_state
        )
        if state != profile.regime_state:
            logger.warning("Profile %s regime: %s -> %s", profile.id, profile.regime_state, state)
            profile.regime_state = state
            action = (
                getattr(profile, "break_down_action", "continue")
                if state == "BREAK_DOWN"
                else getattr(profile, "break_up_action", "stop")
                if state == "BREAK_UP"
                else "continue"
            )
            if action == "stop":
                profile.enabled = False
            await session.commit()
        return state

    async def recommend_trailing_buy(self, session: AsyncSession, profile: GridProfile) -> None:
        """Create one operator-gated recommendation; recommendation mode never buys."""
        existing = await session.scalar(
            select(StrategyRecommendation.id)
            .where(
                StrategyRecommendation.profile_id == profile.id,
                StrategyRecommendation.type == "START_TRAILING_BUY",
                StrategyRecommendation.status == "PENDING",
            )
            .limit(1)
        )
        if existing is not None:
            return
        price = await self.exchange.last_price(profile.symbol)
        grid_range = await self.ensure_current_range(session, profile)
        await self.cancel_orders(
            session, profile.id, range_id=grid_range.id, side="Buy",
            order_roles=GRID_ORDER_ROLES, local_status="CancelledBreakdown",
        )
        old_range_state = grid_range.status
        grid_range.status = "PAUSED"
        profile.regime_state = "RECOMMENDATION_PENDING"
        record_strategy_event(
            session, profile_id=profile.id, event_type="GRID_RANGE_PAUSED",
            from_state=old_range_state, to_state="PAUSED", reason="BREAK_DOWN_RECOMMEND",
            market_price=price, metadata={"range_id": grid_range.id},
        )
        await create_recommendation(
            session,
            profile_id=profile.id,
            type="START_TRAILING_BUY",
            market_price=price,
            payload={
                "source_range_id": grid_range.id,
                "range_low": str(grid_range.lower_price),
                "market_price": str(price),
                "deviation_pct": str(profile.trailing_buy_deviation_pct),
                "target_quote": str(profile.trailing_buy_target_quote),
                "actions": ["start_trailing_buy", "continue_grid", "stop"],
            },
        )
        await session.commit()

    async def start_trailing_buy(self, session: AsyncSession, profile: GridProfile) -> RecoveryTrade:
        """Pause only current-grid BUYs and persist the tracking state before trading."""
        grid_range = await self.ensure_current_range(session, profile)
        existing = await session.scalar(
            select(RecoveryTrade)
            .join(BreakdownEpisode)
            .where(
                BreakdownEpisode.profile_id == profile.id,
                BreakdownEpisode.source_range_id == grid_range.id,
                RecoveryTrade.status.in_({"TRACKING", "TRIGGERED", "ENTERING", "OPEN", "EXITING"}),
            )
            .order_by(RecoveryTrade.id.desc())
            .limit(1)
        )
        if existing is not None:
            return existing

        await self.cancel_orders(
            session, profile.id, range_id=grid_range.id, side="Buy",
            order_roles=GRID_ORDER_ROLES, local_status="CancelledBreakdown",
        )
        old_range_state = grid_range.status
        grid_range.status = "PAUSED"
        price = await self.exchange.last_price(profile.symbol)
        episode = BreakdownEpisode(
            profile_id=profile.id, source_range_id=grid_range.id, status="TRACKING",
        )
        session.add(episode)
        await session.flush()
        trade = self._new_tracking_trade(
            episode, grid_range.id, price, Decimal(profile.trailing_buy_deviation_pct)
        )
        session.add(trade)
        await session.flush()
        profile.regime_state = "TRAILING_BUY"
        record_strategy_event(
            session, profile_id=profile.id, event_type="GRID_RANGE_PAUSED",
            from_state=old_range_state, to_state="PAUSED", reason="BREAK_DOWN_TRAILING_BUY",
            market_price=price, metadata={"range_id": grid_range.id, "episode_id": episode.id},
        )
        record_strategy_event(
            session, profile_id=profile.id, event_type="TRAILING_BUY_STARTED",
            from_state="BREAK_DOWN", to_state="TRAILING_BUY", market_price=price,
            metadata={"episode_id": episode.id, "trade_id": trade.id, "trigger": str(trade.trigger_price)},
        )
        await session.commit()
        return trade

    @staticmethod
    def _new_tracking_trade(
        episode: BreakdownEpisode, source_range_id: int, price: Decimal, deviation_pct: Decimal,
    ) -> RecoveryTrade:
        # The first release deliberately stores a single fixed deviation. The
        # mode is persisted on the profile so an ATR policy can replace only this
        # calculation in a later stage.
        deviation = Decimal(deviation_pct) / Decimal("100")
        return RecoveryTrade(
            breakdown_episode_id=episode.id,
            source_range_id=source_range_id,
            status="TRACKING",
            attempt_number=episode.attempt_count,
            lowest_price=price,
            trigger_price=price * (Decimal("1") + deviation),
        )

    async def manage_recovery(self, session: AsyncSession, profile: GridProfile) -> None:
        episode = await session.scalar(
            select(BreakdownEpisode)
            .where(
                BreakdownEpisode.profile_id == profile.id,
                BreakdownEpisode.status.in_({"TRACKING", "COOLDOWN"}),
            )
            .order_by(BreakdownEpisode.id.desc())
            .limit(1)
        )
        if episode is None:
            return
        trade = await session.scalar(
            select(RecoveryTrade)
            .where(
                RecoveryTrade.breakdown_episode_id == episode.id,
                RecoveryTrade.status.in_({"TRACKING", "TRIGGERED", "ENTERING", "OPEN", "EXITING"}),
            )
            .order_by(RecoveryTrade.id.desc())
            .limit(1)
        )
        now = datetime.now(timezone.utc)
        if (
            trade is not None and trade.status in {"TRACKING", "TRIGGERED"}
            and episode.started_at
            and now >= episode.started_at + timedelta(hours=profile.trailing_buy_timeout_hours)
        ):
            await self._wait_manual(session, profile, episode, trade, "TRAILING_TIMEOUT")
            return
        if episode.status == "COOLDOWN":
            await self._finish_cooldown(session, profile, episode, now)
            return
        if trade is None:
            await self._wait_manual(session, profile, episode, None, "MISSING_RECOVERY_TRADE")
            return
        if trade.status in {"TRACKING", "TRIGGERED"}:
            await self._track_recovery_rebound(session, profile, episode, trade)
        elif trade.status == "ENTERING":
            await self._confirm_recovery_entry(session, profile, episode, trade)
        elif trade.status == "OPEN":
            await self._manage_recovery_long(session, profile, episode, trade)
        elif trade.status == "EXITING":
            await self._confirm_recovery_exit(session, profile, episode, trade)

    async def _track_recovery_rebound(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade,
    ) -> None:
        price = await self.exchange.last_price(profile.symbol)
        deviation = Decimal(profile.trailing_buy_deviation_pct) / Decimal("100")
        if price < Decimal(trade.lowest_price):
            trade.lowest_price = price
            trade.trigger_price = price * (Decimal("1") + deviation)
            if trade.status == "TRIGGERED":
                trade.status = "TRACKING"
                trade.trigger_confirmed_at = None
            await session.commit()
            return
        if trade.status == "TRACKING" and price >= Decimal(trade.trigger_price):
            trade.status = "TRIGGERED"
            record_strategy_event(
                session, profile_id=profile.id, event_type="TRAILING_BUY_TRIGGERED",
                from_state="TRAILING_BUY", to_state="TRAILING_BUY_TRIGGERED",
                market_price=price, metadata={"trade_id": trade.id, "trigger": str(trade.trigger_price)},
            )
            await session.commit()
            return
        if trade.status != "TRIGGERED":
            return
        candles = await self.exchange.klines(profile.symbol, interval="15", limit=2)
        closed = candles[:-1]
        if not closed or closed[-1]["close"] < Decimal(trade.trigger_price):
            return
        trade.trigger_confirmed_at = datetime.now(timezone.utc)
        await self._begin_recovery_entry(session, profile, episode, trade, price)

    async def _begin_recovery_entry(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade, market_price: Decimal,
    ) -> None:
        if episode.attempt_count >= profile.trailing_buy_max_attempts:
            await self._wait_manual(session, profile, episode, trade, "MAX_ATTEMPTS")
            return
        episode.attempt_count += 1
        trade.attempt_number = episode.attempt_count
        trade.status = "ENTERING"
        trade.entry_link_id = trade.entry_link_id or f"p{profile.id}-r{trade.id}-entry"
        profile.regime_state = "RECOVERY_ENTERING"
        record_strategy_event(
            session, profile_id=profile.id, event_type="RECOVERY_ENTERING",
            from_state="TRAILING_BUY_TRIGGERED", to_state="RECOVERY_ENTERING",
            market_price=market_price, metadata={"trade_id": trade.id, "attempt": trade.attempt_number},
        )
        await session.commit()
        await self._ensure_recovery_entry_order(session, profile, trade, market_price)

    async def _ensure_recovery_entry_order(
        self, session: AsyncSession, profile: GridProfile, trade: RecoveryTrade,
        market_price: Decimal,
    ) -> None:
        if trade.entry_order_id is not None:
            return
        remote = None
        if trade.entry_link_id and hasattr(self.exchange, "get_order_by_link_id"):
            remote = await self.exchange.get_order_by_link_id(
                order_link_id=trade.entry_link_id, symbol=profile.symbol
            )
        info = await self.exchange.instrument_info(profile.symbol)
        qty = self.qty_from_quote(Decimal(profile.trailing_buy_target_quote), market_price, info)
        if remote is None:
            response = await self.exchange.place_market_order(
                symbol=profile.symbol, side="Buy", qty=Decimal(profile.trailing_buy_target_quote),
                order_link_id=trade.entry_link_id, market_unit="quoteCoin",
            )
            exchange_order_id = response["result"]["orderId"]
            status = "New"
        else:
            exchange_order_id = remote["orderId"]
            status = remote.get("orderStatus", "New")
        order = GridOrder(
            profile_id=profile.id, range_id=None, exchange_order_id=exchange_order_id,
            order_link_id=trade.entry_link_id, symbol=profile.symbol, side="Buy",
            grid_buy_price=market_price, price=market_price, qty=qty, status=status,
            replacement_for=None, order_role="recovery_entry",
        )
        session.add(order)
        await session.flush()
        trade.entry_order_id = order.id
        await session.commit()

    async def _confirm_recovery_entry(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade,
    ) -> None:
        if trade.entry_order_id is None:
            await self._ensure_recovery_entry_order(
                session, profile, trade, await self.exchange.last_price(profile.symbol)
            )
            return
        order = await session.get(GridOrder, trade.entry_order_id)
        if order is None or order.status != "Filled":
            return
        executions = await session.scalar(
            select(func.count(GridExecution.id)).where(GridExecution.order_id == order.id)
        )
        if not executions:
            return
        entry = Decimal(order.avg_price or order.price)
        trade.entry_price = entry
        trade.entry_qty = Decimal(order.filled_qty or order.qty)
        trade.highest_price = entry
        trade.initial_stop_price = entry * (Decimal("1") - Decimal(profile.recovery_initial_stop_pct) / Decimal("100"))
        trade.effective_stop_price = trade.initial_stop_price
        trade.status = "OPEN"
        profile.regime_state = "RECOVERY_LONG"
        record_strategy_event(
            session, profile_id=profile.id, event_type="RECOVERY_LONG_OPENED",
            from_state="RECOVERY_ENTERING", to_state="RECOVERY_LONG", market_price=entry,
            metadata={"trade_id": trade.id, "stop": str(trade.effective_stop_price)},
        )
        await session.commit()

    async def _manage_recovery_long(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade,
    ) -> None:
        price = await self.exchange.last_price(profile.symbol)
        entry = Decimal(trade.entry_price)
        highest = max(Decimal(trade.highest_price or entry), price)
        trade.highest_price = highest
        stop = Decimal(trade.effective_stop_price or trade.initial_stop_price)
        if price >= entry * (Decimal("1") + Decimal(profile.recovery_break_even_trigger_pct) / Decimal("100")):
            stop = max(stop, entry)
        if price >= entry * (Decimal("1") + Decimal(profile.recovery_trailing_activation_pct) / Decimal("100")):
            stop = max(stop, highest * (Decimal("1") - Decimal(profile.recovery_trailing_pct) / Decimal("100")))
        trade.effective_stop_price = stop
        if profile.pending_hard_stop:
            await self._begin_recovery_exit(session, profile, episode, trade, "HARD_STOP", price)
        elif await self._recovery_return_confirmed(session, profile, trade, price):
            await self._begin_recovery_exit(session, profile, episode, trade, "RECOVERED_TO_RANGE", price)
        elif price <= stop:
            await self._begin_recovery_exit(session, profile, episode, trade, "STOP_LOSS", price)
        else:
            await session.commit()

    async def _recovery_return_confirmed(
        self, session: AsyncSession, profile: GridProfile, trade: RecoveryTrade,
        market_price: Decimal,
    ) -> bool:
        grid_range = await session.get(GridRange, trade.source_range_id)
        if grid_range is None or market_price < Decimal(grid_range.lower_price):
            return False
        bars = profile.breakout_confirm_bars
        candles = await self.exchange.klines(profile.symbol, interval="60", limit=bars + 1)
        closes = [item["close"] for item in candles[:-1]][-bars:]
        return len(closes) == bars and all(
            Decimal(grid_range.lower_price) <= close <= Decimal(grid_range.upper_price)
            for close in closes
        )

    async def _begin_recovery_exit(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade,
        reason: str, market_price: Decimal,
    ) -> None:
        if trade.exit_order_id is not None:
            return
        qty = await session.scalar(
            select(func.coalesce(func.sum(PositionLot.remaining_qty), 0)).where(
                PositionLot.profile_id == profile.id,
                PositionLot.owner_type == "RECOVERY",
                PositionLot.owner_id == trade.id,
                PositionLot.remaining_qty > 0,
            )
        )
        info = await self.exchange.instrument_info(profile.symbol)
        qty = floor_to_step(Decimal(qty or 0), info.base_precision)
        if qty <= 0:
            trade.status = "CLOSED"
            trade.closed_at = datetime.now(timezone.utc)
            if profile.pending_hard_stop:
                profile.pending_hard_stop = False
                profile.enabled = False
                episode.status = "RESOLVED"
                episode.resolution = "HARD_STOP"
                episode.resolved_at = trade.closed_at
                profile.regime_state = "STOPPED"
            elif reason == "RECOVERED_TO_RANGE":
                episode.status = "RESOLVED"
                episode.resolution = reason
                episode.resolved_at = trade.closed_at
                profile.regime_state = "WAIT_RANGE"
            else:
                episode.status = "COOLDOWN"
                trade.cooldown_until = trade.closed_at + timedelta(hours=profile.recovery_cooldown_bars)
                profile.regime_state = "RECOVERY_COOLDOWN"
            await session.commit()
            return
        trade.status = "EXITING"
        trade.exit_reason = reason
        trade.exit_link_id = trade.exit_link_id or f"p{profile.id}-r{trade.id}-exit"
        profile.regime_state = "RECOVERY_EXITING"
        await session.commit()
        await self._ensure_recovery_exit_order(session, profile, episode, trade, market_price)

    async def _ensure_recovery_exit_order(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade, market_price: Decimal,
    ) -> None:
        """Resume a reserved recovery exit after a timeout or worker restart."""
        if trade.exit_order_id is not None:
            return
        qty = await session.scalar(
            select(func.coalesce(func.sum(PositionLot.remaining_qty), 0)).where(
                PositionLot.profile_id == profile.id,
                PositionLot.owner_type == "RECOVERY",
                PositionLot.owner_id == trade.id,
                PositionLot.remaining_qty > 0,
            )
        )
        info = await self.exchange.instrument_info(profile.symbol)
        qty = floor_to_step(Decimal(qty or 0), info.base_precision)
        if qty <= 0:
            await self._wait_manual(session, profile, episode, trade, "RECOVERY_EXIT_WITHOUT_LOTS")
            return
        remote = None
        if hasattr(self.exchange, "get_order_by_link_id"):
            remote = await self.exchange.get_order_by_link_id(
                order_link_id=trade.exit_link_id, symbol=profile.symbol
            )
        base_coin = self._base_coin(profile.symbol)
        available = await self.exchange.available_balance(base_coin) if remote is None else qty
        if remote is None and available + info.base_precision < qty:
            trade.status = "ERROR"
            trade.error = f"recovery qty {qty} exceeds available {available} {base_coin}"
            episode.status = "WAIT_MANUAL"
            episode.resolution = "RECOVERY_BALANCE_MISMATCH"
            episode.resolved_at = datetime.now(timezone.utc)
            profile.regime_state = "WAIT_MANUAL"
            record_strategy_event(
                session, profile_id=profile.id, event_type="RECOVERY_ERROR",
                reason="RECOVERY_BALANCE_MISMATCH", market_price=market_price,
                metadata={"trade_id": trade.id, "required_qty": str(qty), "available_qty": str(available)},
            )
            await session.commit()
            return
        if remote is None:
            response = await self.exchange.place_market_order(
                symbol=profile.symbol, side="Sell", qty=qty,
                order_link_id=trade.exit_link_id,
            )
            exchange_order_id, status = response["result"]["orderId"], "New"
        else:
            exchange_order_id, status = remote["orderId"], remote.get("orderStatus", "New")
        entry = await session.get(GridOrder, trade.entry_order_id)
        order = GridOrder(
            profile_id=profile.id, range_id=None, exchange_order_id=exchange_order_id,
            order_link_id=trade.exit_link_id, symbol=profile.symbol, side="Sell",
            grid_buy_price=Decimal(entry.grid_buy_price), price=market_price, qty=qty,
            status=status, replacement_for=entry.exchange_order_id, order_role="recovery_exit",
        )
        session.add(order)
        await session.flush()
        trade.exit_order_id = order.id
        record_strategy_event(
            session, profile_id=profile.id, event_type="RECOVERY_EXITING",
            from_state="RECOVERY_LONG", to_state="RECOVERY_EXITING", reason=trade.exit_reason,
            market_price=market_price, metadata={"trade_id": trade.id, "stop": str(trade.effective_stop_price)},
        )
        await session.commit()

    async def _confirm_recovery_exit(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade,
    ) -> None:
        if trade.exit_order_id is None:
            await self._ensure_recovery_exit_order(
                session, profile, episode, trade, await self.exchange.last_price(profile.symbol)
            )
            return
        order = await session.get(GridOrder, trade.exit_order_id)
        if order is None or order.status != "Filled":
            return
        executions = await session.scalar(
            select(func.count(GridExecution.id)).where(GridExecution.order_id == order.id)
        )
        if not executions:
            return
        trade.status = "CLOSED"
        trade.closed_at = datetime.now(timezone.utc)
        if profile.pending_hard_stop:
            profile.pending_hard_stop = False
            profile.enabled = False
            episode.status = "RESOLVED"
            episode.resolution = "HARD_STOP"
            episode.resolved_at = trade.closed_at
            profile.regime_state = "STOPPED"
        elif trade.exit_reason == "RECOVERED_TO_RANGE":
            episode.status = "RESOLVED"
            episode.resolution = "RECOVERED_TO_RANGE"
            episode.resolved_at = trade.closed_at
            profile.regime_state = "WAIT_RANGE"
        else:
            episode.status = "COOLDOWN"
            trade.cooldown_until = trade.closed_at + timedelta(hours=profile.recovery_cooldown_bars)
            profile.regime_state = "RECOVERY_COOLDOWN"
        record_strategy_event(
            session, profile_id=profile.id, event_type="RECOVERY_EXIT_FILLED",
            from_state="RECOVERY_EXITING", to_state=profile.regime_state,
            reason=trade.exit_reason, metadata={"trade_id": trade.id, "episode_id": episode.id},
        )
        await session.commit()

    async def _finish_cooldown(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        now: datetime,
    ) -> None:
        latest = await session.scalar(
            select(RecoveryTrade)
            .where(RecoveryTrade.breakdown_episode_id == episode.id)
            .order_by(RecoveryTrade.id.desc()).limit(1)
        )
        if latest is None or latest.cooldown_until is None or now < latest.cooldown_until:
            return
        if episode.attempt_count >= profile.trailing_buy_max_attempts:
            await self._wait_manual(session, profile, episode, latest, "MAX_ATTEMPTS")
            return
        price = await self.exchange.last_price(profile.symbol)
        trade = self._new_tracking_trade(
            episode, episode.source_range_id, price,
            Decimal(profile.trailing_buy_deviation_pct),
        )
        session.add(trade)
        await session.flush()
        episode.status = "TRACKING"
        profile.regime_state = "TRAILING_BUY"
        record_strategy_event(
            session, profile_id=profile.id, event_type="TRAILING_BUY_RESTARTED",
            from_state="RECOVERY_COOLDOWN", to_state="TRAILING_BUY", market_price=price,
            metadata={"episode_id": episode.id, "trade_id": trade.id, "attempt": episode.attempt_count + 1},
        )
        await session.commit()

    async def _wait_manual(
        self, session: AsyncSession, profile: GridProfile, episode: BreakdownEpisode,
        trade: RecoveryTrade | None, reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        episode.status = "WAIT_MANUAL"
        episode.resolution = reason
        episode.resolved_at = now
        if trade is not None and trade.status not in {"CLOSED", "STOPPED"}:
            trade.status = "EXPIRED" if reason == "TRAILING_TIMEOUT" else "STOPPED"
            trade.closed_at = now
        profile.regime_state = "WAIT_MANUAL"
        record_strategy_event(
            session, profile_id=profile.id, event_type="RECOVERY_WAIT_MANUAL",
            from_state="TRAILING_BUY", to_state="WAIT_MANUAL", reason=reason,
            metadata={"episode_id": episode.id, "trade_id": trade.id if trade else None},
        )
        await session.commit()

    async def apply_price_guards(self, session: AsyncSession, profile: GridProfile) -> bool:
        if profile.stop_loss is None and profile.take_profit is None:
            return False
        market_price = await self.exchange.last_price(profile.symbol)
        triggered = (
            profile.stop_loss is not None
            and market_price <= Decimal(profile.stop_loss)
        ) or (
            profile.take_profit is not None
            and market_price >= Decimal(profile.take_profit)
        )
        if not triggered:
            return False
        if profile.regime_state in {"RECOVERY_ENTERING", "RECOVERY_LONG", "RECOVERY_EXITING"}:
            profile.pending_hard_stop = True
            record_strategy_event(
                session, profile_id=profile.id, event_type="RECOVERY_HARD_STOP_REQUESTED",
                reason="GLOBAL_PRICE_GUARD", market_price=market_price,
            )
            await session.commit()
            return False
        profile.enabled = False
        await session.commit()
        await self.cancel_open_orders(session, profile.id)
        logger.warning("Profile %s stopped by price guard at %s", profile.id, market_price)
        return True

    async def sync_open_orders(
        self, session: AsyncSession, profile: GridProfile,
    ) -> list[GridOrder]:
        """Persist exchange state and executions only; this method never trades."""
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.status.in_(SYNC_STATUSES),
            )
        )
        orders = list(result.scalars())
        if not orders:
            return []

        for order in orders:
            remote = await self.exchange.get_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
            if remote is None:
                logger.warning("Order %s not found on exchange", order.exchange_order_id)
                continue

            old_status = order.status
            remote_status = remote.get("orderStatus", order.status)
            if remote_status == "Cancelled" and old_status.startswith("CancelRequested"):
                suffix = old_status.removeprefix("CancelRequested")
                order.status = f"Cancelled{suffix}"
            else:
                order.status = remote_status
            if remote.get("cumExecQty"):
                order.filled_qty = Decimal(remote["cumExecQty"])
            if remote.get("avgPrice"):
                order.avg_price = Decimal(remote["avgPrice"])

            if order.status in {"PartiallyFilled", "Filled"}:
                await self.sync_order_executions(session, order)
            if order.status != old_status:
                record_strategy_event(
                    session, profile_id=profile.id,
                    event_type="ORDER_FILLED" if order.status == "Filled" else "ORDER_SYNCED",
                    from_state=old_status, to_state=order.status,
                    metadata={"order_id": order.id, "exchange_order_id": order.exchange_order_id},
                )

        await session.flush()
        return orders

    async def process_filled_orders(
        self, session: AsyncSession, profile: GridProfile,
    ) -> None:
        """Create replacements only after the exchange state has been synchronized."""
        grid_range = await self.get_current_range(session, profile)
        if grid_range is None or grid_range.status != "ACTIVE":
            return
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.range_id == profile.current_range_id,
                GridOrder.status == "Filled",
                GridOrder.replacement_created.is_(False),
            )
        )
        orders = list(result.scalars())
        if not orders:
            return
        info = await self.exchange.instrument_info(profile.symbol)
        current_range = await self.get_current_range(session, profile)
        for order in orders:
            if profile.strategy == "dca" and order.order_role in {"dca_initial_buy", "dca_grid"}:
                await self._create_dca_sell_ladder(session, profile, order, info)
            elif order.order_role == "dca_sell":
                await self._create_dca_rebuy_if_ladder_complete(session, profile, order, info)
            elif (
                order.side == "Buy"
                and current_range is not None
                and Decimal(order.grid_buy_price) < Decimal(current_range.lower_price)
                and not getattr(profile, "sell_below_grid", False)
            ):
                order.order_role = "below_accumulation"
                order.replacement_created = True
            else:
                await self._create_replacement(session, profile, order, info)
        await session.commit()

    async def seed_dca_orders(self, session: AsyncSession, profile: GridProfile) -> None:
        grid_range = await self.ensure_current_range(session, profile)
        if grid_range.status != "ACTIVE":
            return
        info = await self.exchange.instrument_info(profile.symbol)
        market_price = await self.exchange.last_price(profile.symbol)
        lower = Decimal(grid_range.lower_price)
        upper = Decimal(grid_range.upper_price)
        budget = Decimal(profile.max_investment or 0)
        if budget <= 0:
            raise ValueError("DCA Grid requires max_investment")

        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.range_id == grid_range.id,
            ).order_by(GridOrder.id)
        )
        orders = list(result.scalars())
        initial = next(
            (o for o in reversed(orders) if o.order_role == "dca_initial_buy"), None
        )
        if initial is not None and initial.status in {
            "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"
        }:
            initial = None
        if initial is None and not lower < market_price < upper:
            raise ValueError("DCA Grid can start only while price is inside its range")
        anchor_price = Decimal(initial.price) if initial is not None else market_price
        initial_pct = dca_initial_percent(
            anchor_price, lower, upper, Decimal(profile.initial_buy_percent)
        )
        initial_quote = budget * initial_pct / Decimal("100")

        if initial is None:
            qty = self.qty_from_quote(initial_quote, market_price, info)
            initial = await self._place_market_and_store(
                session=session,
                profile=profile,
                qty=qty,
                quote_amount=initial_quote,
                market_price=market_price,
                grid_buy_price=market_price,
                order_role="dca_initial_buy",
                range_id=grid_range.id,
            )
            orders.append(initial)
            await session.commit()
            return

        for buy_order in orders:
            if (
                buy_order.order_role in {"dca_initial_buy", "dca_grid"}
                and buy_order.status == "Filled"
            ):
                await self._create_dca_sell_ladder(session, profile, buy_order, info)

        buy_prices = [
            buy for buy, _ in reversed(self.range_cells(profile, grid_range, info))
            if buy < anchor_price
        ]
        while buy_prices:
            allocations = ladder_allocations(
                budget - initial_quote,
                len(buy_prices),
                mode=profile.buy_ladder_mode,
                multiplier=Decimal(profile.ladder_multiplier),
            )
            try:
                for buy_price, quote_amount in zip(buy_prices, allocations):
                    self.qty_from_quote(quote_amount, buy_price, info)
                break
            except ValueError:
                buy_prices.pop()
        else:
            allocations = []
        existing = {
            Decimal(order.grid_buy_price): order for order in orders
            if order.order_role == "dca_grid"
        }
        for buy_price, quote_amount in zip(buy_prices, allocations):
            previous = existing.get(buy_price)
            if previous is None:
                await self._seed_buy_quote(
                    session, profile, buy_price, quote_amount, info,
                    order_role="dca_grid", range_id=grid_range.id,
                )
                return
            if previous.status in OPEN_STATUSES:
                return
            if previous.status == "CancelledByUser":
                if buy_price < market_price:
                    return
                continue
            if previous.status in {
                "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled",
                "CancelledSuperseded",
            }:
                if buy_price < market_price:
                    await self._seed_buy_quote(
                        session, profile, buy_price, quote_amount, info,
                        order_role="dca_grid", range_id=grid_range.id,
                    )
                    return
                continue

    async def _create_dca_sell_ladder(
        self, session: AsyncSession, profile: GridProfile, order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        filled_qty = Decimal(order.filled_qty or order.qty)
        anchor = Decimal(order.avg_price or order.price)
        grid_range = await session.get(GridRange, order.range_id) if order.range_id else None
        if grid_range is None:
            raise RuntimeError("DCA order has no GridRange")
        sell_prices = [
            sell for _, sell in self.range_cells(profile, grid_range, info) if sell > anchor
        ]
        if not sell_prices:
            raise ValueError("DCA Grid has no SELL levels above initial purchase")
        available_qty = floor_to_step(
            filled_qty * (Decimal("1") - settings.grid_fee_buffer_pct),
            info.base_precision,
        )
        while sell_prices:
            allocations = ladder_allocations(
                available_qty,
                len(sell_prices),
                mode=profile.sell_ladder_mode,
                multiplier=Decimal(profile.ladder_multiplier),
            )
            planned = []
            placed_qty = Decimal("0")
            try:
                for index, (sell_price, raw_qty) in enumerate(zip(sell_prices, allocations)):
                    qty = floor_to_step(raw_qty, info.base_precision)
                    if index == len(sell_prices) - 1:
                        qty = floor_to_step(available_qty - placed_qty, info.base_precision)
                    self.validate_order(sell_price, qty, info)
                    planned.append((sell_price, qty))
                    placed_qty += qty
                break
            except ValueError:
                sell_prices.pop()
        else:
            raise ValueError("DCA sell inventory is below the exchange minimum order amount")

        result = await session.execute(
            select(GridOrder).where(
                GridOrder.replacement_for == order.exchange_order_id,
                GridOrder.order_role == "dca_sell",
            ).order_by(GridOrder.id)
        )
        latest_by_price = {Decimal(child.price): child for child in result.scalars()}
        for sell_price, qty in planned:
            existing = latest_by_price.get(sell_price)
            if existing is not None and existing.status not in {
                "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"
            }:
                continue
            await self._place_and_store(
                session=session,
                profile=profile,
                side="Sell",
                price=sell_price,
                grid_buy_price=Decimal(order.grid_buy_price),
                qty=qty,
                replacement_for=order.exchange_order_id,
                order_role="dca_sell",
                range_id=order.range_id,
            )
            await session.commit()
        order.replacement_created = True
        await session.commit()

    async def _create_dca_rebuy_if_ladder_complete(
        self, session: AsyncSession, profile: GridProfile, order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        if not order.replacement_for:
            order.replacement_created = True
            return
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.replacement_for == order.replacement_for,
                GridOrder.order_role == "dca_sell",
            ).order_by(GridOrder.id)
        )
        siblings = list(result.scalars())
        if not siblings or any(sibling.status != "Filled" for sibling in siblings):
            return
        sibling_ids = [sibling.exchange_order_id for sibling in siblings]
        existing_rebuy = await session.scalar(
            select(func.count(GridOrder.id)).where(
                GridOrder.replacement_for.in_(sibling_ids),
                GridOrder.side == "Buy",
            )
        )
        if not existing_rebuy:
            qty = floor_to_step(
                sum((Decimal(s.filled_qty or s.qty) for s in siblings), Decimal("0")),
                info.base_precision,
            )
            buy_price = floor_to_step(Decimal(order.grid_buy_price), info.tick_size)
            self.validate_order(buy_price, qty, info)
            await self._place_and_store(
                session=session,
                profile=profile,
                side="Buy",
                price=buy_price,
                grid_buy_price=buy_price,
                qty=qty,
                replacement_for=order.exchange_order_id,
                order_role="dca_grid",
                range_id=order.range_id,
            )
        for sibling in siblings:
            sibling.replacement_created = True
        await session.commit()


    async def backfill_filled_executions(
        self, session: AsyncSession, profile: GridProfile
    ) -> None:
        result = await session.execute(
            select(GridOrder)
            .where(
                GridOrder.profile_id == profile.id,
                GridOrder.status == "Filled",
            )
            .order_by(GridOrder.id.desc())
            .limit(500)
        )
        changed = False
        for order in result.scalars():
            stored_qty = await session.scalar(
                select(func.coalesce(func.sum(GridExecution.exec_qty), 0)).where(
                    GridExecution.order_id == order.id
                )
            )
            expected_qty = Decimal(order.filled_qty or order.qty)
            if Decimal(stored_qty or 0) < expected_qty:
                if await self.sync_order_executions(session, order):
                    changed = True
        if changed:
            await session.commit()

    async def sync_order_executions(
        self, session: AsyncSession, order: GridOrder
    ) -> int:
        remote = await self.exchange.get_executions(
            order_id=order.exchange_order_id, symbol=order.symbol
        )
        if not remote:
            return 0

        existing_result = await session.execute(
            select(GridExecution.exec_id).where(GridExecution.order_id == order.id)
        )
        existing_ids = set(existing_result.scalars())
        inserted = 0
        new_executions: list[GridExecution] = []

        for item in remote:
            exec_id = item.get("execId")
            if not exec_id or exec_id in existing_ids:
                continue
            price = Decimal(item.get("execPrice") or "0")
            qty = Decimal(item.get("execQty") or "0")
            value = Decimal(item.get("execValue") or "0")
            if value == 0 and price and qty:
                value = price * qty
            raw_maker = item.get("isMaker")
            is_maker = (
                raw_maker
                if isinstance(raw_maker, bool)
                else str(raw_maker).lower() == "true"
                if raw_maker is not None
                else None
            )
            execution = GridExecution(
                    order_id=order.id,
                    exec_id=exec_id,
                    exec_price=price,
                    exec_qty=qty,
                    exec_value=value,
                    exec_fee=Decimal(item.get("execFee") or "0"),
                    fee_currency=item.get("feeCurrency") or None,
                    fee_rate=(
                        Decimal(item["feeRate"]) if item.get("feeRate") else None
                    ),
                    is_maker=is_maker,
                    exec_time_ms=(
                        int(item["execTime"]) if item.get("execTime") else None
                    ),
            )
            session.add(execution)
            new_executions.append(execution)
            existing_ids.add(exec_id)
            inserted += 1

        if inserted:
            await session.flush()
            for execution in new_executions:
                if order.side == "Buy":
                    await self.ensure_position_lot_for_execution(session, order, execution)
                elif order.side == "Sell":
                    await self.apply_sell_execution_to_lots(session, order, execution)
            logger.info(
                "Stored %s execution(s) for order %s",
                inserted,
                order.exchange_order_id,
            )
        return inserted

    @staticmethod
    def _base_coin(symbol: str) -> str:
        for quote in ("USDT", "USDC", "BTC", "ETH"):
            if symbol.upper().endswith(quote):
                return symbol.upper()[:-len(quote)]
        return ""

    async def ensure_position_lot_for_execution(
        self, session: AsyncSession, order: GridOrder, execution: GridExecution,
    ) -> PositionLot | None:
        if order.side != "Buy":
            return None
        # A late execution from intentionally unassigned legacy history must not
        # be silently represented as inventory in the current strategy ledger.
        recovery_trade = None
        if order.range_id is None:
            if order.order_role != "recovery_entry":
                return None
            recovery_trade = await session.scalar(
                select(RecoveryTrade).where(RecoveryTrade.entry_order_id == order.id)
            )
            if recovery_trade is None:
                return None
        existing = await session.scalar(
            select(PositionLot).where(PositionLot.source_execution_id == execution.id)
        )
        if existing is not None:
            return existing
        base_coin = self._base_coin(order.symbol)
        fee_currency = (execution.fee_currency or "").upper()
        qty = Decimal(execution.exec_qty)
        fee = Decimal(execution.exec_fee or 0)
        acquired = qty - fee if fee_currency == base_coin else qty
        quote_fee = fee if fee_currency in {"USDT", "USDC"} else Decimal("0")
        lot = PositionLot(
            profile_id=order.profile_id,
            source_execution_id=execution.id,
            origin_type="RECOVERY" if recovery_trade is not None else "GRID",
            owner_type="RECOVERY" if recovery_trade is not None else "GRID",
            owner_id=recovery_trade.id if recovery_trade is not None else order.range_id,
            acquired_qty=acquired,
            remaining_qty=acquired,
            cost_quote=Decimal(execution.exec_value) + quote_fee,
            fees_quote=quote_fee,
            status="OPEN",
        )
        session.add(lot)
        await session.flush()
        record_strategy_event(
            session, profile_id=order.profile_id, event_type="POSITION_LOT_CREATED",
            metadata={"lot_id": lot.id, "execution_id": execution.id, "order_id": order.id},
        )
        return lot

    async def find_origin_buy_order(
        self, session: AsyncSession, sell_order: GridOrder,
    ) -> GridOrder | None:
        parent_id = sell_order.replacement_for
        visited: set[str] = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = await session.scalar(
                select(GridOrder).where(GridOrder.exchange_order_id == parent_id)
            )
            if parent is None:
                return None
            if parent.side == "Buy":
                return parent
            parent_id = parent.replacement_for
        return None

    async def apply_sell_execution_to_lots(
        self, session: AsyncSession, order: GridOrder, execution: GridExecution,
    ) -> None:
        if order.side != "Sell":
            return
        buy = await self.find_origin_buy_order(session, order)
        if buy is None:
            return
        already_consumed = await session.scalar(
            select(func.coalesce(func.sum(LotConsumption.qty), 0)).where(
                LotConsumption.sell_execution_id == execution.id
            )
        )
        lots = list((await session.execute(
            select(PositionLot)
            .join(GridExecution, PositionLot.source_execution_id == GridExecution.id)
            .where(
                GridExecution.order_id == buy.id,
                PositionLot.remaining_qty > 0,
            )
            .order_by(PositionLot.id)
        )).scalars())
        remaining = max(Decimal(execution.exec_qty) - Decimal(already_consumed or 0), Decimal("0"))
        for lot in lots:
            if remaining <= 0:
                break
            already = await session.scalar(
                select(LotConsumption.id).where(
                    LotConsumption.lot_id == lot.id,
                    LotConsumption.sell_execution_id == execution.id,
                )
            )
            if already is not None:
                continue
            consumed = min(Decimal(lot.remaining_qty), remaining)
            if consumed <= 0:
                continue
            session.add(LotConsumption(
                lot_id=lot.id, sell_execution_id=execution.id, qty=consumed,
            ))
            lot.remaining_qty = Decimal(lot.remaining_qty) - consumed
            remaining -= consumed
            if lot.remaining_qty <= Decimal("0.000000000001"):
                lot.remaining_qty = Decimal("0")
                lot.status = "CLOSED"
                lot.closed_at = datetime.now(timezone.utc)
            record_strategy_event(
                session, profile_id=order.profile_id, event_type="POSITION_LOT_CONSUMED",
                metadata={"lot_id": lot.id, "sell_execution_id": execution.id, "qty": str(consumed)},
            )
        await session.flush()

    async def seed_missing_buy_orders(
        self, session: AsyncSession, profile: GridProfile
    ) -> None:
        if profile.current_range_id is None:
            raise RuntimeError("cannot seed GridOrder without profile.current_range_id")
        grid_range = await self.get_current_range(session, profile)
        if grid_range is None:
            raise RuntimeError("profile.current_range_id does not belong to this profile")
        if grid_range.status != "ACTIVE":
            return
        info = await self.exchange.instrument_info(profile.symbol)
        market_price = await self.exchange.last_price(profile.symbol)

        cells = self.range_cells(profile, grid_range, info)

        result = await session.execute(
            select(GridOrder)
            .where(
                GridOrder.profile_id == profile.id,
                GridOrder.range_id == profile.current_range_id,
            )
            .order_by(GridOrder.id)
        )
        latest_by_cell: dict[Decimal, GridOrder] = {}
        for order in result.scalars():
            latest_by_cell[Decimal(order.grid_buy_price)] = order

        retry_statuses = {
            "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled",
            "CancelledSuperseded",
        }
        for buy_price, sell_price in reversed(cells):
            latest = latest_by_cell.get(buy_price)

            if latest is None:
                if profile.strategy == "classic" and sell_price > market_price:
                    await self._seed_market_buy(
                        session, profile, buy_price, market_price, info, grid_range.id,
                    )
                    return
                if buy_price < market_price:
                    await self._seed_buy(session, profile, buy_price, info, grid_range.id)
                    return
                continue

            if latest.status in OPEN_STATUSES:
                if latest.side == "Buy":
                    return
                continue

            if latest.status == "CancelledByUser":
                # Do not bypass a manually cancelled rung while price is still
                # above it. Once price has crossed below, the next rung may arm.
                if buy_price < market_price:
                    return
                continue

            # A filled order should normally already have its replacement. If a
            # replacement did not get committed, retry it instead of creating a
            # second independent cycle.
            if latest.status == "Filled" and not latest.replacement_created:
                if (
                    latest.side == "Buy"
                    and buy_price < Decimal(grid_range.lower_price)
                    and not getattr(profile, "sell_below_grid", False)
                ):
                    latest.order_role = "below_accumulation"
                    latest.replacement_created = True
                    await session.commit()
                else:
                    await self._create_replacement(session, profile, latest, info)
                return

            if latest.status in retry_statuses:
                if latest.side == "Buy":
                    if buy_price < market_price:
                        await self._seed_buy(session, profile, buy_price, info, grid_range.id)
                        return
                else:
                    # A cancelled SELL means this cell may still own BTC from the
                    # preceding BUY. Re-create the SELL instead of buying more.
                    qty = floor_to_step(Decimal(latest.qty), info.base_precision)
                    self.validate_order(sell_price, qty, info)
                    await self._place_and_store(
                        session=session,
                        profile=profile,
                        side="Sell",
                        price=sell_price,
                        grid_buy_price=buy_price,
                        qty=qty,
                        replacement_for=latest.exchange_order_id,
                        order_role=latest.order_role,
                        range_id=latest.range_id,
                    )
                    await session.commit()
                    return
                continue

    @staticmethod
    def range_cells(
        profile: GridProfile, grid_range: GridRange, info: InstrumentInfo,
    ) -> list[tuple[Decimal, Decimal]]:
        raw = strategy_grid_cells(
            Decimal(grid_range.lower_price), Decimal(grid_range.upper_price),
            Decimal(grid_range.step_price), mode=grid_range.grid_mode,
            step_percent=(
                Decimal(grid_range.step_percent)
                if grid_range.step_percent is not None else None
            ),
        )
        extension = getattr(profile, "below_grid_lower_price", None)
        if getattr(profile, "buy_below_grid", True) and extension is not None:
            raw = strategy_grid_cells(
                Decimal(extension), Decimal(grid_range.lower_price),
                Decimal(grid_range.step_price), mode="arithmetic",
            ) + raw
        cells = []
        for buy, sell in raw:
            rounded = (floor_to_step(buy, info.tick_size), floor_to_step(sell, info.tick_size))
            if rounded[0] >= rounded[1]:
                raise ValueError("grid interval is smaller than exchange tick size")
            if not cells or cells[-1] != rounded:
                cells.append(rounded)
        return cells

    async def _seed_market_buy(
        self, session: AsyncSession, profile: GridProfile, grid_buy_price: Decimal,
        market_price: Decimal, info: InstrumentInfo, range_id: int,
    ) -> None:
        qty = self.qty_from_quote(Decimal(profile.quote_per_level), market_price, info)
        link_id = f"g{profile.id}-{uuid.uuid4().hex[:24]}"
        response = await self.exchange.place_market_order(
            symbol=profile.symbol, side="Buy", qty=qty, order_link_id=link_id
        )
        session.add(GridOrder(
            profile_id=profile.id,
            range_id=range_id,
            exchange_order_id=response["result"]["orderId"],
            order_link_id=link_id,
            symbol=profile.symbol,
            side="Buy",
            grid_buy_price=grid_buy_price,
            price=market_price,
            qty=qty,
            status="New",
        ))
        await session.commit()

    async def _seed_buy(
        self,
        session: AsyncSession,
        profile: GridProfile,
        buy_price: Decimal,
        info: InstrumentInfo,
        range_id: int,
    ) -> None:
        qty = self.qty_from_quote(Decimal(profile.quote_per_level), buy_price, info)
        await self._place_and_store(
            session=session,
            profile=profile,
            side="Buy",
            price=buy_price,
            grid_buy_price=buy_price,
            qty=qty,
            replacement_for=None,
            order_role=(
                "below_grid"
                if buy_price < Decimal((await session.get(GridRange, range_id)).lower_price) else "grid"
            ),
            range_id=range_id,
        )
        await session.commit()
        logger.info(
            "Profile %s seeded BUY %s %s @ %s",
            profile.id,
            profile.symbol,
            qty,
            buy_price,
        )

    async def _seed_buy_quote(
        self, session: AsyncSession, profile: GridProfile, buy_price: Decimal,
        quote_amount: Decimal, info: InstrumentInfo, *, order_role: str, range_id: int,
    ) -> None:
        qty = self.qty_from_quote(quote_amount, buy_price, info)
        await self._place_and_store(
            session=session,
            profile=profile,
            side="Buy",
            price=buy_price,
            grid_buy_price=buy_price,
            qty=qty,
            replacement_for=None,
            order_role=order_role,
            range_id=range_id,
        )
        await session.commit()

    async def _place_market_and_store(
        self, *, session: AsyncSession, profile: GridProfile, qty: Decimal,
        quote_amount: Decimal, market_price: Decimal, grid_buy_price: Decimal,
        order_role: str, range_id: int | None,
    ) -> GridOrder:
        link_id = f"g{profile.id}-{uuid.uuid4().hex[:24]}"
        response = await self.exchange.place_market_order(
            symbol=profile.symbol,
            side="Buy",
            qty=quote_amount,
            order_link_id=link_id,
            market_unit="quoteCoin",
        )
        row = GridOrder(
            profile_id=profile.id,
            range_id=range_id,
            exchange_order_id=response["result"]["orderId"],
            order_link_id=link_id,
            symbol=profile.symbol,
            side="Buy",
            grid_buy_price=grid_buy_price,
            price=market_price,
            qty=qty,
            status="New",
            order_role=order_role,
        )
        session.add(row)
        await session.flush()
        return row

    async def _create_replacement(
        self,
        session: AsyncSession,
        profile: GridProfile,
        order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        if order.order_role in GRID_ORDER_ROLES and order.range_id != profile.current_range_id:
            record_strategy_event(
                session, profile_id=profile.id, event_type="ORDER_SYNCED",
                reason="REPLACEMENT_BLOCKED_STALE_RANGE",
                metadata={
                    "order_id": order.id, "order_range_id": order.range_id,
                    "current_range_id": profile.current_range_id,
                },
            )
            return
        grid_range = await session.get(GridRange, order.range_id) if order.range_id else None
        if grid_range is None:
            raise RuntimeError("cannot create Grid replacement without GridRange")
        filled_qty = Decimal(order.filled_qty or order.qty)
        grid_buy_price = Decimal(order.grid_buy_price)

        if (
            order.side == "Buy"
            and grid_buy_price < Decimal(grid_range.lower_price)
            and not getattr(profile, "sell_below_grid", False)
        ):
            order.order_role = "below_accumulation"
            order.replacement_created = True
            await session.commit()
            return

        if order.side == "Buy":
            side = "Sell"
            cells = dict(self.range_cells(profile, grid_range, info))
            raw_price = cells[grid_buy_price]
            qty = floor_to_step(
                filled_qty * (Decimal("1") - settings.grid_fee_buffer_pct),
                info.base_precision,
            )
        else:
            side = "Buy"
            raw_price = grid_buy_price
            qty = floor_to_step(filled_qty, info.base_precision)

        price = floor_to_step(raw_price, info.tick_size)
        self.validate_order(price, qty, info)

        await self._place_and_store(
            session=session,
            profile=profile,
            side=side,
            price=price,
            grid_buy_price=grid_buy_price,
            qty=qty,
            replacement_for=order.exchange_order_id,
            order_role=order.order_role,
            range_id=order.range_id,
        )
        order.replacement_created = True
        await session.commit()

        logger.info(
            "Profile %s: filled %s @ %s -> %s @ %s",
            profile.id,
            order.side,
            order.price,
            side,
            price,
        )

    async def cancel_open_orders(self, session: AsyncSession, profile_id: int) -> None:
        await self.cancel_orders(session, profile_id)

    async def cancel_orders(
        self,
        session: AsyncSession,
        profile_id: int,
        *,
        range_id: int | None = None,
        side: str | None = None,
        order_roles: set[str] | None = None,
        local_status: str = "Cancelled",
    ) -> None:
        conditions = [
            GridOrder.profile_id == profile_id,
            GridOrder.status.in_(OPEN_STATUSES),
        ]
        if range_id is not None:
            conditions.append(GridOrder.range_id == range_id)
        if side is not None:
            conditions.append(GridOrder.side == side)
        if order_roles is not None:
            conditions.append(GridOrder.order_role.in_(order_roles))
        result = await session.execute(
            select(GridOrder).where(*conditions)
        )
        orders = list(result.scalars())
        for order in orders:
            try:
                await self.exchange.cancel_order(
                    order_id=order.exchange_order_id, symbol=order.symbol
                )
                suffix = local_status.removeprefix("Cancelled")
                order.status = f"CancelRequested{suffix}"
                await session.commit()
            except Exception:
                logger.exception("Could not cancel order %s", order.exchange_order_id)

    async def _place_and_store(
        self,
        *,
        session: AsyncSession,
        profile: GridProfile,
        side: str,
        price: Decimal,
        grid_buy_price: Decimal,
        qty: Decimal,
        replacement_for: str | None,
        order_role: str = "grid",
        range_id: int | None = None,
    ) -> GridOrder:
        link_id = f"g{profile.id}-{uuid.uuid4().hex[:24]}"
        response = await self.exchange.place_limit_order(
            symbol=profile.symbol,
            side=side,
            qty=qty,
            price=price,
            order_link_id=link_id,
        )
        result = response["result"]
        row = GridOrder(
            profile_id=profile.id,
            range_id=range_id,
            exchange_order_id=result["orderId"],
            order_link_id=link_id,
            symbol=profile.symbol,
            side=side,
            grid_buy_price=grid_buy_price,
            price=price,
            qty=qty,
            status="New",
            replacement_for=replacement_for,
            order_role=order_role,
        )
        session.add(row)
        await session.flush()
        return row

    @staticmethod
    def qty_from_quote(quote_amount: Decimal, price: Decimal, info: InstrumentInfo) -> Decimal:
        qty = floor_to_step(quote_amount / price, info.base_precision)
        GridEngine.validate_order(price, qty, info)
        return qty

    @staticmethod
    def validate_order(price: Decimal, qty: Decimal, info: InstrumentInfo) -> None:
        if qty <= 0:
            raise ValueError("Calculated order quantity is zero")
        amount = price * qty
        if amount < info.min_order_amt:
            raise ValueError(
                f"Order amount {amount} is below Bybit minOrderAmt={info.min_order_amt}"
            )
