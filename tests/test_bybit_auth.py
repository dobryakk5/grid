import hashlib
import hmac

from app.exchanges.bybit import BybitClient


def test_bybit_auth_headers(monkeypatch):
    client = BybitClient()
    client.api_key = "test-key"
    client.api_secret = "test-secret"
    client.recv_window = "5000"
    monkeypatch.setattr("app.exchanges.bybit.time.time", lambda: 1700000000.123)

    payload = "category=spot&symbol=BTCUSDT"
    headers = client._auth_headers(payload)
    raw = f"1700000000123test-key5000{payload}"
    expected = hmac.new(b"test-secret", raw.encode(), hashlib.sha256).hexdigest()

    assert headers["X-BAPI-API-KEY"] == "test-key"
    assert headers["X-BAPI-TIMESTAMP"] == "1700000000123"
    assert headers["X-BAPI-RECV-WINDOW"] == "5000"
    assert headers["X-BAPI-SIGN"] == expected
