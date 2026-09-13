"""Как менялись держатели и деньги топа — окнами, а не одним числом.

Карточка до сих пор отвечала «сколько за окно». Этого мало: «+34 держателя за
сутки» и «392 держателя» — разные утверждения, и накопление видно только в
первом. Здесь оба ряда режутся на окна 1ч / 6ч / 24ч (держатели) и
1ч / 6ч / 24ч / 72ч (поток топа), плюс сам ряд потока по корзинам, чтобы было
видно, один это всплеск или ровная покупка третий день подряд.

Одно правило на весь модуль: **окно, для которого нет второй точки, не
считается**. Если первый замер держателей сделан 20 часов назад, суточная
дельта не существует — она не «ноль» и не «примерно суточная». А когда точка
нашлась не ровно на границе окна, вместе с цифрой возвращается настоящий
интервал (``actual_hours``), потому что «+34 за 30 часов» и «+34 за 24 часа» —
это разные новости.

Чистые функции: ни базы, ни часов. Проверяются в ``tests/test_intel_history.py``.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["HOLDER_WINDOWS", "FLOW_WINDOWS", "flow_series", "flow_windows", "holder_trend"]

HOUR_MS = 3600_000

#: Окна, за которые считается прирост держателей.
HOLDER_WINDOWS = (1, 6, 24)
#: Окна потока топа. 72 часа — чтобы «покупают третий день» отличалось от «купили один раз».
FLOW_WINDOWS = (1, 6, 24, 72)


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() else None


def holder_trend(samples, *, windows=HOLDER_WINDOWS) -> dict | None:
    """Держатели сейчас и прирост за каждое окно, для которого есть база.

    ``samples`` — строки ``token_holder_samples`` по одной монете в любом
    порядке. Опорной точкой для окна берётся **самый свежий замер не позже**
    границы: ближайший более старый, а не ближайший вообще, — иначе «за час»
    могло бы посчитаться по замеру, сделанному сорок минут назад.
    """
    known = sorted(
        (row for row in samples if getattr(row, "holder_count", None)),
        key=lambda row: row.observed_at_ms,
    )
    if not known:
        return None
    latest = known[-1]
    trend = {
        "observed_at_ms": latest.observed_at_ms,
        "holder_count": latest.holder_count,
        "top10_percent": latest.top10_percent,
        "top10_percent_free": latest.top10_percent_free,
        "samples": len(known),
        "first_at_ms": known[0].observed_at_ms,
        "windows": {},
    }
    for hours in windows:
        target = latest.observed_at_ms - hours * HOUR_MS
        base = None
        for row in known[:-1]:
            if row.observed_at_ms <= target:
                base = row
            else:
                break
        if base is None:
            trend["windows"][str(hours)] = None
            continue
        span = latest.observed_at_ms - base.observed_at_ms
        change = latest.holder_count - base.holder_count
        # Концентрация едет рядом с притоком: «+300 держателей, и топ-10 при
        # этом набрали ещё 6%» — это не то же самое, что просто «+300».
        top_free_now = _decimal(latest.top10_percent_free)
        top_free_then = _decimal(base.top10_percent_free)
        trend["windows"][str(hours)] = {
            "hours": hours,
            "actual_hours": float(Decimal(span) / Decimal(HOUR_MS)),
            "from_ms": base.observed_at_ms,
            "from_count": base.holder_count,
            "change": change,
            "change_pct": float(Decimal(change) / Decimal(base.holder_count) * 100)
            if base.holder_count else None,
            "top10_free_change": float(top_free_now - top_free_then)
            if top_free_now is not None and top_free_then is not None else None,
        }
    return trend


def _blank() -> dict:
    return {"buy_usd": Decimal(0), "sell_usd": Decimal(0), "trades": 0, "unpriced": 0,
            "buyers": 0, "sellers": 0}


def _aggregate(legs) -> dict:
    """Свод по одному набору сторон сделок.

    Покупатели и продавцы считаются по сторонам с оценкой в долларах — ровно
    как в ``flow_of`` на карточке, чтобы «покупателей 5» в окне и «покупателей
    5» в шапке не расходились из-за неоценённой сделки.
    """
    total = _blank()
    people: dict[str, dict] = {}
    for leg in legs:
        side = "buy" if leg.side == "BUY" else "sell"
        total["trades"] += 1
        person = people.setdefault(leg.user_id, {"buy": Decimal(0), "sell": Decimal(0)})
        if leg.value_usd is None:
            total["unpriced"] += 1
            continue
        value = Decimal(leg.value_usd)
        total[side + "_usd"] += value
        person[side] += value
    total["buyers"] = sum(person["buy"] > 0 for person in people.values())
    total["sellers"] = sum(person["sell"] > 0 for person in people.values())
    total["net_usd"] = total["buy_usd"] - total["sell_usd"]
    return total


def flow_windows(legs, *, now_ms: int, windows=FLOW_WINDOWS) -> dict:
    """Деньги топа по вложенным окнам: ``{"1": {...}, "6": {...}, ...}``.

    Окна именно вложенные, а не соседние: «за 24 часа» включает в себя «за
    час». Иначе пришлось бы читать четыре куска и складывать их в уме, а
    вопрос на экране — «сколько принесли за сутки», а не «сколько принесли
    между шестым и двадцать четвёртым часом».
    """
    return {
        str(hours): _aggregate([leg for leg in legs
                                if leg.occurred_at_ms >= now_ms - hours * HOUR_MS])
        for hours in windows
    }


def flow_series(legs, *, now_ms: int, hours: int = 72, bucket_hours: int = 6) -> list[dict]:
    """Поток топа корзинами, от старой к свежей — ряд, а не сумма.

    Один и тот же нетто в +$13k читается по-разному, если это одна покупка
    вчера или четыре подряд за последние сутки, а сумма за окно эти два случая
    не различает. Пустые корзины остаются в ряду нулями: провал в середине —
    это факт, и выкидывать его значит рисовать непрерывную покупку там, где её
    не было.
    """
    if bucket_hours <= 0 or hours <= 0:
        return []
    count = -(-hours // bucket_hours)
    span = bucket_hours * HOUR_MS
    start = now_ms - count * span
    buckets = [[] for _ in range(count)]
    for leg in legs:
        if leg.occurred_at_ms < start:
            continue
        index = min((leg.occurred_at_ms - start) // span, count - 1)
        buckets[int(index)].append(leg)
    series = []
    for index, held in enumerate(buckets):
        total = _aggregate(held)
        series.append({
            "from_ms": start + index * span,
            "to_ms": start + (index + 1) * span,
            "buy_usd": total["buy_usd"],
            "sell_usd": total["sell_usd"],
            "net_usd": total["net_usd"],
            "trades": total["trades"],
        })
    return series
