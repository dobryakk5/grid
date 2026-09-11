"""Normalizers for FOMO's internal API responses.

Field names here are not a stable contract -- this module is the single place
that absorbs a schema change, so the rest of the app never has to know FOMO
renamed something. Every function is pure (no I/O) and is exercised in
``tests/test_fomo_schema.py`` against fixtures captured by
``scripts/fomo_probe.py`` (see ``tests/fixtures/fomo/``), not against guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

__all__ = [
    "Balance",
    "Holder",
    "Trade",
    "TraderRank",
    "normalize_balances",
    "normalize_holders",
    "normalize_leaderboard",
    "normalize_trades",
    "trade_matches_token",
]


def _pick(row: dict, *names: str, default=None):
    """First present key among ``names``, allowing FOMO's naming to drift."""
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _decimal(value, default: str | None = "0") -> Decimal | None:
    if value is None:
        return None if default is None else Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None if default is None else Decimal(default)


def _str_or_none(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class TraderRank:
    user_id: str
    handle: str | None
    display_name: str | None
    rank: int | None
    evm_address: str | None


@dataclass(frozen=True)
class Holder:
    user_id: str
    handle: str | None
    display_name: str | None
    evm_address: str | None
    value_usd: Decimal | None
    token_amount: Decimal | None
    pnl_usd: Decimal | None
    cost_usd: Decimal | None
    first_buy_time_ms: int | None


@dataclass(frozen=True)
class Balance:
    token_address: str | None
    network_id: int | None
    symbol: str | None
    amount: Decimal | None
    price_usd: Decimal | None
    value_usd: Decimal | None
    pnl_usd: Decimal | None


@dataclass(frozen=True)
class Trade:
    trade_id: str | None
    user_address: str | None
    token_address: str | None
    network_id: int | None
    status: str | None
    side_hint: str | None
    token_amount: Decimal | None
    realized_pnl_usd: Decimal | None
    unrealized_pnl_usd: Decimal | None
    created_at_ms: int | None
    closed_at_ms: int | None


def _user_fields(user: dict) -> tuple[str | None, str | None, str | None, str | None]:
    """``(user_id, handle, display_name, evm_address)`` from a nested ``user`` object."""
    user_id = _str_or_none(_pick(user, "id", "userId"))
    handle = _str_or_none(_pick(user, "userHandle", "handle"))
    display_name = _str_or_none(_pick(user, "displayName", "name"))
    evm_address = _str_or_none(_pick(user, "evmAddress", "evm_address"))
    return user_id, handle, display_name, evm_address


def normalize_leaderboard(payload: object) -> list[TraderRank]:
    rows = payload
    if isinstance(rows, dict):
        rows = _pick(rows, "leaderboard", "items", "users", default=[])
    if not isinstance(rows, list):
        return []

    result: list[TraderRank] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else row
        user_id, handle, display_name, evm_address = _user_fields(user)
        if user_id is None:
            continue
        rank = _pick(row, "rank", "position")
        result.append(TraderRank(
            user_id=user_id,
            handle=handle,
            display_name=display_name,
            rank=int(rank) if rank is not None else index + 1,
            evm_address=evm_address,
        ))
    return result


def normalize_holders(payload: object) -> list[Holder]:
    """Handles both a bare token-result dict and the ``[{...,"topHolders":[...]}]`` list."""
    entry = payload
    if isinstance(entry, list):
        entry = entry[0] if entry else {}
    if not isinstance(entry, dict):
        return []
    rows = _pick(entry, "topHolders", "holders", default=[])
    if not isinstance(rows, list):
        return []

    result: list[Holder] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        user_id, handle, display_name, evm_address = _user_fields(user)
        if user_id is None:
            continue
        first_buy = _pick(row, "firstBuyTime", "buyTime", "boughtAt", "firstBuy")
        result.append(Holder(
            user_id=user_id,
            handle=handle,
            display_name=display_name,
            evm_address=evm_address,
            value_usd=_decimal(_pick(row, "value", "valueUsd"), default=None),
            token_amount=_decimal(_pick(row, "humanAmount", "amount"), default=None),
            pnl_usd=_decimal(_pick(row, "pnl", "pnlUsd"), default=None),
            cost_usd=_decimal(
                _pick(row, "costBasis", "cost", "boughtUsd", "buyUsd", "costUsd"), default=None
            ),
            first_buy_time_ms=int(first_buy) if first_buy is not None else None,
        ))
    return result


def normalize_balances(payload: object) -> list[Balance]:
    rows = payload
    if isinstance(rows, dict):
        rows = _pick(rows, "balances", "items", default=[])
    if not isinstance(rows, list):
        return []

    result: list[Balance] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        balance = row.get("balance") if isinstance(row.get("balance"), dict) else row
        filter_result = row.get("tokenFilterResult") if isinstance(row.get("tokenFilterResult"), dict) else {}
        price = _pick(balance, "price", "priceUsd") or filter_result.get("priceUSD")
        result.append(Balance(
            token_address=_str_or_none(_pick(balance, "tokenAddress", "address")),
            network_id=_int_or_none(_pick(balance, "networkId")),
            symbol=_str_or_none(_pick(balance, "symbol")),
            amount=_decimal(_pick(balance, "shiftedBalance", "amount"), default=None),
            price_usd=_decimal(price, default=None),
            value_usd=_decimal(_pick(balance, "value", "valueUsd"), default=None),
            pnl_usd=_decimal(_pick(balance, "pnl", "pnlUsd"), default=None),
        ))
    return result


def _int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unwrap_trade(item: object) -> dict | None:
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("trade"), dict):
        return item["trade"]
    return item


