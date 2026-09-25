"""Команды бота: кому отвечать, на что, и что считать открытой заявкой."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.db.models import DexIntent
from app.dex.repository import DexIntentRepository
from app.notify.commands import CommandPoller, _chunks, _command, open_orders_messages
from app.notify.telegram import MESSAGE_LIMIT, TelegramError


def level(id=1, **overrides) -> DexIntent:
    row = DexIntent(
        id=id, profile_id=None, order_link_id=f"lvl-{id}", symbol="PONSUSDG", side="Buy",
        status="WAITING", limit_price=Decimal("0.2030"), amount_in=Decimal("250"),
        amount_in_coin="USDG",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self.rows


class FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return FakeResult(self.rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for index, row in enumerate(self.added, start=1):
            if getattr(row, "id", None) is None and isinstance(row, DexIntent):
                row.id = index


def factory(session):
    @asynccontextmanager
    async def open_session():
        yield session
    return open_session


class FakeTelegram:
    def __init__(self, updates=(), fail_send=False):
        self.updates = list(updates)
        self.offsets = []
        self.sent = []
        self.menus = []
        self.fail_send = fail_send

    async def set_commands(self, commands):
        self.menus.append(commands)

    async def get_updates(self, offset=None):
        self.offsets.append(offset)
        pending, self.updates = self.updates, []
        return pending

    async def send_message(self, chat_id, text):
        if self.fail_send:
            raise TelegramError("bot was blocked by the user")
        self.sent.append((chat_id, text))
        return 1


def update(update_id, text, chat="-100500", age=0):
    now = datetime.now(timezone.utc).timestamp()
    return {"update_id": update_id,
            "message": {"chat": {"id": int(chat)}, "text": text, "date": int(now - age)}}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "-100500")


def test_command_spellings():
    assert _command("/open") == "open"
    assert _command("/open@GridBot") == "open"
    assert _command("Open") == "open"
    assert _command("/start") is None
    assert _command("") is None


async def test_open_lists_levels_with_their_terms():
    session = FakeSession([level(1), level(2, side="Sell", status="MISSED", profile_id=3,
                                     amount_in=Decimal("1000"), amount_in_coin="PONS")])
    [text] = await open_orders_messages(session)
    assert "Открытые заявки: 2" in text
    assert "PONS-USDG" in text
    assert "Покупка 250 USDG по 0.203 · ждёт · вручную" in text
    assert "не хватило средств · сетка" in text


async def test_no_open_levels_is_said_plainly():
    assert await open_orders_messages(FakeSession()) == ["Открытых заявок нет"]


def test_long_list_is_split_on_whole_lines():
    lines = [f"line {i} " + "x" * 90 for i in range(100)]
    messages = _chunks("header", lines)
    assert len(messages) > 1
    assert all(len(message) <= MESSAGE_LIMIT for message in messages)
    assert "\n".join(messages).split("\n")[1:] == lines


async def test_poller_answers_and_confirms_what_it_read():
    telegram = FakeTelegram([update(10, "/open")])
    poller = CommandPoller(telegram)
    assert await poller.poll(factory(FakeSession([level()]))) == 1
    assert telegram.sent[0][0] == "-100500"
    assert "Открытые заявки: 1" in telegram.sent[0][1]
    assert telegram.menus == [{"open": "Открытые заявки"}]
    await poller.poll(factory(FakeSession()))
    assert telegram.offsets == [None, 11]
    assert len(telegram.menus) == 1


async def test_strangers_and_stale_commands_get_nothing():
    telegram = FakeTelegram([update(1, "/open", chat="777"), update(2, "/open", age=600),
                             update(3, "hello")])
    poller = CommandPoller(telegram)
    assert await poller.poll(factory(FakeSession([level()]))) == 0
    assert telegram.sent == []
    assert poller.offset == 4


async def test_a_failed_reply_does_not_escape_the_poll():
    telegram = FakeTelegram([update(1, "/open")], fail_send=True)
    assert await CommandPoller(telegram).poll(factory(FakeSession([level()]))) == 0


async def test_new_level_is_announced_but_a_retry_is_not():
    session = FakeSession()
    repository = DexIntentRepository(session)
    fields = dict(symbol="PONSUSDG", side="Buy", limit_price=Decimal("0.2"),
                  amount_in=Decimal("250"), amount_in_coin="USDG")
    await repository.create_level(order_link_id="a", **fields)
    await repository.create_level(order_link_id="b", parent_intent_id=1, retry_count=1, **fields)
    kinds = [row.kind for row in session.added if not isinstance(row, DexIntent)]
    assert kinds == ["dex.opened"]
