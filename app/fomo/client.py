"""Async client for FOMO's internal API (``prod-api.fomo.family``).

Not a public developer API: it is the same backend the fomo.family web client
and its own mobile app call, reverse-engineered from authenticated traffic by
third-party tools (see ``README.md`` for the sources). Two consequences follow
directly from that:

* field names are not a stable contract -- ``app.fomo.schema`` is the one place
  that absorbs a schema change, so it stays separate from this module;
* the JWT is a short-lived (~1h) Privy session, not an API key, so a 401 is an
  ordinary, expected outcome rather than a misconfiguration.

This client is deliberately *not* the source of trade truth for the app --
that is ``app.chain.tape``, reading Robinhood Chain directly. FOMO answers
"who is this wallet", not "what did it actually do".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

__all__ = ["FomoAuthError", "FomoClient", "FomoError", "FomoRateLimited"]


class FomoError(RuntimeError):
    """FOMO is unreachable, returned an error, or an unusable response."""


class FomoAuthError(FomoError):
    """401: the JWT is missing, expired, or invalid."""


class FomoRateLimited(FomoError):
    """429: a backoff window is active for this endpoint."""


@dataclass
class _Backoff:
    """Per-endpoint 429 backoff: start low, double, cap high.

    Mirrors the behaviour observed in FOMO's own web client -- a minute,
    doubling up to five minutes -- rather than inventing a new schedule.
    """

    until: float = 0.0
    seconds: float = 0.0

    def active(self, now: float) -> bool:
        return now < self.until

    def trigger(self, *, start: float, cap: float, now: float) -> None:
        self.seconds = min(self.seconds * 2, cap) if self.seconds else start
        self.until = now + self.seconds

    def reset(self) -> None:
        self.seconds = 0.0
        self.until = 0.0


class FomoClient:
    """Read-only FOMO API access: identity and rank, not trade history.

    ``jwt=`` overrides ``settings.fomo_jwt`` -- the page's pasted-in session
    takes precedence over whatever is (or is not) in ``.env`` without a
    restart. ``http=`` is the same injectable-client seam every other client
    in this project uses (see ``app.dex.dexscreener.DexScreenerClient``), so
    tests never touch the network.
    """

    def __init__(self, *, jwt: str | None = None, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.fomo_base_url.rstrip("/")
        self._jwt = jwt if jwt is not None else settings.fomo_jwt
        self.client = http or httpx.AsyncClient(timeout=20.0)
        self._owns_client = http is None
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._ttl = float(settings.fomo_cache_seconds)
        self._backoff: dict[str, _Backoff] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @property
    def has_session(self) -> bool:
        return bool(self._jwt.strip())

    # ---- transport ---------------------------------------------------

    def _key(self, path: str, params: dict | None) -> tuple:
        return (path, tuple(sorted((params or {}).items())))

    async def _get(self, path: str, params: dict | None = None) -> object:
        if not self.has_session:
            raise FomoAuthError("FOMO session is not configured")

        key = self._key(path, params)
        now = time.monotonic()

        backoff = self._backoff.setdefault(path, _Backoff())
        if backoff.active(now):
            cached = self._cache.get(key)
            if cached is not None:
                return cached[1]
            raise FomoRateLimited(f"FOMO {path} is rate-limited; retry later")

        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        url = f"{self.base_url}{path}"
        try:
            response = await self.client.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._jwt}",
                    "Content-Type": "application/json",
                    "X-Supported-Chains": settings.fomo_supported_chains,
                },
            )
        except httpx.HTTPError as exc:
            raise FomoError(f"FOMO request failed: {exc}") from None

        if response.status_code == 401:
            raise FomoAuthError("FOMO session expired or invalid")
        if response.status_code == 429:
            backoff.trigger(
                start=settings.fomo_backoff_start_seconds,
                cap=settings.fomo_backoff_max_seconds,
                now=now,
            )
            if cached is not None:
                return cached[1]
            raise FomoRateLimited(f"FOMO {path} returned 429 with no cached response")
        if response.status_code >= 400:
            raise FomoError(f"FOMO HTTP {response.status_code}: {response.text[:200]}")

        backoff.reset()
        try:
            payload = response.json()
        except ValueError:
            raise FomoError(f"FOMO non-JSON response: {response.text[:200]}") from None

        if isinstance(payload, dict) and "responseObject" in payload:
            payload = payload["responseObject"]

        self._cache[key] = (now, payload)
        return payload

    # ---- endpoints -----------------------------------------------------

    async def leaderboard(self, limit: int | None = None) -> object:
        return await self._get(
            "/v2/leaderboard",
            params={"limit": limit if limit is not None else settings.fomo_leaderboard_limit},
        )

    async def balances(self, user_id: str) -> object:
        return await self._get(f"/v2/users/{user_id}/balances")

    async def trades(self, user_id: str, limit: int = 25) -> object:
        return await self._get("/trades", params={"userId": user_id, "limit": limit})

    async def trade(self, trade_id: str) -> object:
        return await self._get(f"/trades/{trade_id}")

    async def user_by_handle(self, handle: str) -> object:
        return await self._get(f"/v2/users/userHandle/{handle.lstrip('@')}")

    async def holders(self, token_address: str, network_id: int) -> object:
        tokens = json.dumps([{"address": token_address, "networkId": network_id}])
        return await self._get("/hodlers/top", params={"tokens": tokens})
