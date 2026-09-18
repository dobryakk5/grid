"""Minimal Telegram Bot API client: one method, and honest error classes.

Only ``sendMessage`` is implemented, because only ``sendMessage`` is needed --
this is a one-way channel out of the bot, not a conversation. The one piece of
real logic is telling apart the two failures that matter:

``TelegramRetry``
    Rate limit, a 5xx, or the network. The message is still good; try later.
    A 429 carries ``retry_after``, and Telegram means it -- ignoring it is how
    a bot earns a longer ban.

``TelegramError``
    Telegram understood and refused: a bad chat id, a bot blocked by the user,
    malformed markup. Sending the same bytes again will fail the same way, so
    the outbox burns an attempt rather than looping.
"""

from __future__ import annotations

import html
import logging

import httpx

from app.core.config import settings

__all__ = ["MESSAGE_LIMIT", "TelegramClient", "TelegramError", "TelegramRetry", "escape"]

logger = logging.getLogger(__name__)

#: Telegram rejects anything longer, so the caller truncates rather than loses.
MESSAGE_LIMIT = 4096


class TelegramError(RuntimeError):
    """Telegram refused the message; re-sending it unchanged will not help."""


class TelegramRetry(TelegramError):
    """Temporary: rate limit, server error, or an unreachable API."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def escape(value: object) -> str:
    """Escape for ``parse_mode=HTML``.

    Everything interpolated into a message comes from somewhere else -- a pair
    symbol from a registry, a failure reason from an RPC node, a profile name
    typed by a human. A stray ``<`` in any of them is a 400 from Telegram and a
    message that never arrives.
    """
    return html.escape(str(value), quote=False)


class TelegramClient:
    def __init__(self, *, http: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.telegram_api_base.rstrip("/")
        self.token = settings.telegram_bot_token.strip()
        self.client = http or httpx.AsyncClient(timeout=settings.notify_timeout_seconds)
        self._owns_client = http is None

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def send_message(self, chat_id: str, text: str) -> int:
        """Send one HTML message; returns Telegram's message id."""
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        payload = {
            "chat_id": chat_id,
            "text": text[:MESSAGE_LIMIT],
            "parse_mode": "HTML",
            # A contract address in a message must not drag a preview card of
            # whatever site happens to recognise it into the chat.
            "disable_web_page_preview": True,
        }
        try:
            response = await self.client.post(
                f"{self.base_url}/bot{self.token}/sendMessage", json=payload
            )
        except httpx.HTTPError as exc:
            raise TelegramRetry(f"telegram unreachable: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {}
        description = body.get("description") or (response.text or "")[:200]

        if response.status_code == 429:
            retry_after = (body.get("parameters") or {}).get("retry_after")
            raise TelegramRetry(
                f"rate limited: {description}",
                retry_after=float(retry_after) if retry_after is not None else None,
            )
        if response.status_code >= 500:
            raise TelegramRetry(f"telegram {response.status_code}: {description}")
        if response.status_code != 200 or not body.get("ok"):
            # The token never reaches a log line: this message is what ends up
            # in `notifications.last_error`, which the API can serve.
            raise TelegramError(f"telegram {response.status_code}: {description}")
        return int((body.get("result") or {}).get("message_id") or 0)
