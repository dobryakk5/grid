from decimal import Decimal

from sqlalchemy import select, text

from app.db.models import (
    Base,
    GridExecution,
    GridOrder,
    GridProfile,
    GridRange,
    PositionLot,
    StrategyEvent,
)
from app.db.session import SessionLocal, engine


OPEN_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Created"}


def _new_range(profile: GridProfile) -> GridRange:
    return GridRange(
        profile_id=profile.id,
        lower_price=profile.lower_price,
        upper_price=profile.upper_price,
        step_price=profile.step_price,
        grid_mode=profile.grid_mode,
        step_percent=profile.step_percent,
        status="ACTIVE",
    )


async def bootstrap_current_ranges() -> None:
    """Give pre-range profiles exactly one current range without rewriting history."""
    async with SessionLocal() as session:
        profiles = list((await session.execute(select(GridProfile))).scalars())
        changed = False
        for profile in profiles:
            if profile.current_range_id is not None:
                continue
            grid_range = _new_range(profile)
            session.add(grid_range)
            await session.flush()
            profile.current_range_id = grid_range.id
            session.add(StrategyEvent(
                profile_id=profile.id,
                event_type="GRID_RANGE_CREATED",
                to_state="ACTIVE",
                reason="BOOTSTRAP_CURRENT_RANGE",
                event_metadata={"range_id": grid_range.id},
            ))
            session.add(StrategyEvent(
                profile_id=profile.id,
                event_type="GRID_RANGE_ACTIVATED",
                to_state="ACTIVE",
                reason="BOOTSTRAP_CURRENT_RANGE",
                event_metadata={"range_id": grid_range.id},
            ))

            orders = list((await session.execute(
                select(GridOrder).where(GridOrder.profile_id == profile.id)
            )).scalars())
            by_exchange_id = {order.exchange_order_id: order for order in orders}
            live = [
                order for order in orders
                if order.status in OPEN_STATUSES
                or (
                    order.side == "Buy" and order.status == "Filled"
                    and (not order.replacement_created or order.order_role == "below_accumulation")
                )
            ]
            for order in live:
                # The live leaf and only its ancestry are trustworthy. Historical
                # completed chains intentionally stay unassigned.
                current: GridOrder | None = order
                visited: set[str] = set()
                while current is not None and current.exchange_order_id not in visited:
                    visited.add(current.exchange_order_id)
                    if current.range_id is None:
                        current.range_id = grid_range.id
                    current = by_exchange_id.get(current.replacement_for or "")
            changed = True
        if changed:
            await session.commit()


def _base_coin(symbol: str) -> str:
    for quote in ("USDT", "USDC", "BTC", "ETH"):
        if symbol.upper().endswith(quote):
            return symbol.upper()[:-len(quote)]
    return ""


