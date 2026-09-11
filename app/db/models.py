from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB


class Base(DeclarativeBase):
    pass


class GridProfile(Base):
    __tablename__ = "grid_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    exchange: Mapped[str] = mapped_column(
        String(16), nullable=False, default="bybit", server_default="bybit", index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="BTCUSDT", index=True)
    lower_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    upper_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    step_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    quote_per_level: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    regime_state: Mapped[str] = mapped_column(String(24), nullable=False, default="RANGE")
    break_down_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="continue"
    )
    breakout_confirm_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    breakout_ema_period: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    trailing_buy_deviation_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="fixed"
    )
    trailing_buy_deviation_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("2")
    )
    trailing_buy_target_quote: Mapped[Decimal] = mapped_column(
        Numeric(28, 12), nullable=False, default=Decimal("250")
    )
    trailing_buy_max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    trailing_buy_timeout_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=168)
    recovery_initial_stop_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("2.5")
    )
    recovery_trailing_activation_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("3")
    )
    recovery_trailing_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("1.5")
    )
    recovery_break_even_trigger_pct: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("1")
    )
    recovery_cooldown_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    pending_hard_stop: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    break_up_action: Mapped[str] = mapped_column(
        String(16), nullable=False, default="stop"
    )
    below_grid_lower_price: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 12), nullable=True
    )
    buy_below_grid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sell_below_grid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    strategy: Mapped[str] = mapped_column(String(24), nullable=False, default="accumulation")
    grid_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="arithmetic")
    step_percent: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    max_investment: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    initial_buy_percent: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, default=Decimal("20")
    )
    buy_ladder_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="linear")
    sell_ladder_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="linear")
    ladder_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("1.5")
    )
    current_range_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_ranges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    orders: Mapped[list["GridOrder"]] = relationship(back_populates="profile")
    ranges: Mapped[list["GridRange"]] = relationship(
        back_populates="profile",
        foreign_keys="GridRange.profile_id",
        cascade="all, delete-orphan",
    )
    current_range: Mapped["GridRange | None"] = relationship(
        foreign_keys=[current_range_id], post_update=True
    )
    position_lots: Mapped[list["PositionLot"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    breakdown_episodes: Mapped[list["BreakdownEpisode"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class GridRange(Base):
    __tablename__ = "grid_ranges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lower_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    upper_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    step_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    grid_mode: Mapped[str] = mapped_column(String(24), nullable=False, default="arithmetic")
    step_percent: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped[GridProfile] = relationship(
        back_populates="ranges", foreign_keys=[profile_id]
    )
    orders: Mapped[list["GridOrder"]] = relationship(back_populates="grid_range")


class GridOrder(Base):
    __tablename__ = "grid_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    range_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_ranges.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # NULL between the "Created" row's own commit and the venue call that
    # assigns it (see GridEngine._place_and_store); unique still holds since
    # Postgres does not compare NULLs equal to each other.
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True, index=True)
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    grid_buy_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    replacement_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replacement_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    order_role: Mapped[str] = mapped_column(String(32), nullable=False, default="grid")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    profile: Mapped[GridProfile] = relationship(back_populates="orders")
    grid_range: Mapped[GridRange | None] = relationship(back_populates="orders")
    executions: Mapped[list["GridExecution"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class BreakdownEpisode(Base):
    __tablename__ = "breakdown_episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_range_id: Mapped[int] = mapped_column(
        ForeignKey("grid_ranges.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="TRACKING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(48), nullable=True)

    profile: Mapped[GridProfile] = relationship(back_populates="breakdown_episodes")
    source_range: Mapped[GridRange] = relationship()
    recovery_trades: Mapped[list["RecoveryTrade"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


class RecoveryTrade(Base):
    __tablename__ = "recovery_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    breakdown_episode_id: Mapped[int] = mapped_column(
        ForeignKey("breakdown_episodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_range_id: Mapped[int] = mapped_column(
        ForeignKey("grid_ranges.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="TRACKING", index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lowest_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    trigger_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_orders.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    exit_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_orders.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    entry_link_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    exit_link_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    entry_qty: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    highest_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    initial_stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    effective_stop_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    episode: Mapped[BreakdownEpisode] = relationship(back_populates="recovery_trades")
    source_range: Mapped[GridRange] = relationship()
    entry_order: Mapped[GridOrder | None] = relationship(foreign_keys=[entry_order_id])
    exit_order: Mapped[GridOrder | None] = relationship(foreign_keys=[exit_order_id])


class GridExecution(Base):
    __tablename__ = "grid_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("grid_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    exec_id: Mapped[str] = mapped_column(String(96), unique=True, nullable=False, index=True)
    exec_price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    exec_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    exec_value: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    exec_fee: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, default=Decimal("0"))
    fee_currency: Mapped[str | None] = mapped_column(String(24), nullable=True)
    fee_rate: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    is_maker: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exec_time_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # On-chain fills pay gas in the chain's native coin, which is neither the
    # base nor the quote of the pair. `exec_fee`/`fee_currency` stay the
    # quote-denominated figure PnL consumes; these keep the original numbers so
    # the conversion can always be re-derived or audited.
    fee_native_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    fee_native_coin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)

    order: Mapped[GridOrder] = relationship(back_populates="executions")
    position_lot: Mapped["PositionLot | None"] = relationship(
        back_populates="source_execution", uselist=False
    )


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "interval", "timestamp_ms", name="uq_market_candle_series_time"
        ),
        Index("ix_market_candles_lookup", "symbol", "interval", "timestamp_ms"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="60")
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    # Nullable because DEX-sampled candles have honest OHLC but no per-minute
    # volume: DexScreener only reports a rolling 24h figure, and dividing it by
    # 1440 would be a fabricated number sitting in the same column as real ones.
    volume: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    turnover: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="exchange", server_default="exchange"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DexPriceObservation(Base):
    """Raw price samples that 1m DEX candles are folded out of.

    Kept separate from ``market_candles`` so a re-aggregation never has to go
    back to the upstream API, and so a gap in sampling is visible as missing
    rows rather than as a flat candle.
    """

    __tablename__ = "dex_price_observations"
    __table_args__ = (
        UniqueConstraint("symbol", "timestamp_ms", name="uq_dex_observation_symbol_time"),
        Index("ix_dex_observations_lookup", "symbol", "timestamp_ms"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price_quote: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    liquidity_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    volume_h24_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    # Wide enough for a v4 pool id (32 bytes, 66 chars as 0x-hex), not just a
    # 20-byte pool address -- the main PONS/USDG market is a v4 pool.
    pair_address: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DexWallet(Base):
    """Nonce bookkeeping for one trading wallet.

    The row exists to be locked: reserving a nonce happens inside the same
    transaction that records the intent using it, so two workers (or a worker
    and a manual script) cannot hand the same nonce to two transactions.
    """

    __tablename__ = "dex_wallets"

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    next_nonce: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DexIntent(Base):
    """A synthetic limit order: what the grid asked for, and its on-chain trace.

    Status vocabulary and legal transitions live in ``app.dex.intents``. Every
    column from ``wallet_address`` down exists so that a worker restarting mid
    flight can tell "signed but maybe not broadcast" from "never signed" and
    re-broadcast the *same* transaction instead of buying twice.
    """

    __tablename__ = "dex_intents"
    __table_args__ = (
        Index("ix_dex_intents_status_symbol", "status", "symbol"),
        UniqueConstraint("wallet_address", "nonce", name="uq_dex_intent_wallet_nonce"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=True, index=True
    )
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="WAITING", index=True)

    # What the level promises: fill at `limit_price` or better, never worse.
    limit_price: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_in: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_in_coin: Mapped[str] = mapped_column(String(24), nullable=False)
    min_amount_out: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)

    # Market health when the level was armed -- the baseline collapse guards
    # compare against (see app/dex/risk.py).
    baseline_liquidity_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    baseline_volume_h24_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- signing / broadcast trace -------------------------------------
    wallet_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    # The ERC-20 -> Permit2 approval this swap had to send first, if any.
    approval_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    nonce: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Hash of the signed payload, known before broadcast; the recovery key.
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, unique=True, index=True)
    # Held only between signing and confirmation so a crashed worker can
    # re-broadcast verbatim; cleared once the receipt is in.
    raw_tx: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    block_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Set when this row replaces an attempt that had to be abandoned, so a
    # retried level keeps its history instead of looking like a fresh order.
    parent_intent_id: Mapped[int | None] = mapped_column(
        ForeignKey("dex_intents.id", ondelete="SET NULL"), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Realised fill, read back from the receipt rather than from the quote.
    filled_amount_in: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    filled_amount_out: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    gas_native: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    gas_native_coin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Gas valued in the pair's quote currency, with the rate it was valued at,
    # so PnL can sum it without the original figure being lost.
    gas_quote: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    gas_quote_coin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    native_quote_rate: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)

    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("grid_executions.id", ondelete="SET NULL"), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PositionLot(Base):
    __tablename__ = "position_lots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_execution_id: Mapped[int] = mapped_column(
        ForeignKey("grid_executions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    origin_type: Mapped[str] = mapped_column(String(24), nullable=False, default="GRID")
    owner_type: Mapped[str] = mapped_column(String(24), nullable=False, default="GRID")
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    acquired_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    remaining_qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    cost_quote: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    fees_quote: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    profile: Mapped[GridProfile] = relationship(back_populates="position_lots")
    source_execution: Mapped[GridExecution] = relationship(back_populates="position_lot")
    consumptions: Mapped[list["LotConsumption"]] = relationship(
        back_populates="lot", cascade="all, delete-orphan"
    )


class LotConsumption(Base):
    __tablename__ = "lot_consumptions"
    __table_args__ = (UniqueConstraint("lot_id", "sell_execution_id", name="uq_lot_consumption_execution"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lot_id: Mapped[int] = mapped_column(
        ForeignKey("position_lots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sell_execution_id: Mapped[int] = mapped_column(
        ForeignKey("grid_executions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lot: Mapped[PositionLot] = relationship(back_populates="consumptions")
    sell_execution: Mapped[GridExecution] = relationship()


class StrategyRecommendation(Base):
    __tablename__ = "strategy_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StrategyEvent(Base):
    __tablename__ = "strategy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    market_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FomoTrader(Base):
    """A FOMO identity, joined to the on-chain wallet it actually trades from.

    ``evm_address`` is what makes this table useful: without it a FOMO trader
    is just a leaderboard row with no way to watch what they do on chain.
    """

    __tablename__ = "fomo_traders"

    fomo_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evm_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    user_handle: Mapped[str | None] = mapped_column(String(120), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Where evm_address came from: "leaderboard", "holders", "trade", or
    # "manual" for a hand-seeded fallback row.
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="leaderboard")
    # NULL means never backfilled -- the queue chain_tape drains before it
    # advances the global cursor. Set once the wallet's recent history has
    # been scanned, so the trade that got this wallet noticed is not lost.
    backfilled_from_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FomoTraderRank(Base):
    """Rank history, not a snapshot -- rank #5 today says nothing about rank
    #5 a month ago, so overwriting a single row would throw that away."""

    __tablename__ = "fomo_trader_ranks"

    fomo_user_id: Mapped[str] = mapped_column(
        ForeignKey("fomo_traders.fomo_user_id", ondelete="CASCADE"), primary_key=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now()
    )
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ChainTransaction(Base):
    """Raw ERC-20 ``Transfer`` legs for one transaction, kept independent of
    however ``classify()`` currently reads them.

    Exists for one reason: if the classification logic turns out to be wrong
    for some Universal Router path, the fix must not require re-reading the
    chain for the whole history. ``scripts/rebuild-chain-swaps.py`` replays
    this table through the current ``classify()`` with zero RPC calls.
    """

    __tablename__ = "chain_transactions"

    chain_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tx_hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Every Transfer leg observed for this tx, exactly as decoded from logs.
    transfers: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chain_transactions_block", "chain_id", "block_number"),
    )


class ChainSwap(Base):
    """One reconstructed BUY/SELL for one tracked wallet, read straight off
    the chain -- the source of trade truth this app relies on, not FOMO.

    A single transaction can touch more than one tracked wallet (e.g. a
    router batching two users' swaps), hence the composite key rather than
    keying on ``tx_hash`` alone.
    """

    __tablename__ = "chain_swaps"

    tx_hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    wallet_address: Mapped[str] = mapped_column(String(42), primary_key=True)
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    token_address: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY | SELL
    token_amount: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    quote_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    quote_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quote_amount: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    value_usd: Mapped[Decimal | None] = mapped_column(Numeric(38, 12), nullable=True)
    # QUOTE_LEG (priced off the swap's own USD-pegged leg -- the most honest
    # figure available), MARKET_PRICE (fallback to a price feed), or
    # UNPRICED. Kept per-row so aggregates can separate measured from
    # estimated volume rather than silently blending them.
    pricing_source: Mapped[str] = mapped_column(String(16), nullable=False, default="UNPRICED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chain_swaps_token_time", "token_address", "block_time_ms"),
        Index("ix_chain_swaps_wallet_time", "wallet_address", "block_time_ms"),
    )


class ChainScanCursor(Base):
    """How far the tape has scanned, per chain and per scope.

    ``scope`` is ``"realtime"`` for the main forward scan; a wallet backfill
    does not use this table at all -- it is bounded and one-shot, tracked
    instead by ``FomoTrader.backfilled_from_block``.
    """

    __tablename__ = "chain_scan_cursors"

    chain_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(24), primary_key=True)
    last_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


Index("ix_grid_orders_profile_range", GridOrder.profile_id, GridOrder.range_id)
