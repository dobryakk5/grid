import hashlib
import hmac
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import pytest

from app.exchanges import ExchangeError, make_exchange
from app.exchanges.bybit import BybitClient
from app.exchanges.mexc import MexcClient, MexcError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpClient:
    """Captures the single request a client method makes and replays a canned body."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    async def request(self, method, url, headers=None):
        self.calls.append({"method": method, "url": url, "headers": headers or {}})
        return FakeResponse(self.payload, self.status_code)

    async def get(self, url, params=None):
        self.calls.append({"method": "GET", "url": url, "params": params or {}})
        return FakeResponse(self.payload, self.status_code)

    async def aclose(self):
        pass


def _mexc(monkeypatch, payload, status_code=200):
    monkeypatch.setattr("app.exchanges.mexc.time.time", lambda: 1_700_000_000.5)
    client = MexcClient()
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.client = FakeHttpClient(payload, status_code)
    return client


def test_make_exchange_selects_concrete_clients():
    assert isinstance(make_exchange("bybit"), BybitClient)
    assert isinstance(make_exchange("mexc"), MexcClient)
    assert isinstance(make_exchange("MEXC"), MexcClient)


def test_make_exchange_rejects_unknown_venue():
    with pytest.raises(ExchangeError):
        make_exchange("kraken")


async def test_signed_request_signs_full_query_string(monkeypatch):
    client = _mexc(monkeypatch, {"balances": []})
    await client._signed_request("GET", "/api/v3/account", {})
    sent = client.client.calls[0]
    assert sent["headers"]["X-MEXC-APIKEY"] == "test-key"

    query = urlsplit(sent["url"]).query
    body, _, signature = query.rpartition("&signature=")
    params = dict(parse_qsl(body))
    assert params["timestamp"] == "1700000000500"
    assert params["recvWindow"] == "5000"
    expected = hmac.new(b"test-secret", body.encode(), hashlib.sha256).hexdigest()
    assert signature == expected


async def test_place_limit_order_returns_bybit_shaped_result(monkeypatch):
    client = _mexc(monkeypatch, {"orderId": 12345, "clientOrderId": "g1-abc"})
    result = await client.place_limit_order(
        symbol="BTCUSDT", side="Buy", qty=Decimal("0.001"),
        price=Decimal("65000"), order_link_id="g1-abc",
    )
    assert result == {"result": {"orderId": "12345", "orderLinkId": "g1-abc"}}
    url = client.client.calls[0]["url"]
    assert "side=BUY" in url and "type=LIMIT" in url and "quantity=0.001" in url


async def test_market_buy_uses_quote_order_qty(monkeypatch):
    client = _mexc(monkeypatch, {"orderId": 7, "clientOrderId": "x"})
    await client.place_market_order(
        symbol="BTCUSDT", side="Buy", qty=Decimal("250"),
        order_link_id="x", market_unit="quoteCoin",
    )
    url = client.client.calls[0]["url"]
    assert "quoteOrderQty=250" in url and "quantity=" not in url


async def test_get_order_normalizes_status_and_avg_price(monkeypatch):
    client = _mexc(monkeypatch, {
        "orderId": 9, "clientOrderId": "g1-x", "status": "PARTIALLY_FILLED",
        "side": "SELL", "price": "65000", "origQty": "0.01",
        "executedQty": "0.004", "cummulativeQuoteQty": "260",
    })
    order = await client.get_order(order_id="9", symbol="BTCUSDT")
    assert order["orderStatus"] == "PartiallyFilled"
    assert order["side"] == "Sell"
    assert order["cumExecQty"] == "0.004"
    assert Decimal(order["avgPrice"]) == Decimal("65000")


async def test_get_order_returns_none_when_missing(monkeypatch):
    client = _mexc(monkeypatch, {"code": -2013, "msg": "Order does not exist."}, status_code=400)
    assert await client.get_order(order_id="404", symbol="BTCUSDT") is None


async def test_signed_error_raises_with_code(monkeypatch):
    client = _mexc(monkeypatch, {"code": -1121, "msg": "Invalid symbol."}, status_code=400)
    with pytest.raises(MexcError) as excinfo:
        await client.get_executions(order_id="1", symbol="NOPE")
    assert excinfo.value.code == -1121


async def test_klines_map_to_engine_rows(monkeypatch):
    client = _mexc(monkeypatch, [
        [1700000000000, "1", "2", "0.5", "1.5", "10", 1700003599999, "15"],
    ])
    rows = await client.klines("BTCUSDT", interval="60", limit=1)
    assert rows == [{
        "timestamp_ms": 1700000000000,
        "open": Decimal("1"), "high": Decimal("2"), "low": Decimal("0.5"),
        "close": Decimal("1.5"), "volume": Decimal("10"), "turnover": Decimal("15"),
    }]


async def test_klines_reject_unsupported_interval(monkeypatch):
    client = _mexc(monkeypatch, [])
    with pytest.raises(MexcError):
        await client.klines("BTCUSDT", interval="7", limit=1)
