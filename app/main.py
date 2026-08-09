from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.db.init import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Mini Grid Bot", version="0.1.0", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
