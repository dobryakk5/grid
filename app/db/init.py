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
from app.exchanges.base import split_symbol


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
                    base_coin, quote_coin = split_symbol(buy.symbol)
                    acquired = Decimal(execution.exec_qty)
                    if (execution.fee_currency or "").upper() == base_coin:
                        acquired -= Decimal(execution.exec_fee or 0)
                    remaining = max(min(acquired, acquired - sold), Decimal("0"))
                    sold -= max(acquired - remaining, Decimal("0"))
                    if remaining <= 0:
                        continue
                    quote_fee = (
                        Decimal(execution.exec_fee or 0)
                        if (execution.fee_currency or "").upper() == quote_coin
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


# Arbitrary, fixed, and only ever used here. Postgres advisory locks are
# keyed by a bigint the application picks; this one means "schema init".
_INIT_LOCK_KEY = 0x6772_6964_696E_6974  # "gridinit"


async def init_db() -> None:
    """Bring the schema and seed rows up to date, one process at a time.

    Every service calls this on start, and a deploy restarts six of them
    within the same second. Nothing here was written for that: ``create_all``
    checks whether a table exists and then creates it, so two processes can
    both see "missing" and the loser dies on a duplicate ``pg_type`` row --
    which is exactly how the DEX worker and the tape crashed on the deploy
    that added ``dex_wallet_tokens``. The seed profile and both bootstraps
    below are the same check-then-write shape, one level up: two concurrent
    starts could give a profile two current ranges.

    So the whole function runs under a Postgres advisory lock. The first
    process does the work; the rest wait, then find everything already in
    place, which every step here already handles. A session-level lock on a
    connection of its own, because the steps below use their own sessions --
    it is released on unlock, or by Postgres itself if the process dies
    holding it. That costs one pooled connection for the duration, so the
    pool must allow at least two (the default allows five).
    """
    async with engine.connect() as lock:
        await lock.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _INIT_LOCK_KEY})
        try:
            await _init_db()
        finally:
            await lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _INIT_LOCK_KEY})


