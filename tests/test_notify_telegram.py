"""Клиент Telegram: что можно повторить, что бессмысленно, и что экранируется."""
import httpx
import pytest

from app.core.config import settings
from app.notify.telegram import (
    MESSAGE_LIMIT,
    TelegramClient,
    TelegramError,
    TelegramRetry,
    escape,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeHttpClient:
    def __init__(self, payload, status_code=200, raises=None):
        self.payload = payload
        self.status_code = status_code
        self.raises = raises
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.payload, self.status_code)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_api_base", "https://api.telegram.org")


def client(http) -> TelegramClient:
    return TelegramClient(http=http)


async def test_sends_html_without_link_previews():
    http = FakeHttpClient({"ok": True, "result": {"message_id": 42}})
    assert await client(http).send_message("-100500", "<b>ok</b>") == 42
    url, body = http.calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert body["chat_id"] == "-100500"
    assert body["parse_mode"] == "HTML"
    assert body["disable_web_page_preview"] is True


async def test_long_message_is_truncated_not_rejected():
    http = FakeHttpClient({"ok": True, "result": {"message_id": 1}})
    await client(http).send_message("1", "x" * (MESSAGE_LIMIT + 500))
    assert len(http.calls[0][1]["text"]) == MESSAGE_LIMIT


async def test_rate_limit_is_retryable_and_carries_its_delay():
    http = FakeHttpClient(
        {"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}},
        status_code=429,
    )
    with pytest.raises(TelegramRetry) as caught:
        await client(http).send_message("1", "hi")
    assert caught.value.retry_after == 17


async def test_server_error_is_retryable():
    http = FakeHttpClient({"ok": False, "description": "Bad Gateway"}, status_code=502)
    with pytest.raises(TelegramRetry):
        await client(http).send_message("1", "hi")


async def test_network_failure_is_retryable():
    http = FakeHttpClient(None, raises=httpx.ConnectError("no route"))
    with pytest.raises(TelegramRetry):
        await client(http).send_message("1", "hi")


async def test_refusal_is_permanent():
    # Те же байты Telegram откажет так же -- повторять нечего.
    http = FakeHttpClient({"ok": False, "description": "chat not found"}, status_code=400)
    with pytest.raises(TelegramError) as caught:
        await client(http).send_message("1", "hi")
    assert not isinstance(caught.value, TelegramRetry)
    assert "chat not found" in str(caught.value)


async def test_ok_false_on_a_200_is_still_a_refusal():
    http = FakeHttpClient({"ok": False, "description": "can't parse entities"})
    with pytest.raises(TelegramError):
        await client(http).send_message("1", "hi")


async def test_missing_token_is_not_a_network_call(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    http = FakeHttpClient({"ok": True})
    with pytest.raises(TelegramError):
        await client(http).send_message("1", "hi")
    assert http.calls == []


def test_escape_protects_interpolated_text():
    # Тикер или причина отказа с «<» -- это 400 от Telegram и молчание.
    assert escape("a < b & c") == "a &lt; b &amp; c"
