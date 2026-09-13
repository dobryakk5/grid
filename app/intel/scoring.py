"""Three scores, never one, and every point of each traceable to a fact.

The reason there is no single number: "хорошая монета", "прямо сейчас растёт"
and "может отобрать деньги" are different questions, and averaging them into
one score hides exactly the case that matters -- a coin with real momentum and
a mint authority still open. A single number would rank it above a quiet, safe
one, and that is the ranking that loses money.

None of this says what to buy. It says what is known, how strong it is, and
what is missing -- ``coverage`` counts the facts that were never available, so
a confident-looking 80 on three known fields is visibly different from an 80
on ten.

Pure: no I/O, no clock of its own. Everything is exercised in
``tests/test_intel_scoring.py``.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["score"]

HOUR_MS = 3600_000
DAY_MS = 24 * HOUR_MS


def _number(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() else None


def _ramp(value, low, high) -> Decimal | None:
    """0 at ``low`` or below, 1 at ``high`` or above, linear between."""
    number = _number(value)
    if number is None:
        return None
    low, high = Decimal(str(low)), Decimal(str(high))
    if number <= low:
        return Decimal(0)
    if number >= high:
        return Decimal(1)
    return (number - low) / (high - low)


def _points(parts) -> tuple[Decimal, Decimal]:
    """``(earned, available)`` over ``(weight, share or None)`` pairs.

    A missing fact removes its weight from the denominator instead of scoring
    zero: an unknown is not a bad answer, and treating it as one would rank a
    coin nobody has data about below one that is measurably bad.
    """
    earned = sum((weight * share for weight, share in parts if share is not None), Decimal(0))
    available = sum((weight for weight, share in parts if share is not None), Decimal(0))
    return earned, available


def _score(parts) -> tuple[int | None, Decimal]:
    earned, available = _points(parts)
    if available <= 0:
        return None, Decimal(0)
    return int(round(earned / available * 100)), available


def _money(value) -> str:
    number = _number(value) or Decimal(0)
    for limit, suffix in ((Decimal("1e9"), "B"), (Decimal("1e6"), "M"), (Decimal("1e3"), "k")):
        if abs(number) >= limit:
            return f"${number / limit:.1f}{suffix}"
    return f"${number:.0f}"


def quality(market, security, *, now_ms: int) -> tuple[int | None, list[str], int]:
    """Is this a real market with a real float, regardless of today's candle."""
    reasons, unknown = [], 0
    liquidity = _number(getattr(market, "liquidity_usd", None))
    volume = _number(getattr(market, "volume_h24_usd", None))
    created = getattr(market, "pair_created_at_ms", None)
    holders = (security or {}).get("holder_count")
    free_top10 = _number((security or {}).get("top10_percent_free"))
    market_cap = _number(getattr(market, "market_cap_usd", None))
    fdv = _number(getattr(market, "fdv_usd", None))

    depth = _ramp(liquidity, 10_000, 2_000_000) if liquidity is not None else None
    if depth is not None:
        reasons.append(f"ликвидность {_money(liquidity)}")
    else:
        unknown += 1

    age_share = None
    if created:
        days = Decimal(max(0, now_ms - created)) / Decimal(DAY_MS)
        age_share = _ramp(days, 0, 30)
        reasons.append(f"рынку {days:.0f} дн." if days >= 1 else "рынку меньше суток")
    else:
        unknown += 1

    # Turnover: volume against depth. Some is life, a lot is a coin being
    # passed around a pool that cannot absorb it.
    turnover = None
    if liquidity and liquidity > 0 and volume is not None:
        ratio = volume / liquidity
        turnover = Decimal(1) if ratio <= 3 else _ramp(20 - ratio, 0, 17)
        reasons.append(f"оборот к ликвидности {ratio:.1f}×")
    else:
        unknown += 1

    holders_share = _ramp(holders, 100, 20_000) if holders else None
    if holders:
        reasons.append(f"держателей {holders:,}".replace(",", " "))
    else:
        unknown += 1

    spread_share = None
    if free_top10 is not None:
        spread_share = Decimal(1) - _ramp(free_top10, 10, 60)
        reasons.append(f"у топ-10 свободных {free_top10:.0f}%")
    else:
        unknown += 1

    unlock_share = None
    # Only when the two figures agree with each other. DexScreener computes FDV
    # per pool and market cap per token, so the pair can come back with a
    # circulating share above 100% -- that is a disagreement between two
    # numbers, not a fact about supply, and it is dropped rather than shown.
    if market_cap and fdv and fdv > 0 and market_cap <= fdv * Decimal("1.05"):
        unlocked = min(market_cap / fdv, Decimal(1))
        unlock_share = _ramp(unlocked, Decimal("0.2"), Decimal("0.9"))
        reasons.append(f"в обращении {unlocked * 100:.0f}% от FDV")

    value, _ = _score([
        (Decimal(30), depth),
        (Decimal(15), age_share),
        (Decimal(15), turnover),
        (Decimal(15), holders_share),
        (Decimal(15), spread_share),
        (Decimal(10), unlock_share),
    ])
    return value, reasons, unknown