async def _init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not add columns to an existing PostgreSQL table.
        # Keep additive migrations here until the project adopts Alembic.
        for statement in (
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS exchange VARCHAR(16) NOT NULL DEFAULT 'bybit'",
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
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS level_size_multiplier NUMERIC(12, 6) NOT NULL DEFAULT 1",
            "ALTER TABLE grid_profiles ADD COLUMN IF NOT EXISTS current_range_id INTEGER REFERENCES grid_ranges(id) ON DELETE SET NULL",
            "ALTER TABLE grid_orders ADD COLUMN IF NOT EXISTS order_role VARCHAR(32) NOT NULL DEFAULT 'grid'",
            "ALTER TABLE grid_orders ADD COLUMN IF NOT EXISTS range_id INTEGER REFERENCES grid_ranges(id) ON DELETE SET NULL",
            # A "Created" row is committed before the venue assigns an id
            # (GridEngine._place_and_store); NULL is the in-flight state.
            "ALTER TABLE grid_orders ALTER COLUMN exchange_order_id DROP NOT NULL",
            "ALTER TABLE recovery_trades ADD COLUMN IF NOT EXISTS exit_reason VARCHAR(48)",
            # On-chain fills: native gas fee kept next to its quote conversion.
            "ALTER TABLE grid_executions ADD COLUMN IF NOT EXISTS fee_native_amount NUMERIC(38, 18)",
            "ALTER TABLE grid_executions ADD COLUMN IF NOT EXISTS fee_native_coin VARCHAR(24)",
            "ALTER TABLE grid_executions ADD COLUMN IF NOT EXISTS tx_hash VARCHAR(66)",
            "CREATE INDEX IF NOT EXISTS ix_grid_executions_tx_hash ON grid_executions(tx_hash)",
            # DEX-sampled candles carry honest OHLC but no per-minute volume.
            "ALTER TABLE market_candles ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'exchange'",
            "ALTER TABLE market_candles ALTER COLUMN volume DROP NOT NULL",
            "ALTER TABLE market_candles ALTER COLUMN turnover DROP NOT NULL",
            # Realised swap fills, added after dex_intents first shipped.
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS filled_amount_in NUMERIC(38, 18)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS filled_amount_out NUMERIC(38, 18)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS fill_price NUMERIC(38, 18)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS gas_native NUMERIC(38, 18)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS gas_native_coin VARCHAR(24)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS approval_tx_hash VARCHAR(66)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS gas_quote NUMERIC(38, 18)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS gas_quote_coin VARCHAR(24)",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS native_quote_rate NUMERIC(38, 18)",
            # Waiving the liquidity floors, recorded per level rather than globally.
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS ignore_liquidity_gate "
            "BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS parent_intent_id INTEGER REFERENCES dex_intents(id) ON DELETE SET NULL",
            "ALTER TABLE dex_intents ADD COLUMN IF NOT EXISTS token_address VARCHAR(42)",
            # Levels armed before the column: pin each to the one known
            # contract its key names -- ticker and address prefix both. A key
            # that fits two contracts stays unpinned rather than guessed.
            """UPDATE dex_intents i SET token_address = m.address
               FROM (
                   SELECT i2.id, min(t.address) AS address
                   FROM dex_intents i2
                   JOIN (SELECT address, symbol FROM dex_wallet_tokens
                         UNION SELECT address, symbol FROM chain_tokens) t
                     ON lower(t.address) LIKE '0x' || lower(substring(i2.symbol from '-([0-9A-Fa-f]{8})')) || '%'
                    AND left(upper(regexp_replace(coalesce(t.symbol, ''), '[^A-Za-z0-9]', '', 'g')), 12)
                        = split_part(i2.symbol, '-', 1)
                   WHERE i2.token_address IS NULL AND i2.symbol ~ '-[0-9A-Fa-f]{8}'
                   GROUP BY i2.id
                   HAVING count(DISTINCT lower(t.address)) = 1
               ) m
               WHERE i.id = m.id""",
            # v4 pool ids are 32 bytes, not a 20-byte address.
            "ALTER TABLE dex_price_observations ALTER COLUMN pair_address TYPE VARCHAR(80)",
            "CREATE INDEX IF NOT EXISTS ix_grid_profiles_current_range_id ON grid_profiles(current_range_id)",
            "CREATE INDEX IF NOT EXISTS ix_grid_orders_profile_range ON grid_orders(profile_id, range_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_grid_ranges_one_active_per_profile ON grid_ranges(profile_id) WHERE status = 'ACTIVE'",
            # chain_swaps was first keyed (tx_hash, wallet_address), which
            # cannot hold a token-for-token swap: one wallet, one tx, two
            # positions changed. Widen the key to include the token.
            """DO $$ BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    WHERE t.relname = 'chain_swaps' AND c.contype = 'p'
                      AND array_length(c.conkey, 1) = 2
                ) THEN
                    ALTER TABLE chain_swaps DROP CONSTRAINT chain_swaps_pkey;
                    ALTER TABLE chain_swaps ADD PRIMARY KEY (tx_hash, wallet_address, token_address);
                END IF;
            END $$;""",
            # Narrower market windows, added after the card first shipped: an
            # hour of volume separates "оживает" from "торговалось вчера".
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS volume_h1_usd NUMERIC(38, 6)",
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS buys_h6 INTEGER",
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS sells_h6 INTEGER",
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS buys_h1 INTEGER",
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS sells_h1 INTEGER",
            # Ответ DexScreener отдаёт не больше 30 пулов на запрос и не
            # сообщает, что обрезал: снимок обязан отличать «столько и есть» от
            # «не меньше столько».
            "ALTER TABLE token_snapshots ADD COLUMN IF NOT EXISTS pools_capped BOOLEAN",
            # Which wallets the tape actually scans. Everything ever met stays
            # in the table; only the top of this ranking is followed on chain.
            "ALTER TABLE fomo_traders ADD COLUMN IF NOT EXISTS volume_rank INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_fomo_traders_volume_rank ON fomo_traders (volume_rank)",
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
