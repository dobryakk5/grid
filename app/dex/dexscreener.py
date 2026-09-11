"""DexScreener market data -- the cheap watcher half of the price path.

This is deliberately *not* the source of execution truth: ``priceUsd`` is an
indicative mid from the most liquid pool, not a price we can fill at. It answers
two questions only:

* is the market anywhere near a level worth spending a Uniswap quote on?
* is the pool healthy enough to trade at all (liquidity / 24h volume)?

Liquidity and volume are summed across every pool where our base token is the
base side, while the price comes from the one pool that matches our quote token
-- a PONS/USDG level must not be armed off a PONS/WETH print.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings
from app.dex.tokens import DexPair, is_native_symbol, native_alias_addresses

__all__ = ["DexScreenerClient", "DexScreenerError", "MarketSnapshot", "PairQuote"]


class DexScreenerError(RuntimeError):
    """Raised when DexScreener is unreachable or has no usable pool for a pair."""


def _decimal(value, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


@dataclass(frozen=True)
class PairQuote:
    """One DexScreener pool row, normalised."""

    pair_address: str
    dex_id: str
    base_address: str
    base_symbol: str
    quote_address: str
    quote_symbol: str
    price_native: Decimal
    price_usd: Decimal
    liquidity_usd: Decimal
    volume_h24: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """What the risk gate and the level watcher both read."""

    symbol: str
    observed_at_ms: int
    # Price of one base token expressed in the pair's quote token.
    price_quote: Decimal
    price_usd: Decimal
    pair_address: str
    # Depth of the pool we would actually trade in...
    pair_liquidity_usd: Decimal
    # ...versus the token's whole on-chain footprint.
    token_liquidity_usd: Decimal
    token_volume_h24: Decimal
    pools_considered: int


class DexScreenerClient:
    """Async DexScreener reader with a short TTL cache.

    The grid worker ticks every few seconds for every profile; the cache keeps
    that from turning into one upstream request per profile per tick.
    """

    def __init__(self, *, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.dexscreener_base_url.rstrip("/")
        self.client = http or httpx.AsyncClient(timeout=15.0)
        self._owns_client = http is None
        self._cache: dict[tuple[str, str], tuple[float, list[PairQuote]]] = {}
        self._ttl = float(settings.dexscreener_cache_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    # ---- transport -------------------------------------------------------

    async def _fetch_token_pairs(self, chain: str, token_address: str) -> list[PairQuote]:
        key = (chain, token_address.lower())
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        url = f"{self.base_url}/token-pairs/v1/{chain}/{token_address}"
        try:
            response = await self.client.get(url)
        except httpx.HTTPError as exc:
            raise DexScreenerError(f"DexScreener request failed: {exc}") from None
        if response.status_code >= 400:
            raise DexScreenerError(
                f"DexScreener HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload = response.json()
        except ValueError:
            raise DexScreenerError(
                f"DexScreener non-JSON response: {response.text[:200]}"
            ) from None
        # The endpoint returns a bare list; a dict shows up only on errors.
        if isinstance(payload, dict):
            payload = payload.get("pairs") or []
        rows = [self._parse_pair(item) for item in payload if isinstance(item, dict)]
        self._cache[key] = (now, rows)
        return rows

    @staticmethod
    def _parse_pair(item: dict) -> PairQuote:
        base = item.get("baseToken") or {}
        quote = item.get("quoteToken") or {}
        return PairQuote(
            pair_address=str(item.get("pairAddress") or ""),
            dex_id=str(item.get("dexId") or ""),
            base_address=str(base.get("address") or "").lower(),
            base_symbol=str(base.get("symbol") or "").upper(),
            quote_address=str(quote.get("address") or "").lower(),
            quote_symbol=str(quote.get("symbol") or "").upper(),
            price_native=_decimal(item.get("priceNative")),
            price_usd=_decimal(item.get("priceUsd")),
            liquidity_usd=_decimal((item.get("liquidity") or {}).get("usd")),
            volume_h24=_decimal((item.get("volume") or {}).get("h24")),
        )

    # ---- snapshots -------------------------------------------------------

    async def snapshot(self, pair: DexPair) -> MarketSnapshot:
        rows = await self._fetch_token_pairs(pair.chain, pair.base.address)
        base_pools = [
            row for row in rows
            if row.base_address == pair.base.address.lower() and row.liquidity_usd > 0
        ]
        if not base_pools:
            raise DexScreenerError(
                f"no {pair.chain} pool found with {pair.base.symbol} as base token"
            )

        matching = [row for row in base_pools if _matches_quote(row, pair)]
        if not matching:
            raise DexScreenerError(
                f"no {pair.base.symbol}/{pair.quote.symbol} pool on {pair.chain}; "
                f"seen quotes: {sorted({row.quote_symbol for row in base_pools})}"
            )
        best = max(matching, key=lambda row: row.liquidity_usd)

        return MarketSnapshot(
            symbol=pair.symbol,
            observed_at_ms=int(time.time() * 1000),
            price_quote=best.price_native,
            price_usd=best.price_usd,
            pair_address=best.pair_address,
            pair_liquidity_usd=best.liquidity_usd,
            # Risk is judged on the token, not on one pool: a level must not fire
            # just because liquidity migrated to a pool we do not trade.
            token_liquidity_usd=sum(
                (row.liquidity_usd for row in base_pools), Decimal("0")
            ),
            token_volume_h24=sum(
                (row.volume_h24 for row in base_pools), Decimal("0")
            ),
            pools_considered=len(base_pools),
        )


def _matches_quote(row: PairQuote, pair: DexPair) -> bool:
    """Is this pool denominated in our quote token?

    Address match is authoritative. Native ETH has no pool of its own -- pools
    hold WETH -- so the gas token also matches by symbol, which keeps the
    ETH-quoted pairs usable before a WETH address is configured.
    """
    quote_address = pair.quote.address.lower()
    if quote_address and row.quote_address == quote_address:
        return True
    if is_native_symbol(pair.quote.symbol):
        if row.quote_address in native_alias_addresses():
            return True
        return is_native_symbol(row.quote_symbol)
    return row.quote_symbol == pair.quote.symbol.upper()
