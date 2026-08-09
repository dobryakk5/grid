import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.core.config import settings


class BybitError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    base_coin: str
    quote_coin: str
    tick_size: Decimal
    base_precision: Decimal
    min_order_amt: Decimal


class BybitClient:
    def __init__(self) -> None:
        self.base_url = settings.bybit_base_url.rstrip("/")
        self.api_key = settings.bybit_api_key
        self.api_secret = settings.bybit_api_secret
        self.recv_window = "5000"
        self.client = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self.client.aclose()

    def _auth_headers(self, payload: str) -> dict[str, str]:
        if not self.api_key or not self.api_secret:
            raise BybitError("BYBIT_API_KEY/BYBIT_API_SECRET are not configured")
        ts = str(int(time.time() * 1000))
        raw = f"{ts}{self.api_key}{self.recv_window}{payload}"
        signature = hmac.new(
            self.api_secret.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _check(data: dict) -> dict:
        if data.get("retCode") != 0:
            raise BybitError(f"Bybit {data.get('retCode')}: {data.get('retMsg')}")
        return data

    async def public_get(self, path: str, params: dict[str, str]) -> dict:
        response = await self.client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        return self._check(response.json())

    async def private_get(self, path: str, params: dict[str, str]) -> dict:
        query = urlencode(params)
        headers = self._auth_headers(query)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        return self._check(response.json())

    async def private_post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        headers = self._auth_headers(body)
        response = await self.client.post(
            f"{self.base_url}{path}", content=body.encode(), headers=headers
        )
        response.raise_for_status()
        return self._check(response.json())

    async def last_price(self, symbol: str) -> Decimal:
        data = await self.public_get(
            "/v5/market/tickers", {"category": "spot", "symbol": symbol}
        )
        items = data["result"]["list"]
        if not items:
            raise BybitError(f"No ticker for {symbol}")
        return Decimal(items[0]["lastPrice"])

    async def instrument_info(self, symbol: str) -> InstrumentInfo:
        data = await self.public_get(
            "/v5/market/instruments-info", {"category": "spot", "symbol": symbol}
        )
        item = data["result"]["list"][0]
        lot = item["lotSizeFilter"]
        return InstrumentInfo(
            symbol=item["symbol"],
            base_coin=item["baseCoin"],
            quote_coin=item["quoteCoin"],
            tick_size=Decimal(item["priceFilter"]["tickSize"]),
            base_precision=Decimal(lot["basePrecision"]),
            min_order_amt=Decimal(lot["minOrderAmt"]),
        )


    async def api_key_info(self) -> dict:
        return await self.private_get("/v5/user/query-api", {})

    async def wallet_balance(self, coins: str = "USDT,BTC") -> dict:
        return await self.private_get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": coins},
        )

    async def place_limit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        order_link_id: str,
    ) -> dict:
        return await self.private_post(
            "/v5/order/create",
            {
                "category": "spot",
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": decimal_str(qty),
                "price": decimal_str(price),
                "timeInForce": "GTC",
                "orderLinkId": order_link_id,
            },
        )

    async def get_order(self, *, order_id: str, symbol: str) -> dict | None:
        params = {"category": "spot", "symbol": symbol, "orderId": order_id}
        realtime = await self.private_get("/v5/order/realtime", params)
        items = realtime["result"]["list"]
        if items:
            return items[0]

        history = await self.private_get("/v5/order/history", params)
        items = history["result"]["list"]
        return items[0] if items else None

    async def cancel_order(self, *, order_id: str, symbol: str) -> None:
        await self.private_post(
            "/v5/order/cancel",
            {"category": "spot", "symbol": symbol, "orderId": order_id},
        )

    async def apply_demo_usdt(self, amount: Decimal) -> dict:
        if "api-demo.bybit.com" not in self.base_url:
            raise BybitError("Demo funds endpoint is allowed only with api-demo.bybit.com")
        return await self.private_post(
            "/v5/account/demo-apply-money",
            {
                "adjustType": 0,
                "utaDemoApplyMoney": [{"coin": "USDT", "amountStr": decimal_str(amount)}],
            },
        )


def decimal_str(value: Decimal) -> str:
    return format(value.normalize(), "f")
