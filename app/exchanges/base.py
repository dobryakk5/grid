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


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    base_precision: Decimal
    min_order_amt: Decimal


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
      ``execTime``.
    * ``klines`` -> oldest-first ``list[dict]`` with ``timestamp_ms``, ``open``,
      ``high``, ``low``, ``close``, ``volume``, ``turnover``.
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
