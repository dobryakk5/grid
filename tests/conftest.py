"""Что общее для всех тестов."""
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def telegram_off(monkeypatch):
    # Настройки читаются из .env разработчика; с настоящим ботом в нём любая
    # сделка в тесте ставила бы уведомление в очередь. Тесты уведомлений
    # включают Telegram сами.
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")
