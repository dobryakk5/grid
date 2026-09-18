"""Суточный срез по нескольким монетам: что растёт, а что затухает.

Карточка `/intel` отвечает про одну монету и про «сейчас». Здесь другой вопрос:
поставить несколько монет в одну таблицу и увидеть, у какой из них
капитализация, ликвидность, обороты и держатели двигаются вместе, а у какой
цена держится на затухающем обороте.

Считается **только по сохранённым снимкам**, то есть по тем же строкам
`token_snapshots` и `token_holder_samples`, что пишет проход сбора. Ничего не
дозапрашивается: таблица, которая молча ходит в сеть, показывала бы свежую
монету рядом со вчерашней и называла это сравнением.

Два правила на весь модуль, те же, что в `history`:

* **окно без второй точки не считается.** Нет снимка старше суток — суточной
  дельты не существует; она не «ноль» и не «примерно суточная»;
* **интервал возвращается настоящий.** Опорный снимок редко ложится ровно на
  границу, а «+18% за 31 час» и «+18% за сутки» — разные новости, поэтому
  рядом с каждой дельтой едет `baseline_hours`.

Чистые функции: ни базы, ни часов, ни сети.
"""

from __future__ import annotations

from decimal import Decimal

from app.intel.history import holder_trend
from app.intel.market import circulating_pct

__all__ = ["COLUMNS", "HOURS", "render", "row", "table_rows"]

HOUR_MS = 3600_000

#: Окно, за которое считаются дельты таблицы.
HOURS = 24


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return result if result.is_finite() else None


def _change_pct(now, before) -> float | None:
    """Процент между двумя **измеренными** значениями, иначе ничего."""
    now, before = _decimal(now), _decimal(before)
    if now is None or before is None or before <= 0:
        return None
    return float((now - before) / before * 100)


def _fields(snapshot) -> dict:
    """Снимок как словарь — чтобы `circulating_pct` читал одно и то же везде."""
    return {
        "market_cap_usd": getattr(snapshot, "market_cap_usd", None),
        "fdv_usd": getattr(snapshot, "fdv_usd", None),
    }


def row(*, chain_id: int, token_address: str, symbol: str | None,
        latest, baseline=None, holders=(), hours: int = HOURS) -> dict:
    """Одна строка таблицы по одной монете.

    ``latest`` — свежий снимок рынка, ``baseline`` — самый свежий снимок **не
    позже** границы окна (или ``None``, если история короче окна), ``holders`` —
    замеры держателей по этой монете в любом порядке.

    ``hours`` задаёт окно **обеим** дельтам сразу. Рынок и держатели меряются
    по разным рядам с разной частотой, но окно у строки одно: «Δкап за 6 часов»
    рядом с «Δдерж за сутки» под общим заголовком — это не таблица, а ловушка.

    ``source`` едет со строкой не для красоты: `dexscreener` — это весь рынок
    монеты, `tape` — только кошельки, за которыми мы следим. Складывать их
    столбиком нельзя, и таблица обязана показывать, какое из двух чисел читает.
    """
    trend = holder_trend(holders, windows=(hours,)) if holders else None
    window = (trend or {}).get("windows", {}).get(str(hours))
    cap = _decimal(getattr(latest, "market_cap_usd", None))
    volume = _decimal(getattr(latest, "volume_h24_usd", None))
    buys = getattr(latest, "buys_h24", None)
    sells = getattr(latest, "sells_h24", None)
    span = None
    if baseline is not None and getattr(latest, "observed_at_ms", None):
        span = float(Decimal(latest.observed_at_ms - baseline.observed_at_ms)
                     / Decimal(HOUR_MS))
    return {
        "chain_id": chain_id,
        "token_address": token_address,
        "symbol": symbol,
        "source": getattr(latest, "source", None),
        "observed_at_ms": getattr(latest, "observed_at_ms", None),
        # Интервал, за который посчитаны рыночные дельты. None — истории
        # меньше окна, и тогда все колонки с дельтами пусты, а не нулевые.
        "baseline_hours": span,
        "price_usd": _decimal(getattr(latest, "price_usd", None)),
        "market_cap_usd": cap,
        "market_cap_change_pct": _change_pct(cap, getattr(baseline, "market_cap_usd", None)),
        "circulating_pct": circulating_pct(_fields(latest)),
        "liquidity_usd": _decimal(getattr(latest, "liquidity_usd", None)),
        "liquidity_change_pct": _change_pct(getattr(latest, "liquidity_usd", None),
                                            getattr(baseline, "liquidity_usd", None)),
        "pools": getattr(latest, "pools", None),
        # Ответ источника уперся в потолок пулов: ликвидность и объём тогда
        # снизу, а не итог, и таблица обязана это показать.
        "pools_capped": getattr(latest, "pools_capped", None),
        "volume_h24_usd": volume,
        "volume_change_pct": _change_pct(volume, getattr(baseline, "volume_h24_usd", None)),
        # Оборот к капитализации: $6M суточного объёма на $13M капитализации и
        # те же $6M на $300M — это разные монеты, а колонка «объём» одна.
        "turnover_pct": float(volume / cap * 100) if volume is not None and cap else None,
        # Глубина: ликвидность к капитализации. Без неё оборот читается неверно
        # — большой объём на тонком пуле и тот же объём на толстом означают
        # разное, а «сколько стоит сдвинуть цену» отвечает именно это.
        "depth_pct": float(_decimal(getattr(latest, "liquidity_usd", None)) / cap * 100)
        if cap and _decimal(getattr(latest, "liquidity_usd", None)) is not None else None,
        # Соотношение сделок, а не денег: у DexScreener нет объёма по сторонам,
        # только счётчики, и притворяться, что это давление в долларах, нельзя.
        "buy_sell_ratio": float(Decimal(buys) / Decimal(sells)) if buys and sells else None,
        "change_h24_pct": _decimal(getattr(latest, "change_h24", None)),
        "holders": (trend or {}).get("holder_count"),
        "holders_change": window["change"] if window else None,
        "holders_change_pct": window["change_pct"] if window else None,
        "holders_hours": window["actual_hours"] if window else None,
        "top10_free_pct": (trend or {}).get("top10_percent_free"),
    }


