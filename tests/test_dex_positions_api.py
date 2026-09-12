"""The sell endpoint's refusals. Every one of these guards real money."""

from decimal import Decimal

import httpx
import pytest

from app.api import dex_positions
from app.core.config import settings
from app.core.security import hash_password
from app.dex.positions import Position
from app.dex.tokens import Token


class FakeChain:
    wallet_address = "0xa37280C51518F7EB9cF2F12EBc1F7CB9278E1304"

    async def native_balance(self):
        return Decimal("1")

    async def close(self):
        return None


class FakeQuote:
    amount_out = 40_000_000          # 40 USDG, 6 decimals
    gas_fee_native_wei = 10 ** 12


class FakeUniswap:
    def __init__(self, quote=None):
        self.quote = quote or FakeQuote()

    async def quote_exact_in(self, **kwargs):
        return self.quote

    async def close(self):
        return None


CHATGPT = "0x7ec1ffe06c5fe6145035af1fdbc1b186792a22e0"


@pytest.fixture
def wired(monkeypatch):
    """Auth on, chain faked, and the intent write replaced by a recorder."""
    monkeypatch.setattr(settings, "auth_secret", "s", raising=False)
    monkeypatch.setattr(settings, "auth_password_hash", hash_password("p"), raising=False)
    monkeypatch.setattr(settings, "auth_service_token", "svc-token", raising=False)
    monkeypatch.setattr(dex_positions, "ChainClient", lambda: FakeChain())
    monkeypatch.setattr(dex_positions, "UniswapClient", lambda: FakeUniswap())

    async def no_dynamic_load(_factory):
        return 0

    monkeypatch.setattr(dex_positions, "load_dynamic_tokens", no_dynamic_load)

    async def held(_factory, _chain, **_kw):
        return [Position(CHATGPT, "ChatGpt", 18, 8 * 10 ** 18)]

    monkeypatch.setattr(dex_positions, "open_positions", held)

    armed = []

    class Repo:
        def __init__(self, session):
            pass

        async def create_level(self, **kwargs):
            armed.append(kwargs)
            return type("Intent", (), {"id": 77})()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(dex_positions, "DexIntentRepository", Repo)
    monkeypatch.setattr(dex_positions, "SessionLocal", lambda: Session())
    return armed


def client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


AUTH = {"Authorization": "Bearer svc-token"}


async def test_selling_needs_authentication(wired):
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/sell", json={"percent": 25})
    assert response.status_code == 401
    assert not wired, "неавторизованный запрос не должен армить ордер"


async def test_half_a_position_is_armed_as_a_bounded_sell(wired, monkeypatch):
    monkeypatch.setattr(dex_positions, "_pair_for", lambda symbol, address: _pair())
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/sell",
                                   json={"percent": 50}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert Decimal(body["amount"]) == Decimal(4) and body["intent_id"] == 77
    # 4 tokens quoted at 40 USDG -> 10 each, less the 0.5% cap -> 9.95
    assert Decimal(body["limit_price"]) == Decimal("9.95")
    assert wired[0]["side"] == "Sell" and wired[0]["limit_price"] == Decimal("9.95")


async def test_a_ticker_pointing_at_another_contract_stops_the_sale(wired, monkeypatch):
    # Same ticker, different address: selling this would spend the wrong coin.
    monkeypatch.setattr(dex_positions, "_pair_for", dex_positions._pair_for)
    monkeypatch.setattr(dex_positions, "resolve_pair",
                        lambda symbol: _pair(base_address="0x" + "de" * 20))
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/sell",
                                   json={"percent": 100}, headers=AUTH)
    assert response.status_code == 409, response.text
    assert not wired, "несовпадение адреса обязано остановить продажу"


async def test_a_position_the_wallet_does_not_hold_is_not_sellable(wired):
    async with client() as http:
        response = await http.post(f"/api/dex/positions/0x{'ab' * 20}/sell",
                                   json={"percent": 25}, headers=AUTH)
    assert response.status_code == 404
    assert not wired


