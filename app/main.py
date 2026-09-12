from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.api.fomo_activity import router as fomo_activity_router
from app.core.config import settings
from app.db.init import init_db
from app.web.routes import router as web_router


def fomo_bridge_origins() -> list[str]:
    """Origins allowed to POST a session to this local API, or ``[]``.

    Optional manual session helper. The automatic browser collector imports
    public activity through Python and does not require this CORS bridge.
    This stays empty unless ``fomo_token_bridge`` is turned on.
    """
    return ["https://fomo.family"] if settings.fomo_token_bridge else []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Mini Grid Bot", version="0.7.0", lifespan=lifespan)

# When the token bridge is on, the fomo.family tab may POST /api/fomo/session
# from its own origin. Credentials stay off: this carries a bearer token in the
# body, not a cookie, so it needs no cross-origin credential handling.
_bridge_origins = fomo_bridge_origins()
if _bridge_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_bridge_origins,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=False,
    )

app.include_router(web_router)
app.include_router(api_router)
app.include_router(fomo_activity_router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
