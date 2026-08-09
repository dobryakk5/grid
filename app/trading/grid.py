import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import BotState, GridOrder
from app.exchanges.bybit import BybitClient, InstrumentInfo
from app.trading.math import buy_level, floor_to_step, one_step_down, one_step_up

logger = logging.getLogger(__name__)

OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Created"}
TERMINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"}


class GridEngine:
    def __init__(self, exchange: BybitClient) -> None:
        self.exchange = exchange

    async def tick(self, session: AsyncSession) -> None:
        state = await session.get(BotState, 1)
        if state is None:
            return

        if not state.enabled:
            await self.cancel_open_orders(session, state.symbol)
            return

        await self.refresh_open_orders(session, state)

        active_count = await session.scalar(
            select(func.count(GridOrder.id)).where(
                GridOrder.symbol == state.symbol,
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        if not active_count:
            await self.seed(session, state)

    async def seed(self, session: AsyncSession, state: BotState) -> None:
        info = await self.exchange.instrument_info(state.symbol)
        anchor = await self.exchange.last_price(state.symbol)
        state.anchor_price = anchor
        logger.info("Seeding %s grid at anchor=%s", state.symbol, anchor)

        for level in range(1, state.levels + 1):
            raw_price = buy_level(anchor, Decimal(state.step_pct), level)
            price = floor_to_step(raw_price, info.tick_size)
            qty = self.qty_from_quote(Decimal(state.quote_per_level), price, info)
            await self._place_and_store(
                session=session,
                symbol=state.symbol,
                side="Buy",
                price=price,
                qty=qty,
                replacement_for=None,
            )

        await session.commit()

    async def refresh_open_orders(self, session: AsyncSession, state: BotState) -> None:
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.symbol == state.symbol,
                GridOrder.status.in_(OPEN_STATUSES),
            )
        )
        orders = list(result.scalars())
        if not orders:
            return

        info = await self.exchange.instrument_info(state.symbol)

        for order in orders:
            remote = await self.exchange.get_order(
                order_id=order.exchange_order_id, symbol=order.symbol
            )
            if remote is None:
                logger.warning("Order %s not found on exchange", order.exchange_order_id)
                continue

            new_status = remote.get("orderStatus", order.status)
            order.status = new_status
            if remote.get("cumExecQty"):
                order.filled_qty = Decimal(remote["cumExecQty"])
            if remote.get("avgPrice"):
                order.avg_price = Decimal(remote["avgPrice"])

            if new_status == "Filled" and not order.replacement_created:
                await self._create_replacement(session, state, order, info)

        await session.commit()

    async def _create_replacement(
        self,
        session: AsyncSession,
        state: BotState,
        order: GridOrder,
        info: InstrumentInfo,
    ) -> None:
        step = Decimal(state.step_pct)
        filled_qty = Decimal(order.filled_qty or order.qty)

        if order.side == "Buy":
            side = "Sell"
            raw_price = one_step_up(Decimal(order.price), step)
            qty = floor_to_step(
                filled_qty * (Decimal("1") - settings.grid_fee_buffer_pct),
                info.base_precision,
            )
        else:
            side = "Buy"
            raw_price = one_step_down(Decimal(order.price), step)
            qty = floor_to_step(filled_qty, info.base_precision)

        price = floor_to_step(raw_price, info.tick_size)
        self.validate_order(price, qty, info)

        await self._place_and_store(
            session=session,
            symbol=order.symbol,
            side=side,
            price=price,
            qty=qty,
            replacement_for=order.exchange_order_id,
        )
        order.replacement_created = True
        logger.info(
            "Filled %s %s @ %s -> new %s %s @ %s",
            order.side,
            order.qty,
            order.price,
            side,
            qty,
            price,
        )

    async def cancel_open_orders(self, session: AsyncSession, symbol: str) -> None:
        result = await session.execute(
            select(GridOrder).where(
                GridOrder.symbol == symbol,
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
            except Exception:
                logger.exception("Could not cancel order %s", order.exchange_order_id)
        if orders:
            await session.commit()

    async def _place_and_store(
        self,
        *,
        session: AsyncSession,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        replacement_for: str | None,
    ) -> GridOrder:
        link_id = f"grid-{uuid.uuid4().hex[:24]}"
        response = await self.exchange.place_limit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_link_id=link_id,
        )
        result = response["result"]
        row = GridOrder(
            exchange_order_id=result["orderId"],
            order_link_id=link_id,
            symbol=symbol,
            side=side,
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
