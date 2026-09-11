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
from app.dex.chain import ChainClient, ChainError
from app.dex.dexscreener import DexScreenerClient, MarketSnapshot
from app.dex.risk import RiskVerdict, evaluate
from app.dex.tokens import DexConfigError, list_pairs, resolve_pair, resolve_token
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

    def __init__(
        self,
        *,
        market: DexScreenerClient | None = None,
        chain: ChainClient | None = None,
    ) -> None:
        self.chain_id = settings.rh_chain_id
        self.chain = settings.dex_chain_slug
        self.market = market or DexScreenerClient()
        self._owns_market = market is None
        # The RPC client is built on first use: price and candle reads must keep
        # working on a host that has no RPC configured at all.
        self._chain = chain
        self._owns_chain = chain is None

    async def close(self) -> None:
        if self._owns_market:
            await self.market.close()
        if self._chain is not None and self._owns_chain:
            await self._chain.close()

    def _rpc(self) -> ChainClient:
        if self._chain is None:
            try:
                self._chain = ChainClient()
            except ChainError as exc:
                raise RobinhoodError(str(exc)) from None
        return self._chain

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
                "walletConfigured": bool(settings.rh_private_key),
                "dryRun": settings.dex_dry_run,
                "note": "swaps run through scripts/dex_buy.py; the engine path "
                        "is not wired up yet",
            }
        }

    async def wallet_balance(self, coins: str = "ETH") -> dict:
        """Balances for the named coins, in the Bybit-ish shape callers expect."""
        balances = []
        for symbol in (item.strip().upper() for item in coins.split(",") if item.strip()):
            try:
                amount = await self.available_balance(symbol)
            except RobinhoodError as exc:
                balances.append({"coin": symbol, "error": str(exc)})
                continue
            balances.append({"coin": symbol, "walletBalance": str(amount)})
        return {
            "result": {
                "wallet": self._rpc().wallet_address,
                "chainId": self.chain_id,
                "balances": balances,
            }
        }

    async def available_balance(self, coin: str) -> Decimal:
        try:
            token = resolve_token(coin)
        except DexConfigError as exc:
            raise RobinhoodError(str(exc)) from None
        chain = self._rpc()
        try:
            if token.native:
                return await chain.native_balance()
            return await chain.token_balance(token)
        except ChainError as exc:
            raise RobinhoodError(str(exc)) from None

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
        # Swapping itself works (app/dex/execution.py, scripts/dex_buy.py); what
        # is missing is the worker that owns the intent a swap belongs to.
        raise _pending("engine-driven swaps", "5 (DexIntent worker)")

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None:
        raise _pending("order lookup", "5 (DexIntent worker)")

    async def get_order_by_link_id(
        self, *, order_link_id: str, symbol: str,
    ) -> dict | None:
        raise _pending("order lookup", "5 (DexIntent worker)")

    async def get_executions(self, *, order_id: str, symbol: str) -> list[dict]:
        # Fills are parsed from the receipt when a swap confirms and written to
        # Postgres there; nothing scans the chain back on demand.
        raise _pending("execution lookup", "5 (DexIntent worker)")

    async def cancel_order(self, *, order_id: str, symbol: str) -> None:
        raise _pending("cancellation", "5 (DexIntent worker)")
