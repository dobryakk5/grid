from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    orders: Mapped[list["GridOrder"]] = relationship(back_populates="profile")


class GridOrder(Base):
    __tablename__ = "grid_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("grid_profiles.id", ondelete="CASCADE"), nullable=False, index=True
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
    executions: Mapped[list["GridExecution"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


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
