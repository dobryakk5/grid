"""Подписка на операции: шаблоны, фан-аут по чатам и «выключено» как режим."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import Notification
from app.notify.events import (
    DEX_NOTIFIABLE,
    chat_ids,
    dex_kind,
    grid_kind,
    is_configured,
    is_enabled,
)
from app.notify.outbox import PENDING, SENT, enqueue, mark_failed, mark_sent


class FakeSession:
    """Достаточно для enqueue: он только добавляет строки в сессию."""

    def __init__(self) -> None:
        self.added = []

    def add(self, row) -> None:
        self.added.append(row)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "-100500")
    return settings


def test_kinds_are_lowercased_namespaces():
    assert dex_kind("FILLED") == "dex.filled"
    assert grid_kind("ORDER_FILLED") == "grid.order_filled"


def test_machinery_states_are_not_notifiable():
    # Уровень по пути к исполнению не должен звонить шесть раз.
    for status in ("WAITING", "TRIGGERED", "QUOTED", "SIGNING", "SUBMITTING", "PENDING"):
        assert status not in DEX_NOTIFIABLE
    assert "FILLED" in DEX_NOTIFIABLE


def test_patterns_match_by_glob(monkeypatch):
    monkeypatch.setattr(settings, "notify_events", "dex.filled,grid.recovery_*")
    assert is_enabled("dex.filled")
    assert is_enabled("grid.recovery_long_opened")
    assert not is_enabled("dex.blocked")
    assert not is_enabled("grid.order_synced")


def test_star_subscribes_to_everything(monkeypatch):
    monkeypatch.setattr(settings, "notify_events", "*")
    assert is_enabled("grid.position_lot_created")


def test_default_subscription_is_money_and_attention(monkeypatch):
    # Значения по умолчанию — это тоже решение: исполнения и ошибки да,
    # бухгалтерия и переспрашивание риск-гейта каждые 30 секунд нет.
    for kind in ("dex.opened", "dex.filled", "dex.failed", "dex.missed", "grid.order_filled"):
        assert is_enabled(kind), kind
    for kind in ("dex.blocked", "grid.order_synced", "grid.position_lot_created"):
        assert not is_enabled(kind), kind


def test_blank_token_or_chat_means_off(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "-100500")
    assert not is_configured()
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "  ")
    assert not is_configured()


def test_enqueue_is_a_noop_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    session = FakeSession()
    assert enqueue(session, "dex.filled", {"intent_id": 1}) == []
    assert session.added == []


def test_enqueue_skips_unsubscribed_kinds(configured, monkeypatch):
    monkeypatch.setattr(settings, "notify_events", "dex.filled")
    session = FakeSession()
    assert enqueue(session, "dex.blocked", {"intent_id": 1}) == []
    assert session.added == []


def test_enqueue_fans_out_one_row_per_chat(configured, monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", "-100500, 777 ,")
    monkeypatch.setattr(settings, "notify_events", "*")
    session = FakeSession()
    rows = enqueue(session, "dex.filled", {"intent_id": 7})
    assert [row.chat_id for row in rows] == ["-100500", "777"]
    assert session.added == rows
    assert all(row.status == PENDING for row in rows)


def test_enqueue_stringifies_decimals(configured, monkeypatch):
    # JSONB отказывается от Decimal, а потерянный payload — потерянное сообщение.
    monkeypatch.setattr(settings, "notify_events", "*")
    session = FakeSession()
    rows = enqueue(session, "dex.filled", {
        "limit_price": Decimal("0.2030"),
        "metadata": {"levels": [Decimal("1.5")]},
    })
    assert rows[0].payload == {"limit_price": "0.2030", "metadata": {"levels": ["1.5"]}}


def test_chat_ids_ignores_blanks(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_id", " , 1, ,2 ")
    assert chat_ids() == ["1", "2"]


def test_retry_backs_off_then_gives_up(monkeypatch):
    monkeypatch.setattr(settings, "notify_max_attempts", 3)
    row = Notification(kind="dex.filled", chat_id="1", payload={}, status=PENDING, attempts=0)
    first = row.scheduled_at
    mark_failed(row, "telegram 500")
    assert row.status == PENDING and row.attempts == 1 and row.scheduled_at != first
    mark_failed(row, "telegram 500")
    assert row.status == PENDING and row.attempts == 2
    mark_failed(row, "telegram 500")
    # Недоставленное сообщение не повод держать очередь.
    assert row.status == "FAILED" and row.attempts == 3


def test_retry_after_wins_over_the_backoff(monkeypatch):
    monkeypatch.setattr(settings, "notify_max_attempts", 9)
    row = Notification(kind="dex.filled", chat_id="1", payload={}, status=PENDING, attempts=0)
    mark_failed(row, "rate limited", retry_after=120)
    # Собственный бэкофф на первой попытке -- 2 секунды; названные Telegram
    # 120 игнорировать нельзя, за это банят дольше.
    delay = (row.scheduled_at - datetime.now(timezone.utc)).total_seconds()
    assert 100 < delay <= 120
    assert row.last_error == "rate limited"


def test_mark_sent_clears_the_last_error():
    row = Notification(kind="dex.filled", chat_id="1", payload={}, status=PENDING)
    mark_failed(row, "telegram 500")
    mark_sent(row)
    assert row.status == SENT and row.sent_at is not None and row.last_error is None