def table_rows(keys, *, names, snapshots, baselines, holders,
               hours: int = HOURS) -> list[dict]:
    """Строки в порядке ``keys``; монета без единого снимка попадает пустой.

    Именно пустой, а не пропущенной: «монету смотрим, данных пока нет» и «такой
    монеты в списке нет» — разные состояния, и watchlist обязан отличать одно
    от другого, иначе опечатка в адресе выглядит как тишина на рынке.
    """
    rows = []
    for key in keys:
        latest = snapshots.get(key)
        symbol = (names.get(key) or (None, None))[0]
        if latest is None:
            rows.append({"chain_id": key[0], "token_address": key[1], "symbol": symbol,
                         "source": None, "observed_at_ms": None})
            continue
        rows.append(row(chain_id=key[0], token_address=key[1], symbol=symbol,
                        latest=latest, baseline=baselines.get(key),
                        holders=holders.get(key, []), hours=hours))
    return rows


#: ``(ключ, заголовок, как печатать)``. Порядок — порядок колонок.
COLUMNS = (
    ("symbol", "монета", "text"),
    ("market_cap_usd", "кап.", "money"),
    ("market_cap_change_pct", "Δкап", "pct"),
    ("circulating_pct", "обращ.", "pct_plain"),
    ("liquidity_usd", "ликв.", "money"),
    ("liquidity_change_pct", "Δликв", "pct"),
    ("depth_pct", "глубина", "pct_plain"),
    ("pools", "пулов", "pools"),
    ("volume_h24_usd", "объём 24ч", "money"),
    ("volume_change_pct", "Δобъём", "pct"),
    ("turnover_pct", "оборот", "pct_plain"),
    ("buy_sell_ratio", "buy/sell", "ratio"),
    ("holders", "держ.", "int"),
    ("holders_change", "Δдерж", "signed"),
    ("top10_free_pct", "топ-10 своб.", "pct_plain"),
    ("source", "источник", "text"),
)


def _money(value) -> str:
    value = _decimal(value)
    if value is None:
        return "—"
    number = float(abs(value))
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if number >= cut:
            return f"${number / cut:,.2f}{suffix}"
    return f"${number:,.2f}"


def _cell(value, kind: str) -> str:
    """Незаполненное всегда «—». Ноль — это измерение, прочерк — его отсутствие."""
    if value is None:
        return "—"
    if kind == "text":
        return str(value)
    if kind == "int":
        return f"{int(value):,}".replace(",", " ")
    if kind == "money":
        return _money(value)
    if kind == "signed":
        return f"{int(value):+,}".replace(",", " ")
    if kind == "pct":
        return f"{float(value):+.1f}%"
    if kind == "pct_plain":
        return f"{float(value):.1f}%"
    if kind == "ratio":
        return f"{float(value):.2f}"
    return str(value)


def _format(item, key: str, kind: str) -> str:
    """Ячейка по строке целиком: у «пулов» ответ зависит от соседнего факта."""
    if kind != "pools":
        return _cell(item.get(key), kind)
    value = item.get("pools")
    if value is None:
        return "—"
    return f"{int(value)}+" if item.get("pools_capped") else str(int(value))


def render(rows, *, hours: int = HOURS) -> str:
    """Таблица как текст, с подписью под ней о том, чем считали дельты.

    Подпись обязательна: колонки «Δ» ничего не стоят без ответа на вопрос, за
    какой интервал они посчитаны, а интервал у каждой монеты свой — он зависит
    от того, когда по ней был предыдущий снимок.
    """
    headers = [title for _key, title, _kind in COLUMNS]
    body = [[_format(item, key, kind) for key, _title, kind in COLUMNS] for item in rows]
    widths = [max(len(headers[index]), *(len(line[index]) for line in body)) if body
              else len(headers[index]) for index in range(len(COLUMNS))]

    def line(cells):
        return "  ".join(text.ljust(widths[index]) if COLUMNS[index][2] == "text"
                         else text.rjust(widths[index]) for index, text in enumerate(cells))

    out = [line(headers), "  ".join("-" * width for width in widths)]
    out += [line(cells) for cells in body]

    out.append("")
    if any(item.get("pools_capped") for item in rows):
        out.append("«+» у числа пулов: ответ источника уперся в свой потолок — "
                   "ликвидность и объём по такой монете снизу, а не итог.")
        out.append("")
    out.append(f"Дельты — к последнему снимку не позже {hours} ч назад:")
    for item in rows:
        label = item.get("symbol") or item["token_address"][:10]
        span = item.get("baseline_hours")
        holders_span = item.get("holders_hours")
        if item.get("observed_at_ms") is None:
            out.append(f"  {label}: снимков ещё нет")
            continue
        parts = [f"рынок за {span:.1f} ч" if span is not None
                 else f"рынка старше {hours} ч нет — колонки Δ пусты"]
        parts.append(f"держатели за {holders_span:.1f} ч" if holders_span is not None
                     else "второго замера держателей нет")
        out.append(f"  {label}: " + "; ".join(parts))
    return "\n".join(out)
