import logging
import time
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import GridExecution, GridOrder, GridProfile
from app.exchanges.bybit import BybitClient, InstrumentInfo
from app.trading.math import (
    configured_grid_cells,
    dca_initial_percent,
    floor_to_step,
    ladder_allocations,
    strategy_grid_cells,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Created"}


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

                if not profile.enabled:
                    await self.cancel_open_orders(session, profile.id)
                    continue

                await self.update_regime(session, profile)
                if not profile.enabled:
                    await self.cancel_open_orders(session, profile.id)
                    continue
                if await self.apply_price_guards(session, profile):
                    continue
                await self.refresh_open_orders(session, profile)
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
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.side == "Buy",
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

    async def update_regime(self, session: AsyncSession, profile: GridProfile) -> str:
        """Pause a profile after two confirmed hourly closes outside its range."""
        now = time.monotonic()
        cached = self._regime_cache.get(profile.symbol)
        if cached is None or now - cached[0] >= 300:
            candles = await self.exchange.klines(profile.symbol, interval="60", limit=3)
            closes = [item["close"] for item in candles[:-1]][-2:]
            self._regime_cache[profile.symbol] = (now, closes)
        else:
            closes = cached[1]
        lower = Decimal(profile.lower_price)
        upper = Decimal(profile.upper_price)
        state = classify_regime(closes, lower, upper) or profile.regime_state
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
        profile.enabled = False
        await session.commit()
        await self.cancel_open_orders(session, profile.id)
        logger.warning("Profile %s stopped by price guard at %s", profile.id, market_price)
        return True

    async def refresh_open_orders(self, session: AsyncSession, profile: GridProfile) -> None:
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile.id,
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        orders = list(result.scalars())
        if not orders:
            return

        info = await self.exchange.instrument_info(profile.symbol)

        for order in orders:
            remote = await self.exchange.get_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
            if remote is None:
                logger.warning("Order %s not found on exchange", order.exchange_order_id)
                continue

            order.status = remote.get("orderStatus", order.status)
            if remote.get("cumExecQty"):
                order.filled_qty = Decimal(remote["cumExecQty"])
            if remote.get("avgPrice"):
                order.avg_price = Decimal(remote["avgPrice"])

            if order.status in {"PartiallyFilled", "Filled"}:
                await self.sync_order_executions(session, order)

            if order.status == "Filled" and not order.replacement_created:
                if profile.strategy == "dca" and order.order_role in {"dca_initial_buy", "dca_grid"}:
                    await self._create_dca_sell_ladder(session, profile, order, info)
                elif order.order_role == "dca_sell":
                    await self._create_dca_rebuy_if_ladder_complete(
                        session, profile, order, info
                    )
                elif (
                    order.side == "Buy"
                    and Decimal(order.grid_buy_price) < Decimal(profile.lower_price)
                    and not getattr(profile, "sell_below_grid", False)
                ):
                    order.order_role = "below_accumulation"
                    order.replacement_created = True
                else:
                    await self._create_replacement(session, profile, order, info)

        await session.commit()

    async def seed_dca_orders(self, session: AsyncSession, profile: GridProfile) -> None:
        info = await self.exchange.instrument_info(profile.symbol)
        market_price = await self.exchange.last_price(profile.symbol)
        lower = Decimal(profile.lower_price)
        upper = Decimal(profile.upper_price)
        budget = Decimal(profile.max_investment or 0)
        if budget <= 0:
            raise ValueError("DCA Grid requires max_investment")

        result = await session.execute(
            select(GridOrder).where(GridOrder.profile_id == profile.id).order_by(GridOrder.id)
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
            buy for buy, _ in reversed(self.profile_cells(profile, info))
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
                    order_role="dca_grid",
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
                        order_role="dca_grid",
                    )
                    return
                continue

    async def _create_dca_sell_ladder(
        self, session: AsyncSession, profile: GridProfile, order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        filled_qty = Decimal(order.filled_qty or order.qty)
        anchor = Decimal(order.avg_price or order.price)
        sell_prices = [
            sell for _, sell in self.profile_cells(profile, info) if sell > anchor
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
            session.add(
                GridExecution(
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
            )
            existing_ids.add(exec_id)
            inserted += 1

        if inserted:
            await session.flush()
            logger.info(
                "Stored %s execution(s) for order %s",
                inserted,
                order.exchange_order_id,
            )
        return inserted

    async def seed_missing_buy_orders(
        self, session: AsyncSession, profile: GridProfile
    ) -> None:
        info = await self.exchange.instrument_info(profile.symbol)
        market_price = await self.exchange.last_price(profile.symbol)

        cells = self.profile_cells(profile, info)

        result = await session.execute(
            select(GridOrder)
            .where(GridOrder.profile_id == profile.id)
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
                    await self._seed_market_buy(session, profile, buy_price, market_price, info)
                    return
                if buy_price < market_price:
                    await self._seed_buy(session, profile, buy_price, info)
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
                    and buy_price < Decimal(profile.lower_price)
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
                        await self._seed_buy(session, profile, buy_price, info)
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
                    )
                    await session.commit()
                    return
                continue

    @staticmethod
    def profile_cells(profile: GridProfile, info: InstrumentInfo) -> list[tuple[Decimal, Decimal]]:
        raw = configured_grid_cells(profile)
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
        market_price: Decimal, info: InstrumentInfo,
    ) -> None:
        qty = self.qty_from_quote(Decimal(profile.quote_per_level), market_price, info)
        link_id = f"g{profile.id}-{uuid.uuid4().hex[:24]}"
        response = await self.exchange.place_market_order(
            symbol=profile.symbol, side="Buy", qty=qty, order_link_id=link_id
        )
        session.add(GridOrder(
            profile_id=profile.id,
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
                if buy_price < Decimal(profile.lower_price) else "grid"
            ),
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
        quote_amount: Decimal, info: InstrumentInfo, *, order_role: str,
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
        )
        await session.commit()

    async def _place_market_and_store(
        self, *, session: AsyncSession, profile: GridProfile, qty: Decimal,
        quote_amount: Decimal, market_price: Decimal, grid_buy_price: Decimal,
        order_role: str,
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
        filled_qty = Decimal(order.filled_qty or order.qty)
        grid_buy_price = Decimal(order.grid_buy_price)

        if (
            order.side == "Buy"
            and grid_buy_price < Decimal(profile.lower_price)
            and not getattr(profile, "sell_below_grid", False)
        ):
            order.order_role = "below_accumulation"
            order.replacement_created = True
            await session.commit()
            return

        if order.side == "Buy":
            side = "Sell"
            cells = dict(self.profile_cells(profile, info))
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
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.profile_id == profile_id,
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        orders = list(result.scalars())
        for order in orders:
            try:
                await self.exchange.cancel_order(
                    order_id=order.exchange_order_id, symbol=order.symbol
                )
                order.status = "Cancelled"
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
