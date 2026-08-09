from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    levels: Mapped[int] = mapped_column(Integer, nullable=False)
    step_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    quote_per_level: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    anchor_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GridOrder(Base):
    __tablename__ = "grid_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(28, 12), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    replacement_created: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    replacement_for: Mapped[str | None] = mapped_column(String(64), nullable=True)
    filled_qty: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
