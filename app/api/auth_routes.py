"""Login. Mounted without the auth dependency, for obvious reasons."""

import asyncio
import time

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.auth import auth_configured, bearer_of
from app.core.config import settings
from app.core.security import AuthError, issue_token, read_token, verify_password

router = APIRouter(prefix="/api/auth")

# scrypt already makes a guess expensive for us as well as for an attacker, so
# this is about not letting one open port become a password oracle at network
# speed. Per-process and in memory: a restart forgives, which is the right
# trade for a single-operator box that must never lock its owner out for good.
_ATTEMPT_WINDOW = 300.0
_MAX_ATTEMPTS = 8
_attempts: dict[str, list[float]] = {}


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _throttle(client: str) -> None:
    now = time.monotonic()
    recent = [t for t in _attempts.get(client, []) if now - t < _ATTEMPT_WINDOW]
    _attempts[client] = recent
    if len(recent) >= _MAX_ATTEMPTS:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Слишком много попыток входа; подождите несколько минут",
        )


def _record(client: str) -> None:
    _attempts.setdefault(client, []).append(time.monotonic())


@router.get("/status")
async def status_(request: Request) -> dict:
    """Whether auth exists here, and whether this caller already satisfies it."""
    if not auth_configured():
        return {"configured": False, "authenticated": False, "trading_enabled": False}
    try:
        read_token(settings.auth_secret, bearer_of(request))
        authenticated = True
    except AuthError:
        authenticated = False
    return {"configured": True, "authenticated": authenticated, "trading_enabled": True}


@router.post("/login")
async def login(payload: LoginRequest, request: Request) -> dict:
    if not auth_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Вход не настроен: задайте AUTH_SECRET и AUTH_PASSWORD_HASH",
        )
    client = request.client.host if request.client else "unknown"
    _throttle(client)
    if not verify_password(payload.password, settings.auth_password_hash):
        _record(client)
        # Same cost whether the password was close or nonsense.
        await asyncio.sleep(0.5)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный пароль")
    _attempts.pop(client, None)
    ttl = max(1, settings.auth_token_ttl_minutes) * 60
    return {"token": issue_token(settings.auth_secret, ttl_seconds=ttl), "expires_in": ttl}
