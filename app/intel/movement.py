"""Что изменилось с предыдущего прохода — приток, а не накопленное за сутки.

Колонки карточки отвечают на вопрос «сколько за окно», и на суточном окне
свежая покупка выглядит так же, как вчерашняя. Здесь окно другое: от момента
предыдущего снимка рынка до нынешнего. Это единственная пара точек, про которые
точно известно, что обе измерены, — поэтому сравнение честное, а не «сейчас
против того, что мы додумали».

Про сделки важная оговорка: в расчёт входят те, что **случились** после
предыдущего снимка, а знаем мы о них только после прохода сборщика FOMO. Если
`fomo-sync` не запускался, приток будет нулевым не потому, что его не было, а
потому, что он ещё не импортирован.

Чистая функция: ни базы, ни часов.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["movement"]

HOUR_MS = 3600_000


def _change_pct(now, before) -> Decimal | None:
    """Процент между двумя измеренными значениями, иначе ничего."""
    if now is None or before is None:
        return None
    now, before = Decimal(now), Decimal(before)
    if before <= 0:
        return None
    return (now - before) / before * 100


def movement(latest, previous, legs=(), notes=()) -> dict | None:
    """``None``, пока проход был всего один: сравнивать не с чем.

    Это не «изменение за час» — интервал между проходами задаёт воркер, поэтому
    он всегда возвращается вместе с цифрами (`since_ms`, `hours`).
    """
    if latest is None or previous is None:
        return None
    since = previous.observed_at_ms
    if since is None or latest.observed_at_ms is None or latest.observed_at_ms <= since:
        return None

    buy_usd = sell_usd = Decimal(0)
    trades = 0
    for leg in legs:
        if leg.occurred_at_ms < since:
            continue
        trades += 1
        if leg.value_usd is None:
            continue
        if leg.side == "BUY":
            buy_usd += leg.value_usd
        else:
            sell_usd += leg.value_usd
    fresh_notes = sum(1 for note in notes if (note.get("created_at_ms") or 0) >= since)

    return {
        "since_ms": since,
        "hours": float(Decimal(latest.observed_at_ms - since) / Decimal(HOUR_MS)),
        "price_change_pct": _change_pct(latest.price_usd, previous.price_usd),
        "liquidity_change_pct": _change_pct(latest.liquidity_usd, previous.liquidity_usd),
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "net_usd": buy_usd - sell_usd,
        "trades": trades,
        "theses": fresh_notes,
    }
