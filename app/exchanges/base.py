"""Exchange-agnostic contract shared by every concrete client.

The grid engine and the API talk to exchanges only through this surface.
Concrete clients (``BybitClient``, ``MexcClient``) normalise their native
payloads into the shapes documented on each method so the engine never has to
branch on the venue.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ExchangeError(RuntimeError):
    """Raised for any venue-reported failure (auth, rejected order, bad symbol)."""


class OrderNotCancellable(ExchangeError):
    """The order is past the point where the venue can still withdraw it.

    On-chain venues sign and broadcast; once they have, no cancel exists. The
    engine treats this as a decision, not a transient failure: it stops asking.
    """


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    base_precision: Decimal
    min_order_amt: Decimal


# Quote currencies this deployment trades against, longest-lived first. Symbols
# carry no separator, so the quote suffix is the only way to split them.
QUOTE_COINS = ("USDT", "USDC", "USDG", "BTC", "ETH")


def split_symbol(symbol: str) -> tuple[str, str]:
    """``("PONS", "USDG")`` -- base and quote, or ``("", "")`` if unknown."""
    upper = symbol.upper()
    for quote in QUOTE_COINS:
        if upper.endswith(quote):
            base = upper[: -len(quote)]
            # A bare quote coin is not a pair: "USDG" must not read as base "".
            if base:
                return base, quote
    return "", ""


def decimal_str(value: Decimal) -> str:
    """Plain-decimal string (no scientific notation) for request payloads."""
    return format(Decimal(value).normalize(), "f")


@runtime_checkable
class ExchangeClient(Protocol):
    """Structural type implemented by every concrete exchange client.

    Return-shape contract (kept identical across venues):

    * ``place_limit_order`` / ``place_market_order`` -> ``{"result": {"orderId": str, ...}}``
    * ``get_order`` / ``get_order_by_link_id`` -> ``dict`` with ``orderId``,
      ``orderStatus`` (Bybit vocabulary: ``New`` / ``PartiallyFilled`` /
      ``Filled`` / ``Cancelled`` / ``Rejected`` / ``Deactivated``),
      ``cumExecQty``, ``avgPrice``, ``orderLinkId`` -- or ``None`` when unknown.
    * ``get_executions`` -> ``list[dict]`` with ``execId``, ``execPrice``,
      ``execQty``, ``execValue``, ``execFee``, ``feeCurrency``, ``isMaker``,
      ``execTime``. On-chain venues additionally carry ``feeNativeAmount``,
      ``feeNativeCoin`` and ``txHash`` -- gas paid in a coin that is neither
      base nor quote, and the transaction it was paid in. Other venues omit
      these three keys entirely.
    * ``klines`` -> oldest-first ``list[dict]`` with ``timestamp_ms``, ``open``,
      ``high``, ``low``, ``close``, ``volume``, ``turnover``.
    * ``cancel_order`` raises ``OrderNotCancellable`` for an order the venue can
      no longer withdraw (e.g. an already-broadcast on-chain swap).
    """

    name: str

    async def close(self) -> None: ...

    async def last_price(self, symbol: str) -> Decimal: ...

    async def instrument_info(self, symbol: str) -> InstrumentInfo: ...

    async def klines(
        self, symbol: str, *, interval: str = "60", limit: int = 720,
    ) -> list[dict]: ...

    async def wallet_balance(self, coins: str = "USDT,BTC") -> dict: ...

    async def available_balance(self, coin: str) -> Decimal: ...

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: Decimal, price: Decimal,
        order_link_id: str,
    ) -> dict: ...

    async def place_market_order(
        self, *, symbol: str, side: str, qty: Decimal, order_link_id: str,
        market_unit: str = "baseCoin",
    ) -> dict: ...

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None: ...

    async def get_order_by_link_id(
        self, *, order_link_id: str, symbol: str,
    ) -> dict | None: ...

    async def get_executions(self, *, order_id: str, symbol: str) -> list[dict]: ...

    async def cancel_order(self, *, order_id: str, symbol: str) -> None: ...

    async def api_key_info(self) -> dict: ...
