"""Монеты, за которыми смотрим независимо от того, торгует ли их топ.

Список кандидатов до сих пор целиком задавала когорта: что топ трогал, про то
и собираются факты. Для наблюдения за конкретной монетой этого мало — пока
никто из топа её не купил, о ней не пишется ни одного снимка, а история
капитализации и держателей задним числом не восстанавливается ниоткуда.

Здесь пин-лист из `.env`. Эти монеты собираются каждым проходом и **не
вытесняются** лимитом `INTEL_MAX_TOKENS`: лимит защищает проход от разросшейся
когорты, а пин-лист — это явно названные человеком монеты, и молча выкинуть
одну из них хуже, чем сделать лишний запрос.

Формат записи — `<сеть>:<адрес>` через запятую или перевод строки, где сеть
названа слагом (`solana`, `robinhood`, `arc`, …) или числом (`4663`). Слаги —
те же, что у DexScreener, и живут в одном месте: `app.fomo.tokens.CHAIN_SLUGS`.

Одно правило: **непонятная запись не превращается в монету**. Разбор возвращает
и то, что понял, и то, что отверг, — потому что тихо пропущенная опечатка в
адресе выглядит на экране ровно как «монету смотрим, просто ничего не
происходит», а это худшее, чем может кончиться watchlist.
"""

from __future__ import annotations

import re

from app.core.config import settings
from app.fomo.tokens import CHAIN_SLUGS
from app.intel.security import plausible

__all__ = ["parse", "watchlist"]

#: Слаг → id. Обратная сторона `CHAIN_SLUGS`: человек пишет сеть именем.
CHAIN_IDS = {slug: chain_id for chain_id, slug in CHAIN_SLUGS.items()}

_SEPARATORS = re.compile(r"[,\s]+")


def known_chains() -> set[int]:
    """Сети, про которые этот проход вообще умеет что-то собрать.

    Слаги DexScreener плюс Robinhood Chain, которую вдобавок закрывает
    собственная лента. Сеть вне этого набора — не «пока не поддержана», а
    опечатка в номере: собрать по ней нечего ни одним способом, и монета, молча
    ушедшая в такую сеть, никогда не появится в таблице.
    """
    return set(CHAIN_SLUGS) | {settings.rh_chain_id}


def _chain(name: str) -> int | None:
    chain_id = int(name) if name.isdigit() else CHAIN_IDS.get(name.lower())
    return chain_id if chain_id in known_chains() else None


def parse(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    """``([(chain_id, address)], [что не разобрали])``, в порядке записи.

    Адрес проверяется на принадлежность семейству сети тем же правилом, что и
    перед запросом в GoPlus: solana-минт, записанный с EVM-сетью, — это
    опечатка, а не монета, и лучше узнать о ней из лога, чем из пустой строки
    таблицы. Повтор одной монеты схлопывается: два одинаковых адреса не должны
    удваивать запросы.
    """
    keys: list[tuple[int, str]] = []
    rejected: list[str] = []
    seen: set[tuple[int, str]] = set()
    for entry in _SEPARATORS.split(text or ""):
        if not entry:
            continue
        name, _, address = entry.partition(":")
        chain_id = _chain(name.strip()) if address else None
        address = address.strip()
        if chain_id is None or not plausible(chain_id, address):
            rejected.append(entry)
            continue
        key = (chain_id, address)
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys, rejected


def watchlist() -> tuple[list[tuple[int, str]], list[str]]:
    """То же, но для настройки `INTEL_WATCHLIST`. Пусто — это рабочее состояние."""
    return parse(settings.intel_watchlist)
