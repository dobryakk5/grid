"""Turning a queued operation into the message that actually arrives.

Rendering happens at send time, not at enqueue time, so that a call site deep
in the grid engine can queue ``{"profile_id": 3}`` without loading the profile
inside a trading transaction. The cost is that the text describes the row as it
is now; the payload therefore carries everything that *changes* -- the status,
the reason, the states either side of a transition -- and only the stable parts
(pair, amounts, price, tx hash) are read back from the database.

A renderer that cannot find its row returns ``None``. That is a dropped
message, deliberately: a notification about a profile someone has since deleted
has nothing left to say.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DexIntent, GridProfile, Notification
from app.dex.intents import IntentStatus
from app.dex.tokens import DexConfigError, plain_symbol, resolve_pair
from app.notify.events import DEX_OPENED
from app.notify.telegram import escape

__all__ = ["display_symbol", "render"]


#: Headline per intent status: an emoji a glance can read, then the word.
DEX_HEADLINES = {
    IntentStatus.FILLED: ("✅", "Исполнено"),
    IntentStatus.FAILED: ("⛔️", "Ошибка"),
    IntentStatus.MISSED: ("⚠️", "Не хватило средств"),
    IntentStatus.BLOCKED: ("🚧", "Заблокировано риск-фильтром"),
    IntentStatus.EXPIRED: ("⌛️", "Истёк срок"),
    IntentStatus.CANCELLED: ("🚫", "Отменено"),
}
#: Not a status: an armed level is WAITING, and so is one back from BLOCKED.
OPENED_HEADLINE = ("🆕", "Открыта заявка")

#: Strategy events phrased for a reader. Anything missing falls back to its own
#: name, so a new event type is a plain message rather than a silent one.
GRID_HEADLINES = {
    "ORDER_FILLED": ("✅", "Ордер исполнен"),
    "ORDER_CANCEL_REFUSED": ("⚠️", "Отмена ордера отклонена"),
    "GRID_BUDGET_BLOCKED": ("🚧", "Бюджет сетки исчерпан"),
    "GRID_RANGE_CREATED": ("🆕", "Создан диапазон"),
    "GRID_RANGE_ACTIVATED": ("▶️", "Диапазон активирован"),
    "GRID_RANGE_PAUSED": ("⏸", "Диапазон остановлен"),
    "TRAILING_BUY_STARTED": ("🎯", "Запущен трейлинг-вход"),
    "TRAILING_BUY_RESTARTED": ("🎯", "Трейлинг-вход перезапущен"),
    "TRAILING_BUY_TRIGGERED": ("🎯", "Трейлинг-вход сработал"),
    "RECOVERY_ENTERING": ("🛟", "Вход в восстановление"),
    "RECOVERY_LONG_OPENED": ("🛟", "Открыт лонг восстановления"),
    "RECOVERY_EXITING": ("🛟", "Выход из восстановления"),
    "RECOVERY_EXIT_FILLED": ("🛟", "Выход из восстановления исполнен"),
    "RECOVERY_HARD_STOP_REQUESTED": ("🛑", "Запрошен жёсткий стоп"),
    "RECOVERY_WAIT_MANUAL": ("✋", "Ждёт ручного решения"),
    "RECOVERY_ERROR": ("⛔️", "Ошибка восстановления"),
    "RECOMMENDATION_CREATED": ("💡", "Новая рекомендация"),
    "RECOMMENDATION_ACCEPTED": ("👍", "Рекомендация принята"),
    "RECOMMENDATION_REJECTED": ("👎", "Рекомендация отклонена"),
    "RECOMMENDATION_EXPIRED": ("⌛️", "Рекомендация истекла"),
}

SIDES = {"buy": "Покупка", "sell": "Продажа"}


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def number(value) -> str | None:
    """A figure at a precision a person reads, not the 18 decimals we store.

    Scaled by size rather than fixed: 0.00004182 and 1 234.5 both need to be
    legible, and one number of decimal places cannot do both.
    """
    amount = _decimal(value)
    if amount is None:
        return None
    if amount == 0:
        return "0"
    size = abs(amount)
    places = 2 if size >= 100 else 4 if size >= 1 else 8
    quantised = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(quantised, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    # Thousands read better spaced in Russian than comma-grouped.
    return (text or "0").replace(",", " ")


def display_symbol(symbol: str) -> str:
    """``PONSUSDG`` -> ``PONS-USDG``, without inventing a pair we cannot resolve.

    Mirrors what the history page shows (``/api/dex/orders``): the pair key is
    what everything else is addressed by, but it is not what a person reads.
    """
    try:
        pair = resolve_pair(symbol)
    except DexConfigError:
        bare = (symbol or "").upper()
        for quote in ("USDG", "WETH", "ETH", "USDT", "USDC"):
            if bare.endswith(quote) and len(bare) > len(quote):
                return f"{plain_symbol(bare[: -len(quote)])}-{quote}"
        return plain_symbol(bare) or symbol
    return f"{plain_symbol(pair.base.symbol)}-{pair.quote_coin}"


def received_coin(symbol: str, side: str) -> str:
    """The ticker a fill pays *out*, or ``""`` when the pair cannot be resolved.

    The intent row only stores what it spends; the other leg is a property of
    the pair. Guessing it from the key would be worse than leaving the figure
    unlabelled, so an unresolvable pair gets no label at all.
    """
    try:
        pair = resolve_pair(symbol)
    except DexConfigError:
        return ""
    return plain_symbol(pair.base.symbol) if side.strip().lower() == "buy" else pair.quote_coin


def _link(path: str, label: str) -> str | None:
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        return None
    return f'<a href="{escape(base + path)}">{escape(label)}</a>'


async def _profile_line(session: AsyncSession, profile_id) -> str | None:
    if not profile_id:
        return None
    profile = await session.get(GridProfile, int(profile_id))
    if profile is None:
        return None
    return f"Профиль: <b>{escape(profile.name)}</b> ({escape(profile.symbol)})"


async def _render_dex(session: AsyncSession, notification: Notification) -> str | None:
    payload = notification.payload or {}
    status = str(payload.get("status") or "").upper()
    emoji, headline = DEX_HEADLINES.get(status, ("•", status or "Событие"))
    if notification.kind == DEX_OPENED:
        emoji, headline = OPENED_HEADLINE

    intent = None
    if payload.get("intent_id"):
        intent = await session.get(DexIntent, int(payload["intent_id"]))

    symbol = payload.get("symbol") or (intent.symbol if intent else "")
    side = str(payload.get("side") or (intent.side if intent else "")).strip()
    pair = display_symbol(symbol) if symbol else "—"

    lines = [f"{emoji} <b>{escape(headline)}</b> · {escape(pair)}"]

    if notification.kind != DEX_OPENED and status == IntentStatus.FILLED and intent is not None:
        spent, got = number(intent.filled_amount_in), number(intent.filled_amount_out)
        # Which coin is spent and which received flips with the side; the
        # intent only stores what it pays *in*, so the other one comes off the
        # pair. Left off entirely rather than guessed when the pair is unknown.
        traded = f"{SIDES.get(side.lower(), side)}"
        if spent:
            traded += f": {spent} {escape(intent.amount_in_coin)}"
        if got:
            out_coin = received_coin(symbol, side)
            traded += f" → {got} {escape(out_coin)}".rstrip()
        lines.append(traded)
        fill, limit = number(intent.fill_price), number(intent.limit_price)
        if fill:
            lines.append(f"Цена: <b>{fill}</b>" + (f" (лимит {limit})" if limit else ""))
        gas = number(intent.gas_quote)
        if gas:
            lines.append(f"Газ: {gas} {escape(intent.gas_quote_coin or '')}".rstrip())
        if intent.tx_hash:
            lines.append(f"tx: <code>{escape(intent.tx_hash)}</code>")
    else:
        amount = number(payload.get("amount_in") or (intent.amount_in if intent else None))
        coin = payload.get("amount_in_coin") or (intent.amount_in_coin if intent else "")
        limit = number(payload.get("limit_price") or (intent.limit_price if intent else None))
        detail = SIDES.get(side.lower(), side)
        if amount:
            detail += f": {amount} {escape(coin)}"
        if limit:
            detail += f" по {limit}"
        if detail:
            lines.append(detail)

    reason = payload.get("reason")
    if reason:
        lines.append(f"Причина: {escape(reason)}")

    profile_line = await _profile_line(
        session, payload.get("profile_id") or (intent.profile_id if intent else None)
    )
    lines.append(profile_line or "Источник: вручную")

    link = _link("/history", "История сделок")
    if link:
        lines.append(link)
    return "\n".join(lines)


async def _render_grid(session: AsyncSession, notification: Notification) -> str | None:
    payload = notification.payload or {}
    event_type = str(payload.get("event_type") or "").upper()
    emoji, headline = GRID_HEADLINES.get(event_type, ("•", event_type.replace("_", " ").capitalize()))

    profile = None
    if payload.get("profile_id"):
        profile = await session.get(GridProfile, int(payload["profile_id"]))
    if profile is None:
        # The profile is the whole subject of a strategy event; without it the
        # message would say that something happened, somewhere, to something.
        return None

    lines = [f"{emoji} <b>{escape(headline)}</b> · {escape(profile.symbol)}"]
    lines.append(f"Профиль: <b>{escape(profile.name)}</b>")

    from_state, to_state = payload.get("from_state"), payload.get("to_state")
    if from_state and to_state:
        lines.append(f"{escape(from_state)} → <b>{escape(to_state)}</b>")
    elif to_state:
        lines.append(f"Состояние: <b>{escape(to_state)}</b>")

    price = number(payload.get("market_price"))
    if price:
        lines.append(f"Цена: {price}")
    if payload.get("reason"):
        lines.append(f"Причина: {escape(payload['reason'])}")

    link = _link("/", "Профили")
    if link:
        lines.append(link)
    return "\n".join(lines)


async def render(session: AsyncSession, notification: Notification) -> str | None:
    """The message for one queued row, or ``None`` if it no longer has one."""
    namespace = notification.kind.split(".", 1)[0]
    if namespace == "dex":
        return await _render_dex(session, notification)
    if namespace == "grid":
        return await _render_grid(session, notification)
    return None
