"""Текст сообщения: что в нём обязано быть, и когда его не должно быть вовсе."""
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import DexIntent, GridProfile, Notification
from app.notify.render import display_symbol, number, received_coin, render


@dataclass
class FakeSession:
    """``session.get(Model, id)`` -- это всё, что нужно рендеру."""

    intents: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)

    async def get(self, model, key):
        store = self.intents if model is DexIntent else self.profiles
        return store.get(int(key))


def intent(**overrides) -> DexIntent:
    row = DexIntent(
        id=7, profile_id=None, order_link_id="lvl-7", symbol="PONSUSDG", side="Buy",
        status="FILLED", limit_price=Decimal("0.2030"), amount_in=Decimal("250"),
        amount_in_coin="USDG",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def note(kind: str, payload: dict) -> Notification:
    return Notification(kind=kind, chat_id="1", payload=payload, status="PENDING")


@pytest.fixture(autouse=True)
def no_public_url(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "")


# ---- числа и тикеры ---------------------------------------------------------

def test_precision_follows_the_size_of_the_number():
    # Один фиксированный знак не может обслужить и 65 000, и 0.00004182.
    assert number("65000") == "65 000"
    assert number("1234.5678") == "1 234.57"
    assert number("0.000041823456") == "0.00004182"
    assert number("0.2030") == "0.203"
    assert number(0) == "0"
    assert number(None) is None
    assert number("не число") is None


def test_pair_key_is_shown_as_a_ticker():
    assert display_symbol("PONSUSDG") == "PONS-USDG"
    # Незнакомая пара всё равно читается -- по форме самого ключа.
    assert display_symbol("WHATEVERUSDT") == "WHATEVER-USDT"


def test_received_coin_flips_with_the_side():
    assert received_coin("PONSUSDG", "Buy") == "PONS"
    assert received_coin("PONSUSDG", "Sell") == "USDG"
    # Нераспознанная пара не получает выдуманный тикер.
    assert received_coin("ZZZ", "Buy") == ""


# ---- DEX --------------------------------------------------------------------

async def test_fill_reports_both_legs_price_gas_and_tx():
    row = intent(
        filled_amount_in=Decimal("250"), filled_amount_out=Decimal("1234.5"),
        fill_price=Decimal("0.2025"), gas_quote=Decimal("0.31"), gas_quote_coin="USDG",
        tx_hash="0x" + "ab" * 32,
    )
    session = FakeSession(intents={7: row})
    text = await render(session, note("dex.filled", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FILLED",
    }))
    assert "Исполнено" in text and "PONS-USDG" in text
    assert "250 USDG" in text and "1 234.5 PONS" in text
    assert "0.2025" in text and "лимит 0.203" in text
    assert "0.31 USDG" in text
    assert "0x" + "ab" * 32 in text


async def test_hand_placed_level_says_so():
    session = FakeSession(intents={7: intent(filled_amount_in=Decimal("250"))})
    text = await render(session, note("dex.filled", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FILLED",
    }))
    assert "вручную" in text


async def test_grid_level_names_its_profile():
    profile = GridProfile(id=3, name="PONS 0.19–0.21", symbol="PONSUSDG")
    session = FakeSession(
        intents={7: intent(profile_id=3, filled_amount_in=Decimal("250"))},
        profiles={3: profile},
    )
    text = await render(session, note("dex.filled", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FILLED",
        "profile_id": 3,
    }))
    assert "PONS 0.19–0.21" in text


async def test_failure_carries_the_reason_not_the_amounts():
    row = intent(status="FAILED", last_error="execution reverted")
    session = FakeSession(intents={7: row})
    text = await render(session, note("dex.failed", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FAILED",
        "reason": "execution reverted",
    }))
    assert "Ошибка" in text
    assert "execution reverted" in text
    assert "Покупка: 250 USDG по 0.203" in text


async def test_status_comes_from_the_payload_not_the_row():
    # Заблокированный уровень через 30 секунд снова WAITING; сообщение обязано
    # описывать то, что произошло, а не то, чем строка стала потом.
    session = FakeSession(intents={7: intent(status="WAITING")})
    text = await render(session, note("dex.missed", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "MISSED",
        "reason": "balance 10 USDG < 250 USDG",
    }))
    assert "Не хватило средств" in text
    assert "balance 10 USDG" in text


async def test_dex_message_survives_a_deleted_intent():
    text = await render(FakeSession(), note("dex.expired", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "EXPIRED",
    }))
    assert text is not None and "Истёк срок" in text


# ---- сетка ------------------------------------------------------------------

async def test_grid_event_names_profile_states_and_price():
    session = FakeSession(profiles={3: GridProfile(id=3, name="BTC 62–67k", symbol="BTCUSDT")})
    text = await render(session, note("grid.order_filled", {
        "profile_id": 3, "event_type": "ORDER_FILLED",
        "from_state": "New", "to_state": "Filled", "market_price": "65000",
    }))
    assert "Ордер исполнен" in text and "BTCUSDT" in text
    assert "BTC 62–67k" in text
    assert "New → " in text and "Filled" in text
    assert "65 000" in text


async def test_unknown_event_type_is_still_readable():
    session = FakeSession(profiles={3: GridProfile(id=3, name="BTC", symbol="BTCUSDT")})
    text = await render(session, note("grid.something_new", {
        "profile_id": 3, "event_type": "SOMETHING_NEW",
    }))
    assert "Something new" in text


async def test_grid_message_is_dropped_when_the_profile_is_gone():
    # Без профиля сообщение сказало бы, что что-то где-то случилось с чем-то.
    assert await render(FakeSession(), note("grid.order_filled", {
        "profile_id": 3, "event_type": "ORDER_FILLED",
    })) is None


async def test_unknown_namespace_is_dropped():
    assert await render(FakeSession(), note("weather.rain", {})) is None


# ---- ссылка и экранирование -------------------------------------------------

async def test_public_url_adds_a_link_to_the_history_page(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://example.org/grid/")
    session = FakeSession(intents={7: intent(filled_amount_in=Decimal("250"))})
    text = await render(session, note("dex.filled", {
        "intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FILLED",
    }))
    assert '<a href="https://example.org/grid/history">' in text


async def test_markup_in_a_profile_name_is_escaped():
    session = FakeSession(profiles={3: GridProfile(id=3, name="BTC <b>", symbol="BTCUSDT")})
    text = await render(session, note("grid.order_filled", {
        "profile_id": 3, "event_type": "ORDER_FILLED",
    }))
    assert "BTC &lt;b&gt;" in text