def _holder_window(holders, *, prefer=(6, 24, 1)) -> dict | None:
    """Первое окно из ``prefer``, у которого вообще есть вторая точка.

    Шесть часов впереди суток намеренно: импульс отвечает на «что происходит
    сейчас», и суточная дельта на этот вопрос отвечает вчерашней новостью. Но
    окно, для которого базы нет, не подменяется соседним молча — вместе с
    цифрой уходит и подпись, за сколько часов она на самом деле набрана.
    """
    windows = (holders or {}).get("windows") or {}
    for hours in prefer:
        window = windows.get(str(hours))
        if window:
            return window
    return None


def momentum(market, flow, catalysts, *, now_ms: int, holders=None) -> tuple[int | None, list[str], str]:
    """What is happening right now, with the cohort's own money weighted first.

    The cohort's flow leads because it is the one number here that is measured
    rather than reported: these are swaps we read ourselves, by people the
    leaderboard ranks, not a screener's aggregate of everyone.
    """
    reasons = []
    buy_usd = _number(flow.get("buy_usd")) or Decimal(0)
    sell_usd = _number(flow.get("sell_usd")) or Decimal(0)
    net = buy_usd - sell_usd
    buyers = int(flow.get("buyers") or 0)
    sellers = int(flow.get("sellers") or 0)
    liquidity = _number(getattr(market, "liquidity_usd", None))

    net_share = None
    if buy_usd or sell_usd:
        # Against the pool where it lands, when we know it; on its own when we
        # do not. $50k into a $200k pool is a different event from $50k into $20M.
        net_share = _ramp(net / liquidity * 100, -5, 15) if liquidity and liquidity > 0 \
            else _ramp(net, -20_000, 100_000)
        reasons.append(f"топ купил {_money(buy_usd)}, продал {_money(sell_usd)}")

    people_share = None
    if buyers or sellers:
        people_share = _ramp(buyers - sellers, -3, 6)
        reasons.append(f"покупателей {buyers}, продавцов {sellers}")

    rank_share = None
    best_rank = flow.get("rank_best")
    if best_rank:
        rank_share = Decimal(1) - _ramp(best_rank, 1, 50)
        reasons.append(f"лучший ранг среди покупателей #{best_rank}")

    market_share = None
    buys, sells = getattr(market, "buys_h24", None), getattr(market, "sells_h24", None)
    if buys is not None and sells is not None and (buys + sells) > 0:
        market_share = _ramp(Decimal(buys) / Decimal(buys + sells), Decimal("0.4"), Decimal("0.65"))
        reasons.append(f"сделок рынка {buys} к {sells}")

    accel_share = None
    volume_6, volume_24 = _number(getattr(market, "volume_h6_usd", None)), _number(getattr(market, "volume_h24_usd", None))
    if volume_6 is not None and volume_24 and volume_24 > 0:
        # Six hours is a quarter of a day: above 1 means the last six hours
        # were busier than the day's own average.
        pace = volume_6 * 4 / volume_24
        accel_share = _ramp(pace, Decimal("0.6"), Decimal("2"))
        reasons.append(f"темп объёма {pace:.1f}× к суткам")

    price_share = None
    change_h6 = _number(getattr(market, "change_h6", None))
    if change_h6 is not None:
        # A hump, not a ramp: rising is good up to a point, and a coin that has
        # already tripled in six hours is not three times as attractive -- it is
        # a chase. Full credit to +100%, decaying to nothing by +200%.
        price_share = min(_ramp(change_h6, -20, 30), _ramp(200 - change_h6, 0, 100))
        reasons.append(f"цена за 6 ч {change_h6:+.1f}%")

    # Новые держатели -- единственная здешняя цифра, которую нельзя нарисовать
    # объёмом: деньги гоняются по кругу между двумя кошельками, а количество
    # адресов от этого не растёт. Поэтому она стоит рядом с потоком, а не
    # вместо него.
    holders_share = None
    window = _holder_window(holders)
    if window and window.get("change_pct") is not None:
        hours = window["hours"]
        # Порог соразмерен окну: +3% держателей за шесть часов и +3% за сутки --
        # разные события, и одна шкала на оба ранжировала бы их одинаково.
        ceiling = Decimal(3) if hours <= 6 else Decimal(10)
        holders_share = _ramp(Decimal(str(window["change_pct"])), 0, ceiling)
        reasons.append(
            f"держателей за {window['actual_hours']:.0f} ч {window['change']:+}"
            f" ({window['change_pct']:+.1f}%)"
        )

    fresh = [item for item in catalysts if now_ms - int(item.get("created_at_ms") or 0) <= DAY_MS]
    catalyst_share = None
    if fresh:
        loud = sum(item.get("importance") == "HIGH" for item in fresh)
        catalyst_share = _ramp(len(fresh) + loud, 1, 5)
        reasons.append(f"тезисов за сутки {len(fresh)}" + (f", из них весомых {loud}" if loud else ""))

    value, _ = _score([
        (Decimal(30), net_share),
        (Decimal(15), people_share),
        (Decimal(10), rank_share),
        (Decimal(15), market_share),
        (Decimal(10), accel_share),
        (Decimal(10), price_share),
        (Decimal(15), holders_share),
        (Decimal(10), catalyst_share),
    ])
    signal = "тихо"
    if buy_usd or sell_usd:
        if net > 0 and buyers > sellers:
            signal = "набирают"
        elif net < 0 and sellers >= buyers:
            signal = "разгружают"
        else:
            signal = "смешанно"
    return value, reasons, signal


