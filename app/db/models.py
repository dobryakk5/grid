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
    exchange_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
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
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
    turnover: Mapped[Decimal] = mapped_column(Numeric(38, 12), nullable=False)
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


Index("ix_grid_orders_profile_range", GridOrder.profile_id, GridOrder.range_id)
