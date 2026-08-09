from sqlalchemy import select

from app.core.config import settings
from app.db.models import Base, BotState
from app.db.session import SessionLocal, engine


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        state = await session.get(BotState, 1)
        if state is None:
            session.add(
                BotState(
                    id=1,
                    enabled=False,
                    symbol=settings.grid_symbol.upper(),
                    levels=settings.grid_levels,
                    step_pct=settings.grid_step_pct,
                    quote_per_level=settings.grid_quote_per_level,
                )
            )
            await session.commit()
