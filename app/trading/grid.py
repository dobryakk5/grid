import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import GridOrder, GridProfile
from app.exchanges.bybit import BybitClient, InstrumentInfo
from app.trading.math import floor_to_step, grid_buy_levels

logger = logging.getLogger(__name__)

OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Created"}


class GridEngine:
    def __init__(self, exchange: BybitClient) -> None:
        self.exchange = exchange

    async def tick(self, session: AsyncSession) -> None:
        result = await session.execute(select(GridProfile).order_by(GridProfile.id))
        profiles = list(result.scalars())

        for profile in profiles:
            try:
                if not profile.enabled:
                    await self.cancel_open_orders(session, profile.id)
                    continue

                await self.refresh_open_orders(session, profile)
                await self.seed_missing_buy_orders(session, profile)
            except Exception:
                logger.exception("Profile %s (%s) tick failed", profile.id, profile.name)

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

            if order.status == "Filled" and not order.replacement_created:
                await self._create_replacement(session, profile, order, info)

        await session.commit()

    async def seed_missing_buy_orders(
        self, session: AsyncSession, profile: GridProfile
    ) -> None:
        info = await self.exchange.instrument_info(profile.symbol)
        market_price = await self.exchange.last_price(profile.symbol)

        buy_levels = [
            floor_to_step(level, info.tick_size)
            for level in grid_buy_levels(
                Decimal(profile.lower_price),
                Decimal(profile.upper_price),
                Decimal(profile.step_price),
            )
        ]

        result = await session.execute(
            select(GridOrder)
            .where(GridOrder.profile_id == profile.id)
            .order_by(GridOrder.id)
        )
        latest_by_cell: dict[Decimal, GridOrder] = {}
        for order in result.scalars():
            latest_by_cell[Decimal(order.grid_buy_price)] = order

        for buy_price in buy_levels:
            latest = latest_by_cell.get(buy_price)

            if latest is None:
                if buy_price < market_price:
                    await self._seed_buy(session, profile, buy_price, info)
                continue

            if latest.status in OPEN_STATUSES:
                continue

            # A filled order should normally already have its replacement. If a
            # replacement did not get committed, retry it instead of creating a
            # second independent cycle.
            if latest.status == "Filled" and not latest.replacement_created:
                await self._create_replacement(session, profile, latest, info)
                continue

            if latest.status in {"Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}:
                if latest.side == "Buy":
                    if buy_price < market_price:
                        await self._seed_buy(session, profile, buy_price, info)
                else:
                    # A cancelled SELL means this cell may still own BTC from the
                    # preceding BUY. Re-create the SELL instead of buying more.
                    sell_price = floor_to_step(
                        buy_price + Decimal(profile.step_price), info.tick_size
                    )
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
                    )
                    await session.commit()
                continue

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
        )
        await session.commit()
        logger.info(
            "Profile %s seeded BUY %s %s @ %s",
            profile.id,
            profile.symbol,
            qty,
            buy_price,
        )

    async def _create_replacement(
        self,
        session: AsyncSession,
        profile: GridProfile,
        order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        filled_qty = Decimal(order.filled_qty or order.qty)
        grid_buy_price = Decimal(order.grid_buy_price)

        if order.side == "Buy":
            side = "Sell"
            raw_price = grid_buy_price + Decimal(profile.step_price)
            if raw_price > Decimal(profile.upper_price):
                raise ValueError("replacement SELL would be above profile upper_price")
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