def risk(market, security, flow, *, now_ms: int, holders=None) -> tuple[int, list[dict], int]:
    """Higher is worse. Unknown is its own penalty, deliberately.

    A coin nobody could check is not a safe coin. The penalty for "unchecked"
    is smaller than for a known danger and larger than for a clean answer --
    which is the honest ordering, and the one that stops an outage at GoPlus
    from quietly promoting everything it failed to answer about.

    Reasons here carry their cost (``{"text", "points"}``) rather than being
    bare sentences: risk is a sum of penalties, so "what made this 40" has an
    exact answer, and a line that cost nothing -- "опасных свойств не найдено"
    -- is visibly not a finding.
    """
    from app.intel.security import dangers

    reasons, unknown = [], 0
    points = Decimal(0)

    def note(text, cost=Decimal(0)):
        reasons.append({"text": text, "points": int(cost)})

    if security:
        found = dangers(security)
        fatal = {"не даёт продавать (honeypot)", "нельзя продать весь объём", "покупка заблокирована"}
        for flag in found:
            cost = Decimal(40) if flag in fatal else Decimal(12)
            points += cost
            note(flag, cost)
        if not found:
            note("опасных свойств контракта не найдено")
    else:
        points += Decimal(15)
        unknown += 1
        note("контракт не проверен", Decimal(15))

    free_top10 = _number((security or {}).get("top10_percent_free"))
    if free_top10 is not None:
        if free_top10 >= 50:
            points += Decimal(20)
            note(f"топ-10 свободно держат {free_top10:.0f}%", Decimal(20))
        elif free_top10 >= 30:
            points += Decimal(10)
            note(f"топ-10 свободно держат {free_top10:.0f}%", Decimal(10))
        else:
            note(f"топ-10 свободно держат {free_top10:.0f}%")
    elif security:
        points += Decimal(5)
        unknown += 1
        note("распределение держателей неизвестно", Decimal(5))

    liquidity = _number(getattr(market, "liquidity_usd", None))
    if liquidity is None:
        points += Decimal(10)
        unknown += 1
        note("ликвидность неизвестна", Decimal(10))
    elif liquidity < 50_000:
        points += Decimal(20)
        note(f"тонкий рынок: ликвидность {_money(liquidity)}", Decimal(20))
    elif liquidity < 200_000:
        points += Decimal(8)
        note(f"невысокая ликвидность {_money(liquidity)}", Decimal(8))

    created = getattr(market, "pair_created_at_ms", None)
    if created and now_ms - created < DAY_MS:
        points += Decimal(15)
        note("рынок моложе суток", Decimal(15))
    elif created and now_ms - created < 7 * DAY_MS:
        points += Decimal(7)
        note("рынку меньше недели", Decimal(7))

    buy_usd = _number(flow.get("buy_usd")) or Decimal(0)
    sell_usd = _number(flow.get("sell_usd")) or Decimal(0)
    if sell_usd > buy_usd and sell_usd > 0:
        points += Decimal(12)
        note(f"топ продаёт больше, чем покупает ({_money(sell_usd - buy_usd)} нетто)", Decimal(12))

    # Держатели, уходящие на растущей цене, -- это раздача, и на карточке она
    # иначе выглядит как импульс: цена вверх, объём есть, поток топа плюсовой.
    day = ((holders or {}).get("windows") or {}).get("24")
    if day and day.get("change_pct") is not None:
        drift = Decimal(str(day["change_pct"]))
        if drift <= -2:
            points += Decimal(12)
            note(f"держателей за {day['actual_hours']:.0f} ч {day['change']:+}"
                 f" ({drift:+.1f}%)", Decimal(12))
        concentration = day.get("top10_free_change")
        if concentration is not None and Decimal(str(concentration)) >= 5:
            points += Decimal(10)
            note(f"свободная доля топ-10 выросла на {Decimal(str(concentration)):+.1f} п.п.",
                 Decimal(10))

    change_h24 = _number(getattr(market, "change_h24", None))
    if change_h24 is not None and change_h24 >= 100:
        points += Decimal(10)
        note(f"уже +{change_h24:.0f}% за сутки — вход после движения", Decimal(10))

    return int(min(points, Decimal(100))), reasons, unknown


def score(*, market=None, security=None, flow=None, catalysts=None, holders=None,
          now_ms: int) -> dict:
    """The card's three numbers, their reasons, and how much was unknown."""
    flow = flow or {}
    catalysts = catalysts or []
    quality_value, quality_reasons, quality_unknown = quality(market, security, now_ms=now_ms)
    momentum_value, momentum_reasons, signal = momentum(market, flow, catalysts,
                                                        now_ms=now_ms, holders=holders)
    risk_value, risk_reasons, risk_unknown = risk(market, security, flow,
                                                  now_ms=now_ms, holders=holders)
    return {
        "quality": quality_value,
        "momentum": momentum_value,
        "risk": risk_value,
        "signal": signal,
        "reasons": {
            "quality": quality_reasons,
            "momentum": momentum_reasons,
            "risk": risk_reasons,
        },
        "unknown_facts": quality_unknown + risk_unknown,
        "market_source": getattr(market, "source", None),
    }