async def test_dust_below_the_minimum_order_is_refused(wired, monkeypatch):
    monkeypatch.setattr(dex_positions, "_pair_for", lambda symbol, address: _pair())
    monkeypatch.setattr(dex_positions, "UniswapClient",
                        lambda: FakeUniswap(type("Q", (), {"amount_out": 1_000_000,
                                                           "gas_fee_native_wei": 0})()))
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/sell",
                                   json={"percent": 25}, headers=AUTH)
    assert response.status_code == 422 and "Минимальный ордер" in response.text
    assert not wired


async def test_a_wallet_that_cannot_pay_the_gas_arms_nothing(wired, monkeypatch):
    monkeypatch.setattr(dex_positions, "_pair_for", lambda symbol, address: _pair())

    class Broke(FakeChain):
        async def native_balance(self):
            return Decimal("0.0000000001")

    monkeypatch.setattr(dex_positions, "ChainClient", lambda: Broke())
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/sell",
                                   json={"percent": 100}, headers=AUTH)
    assert response.status_code == 422 and "газ" in response.text
    assert not wired


def _pair(base_address=CHATGPT):
    from app.dex.tokens import DexPair
    return DexPair(
        symbol="CHATGPTUSDG",
        base=Token(symbol="CHATGPT", address=base_address, decimals=18),
        quote=Token(symbol="USDG", address="0x5fc5360d0400a0fd4f2af552add042d716f1d168", decimals=6),
        chain="robinhood", tick_size=Decimal("0.000001"),
        min_order_quote=Decimal("10"),
    )


async def test_buying_needs_authentication(wired):
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/buy",
                                   json={"quote_amount": "25"})
    assert response.status_code == 401
    assert not wired


@pytest.fixture
def buyable(wired, monkeypatch):
    """A known token, a funded wallet, and a quote of 8 tokens for 40 USDG."""
    monkeypatch.setattr(dex_positions, "_pair_for", lambda symbol, address: _pair())

    async def known(address):
        return type("Token", (), {"symbol": "ChatGpt", "decimals": 18, "address": address})()

    monkeypatch.setattr(dex_positions, "_known_token", known)

    class Funded(FakeChain):
        async def token_balance(self, token):
            return Decimal("100")

    monkeypatch.setattr(dex_positions, "ChainClient", lambda: Funded())
    # 40 USDG in, 8 tokens out -> 5 USDG each.
    monkeypatch.setattr(dex_positions, "UniswapClient",
                        lambda: FakeUniswap(type("Q", (), {"amount_out": 8 * 10 ** 18,
                                                           "gas_fee_native_wei": 0})()))
    return wired


async def test_a_market_buy_caps_the_price_above_the_quote(buyable):
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/buy",
                                   json={"quote_amount": "40"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_type"] == "market"
    # 5 USDG each plus the 0.5% cap -- above, because a buyer is hurt by a
    # higher price, not a lower one.
    assert Decimal(body["limit_price"]) == Decimal("5.025")
    assert buyable[0]["side"] == "Buy" and buyable[0]["amount_in_coin"] == "USDG"


async def test_a_limit_buy_uses_the_price_given_and_is_not_quoted(buyable, monkeypatch):
    def no_quotes():
        raise AssertionError("лимитку не нужно котировать при постановке")

    monkeypatch.setattr(dex_positions, "UniswapClient", no_quotes)
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/buy",
                                   json={"quote_amount": "40", "limit_price": "3.5"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_type"] == "limit" and Decimal(body["limit_price"]) == Decimal("3.5")
    assert body["quoted_receive"] is None


async def test_a_buy_larger_than_the_cash_on_hand_is_refused(buyable, monkeypatch):
    class Broke(FakeChain):
        async def token_balance(self, token):
            return Decimal("12")

    monkeypatch.setattr(dex_positions, "ChainClient", lambda: Broke())
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/buy",
                                   json={"quote_amount": "40"}, headers=AUTH)
    assert response.status_code == 422 and "кошельке" in response.text
    assert not buyable


async def test_a_buy_below_the_minimum_order_never_reaches_the_chain(buyable):
    async with client() as http:
        response = await http.post(f"/api/dex/positions/{CHATGPT}/buy",
                                   json={"quote_amount": "1"}, headers=AUTH)
    assert response.status_code == 422 and "Минимальный ордер" in response.text
    assert not buyable
