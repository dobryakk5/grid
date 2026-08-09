from decimal import Decimal

from sqlalchemy import select

from app.db.models import Base, GridProfile
from app.db.session import SessionLocal, engine


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
