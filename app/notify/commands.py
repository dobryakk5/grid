"""The few commands the bot answers, read off ``getUpdates``.

Everything else the bot says is pushed through the outbox; this is the one
place it is asked. Answers go straight to Telegram rather than through the
queue: a reply is only worth anything to the person who is waiting for it, so
there is nothing to retry after a restart.

Only chats listed in ``TELEGRAM_CHAT_ID`` get an answer. The bot's username is
public, and a list of open orders is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DexIntent
from app.dex.intents import TERMINAL_STATUSES, IntentStatus
from app.dex.tokens import plain_symbol
from app.notify.events import chat_ids
from app.notify.render import SIDES, display_symbol, number
from app.notify.telegram import MESSAGE_LIMIT, TelegramClient, TelegramError, escape

__all__ = ["COMMANDS", "CommandPoller", "open_orders_messages"]

logger = logging.getLogger(__name__)

#: Shown in Telegram's command menu.
COMMANDS = {"open": "Открытые заявки"}

#: A command older than this is from before a restart; answering it now would
#: answer a question nobody is still asking.
STALE_SECONDS = 120

#: Said only when a level is not simply waiting: those are the ones to look at.
ATTENTION = {
    IntentStatus.BLOCKED: "🚧 риск-фильтр",
    IntentStatus.MISSED: "⚠️ не хватило средств",
}


def _line(intent: DexIntent) -> str:
    """``Продажа 723.44 PONS по 0.723`` -- the pair is already in the heading."""
    side = SIDES.get(intent.side.strip().lower(), intent.side)
    amount, limit = number(intent.amount_in), number(intent.limit_price)
    text = escape(side)
    if amount:
        text += f" {amount} {escape(plain_symbol(intent.amount_in_coin))}"
    if limit:
        text += f" по {limit}"
    if intent.status != IntentStatus.WAITING:
        text += f" · {ATTENTION.get(intent.status, '⏳ исполняется')}"
    return text


def _order(intent: DexIntent) -> tuple:
    """Sells, then buys; each nearest-to-market first, so the next fill leads."""
    selling = intent.side.strip().lower() == "sell"
    price = intent.limit_price or 0
    return (0, price) if selling else (1, -price)


def _chunks(header: str, lines: list[str]) -> list[str]:
    """Whole lines per message: a list cut mid-line reads as a broken order."""
    messages, current = [], header
    for line in lines:
        if len(current) + 1 + len(line) > MESSAGE_LIMIT:
            messages.append(current)
            current = line
        else:
            current += "\n" + line
    messages.append(current)
    return messages


async def open_orders_messages(session: AsyncSession) -> list[str]:
    """The ``/open`` answer: every level the history page lists as open,
    under one heading per pair."""
    statement = (
        select(DexIntent)
        .where(DexIntent.status.not_in(tuple(TERMINAL_STATUSES)))
        .order_by(DexIntent.id)
    )
    intents = list((await session.execute(statement)).scalars())
    if not intents:
        return ["Открытых заявок нет"]
    # Grouped in order of first appearance, so the oldest position leads.
    groups: dict[str, list[DexIntent]] = {}
    for intent in intents:
        groups.setdefault(display_symbol(intent.symbol), []).append(intent)
    lines: list[str] = []
    for pair, levels in groups.items():
        lines += ["", f"<b>{escape(pair)}</b>"]
        lines += [_line(intent) for intent in sorted(levels, key=_order)]
    return _chunks(f"📋 <b>Открытые заявки: {len(intents)}</b>", lines)


def _command(text: str) -> str | None:
    """``/open``, ``/open@SomeBot`` and a bare ``open`` are the same command."""
    words = (text or "").strip().split()
    if not words:
        return None
    name = words[0].split("@", 1)[0].lstrip("/").lower()
    return name if name in COMMANDS else None


class CommandPoller:
    """Confirms updates as it reads them, so each command is answered once."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.offset: int | None = None
        self.menu_set = False

    async def poll(self, session_factory) -> int:
        """Answer every pending command; returns how many were answered."""
        if not self.menu_set:
            # Once per process; a failure only means no menu, never no answers.
            self.menu_set = True
            try:
                await self.client.set_commands(COMMANDS)
            except Exception as exc:
                logger.warning("setMyCommands failed: %s", exc)

        updates = await self.client.get_updates(self.offset)
        allowed = set(chat_ids())
        now = datetime.now(timezone.utc).timestamp()
        answered = 0
        for update in updates:
            self.offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            chat = str((message.get("chat") or {}).get("id", ""))
            command = _command(message.get("text", ""))
            if command is None or chat not in allowed:
                continue
            if now - float(message.get("date") or 0) > STALE_SECONDS:
                continue
            async with session_factory() as session:
                replies = await open_orders_messages(session)
            # Caught here, so that whatever escapes poll() is about getUpdates
            # itself -- the worker treats that as the channel being unusable.
            try:
                for reply in replies:
                    await self.client.send_message(chat, reply)
            except TelegramError as exc:
                logger.warning("/%s reply to %s failed: %s", command, chat, exc)
                continue
            answered += 1
        return answered
