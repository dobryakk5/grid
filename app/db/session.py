from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def database_target() -> str:
    """``database@host`` the API writes to, for the CLI to print.

    The import runs against whatever database the *server* was started with,
    which is not necessarily the one in the caller's ``.env``: a stray
    ``DATABASE_URL=...grid_fomo_local`` in the shell that launched uvicorn is
    enough to quietly land a whole import on a laptop. Never includes the
    user or password.
    """
    url = engine.url
    return f"{url.database}@{url.host or 'local'}"
