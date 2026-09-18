"""Цикл доставки: одна строка -- одна транзакция, отказ не останавливает очередь."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import DexIntent, GridProfile, Notification
from app.notify.telegram import TelegramError, TelegramRetry
from app.workers.notifier import tick


@dataclass
class FakeSession:
    """Очередь, отдаваемая по одной строке, как это делает claim(limit=1)."""

    queue: list = field(default_factory=list)
    profiles: dict = field(default_factory=dict)
    intents: dict = field(default_factory=dict)
    commits: int = 0
    rollbacks: int = 0

    async def execute(self, _statement):
        # Как и настоящий claim(): только PENDING и только то, чей срок пришёл.
        now = datetime.now(timezone.utc)
        due = [
            row for row in self.queue
            if row.status == "PENDING" and (row.scheduled_at is None or row.scheduled_at <= now)
        ]
        return FakeResult(due[:1])

    async def get(self, model, key):
        store = self.intents if model is DexIntent else self.profiles
        return store.get(int(key))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@dataclass
class FakeResult:
    rows: list

    def scalars(self):
        return self.rows


class FakeTelegram:
    def __init__(self, *errors):
        # Один элемент на вызов: None -- успех, иначе исключение.
        self.errors = list(errors)
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error
        return len(self.sent)


def note(kind="grid.order_filled", **payload) -> Notification:
    body = {"profile_id": 3, "event_type": "ORDER_FILLED", **payload}
    return Notification(kind=kind, chat_id="-1", payload=body, status="PENDING", attempts=0)


def session_with(*rows) -> FakeSession:
    return FakeSession(
        queue=list(rows),
        profiles={3: GridProfile(id=3, name="BTC 62–67k", symbol="BTCUSDT")},
    )


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(settings, "notify_send_pause_seconds", 0)
    monkeypatch.setattr(settings, "notify_batch", 10)
    monkeypatch.setattr(settings, "notify_max_attempts", 6)
    monkeypatch.setattr(settings, "public_base_url", "")


async def test_empty_queue_sends_nothing_and_holds_no_transaction():
    session = FakeSession()
    assert await tick(session, FakeTelegram()) == 0
    assert session.rollbacks == 1 and session.commits == 0


async def test_each_message_is_committed_on_its_own():
    rows = [note(), note(), note()]
    session = session_with(*rows)
    telegram = FakeTelegram()
    assert await tick(session, telegram) == 3
    assert len(telegram.sent) == 3
    assert all(row.status == "SENT" for row in rows)
    # Три отправки, три коммита, плюс откат на пустой очереди в конце.
    assert session.commits == 3 and session.rollbacks == 1


async def test_rate_limit_defers_the_row_and_stops_retrying_it_this_pass():
    row = note()
    session = session_with(row)
    telegram = FakeTelegram(TelegramRetry("rate limited", retry_after=30))
    assert await tick(session, telegram) == 0
    assert row.status == "PENDING" and row.attempts == 1
    # Отложенная строка больше не выбирается: иначе один 429 съел бы весь пакет.
    assert len(telegram.sent) == 1


async def test_a_refusal_does_not_block_the_messages_behind_it():
    bad, good = note(), note()
    session = session_with(bad, good)
    telegram = FakeTelegram(TelegramError("chat not found"), None)
    assert await tick(session, telegram) == 1
    assert bad.status == "PENDING" and bad.attempts == 1
    assert good.status == "SENT"


async def test_a_message_with_nothing_left_to_say_is_dropped_not_retried():
    orphan = note(profile_id=999)
    session = session_with(orphan)
    telegram = FakeTelegram()
    assert await tick(session, telegram) == 0
    assert orphan.status == "SENT" and telegram.sent == []


async def test_batch_size_bounds_one_pass(monkeypatch):
    monkeypatch.setattr(settings, "notify_batch", 2)
    rows = [note(), note(), note()]
    session = session_with(*rows)
    telegram = FakeTelegram()
    assert await tick(session, telegram) == 2
    assert [row.status for row in rows] == ["SENT", "SENT", "PENDING"]


async def test_dex_fill_renders_through_the_worker():
    row = Notification(
        kind="dex.filled", chat_id="-1", status="PENDING", attempts=0,
        payload={"intent_id": 7, "symbol": "PONSUSDG", "side": "Buy", "status": "FILLED"},
    )
    session = FakeSession(queue=[row], intents={7: DexIntent(
        id=7, order_link_id="lvl-7", symbol="PONSUSDG", side="Buy", status="FILLED",
        limit_price=Decimal("0.2030"), amount_in=Decimal("250"), amount_in_coin="USDG",
        filled_amount_in=Decimal("250"), filled_amount_out=Decimal("1234.5"),
        fill_price=Decimal("0.2025"),
    )})
    telegram = FakeTelegram()
    assert await tick(session, telegram) == 1
    assert "PONS-USDG" in telegram.sent[0][1]
