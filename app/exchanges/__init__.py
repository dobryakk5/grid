"""Exchange client registry.

``make_exchange`` is the one place that maps a venue name to a concrete client.
Callers pass the profile's ``exchange`` value (or ``None`` for the configured
default) and get a client that satisfies ``ExchangeClient``.
"""

from app.core.config import settings
from app.exchanges.base import (
    ExchangeClient,
    ExchangeError,
    InstrumentInfo,
    OrderNotCancellable,
    decimal_str,
    split_symbol,
)
from app.exchanges.bybit import BybitClient, BybitError
from app.exchanges.mexc import MexcClient, MexcError
from app.exchanges.robinhood import RobinhoodClient, RobinhoodError

__all__ = [
    "ExchangeClient",
    "ExchangeError",
    "OrderNotCancellable",
    "InstrumentInfo",
    "decimal_str",
    "split_symbol",
    "BybitClient",
    "BybitError",
    "MexcClient",
    "MexcError",
    "RobinhoodClient",
    "RobinhoodError",
    "SUPPORTED_EXCHANGES",
    "make_exchange",
]

SUPPORTED_EXCHANGES = ("bybit", "mexc", "robinhood")

_CLIENTS = {"bybit": BybitClient, "mexc": MexcClient, "robinhood": RobinhoodClient}


def make_exchange(name: str | None = None) -> ExchangeClient:
    key = (name or settings.exchange or "bybit").strip().lower()
    try:
        return _CLIENTS[key]()
    except KeyError:
        raise ExchangeError(
            f"unknown exchange {key!r}; expected one of {SUPPORTED_EXCHANGES}"
        ) from None
