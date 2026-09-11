"""MEXC spot (v3) client.

Live trading only -- MEXC has no demo/paper spot environment. Every method
normalises MEXC's native payloads into the shapes documented in
``app.exchanges.base.ExchangeClient`` so ``GridEngine`` stays venue-agnostic.
"""

import hashlib
import hmac
import time
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.exchanges.base import ExchangeError, InstrumentInfo, decimal_str

__all__ = ["MexcClient", "MexcError"]


class MexcError(ExchangeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# Bybit-minute interval string -> MEXC interval token. The engine only ever asks
# for "60", "15" and "1"; the rest are here so ad-hoc callers do not surprise us.
_INTERVALS = {
    "1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "60m",
    "240": "4h", "D": "1d", "M": "1M",
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "60m",
    "4h": "4h", "1d": "1d",
}

# MEXC order status -> Bybit vocabulary the engine branches on.
_ORDER_STATUS = {
    "NEW": "New",
    "PARTIALLY_FILLED": "PartiallyFilled",
    "FILLED": "Filled",
    "CANCELED": "Cancelled",
    "PARTIALLY_CANCELED": "PartiallyFilledCanceled",
    "PENDING_CANCEL": "New",
    "REJECTED": "Rejected",
    "EXPIRED": "Deactivated",
}

_MAX_KLINES_PER_CALL = 1000
_ORDER_NOT_FOUND = -2013


class MexcClient:
    name = "mexc"

    def __init__(self) -> None:
        self.base_url = settings.mexc_base_url.rstrip("/")
        self.api_key = settings.mexc_api_key
        self.api_secret = settings.mexc_api_secret
        self.recv_window = "5000"
        self.client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.client.aclose()

    # ---- transport -------------------------------------------------------

    @staticmethod
    def _check(response: httpx.Response):
        try:
            data = response.json()
        except ValueError:
            response.raise_for_status()
            raise MexcError(f"MEXC non-JSON response: {response.text[:200]}")
        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, 0, 200):
                raise MexcError(f"MEXC {code}: {data.get('msg')}", code=code)
        if response.status_code >= 400:
            raise MexcError(f"MEXC HTTP {response.status_code}: {response.text[:200]}")
        return data

    async def _public_get(self, path: str, params: dict[str, str]) -> object:
        response = await self.client.get(f"{self.base_url}{path}", params=params)
        return self._check(response)

    async def _signed_request(
        self, method: str, path: str, params: dict[str, str] | None = None,
    ) -> object:
        if not self.api_key or not self.api_secret:
            raise MexcError("MEXC_API_KEY/MEXC_API_SECRET are not configured")
        query: dict[str, str] = {k: v for k, v in (params or {}).items() if v is not None}
        query["timestamp"] = str(int(time.time() * 1000))
        query["recvWindow"] = self.recv_window
        payload = urlencode(query)
        signature = hmac.new(
            self.api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{self.base_url}{path}?{payload}&signature={signature}"
        response = await self.client.request(
            method, url,
            headers={
                "X-MEXC-APIKEY": self.api_key,
                "Content-Type": "application/json",
            },
        )
        return self._check(response)

    async def _account(self) -> dict:
        return await self._signed_request("GET", "/api/v3/account", {})

    # ---- market data --------------------------------------------------------

    async def last_price(self, symbol: str) -> Decimal:
        data = await self._public_get("/api/v3/ticker/price", {"symbol": symbol})
        if isinstance(data, list):
            data = data[0] if data else {}
        price = data.get("price") if isinstance(data, dict) else None
        if price is None:
            raise MexcError(f"No ticker for {symbol}")
        return Decimal(price)

    @staticmethod
    def _precision_step(digits) -> Decimal:
        try:
            return Decimal(1).scaleb(-int(digits))
        except (TypeError, ValueError):
            return Decimal("0")

    async def instrument_info(self, symbol: str) -> InstrumentInfo:
        data = await self._public_get("/api/v3/exchangeInfo", {"symbol": symbol})
        items = data.get("symbols") if isinstance(data, dict) else None
        if not items:
            raise MexcError(f"No instrument info for {symbol}")
        item = items[0]
        filters = {
            f.get("filterType"): f for f in item.get("filters", []) if isinstance(f, dict)
        }
        tick_size = (
            filters.get("PRICE_FILTER", {}).get("tickSize")
            or self._precision_step(item.get("quotePrecision"))
        )
        base_precision = (
            filters.get("LOT_SIZE", {}).get("stepSize")
            or item.get("baseSizePrecision")
            or self._precision_step(item.get("baseAssetPrecision"))
        )
        min_order_amt = (
            filters.get("MIN_NOTIONAL", {}).get("minNotional")
            or item.get("quoteAmountPrecision")
            or "1"
        )
        return InstrumentInfo(
            symbol=item.get("symbol", symbol),
            base_coin=item.get("baseAsset", ""),
            quote_coin=item.get("quoteAsset", ""),
            tick_size=Decimal(str(tick_size)),
            base_precision=Decimal(str(base_precision)),
            min_order_amt=Decimal(str(min_order_amt)),
        )

    async def klines(
        self, symbol: str, *, interval: str = "60", limit: int = 720,
    ) -> list[dict]:
        token = _INTERVALS.get(str(interval))
        if token is None:
            raise MexcError(f"MEXC does not support kline interval {interval!r}")
        rows: dict[int, dict] = {}
        end: int | None = None
        while len(rows) < limit:
            page_size = min(_MAX_KLINES_PER_CALL, limit - len(rows))
            params = {"symbol": symbol, "interval": token, "limit": str(page_size)}
            if end is not None:
                params["endTime"] = str(end)
            page = await self._public_get("/api/v3/klines", params)
            if not page:
                break
            for row in page:
                timestamp = int(row[0])
                rows[timestamp] = {
                    "timestamp_ms": timestamp,
                    "open": Decimal(str(row[1])),
                    "high": Decimal(str(row[2])),
                    "low": Decimal(str(row[3])),
                    "close": Decimal(str(row[4])),
                    "volume": Decimal(str(row[5])),
                    "turnover": Decimal(str(row[7])) if len(row) > 7 else Decimal("0"),
                }
            oldest = min(int(row[0]) for row in page)
            if len(page) < page_size:
                break
            end = oldest - 1
        return [rows[key] for key in sorted(rows)][-limit:]

    # ---- account ----------------------------------------------------------

    async def wallet_balance(self, coins: str = "USDT,BTC") -> dict:
        return await self._account()

    async def available_balance(self, coin: str) -> Decimal:
        data = await self._account()
        for balance in data.get("balances", []):
            if balance.get("asset", "").upper() == coin.upper():
                return Decimal(balance.get("free") or "0")
        return Decimal("0")

    async def api_key_info(self) -> dict:
        data = await self._account()
        can_trade = bool(data.get("canTrade", True))
        permissions = data.get("permissions", []) or []
        spot_ok = can_trade and ("SPOT" in permissions or not permissions)
        return {
            "result": {
                "apiKey": self.api_key,
                "readOnly": 0 if can_trade else 1,
                "permissions": {"Spot": ["SpotTrade"] if spot_ok else []},
                "ips": [],
                "uta": None,
                "note": "MEXC spot (live)",
            }
        }

    # ---- orders ---------------------------------------------------------

    async def place_limit_order(
        self, *, symbol: str, side: str, qty: Decimal, price: Decimal,
        order_link_id: str,
    ) -> dict:
        data = await self._signed_request(
            "POST", "/api/v3/order",
            {
                "symbol": symbol,
                "side": side.upper(),
                "type": "LIMIT",
                "quantity": decimal_str(qty),
                "price": decimal_str(price),
                "newClientOrderId": order_link_id,
            },
        )
        return {
            "result": {
                "orderId": str(data.get("orderId")),
                "orderLinkId": data.get("clientOrderId", order_link_id),
            }
        }

    async def place_market_order(
        self, *, symbol: str, side: str, qty: Decimal, order_link_id: str,
        market_unit: str = "baseCoin",
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "newClientOrderId": order_link_id,
        }
        if market_unit == "quoteCoin":
            params["quoteOrderQty"] = decimal_str(qty)
        else:
            params["quantity"] = decimal_str(qty)
        data = await self._signed_request("POST", "/api/v3/order", params)
        return {
            "result": {
                "orderId": str(data.get("orderId")),
                "orderLinkId": data.get("clientOrderId", order_link_id),
            }
        }

    @staticmethod
    def _normalize_order(data: dict) -> dict:
        executed = Decimal(data.get("executedQty") or "0")
        cumulative_quote = Decimal(data.get("cummulativeQuoteQty") or "0")
        if executed > 0 and cumulative_quote > 0:
            avg_price = cumulative_quote / executed
        else:
            avg_price = Decimal(data.get("price") or "0")
        return {
            "orderId": str(data.get("orderId")),
            "orderLinkId": data.get("clientOrderId"),
            "orderStatus": _ORDER_STATUS.get(
                data.get("status"), data.get("status") or "New"
            ),
            "side": "Buy" if str(data.get("side", "")).upper() == "BUY" else "Sell",
            "cumExecQty": decimal_str(executed),
            "avgPrice": decimal_str(avg_price) if avg_price > 0 else "",
            "price": data.get("price"),
            "qty": data.get("origQty"),
        }

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None:
        try:
            data = await self._signed_request(
                "GET", "/api/v3/order", {"symbol": symbol, "orderId": str(order_id)}
            )
        except MexcError as exc:
            if exc.code == _ORDER_NOT_FOUND:
                return None
            raise
        return self._normalize_order(data)

    async def get_order_by_link_id(
        self, *, order_link_id: str, symbol: str,
    ) -> dict | None:
        try:
            data = await self._signed_request(
                "GET", "/api/v3/order",
                {"symbol": symbol, "origClientOrderId": order_link_id},
            )
        except MexcError as exc:
            if exc.code == _ORDER_NOT_FOUND:
                return None
            raise
        return self._normalize_order(data)

    async def get_executions(self, *, order_id: str, symbol: str) -> list[dict]:
        data = await self._signed_request(
            "GET", "/api/v3/myTrades",
            {"symbol": symbol, "orderId": str(order_id), "limit": "1000"},
        )
        executions: list[dict] = []
        for item in data or []:
            price = Decimal(item.get("price") or "0")
            qty = Decimal(item.get("qty") or "0")
            quote_qty = item.get("quoteQty")
            executions.append(
                {
                    "execId": str(item.get("id")),
                    "execPrice": item.get("price"),
                    "execQty": item.get("qty"),
                    "execValue": (
                        quote_qty if quote_qty is not None
                        else decimal_str(price * qty)
                    ),
                    "execFee": item.get("commission") or "0",
                    "feeCurrency": item.get("commissionAsset"),
                    "feeRate": None,
                    "isMaker": bool(item.get("isMaker")),
                    "execTime": item.get("time"),
                }
            )
        return executions

    async def cancel_order(self, *, order_id: str, symbol: str) -> None:
        await self._signed_request(
            "DELETE", "/api/v3/order", {"symbol": symbol, "orderId": str(order_id)}
        )