def normalize_trades(payload: object) -> list[Trade]:
    """Three response shapes observed for ``/trades``: bare list, ``items``, or
    ``{activeTrades, closedTrades}``, each entry optionally wrapping the real
    trade under a ``trade`` key alongside ``swaps``/``transfers``/``comment``.
    """
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        if "items" in payload:
            raw_rows = payload.get("items") or []
        elif "activeTrades" in payload or "closedTrades" in payload:
            raw_rows = list(payload.get("activeTrades") or []) + list(payload.get("closedTrades") or [])
        else:
            raw_rows = []
    else:
        raw_rows = []

    result: list[Trade] = []
    for item in raw_rows:
        trade = _unwrap_trade(item)
        if trade is None:
            continue
        user_address = _pick(trade, "userAddress")
        if user_address is None and isinstance(item, dict):
            # In the `items` shape, `swaps` is a sibling of `trade` on the
            # wrapper, not a field of the trade itself.
            swaps = item.get("swaps")
            if isinstance(swaps, list) and swaps and isinstance(swaps[0], dict):
                user_address = swaps[0].get("address")
        token_meta = trade.get("tokenMetadata") if isinstance(trade.get("tokenMetadata"), dict) else {}
        token_address = _pick(trade, "tokenAddress") or token_meta.get("address")
        created_at = _pick(trade, "createdAt")
        closed_at = _pick(trade, "closedAt")
        result.append(Trade(
            trade_id=_str_or_none(_pick(trade, "id")),
            user_address=_str_or_none(user_address),
            token_address=_str_or_none(token_address),
            network_id=_int_or_none(_pick(trade, "networkId")),
            status=_str_or_none(_pick(trade, "status")),
            side_hint=_str_or_none(_pick(trade, "side", "type")),
            token_amount=_decimal(_pick(trade, "humanTokenAmount"), default=None),
            realized_pnl_usd=_decimal(_pick(trade, "realizedPnlUsd"), default=None),
            unrealized_pnl_usd=_decimal(_pick(trade, "unrealizedPnlUsd"), default=None),
            created_at_ms=_int_or_none(created_at),
            closed_at_ms=_int_or_none(closed_at),
        ))
    return result


def trade_matches_token(trade: Trade, token_address: str) -> bool:
    return bool(trade.token_address) and trade.token_address.lower() == token_address.lower()
