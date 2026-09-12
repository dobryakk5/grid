"""FOMO swap legs and aggregates across chains (independent of chain_tape).

Observed schema references (not an official stable API):
https://github.com/cyberknight01/fomo-monitor/blob/main/接口说明.md
https://github.com/petteryyf/Quick-LP/blob/main/docs/fomo_api.md
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation


SWAP_FIELDS = (
    "id", "signature", "createdAt", "inTokenAddress", "outTokenAddress",
    "inNetworkId", "outNetworkId", "inHumanAmount", "outHumanAmount",
    "humanUsdAmountIn", "humanUsdAmountOut", "inTokenSymbol", "outTokenSymbol",
)


def swap_rows(payload):
    if isinstance(payload, dict):
        payload = payload.get("responseObject", payload)
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("swaps", "items", "list"):
            if isinstance(payload.get(key), list):
                more = payload.get("hasNextPage")
                return payload[key], more if isinstance(more, bool) else None
    raise ValueError("FOMO swaps: неизвестный формат ответа")


def number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 and result < Decimal("1e20") else None
    except (InvalidOperation, ValueError):
        return None


def timestamp_ms(value):
    try:
        if isinstance(value, str) and "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            return int(dt.timestamp() * 1000)
        n = float(value)
        # Today's records are either Unix seconds or Unix milliseconds.
        result = int(n * 1000 if n < 100_000_000_000 else n)
        return result if 946684800000 <= result <= 4102444800000 else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_swap(row):
    """in is spent (SELL), out is received (BUY), including quote assets.

    No USD price inference. Missing USD stays unknown. Both legs must be
    identified; a malformed row is counted as rejected, never half a swap.
    EVM addresses are case-insensitive; Solana addresses are not.
    """
    if not isinstance(row, dict):
        return []
    swap_id = row.get("id")
    created = timestamp_ms(row.get("createdAt"))
    if not isinstance(swap_id, str) or not 1 <= len(swap_id) <= 160 or created is None:
        return []
    legs = []
    for prefix, side in (("in", "SELL"), ("out", "BUY")):
        address = row.get(prefix + "TokenAddress")
        try:
            network = int(row.get(prefix + "NetworkId"))
        except (ValueError, TypeError):
            return []
        amount = number(row.get(prefix + "HumanAmount"))
        if (not isinstance(address, str) or not address.strip() or len(address) > 128
                or network <= 0 or network > 2**31 - 1 or amount is None):
            return []
        symbol = row.get(prefix + "TokenSymbol")
        legs.append({
            "swap_id": swap_id, "side": side, "chain_id": network,
            "token_address": address.lower() if address.startswith("0x") else address,
            "symbol": symbol[:64] if isinstance(symbol, str) else None,
            "token_amount": amount,
            "value_usd": number(row.get("humanUsdAmount" + ("In" if prefix == "in" else "Out"))),
            "occurred_at_ms": created,
        })
    return legs


def aggregate_legs(legs, identities, names=None):
    """``names`` maps ``(chain_id, token_address)`` to ``(symbol, name)``.

    The swap feed itself carries no symbol, so without this every coin is a
    bare address. A leg that does carry one still wins: it came from the trade
    record, while a looked-up name is only the best current guess about which
    of several same-ticker pools this address is.
    """
    names = names or {}
    coins = {}
    for leg in legs:
        key = (leg.chain_id, leg.token_address)
        looked_up, full_name = names.get(key, (None, None))
        coin = coins.setdefault(key, {
            "chain_id": leg.chain_id, "token_address": leg.token_address,
            "symbol": leg.symbol or looked_up, "name": full_name,
            "buy_usd": Decimal(0), "sell_usd": Decimal(0),
            "unpriced": 0, "last_at_ms": 0, "traders": {},
        })
        if leg.symbol:
            coin["symbol"] = leg.symbol
        identity = identities[leg.user_id]
        person = coin["traders"].setdefault(leg.user_id, {
            **identity, "buy_usd": Decimal(0), "sell_usd": Decimal(0),
            "buy_amount": Decimal(0), "sell_amount": Decimal(0),
            "buys": 0, "sells": 0, "unpriced": 0, "last_at_ms": 0,
        })
        side = "buy" if leg.side == "BUY" else "sell"
        person[side + "s"] += 1
        person[side + "_amount"] += leg.token_amount
        if leg.value_usd is None:
            coin["unpriced"] += 1
            person["unpriced"] += 1
        else:
            coin[side + "_usd"] += leg.value_usd
            person[side + "_usd"] += leg.value_usd
        coin["last_at_ms"] = max(coin["last_at_ms"], leg.occurred_at_ms)
        person["last_at_ms"] = max(person["last_at_ms"], leg.occurred_at_ms)
    for coin in coins.values():
        coin["traders"] = sorted(coin["traders"].values(), key=lambda t: t["rank"])
        coin["buyers"] = sum(t["buys"] > 0 for t in coin["traders"])
        coin["sellers"] = sum(t["sells"] > 0 for t in coin["traders"])
        coin["net_usd"] = coin["buy_usd"] - coin["sell_usd"]
    return sorted(coins.values(), key=lambda c: c["buy_usd"] + c["sell_usd"], reverse=True)