async def bootstrap_live_position_lots() -> None:
    """Create lots only for inventory still represented by the current range."""
    async with SessionLocal() as session:
        profiles = list((await session.execute(select(GridProfile))).scalars())
        changed = False
        for profile in profiles:
            if profile.current_range_id is None:
                continue
            orders = list((await session.execute(
                select(GridOrder).where(
                    GridOrder.profile_id == profile.id,
                    GridOrder.range_id == profile.current_range_id,
                )
            )).scalars())
            by_exchange_id = {order.exchange_order_id: order for order in orders}
            executions = list((await session.execute(
                select(GridExecution).join(GridOrder).where(
                    GridOrder.profile_id == profile.id,
                    GridOrder.range_id == profile.current_range_id,
                )
            )).scalars())
            executions_by_order: dict[int, list[GridExecution]] = {}
            for execution in executions:
                executions_by_order.setdefault(execution.order_id, []).append(execution)
            existing = set((await session.execute(
                select(PositionLot.source_execution_id).where(PositionLot.profile_id == profile.id)
            )).scalars())

            for buy in (order for order in orders if order.side == "Buy" and order.status == "Filled"):
                buy_executions = executions_by_order.get(buy.id, [])
                if not buy_executions:
                    continue
                sold = Decimal("0")
                for sell in (item for item in orders if item.side == "Sell"):
                    parent = sell.replacement_for
                    visited: set[str] = set()
                    while parent and parent not in visited:
                        visited.add(parent)
                        ancestor = by_exchange_id.get(parent)
                        if ancestor is None:
                            break
                        if ancestor.exchange_order_id == buy.exchange_order_id:
                            sold += sum(
                                (Decimal(e.exec_qty) for e in executions_by_order.get(sell.id, [])),
                                Decimal("0"),
                            )
                            break
                        parent = ancestor.replacement_for
                for execution in sorted(buy_executions, key=lambda item: item.id):
                    if execution.id in existing:
                        continue
                    acquired = Decimal(execution.exec_qty)
                    if (execution.fee_currency or "").upper() == _base_coin(buy.symbol):
                        acquired -= Decimal(execution.exec_fee or 0)
                    remaining = max(min(acquired, acquired - sold), Decimal("0"))
                    sold -= max(acquired - remaining, Decimal("0"))
                    if remaining <= 0:
                        continue
                    quote_fee = (
                        Decimal(execution.exec_fee or 0)
                        if (execution.fee_currency or "").upper() in {"USDT", "USDC"}
                        else Decimal("0")
                    )
                    session.add(PositionLot(
                        profile_id=profile.id,
                        source_execution_id=execution.id,
                        origin_type="GRID",
                        owner_type="GRID",
                        owner_id=buy.range_id,
                        acquired_qty=acquired,
                        remaining_qty=remaining,
                        cost_quote=Decimal(execution.exec_value) + quote_fee,
                        fees_quote=quote_fee,
                        status="OPEN",
                    ))
                    session.add(StrategyEvent(
                        profile_id=profile.id,
                        event_type="POSITION_LOT_CREATED",
                        reason="BOOTSTRAP_LIVE_INVENTORY",
                        event_metadata={"source_execution_id": execution.id},
                    ))
                    changed = True
        if changed:
            await session.commit()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not add columns to an existing PostgreSQL table.
        # Keep additive migrations here until the project adopts Alembic.
        for statement in (
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS regime_state VARCHAR(24) NOT NULL DEFAULT 'RANGE'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS break_down_action VARCHAR(16) NOT NULL DEFAULT 'continue'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS breakout_confirm_bars INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS breakout_ema_period INTEGER NOT NULL DEFAULT 50",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS trailing_buy_deviation_mode VARCHAR(16) NOT NULL DEFAULT 'fixed'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS trailing_buy_deviation_pct NUMERIC(8, 4) NOT NULL DEFAULT 2",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS trailing_buy_target_quote NUMERIC(28, 12) NOT NULL DEFAULT 250",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS trailing_buy_max_attempts INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS trailing_buy_timeout_hours INTEGER NOT NULL DEFAULT 168",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS recovery_initial_stop_pct NUMERIC(8, 4) NOT NULL DEFAULT 2.5",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS recovery_trailing_activation_pct NUMERIC(8, 4) NOT NULL DEFAULT 3",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS recovery_trailing_pct NUMERIC(8, 4) NOT NULL DEFAULT 1.5",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS recovery_break_even_trigger_pct NUMERIC(8, 4) NOT NULL DEFAULT 1",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS recovery_cooldown_bars INTEGER NOT NULL DEFAULT 4",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS pending_hard_stop BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS break_up_action VARCHAR(16) NOT NULL DEFAULT 'stop'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS below_grid_lower_price NUMERIC(28, 12)",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS buy_below_grid BOOLEAN NOT NULL DEFAULT TRUE",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS sell_below_grid BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS strategy VARCHAR(24) NOT NULL DEFAULT 'accumulation'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS grid_mode VARCHAR(24) NOT NULL DEFAULT 'arithmetic'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS step_percent NUMERIC(12, 6)",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS max_investment NUMERIC(28, 12)",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS stop_loss NUMERIC(28, 12)",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS take_profit NUMERIC(28, 12)",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS initial_buy_percent NUMERIC(8, 4) NOT NULL DEFAULT 20",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS buy_ladder_mode VARCHAR(24) NOT NULL DEFAULT 'linear'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS sell_ladder_mode VARCHAR(24) NOT NULL DEFAULT 'linear'",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS ladder_multiplier NUMERIC(12, 6) NOT NULL DEFAULT 1.5",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS current_range_id INTEGER REFERENCES grid_ranges(id) ON DELETE SET NULL",
            "ALTER TABLE grid_orders ADD COLUMN IF NOT EXISTS order_role VARCHAR(32) NOT NULL DEFAULT 'grid'",
            "ALTER TABLE grid_orders ADD COLUMN IF NOT EXISTS range_id INTEGER REFERENCES grid_ranges(id) ON DELETE SET NULL",
            "ALTER TABLE recovery_trades ADD COLUMN IF NOT EXISTS exit_reason VARCHAR(48)",
            "CREATE INDEX IF NOT EXISTS ix_grid_profiles_current_range_id ON grid_profiles(current_range_id)",
            "CREATE INDEX IF NOT EXISTS ix_grid_orders_profile_range ON grid_orders(profile_id, range_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_grid_ranges_one_active_per_profile ON grid_ranges(profile_id) WHERE status = 'ACTIVE'",
        ):
            await conn.execute(text(statement))

    # Handy first profile for a fresh demo database.
    async with SessionLocal() as session:
        existing = await session.scalar(select(GridProfile.id).limit(1))
        if existing is None:
            session.add(GridProfile(
                name="BTC 62–67k", enabled=False, symbol="BTCUSDT",
                lower_price=Decimal("62000"), upper_price=Decimal("67000"),
                step_price=Decimal("1000"), quote_per_level=Decimal("25"),
                below_grid_lower_price=Decimal("55000"), buy_below_grid=True,
                sell_below_grid=False,
            ))
            await session.commit()

    await bootstrap_current_ranges()
    await bootstrap_live_position_lots()
