from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router as api_router
from app.db.init import init_db
from app.web.routes import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Mini Grid Bot", version="0.5.0", lifespan=lifespan)
app.include_router(web_router)
app.include_router(api_router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
