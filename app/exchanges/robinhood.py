"""Robinhood Chain (Uniswap) client -- read-only stage.

Implements the market-data half of ``ExchangeClient`` so the rest of the app can
already price an on-chain pair, chart it and risk-check it. Every method that
would move funds raises :class:`DexNotImplementedError` naming the stage that
lands it, rather than returning something plausible.

Two deliberate shape differences from a CEX client:

* ``klines`` reads this project's own ``market_candles`` -- a DEX has no kline
  endpoint, and ``GridEngine`` asks the client, not the database. This is the
  only place the client touches Postgres, and it is read-only. Intent lifecycle
  stays with the DEX worker and its repository, never here.
* ``last_price`` is an indicative pool mid, not an executable price. Deciding
  that a level is reachable is the Uniswap quote's job; this is the cheap
  watcher that says when to bother asking.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.config import settings
from app.db.session import SessionLocal
from app.dex.candles import DEX_INTERVALS, load_candles
from app.dex.dexscreener import DexScreenerClient, MarketSnapshot
from app.dex.risk import RiskVerdict, evaluate
from app.dex.tokens import DexConfigError, list_pairs, resolve_pair
from app.exchanges.base import ExchangeError, InstrumentInfo

__all__ = ["RobinhoodClient", "RobinhoodError", "DexNotImplementedError"]


class RobinhoodError(ExchangeError):
    """Any Robinhood Chain / Uniswap failure surfaced to the engine."""


class DexNotImplementedError(RobinhoodError):
    """A venue capability that a later stage will provide."""


def _pending(capability: str, stage: str) -> DexNotImplementedError:
    return DexNotImplementedError(
        f"{capability} is not available on the Robinhood Chain client yet "
        f"(lands in stage {stage})"
    )


class RobinhoodClient:
    name = "robinhood"

    def __init__(self, *, market: DexScreenerClient | None = None) -> None:
        self.chain_id = settings.rh_chain_id
        self.chain = settings.dex_chain_slug
        self.market = market or DexScreenerClient()
        self._owns_market = market is None

    async def close(self) -> None:
        if self._owns_market:
            await self.market.close()

    # ---- market data -----------------------------------------------------

    def _pair(self, symbol: str):
        try:
            return resolve_pair(symbol)
        except DexConfigError as exc:
            raise RobinhoodError(str(exc)) from None

    async def market_snapshot(self, symbol: str) -> MarketSnapshot:
        """Pool price plus the health metrics the risk gate reads."""
        return await self.market.snapshot(self._pair(symbol))

    async def risk_verdict(self, symbol: str) -> RiskVerdict:
        return evaluate(await self.market_snapshot(symbol))

    async def last_price(self, symbol: str) -> Decimal:
        snapshot = await self.market_snapshot(symbol)
        if snapshot.price_quote <= 0:
            raise RobinhoodError(f"pool for {symbol} reports no usable price")
        return snapshot.price_quote

    async def instrument_info(self, symbol: str) -> InstrumentInfo:
        pair = self._pair(symbol)
        return InstrumentInfo(
            symbol=pair.symbol,
            base_coin=pair.base_coin,
            quote_coin=pair.quote_coin,
            tick_size=pair.tick_size,
            # ERC-20 decimals are the only real size granularity on chain.
            base_precision=pair.base.unit,
            min_order_amt=pair.min_order_quote,
        )

    async def klines(
        self, symbol: str, *, interval: str = "60", limit: int = 720,
    ) -> list[dict]:
        token = str(interval)
        if token not in DEX_INTERVALS:
            raise RobinhoodError(
                f"DEX candles are sampled at intervals {DEX_INTERVALS}, not {interval!r}"
            )
        pair = self._pair(symbol)
        async with SessionLocal() as session:
            return await load_candles(
                session, pair.symbol, interval=token, limit=limit
            )

    # ---- account ---------------------------------------------------------

    async def api_key_info(self) -> dict:
        """Configuration readout -- no key material is read or returned."""
        return {
            "result": {
                "venue": self.name,
                "chainId": self.chain_id,
                "chain": self.chain,
                "rpcConfigured": bool(settings.rh_rpc_url),
                "universalRouterVersion": settings.rh_universal_router_version,
                "pairs": list(list_pairs()),
                "note": "read-only stage: quoting and signing are not wired up",
            }
        }

    async def wallet_balance(self, coins: str = "USDT,BTC") -> dict:
        raise _pending("wallet balance", "2 (AsyncWeb3 RPC)")

    async def available_balance(self, coin: str) -> Decimal:
        raise _pending("wallet balance", "2 (AsyncWeb3 RPC)")

    # ---- orders ----------------------------------------------------------

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: Decimal, price: Decimal,
        order_link_id: str,
    ) -> dict:
        raise _pending("synthetic limit orders", "5 (DexIntent worker)")

    async def place_market_order(
        self, *, symbol: str, side: str, qty: Decimal, order_link_id: str,
        market_unit: str = "baseCoin",
    ) -> dict:
        raise _pending("swaps", "2 (native ETH buy)")

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None:
        raise _pending("order lookup", "5 (DexIntent worker)")

    async def get_order_by_link_id(
        self, *, order_link_id: str, symbol: str,
    ) -> dict | None:
        raise _pending("order lookup", "5 (DexIntent worker)")

    async def get_executions(self, *, order_id: str, symbol: str) -> list[dict]:
        # Fills are recorded from the receipt by the DEX worker, not scanned
        # back out of the chain on demand.
        raise _pending("execution lookup", "4 (receipt parser)")

    async def cancel_order(self, *, order_id: str, symbol: str) -> None:
        raise _pending("cancellation", "5 (DexIntent worker)")
