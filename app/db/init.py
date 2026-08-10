from decimal import Decimal

from sqlalchemy import select, text

from app.db.models import Base, GridProfile
from app.db.session import SessionLocal, engine


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all does not add columns to an existing PostgreSQL table.
        # Keep this small, additive migration here until the project adopts Alembic.
        for statement in (
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
            "ALTER TABLE grid_orders ADD COLUMN IF NOT EXISTS order_role VARCHAR(32) NOT NULL DEFAULT 'grid'",
        ):
            await conn.execute(text(statement))

    # Handy first profile for a fresh demo database.
    async with SessionLocal() as session:
        existing = await session.scalar(select(GridProfile.id).limit(1))
        if existing is None:
            session.add(
                GridProfile(
                    name="BTC 62–67k",
                    enabled=False,
                    symbol="BTCUSDT",
                    lower_price=Decimal("62000"),
                    upper_price=Decimal("67000"),
                    step_price=Decimal("1000"),
                    quote_per_level=Decimal("25"),
                )
            )
            await session.commit()
