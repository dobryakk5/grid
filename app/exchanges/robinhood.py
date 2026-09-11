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
from app.dex.intents import SIGNED_STATUSES, IntentStatus
from app.dex.repository import DexIntentRepository
from app.dex.risk import RiskVerdict, evaluate
from app.dex.tokens import DexConfigError, list_pairs, resolve_pair, resolve_token
from app.exchanges.base import (
    ExchangeError,
    InstrumentInfo,
    OrderNotCancellable,
    decimal_str,
)

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
        """Record a level and return at once; the DEX worker executes it.

        The engine speaks base quantity at a limit price, as it does on any
        venue. On chain the spent token differs by direction, so a buy turns
        into "spend qty * price of the quote token" and a sell into "spend qty
        of the base token".

        The intent is written in its own transaction, because it is the remote
        order: if the engine's own transaction rolls back afterwards, the level
        still exists here, exactly as an exchange-side order would.
        """
        pair = self._pair(symbol)
        selling = side.strip().lower() == "sell"
        async with SessionLocal() as session:
            intent = await DexIntentRepository(session).create_level(
                symbol=pair.symbol,
                side="Sell" if selling else "Buy",
                limit_price=price,
                amount_in=qty if selling else qty * price,
                amount_in_coin=pair.base_coin if selling else pair.quote_coin,
                order_link_id=order_link_id,
                profile_id=_profile_id(order_link_id),
            )
            await session.commit()
            return {
                "result": {
                    "orderId": str(intent.id),
                    "orderLinkId": intent.order_link_id,
                }
            }

    async def place_market_order(
        self, *, symbol: str, side: str, qty: Decimal, order_link_id: str,
        market_unit: str = "baseCoin",
    ) -> dict:
        # Swapping itself works (app/dex/execution.py, scripts/dex_buy.py); what
        # is missing is the worker that owns the intent a swap belongs to.
        raise _pending("engine-driven swaps", "5 (DexIntent worker)")

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None:
        async with SessionLocal() as session:
            intent = await DexIntentRepository(session).by_id(int(order_id))
            return _order_dict(intent) if intent is not None else None

    async def get_order_by_link_id(
        self, *, order_link_id: str, symbol: str,
    ) -> dict | None:
        async with SessionLocal() as session:
            intent = await DexIntentRepository(session).by_link_id(order_link_id)
            return _order_dict(intent) if intent is not None else None

    async def get_executions(self, *, order_id: str, symbol: str) -> list[dict]:
        """The single fill a confirmed swap produced, if it has confirmed.

        Read from Postgres, where the receipt parser wrote it -- a swap is one
        atomic fill, and nothing scans the chain back on demand.
        """
        async with SessionLocal() as session:
            intent = await DexIntentRepository(session).by_id(int(order_id))
        if intent is None or intent.status != IntentStatus.FILLED:
            return []
        return [_execution_dict(intent)]

    async def cancel_order(self, *, order_id: str, symbol: str) -> None:
        """Withdraw a level that has not been signed yet.

        There is nothing to cancel on chain: an unsigned level exists only here.
        Once a transaction is signed the nonce is spent and only the worker's
        replacement path can end it, so cancelling one is refused.
        """
        async with SessionLocal() as session:
            repository = DexIntentRepository(session)
            intent = await repository.by_id(int(order_id))
            if intent is None:
                raise RobinhoodError(f"no level with id {order_id}")
            if intent.status in SIGNED_STATUSES:
                raise OrderNotCancellable(
                    f"level {order_id} is already broadcast as {intent.tx_hash}; "
                    "it cannot be cancelled, only replaced by the worker"
                )
            await repository.transition(intent, IntentStatus.CANCELLED)
            await session.commit()


# Intent status -> the Bybit vocabulary the engine branches on. Everything still
# working reads as "New": a level watching for its price and a swap waiting for
# a block are both simply not done yet.
_ORDER_STATUS = {
    IntentStatus.WAITING: "New",
    IntentStatus.TRIGGERED: "New",
    IntentStatus.QUOTED: "New",
    IntentStatus.SIGNING: "New",
    IntentStatus.SUBMITTING: "New",
    IntentStatus.PENDING: "New",
    IntentStatus.BLOCKED: "New",
    IntentStatus.FILLED: "Filled",
    IntentStatus.CANCELLED: "Cancelled",
    IntentStatus.EXPIRED: "Deactivated",
    IntentStatus.FAILED: "Rejected",
}


def _profile_id(order_link_id: str) -> int | None:
    """The engine tags every order ``g<profile_id>-<uuid>``; anything else is manual."""
    head = order_link_id.split("-", 1)[0]
    return int(head[1:]) if head.startswith("g") and head[1:].isdigit() else None


def _base_amounts(intent) -> tuple[Decimal, Decimal]:
    """``(ordered, filled)`` in base-token units, whichever way the swap runs."""
    selling = intent.side.strip().lower() == "sell"
    amount_in = Decimal(intent.amount_in)
    limit = Decimal(intent.limit_price)
    ordered = amount_in if selling else (amount_in / limit if limit > 0 else Decimal("0"))
    if selling:
        filled = Decimal(intent.filled_amount_in or 0)
    else:
        filled = Decimal(intent.filled_amount_out or 0)
    return ordered, filled


def _order_dict(intent) -> dict:
    ordered, filled = _base_amounts(intent)
    return {
        "orderId": str(intent.id),
        "orderLinkId": intent.order_link_id,
        "orderStatus": _ORDER_STATUS.get(intent.status, "New"),
        "side": intent.side,
        "cumExecQty": decimal_str(filled),
        "avgPrice": decimal_str(Decimal(intent.fill_price)) if intent.fill_price else "",
        "price": decimal_str(Decimal(intent.limit_price)),
        "qty": decimal_str(ordered),
        "txHash": intent.tx_hash,
    }


def _execution_dict(intent) -> dict:
    selling = intent.side.strip().lower() == "sell"
    base = Decimal(intent.filled_amount_in if selling else intent.filled_amount_out or 0)
    quote = Decimal(intent.filled_amount_out if selling else intent.filled_amount_in or 0)
    return {
        # A transaction is one atomic fill, so its hash is the execution id.
        "execId": intent.tx_hash,
        "execPrice": decimal_str(Decimal(intent.fill_price or 0)),
        "execQty": decimal_str(base),
        "execValue": decimal_str(quote),
        # Gas, already converted into the quote currency PnL sums.
        "execFee": decimal_str(Decimal(intent.gas_quote or 0)),
        "feeCurrency": intent.gas_quote_coin,
        "feeRate": None,
        "isMaker": False,
        "execTime": (
            int(intent.submitted_at.timestamp() * 1000) if intent.submitted_at else None
        ),
        # Gas in its native coin (ETH), alongside the quote-converted figure
        # above: PnL sums the quote side, but the native amount stays available
        # for reconciliation against the chain.
        "feeNativeAmount": (
            decimal_str(Decimal(intent.gas_native)) if intent.gas_native else None
        ),
        "feeNativeCoin": intent.gas_native_coin,
        "txHash": intent.tx_hash,
    }
