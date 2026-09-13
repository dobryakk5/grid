"""Editing a level that has not started executing -- and refusing the rest.

The pencil on the history page writes through this endpoint, so every refusal
here is a row the page must not pretend is editable.
"""

from decimal import Decimal

import httpx
import pytest

from app.api import routes
from app.core.config import settings
from app.core.security import hash_password
from app.dex.tokens import Token


CHATGPT = "0x7ec1ffe06c5fe6145035af1fdbc1b186792a22e0"
AUTH = {"Authorization": "Bearer svc-token"}


def _pair():
    from app.dex.tokens import DexPair
    return DexPair(
        symbol="PONSUSDG",
        base=Token(symbol="PONS", address=CHATGPT, decimals=18),
        quote=Token(symbol="USDG", address="0x5fc5360d0400a0fd4f2af552add042d716f1d168", decimals=6),
        chain="robinhood", tick_size=Decimal("0.000001"),
        min_order_quote=Decimal("10"),
    )


class _Level:
    """Just the columns the endpoint reads and writes."""

    def __init__(self, **kw):
        self.id = 7
        self.profile_id = None
        self.status = "WAITING"
        self.symbol = "PONSUSDG"
        self.side = "Buy"
        self.limit_price = Decimal("0.5")
        self.amount_in = Decimal(100)
        self.amount_in_coin = "USDG"
        self.ignore_liquidity_gate = False
        self.__dict__.update(kw)


@pytest.fixture
def wired(monkeypatch):
    """Auth on, the registry stubbed, and one level behind ``session.get``."""
    monkeypatch.setattr(settings, "auth_secret", "s", raising=False)
    monkeypatch.setattr(settings, "auth_password_hash", hash_password("p"), raising=False)
    monkeypatch.setattr(settings, "auth_service_token", "svc-token", raising=False)
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal(10), raising=False)
    monkeypatch.setattr(routes, "resolve_pair", lambda symbol: _pair())

    async def no_dynamic_load(_factory):
        return 0

    monkeypatch.setattr(routes, "load_dynamic_tokens", no_dynamic_load)

    state = {"level": _Level(), "committed": False}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return state["level"]

        async def commit(self):
            state["committed"] = True

    monkeypatch.setattr(routes, "SessionLocal", lambda: Session())
    return state


def client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_editing_needs_authentication(wired):
    async with client() as http:
        response = await http.patch("/api/fomo/limit-order/7", json={"limit_price": "0.6"})
    assert response.status_code == 401
    assert not wired["committed"], "неавторизованный запрос не должен менять уровень"


async def test_a_waiting_level_moves_to_the_new_price_and_size(wired):
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"limit_price": "0.6", "amount": "150"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert wired["level"].limit_price == Decimal("0.6")
    assert wired["level"].amount_in == Decimal(150)
    assert wired["committed"]


async def test_one_field_alone_leaves_the_other_as_it_was(wired):
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"amount": "250"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert wired["level"].amount_in == Decimal(250)
    assert wired["level"].limit_price == Decimal("0.5"), "цену не просили трогать"


async def test_a_grid_level_is_not_a_buttons_to_move(wired):
    wired["level"] = _Level(profile_id=3)
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"limit_price": "0.6"}, headers=AUTH)
    assert response.status_code == 409
    assert not wired["committed"]


async def test_a_level_already_executing_is_too_late_to_edit(wired):
    # Past WAITING a nonce may be reserved or a transaction signed; the row
    # would then describe something other than what the chain is doing.
    wired["level"] = _Level(status="TRIGGERED")
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"limit_price": "0.6"}, headers=AUTH)
    assert response.status_code == 409 and "too late" in response.text
    assert not wired["committed"]


async def test_an_edit_cannot_walk_an_order_under_the_minimum(wired):
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"amount": "2"}, headers=AUTH)
    assert response.status_code == 422 and "minimum order" in response.text
    assert wired["level"].amount_in == Decimal(100), "отказ обязан оставить уровень нетронутым"


async def test_a_sell_is_never_refused_for_being_small(wired):
    """30 tokens at 0.2 is 6 USDG, under the buy floor -- and allowed anyway.

    There is no size at which closing a position becomes the wrong thing to
    let someone do; the floor only guards the way in.
    """
    wired["level"] = _Level(side="Sell", amount_in=Decimal(30), amount_in_coin="PONS")
    async with client() as http:
        response = await http.patch(
            "/api/fomo/limit-order/7", json={"limit_price": "0.2"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert wired["level"].limit_price == Decimal("0.2")


async def test_an_empty_edit_is_refused_rather_than_committed(wired):
    async with client() as http:
        response = await http.patch("/api/fomo/limit-order/7", json={}, headers=AUTH)
    assert response.status_code == 422
    assert not wired["committed"]


async def test_arming_carries_the_liquidity_waiver_onto_the_new_level(wired, monkeypatch):
    """What the copy button needs: a repeat of a waived buy is waived too.

    Without this a copied order silently becomes one that sits in BLOCKED,
    which is the confusion the checkbox exists to remove.
    """
    armed = []

    class Repo:
        def __init__(self, session):
            pass

        async def create_level(self, **kwargs):
            armed.append(kwargs)
            return type("Intent", (), {"id": 99, "order_link_id": "link"})()

    monkeypatch.setattr(routes, "DexIntentRepository", Repo)
    async with client() as http:
        response = await http.post("/api/fomo/limit-order", headers=AUTH, json={
            "symbol": "PONSUSDG", "side": "Buy", "limit_price": "0.5",
            "amount": "100", "ignore_liquidity": True})
    assert response.status_code == 200, response.text
    assert response.json()["ignore_liquidity"] is True
    assert armed[0]["ignore_liquidity_gate"] is True


async def test_arming_without_the_waiver_keeps_the_floors(wired, monkeypatch):
    armed = []

    class Repo:
        def __init__(self, session):
            pass

        async def create_level(self, **kwargs):
            armed.append(kwargs)
            return type("Intent", (), {"id": 99, "order_link_id": "link"})()

    monkeypatch.setattr(routes, "DexIntentRepository", Repo)
    async with client() as http:
        response = await http.post("/api/fomo/limit-order", headers=AUTH, json={
            "symbol": "PONSUSDG", "side": "Buy", "limit_price": "0.5", "amount": "100"})
    assert response.status_code == 200, response.text
    assert armed[0]["ignore_liquidity_gate"] is False


async def test_an_edit_can_waive_the_liquidity_floors(wired):
    async with client() as http:
        response = await http.patch("/api/fomo/limit-order/7",
                                    json={"ignore_liquidity": True}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json()["ignore_liquidity"] is True
    assert wired["level"].ignore_liquidity_gate is True


async def test_an_edit_can_put_the_floors_back(wired):
    wired["level"] = _Level(ignore_liquidity_gate=True)
    async with client() as http:
        response = await http.patch("/api/fomo/limit-order/7",
                                    json={"ignore_liquidity": False}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert wired["level"].ignore_liquidity_gate is False


async def test_editing_the_price_alone_leaves_an_existing_waiver_alone(wired):
    """Absent is not False: moving the price must not re-arm the floors.

    A level placed deliberately without them would otherwise start sitting in
    BLOCKED after an edit that said nothing about liquidity at all.
    """
    wired["level"] = _Level(ignore_liquidity_gate=True)
    async with client() as http:
        response = await http.patch("/api/fomo/limit-order/7",
                                    json={"limit_price": "0.6"}, headers=AUTH)
    assert response.status_code == 200, response.text
    assert wired["level"].ignore_liquidity_gate is True
