"""What a thesis claims, read by rules before anything cleverer is tried.

Rules run first and always, for three reasons: they cost nothing, they give
the same answer twice, and they are the part a test can pin down. The optional
model pass (``app.intel.llm``) only ever sees the notes these rules could not
read, and its answer lands in a separate row with ``source="llm"`` so the two
readings never overwrite each other.

Nothing here decides whether a coin is worth buying. It extracts *claims* --
"this says a buyback of $15,000 happened" -- which is the thing a later pass
can check against the chain. An unverified claim stays labelled as one.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

__all__ = ["KINDS", "classify", "extract_numbers"]

#: Event kinds, each with the words that give it away. Russian and English in
#: the same list because FOMO's leaderboard writes in both.
KINDS = {
    "BUYBACK": ("buyback", "buy back", "bought back", "выкуп", "выкупил", "выкупили"),
    "BURN": ("burn", "burned", "burning", "сжиг", "сожг", "сжёг"),
    "LISTING": ("listing", "listed", "cex", "binance", "coinbase", "upbit", "bybit",
                "листинг", "листанул", "биржу"),
    "PARTNERSHIP": ("partnership", "partnered", "collab", "integration", "partner",
                    "партнёрств", "партнерств", "коллаб", "интеграц"),
    "TREASURY": ("treasury", "казна", "казначейств"),
    "LAUNCH": ("launch", "launched", "mainnet", "tge", "запуск", "запустил", "релиз"),
    "AIRDROP": ("airdrop", "аирдроп", "эйрдроп", "раздач"),
    "UNLOCK": ("unlock", "vesting", "cliff", "разлок", "анлок", "вестинг"),
    "REVENUE": ("revenue", "fees", "buy pressure", "выручк", "доход", "комисси"),
    "ENTRY": ("bought", "buying", "added", "accumulating", "long", "beru", "aped",
              "беру", "взял", "купил", "докупил", "добрал", "набираю"),
    "EXIT": ("sold", "selling", "exit", "took profit", "tp", "stop",
             "продал", "вышел", "фиксирую", "фикс", "стоп"),
    "RISK": ("rug", "scam", "honeypot", "dump", "insider", "скам", "рагпул",
             "слив", "инсайдер"),
}

#: Which kinds are claims about the project, i.e. worth checking on-chain
#: later, as opposed to a trader narrating their own position.
VERIFIABLE = frozenset({"BUYBACK", "BURN", "TREASURY", "UNLOCK", "REVENUE"})

_POSITIVE = ("BUYBACK", "BURN", "LISTING", "PARTNERSHIP", "TREASURY", "LAUNCH",
             "REVENUE", "AIRDROP", "ENTRY")
_NEGATIVE = ("EXIT", "RISK", "UNLOCK")

# $15,000 / 15k$ / 15 000 USD / 1.2m usd -- the shapes people actually type.
_MONEY = re.compile(
    r"(?:\$\s*(?P<lead>[\d][\d\s,.]*)\s*(?P<lead_scale>[kкmмbб])?"
    r"|(?P<trail>[\d][\d\s,.]*)\s*(?P<trail_scale>[kкmмbб])?\s*(?:\$|usd|usdc|usdt|долл)"
    r")", re.IGNORECASE)
_PERCENT = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*%")
_TICKER = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")
_SCALES = {"k": 1_000, "к": 1_000, "m": 1_000_000, "м": 1_000_000, "b": 1_000_000_000, "б": 1_000_000_000}

# Stems must start a word. Plain substring matching read "выкупили" (a buyback)
# as "купил" (the author bought) as well, and labelled a project announcement
# as someone narrating their own entry.
_PATTERNS = {
    kind: re.compile(r"(?<!\w)(?:" + "|".join(re.escape(word) for word in words) + ")",
                     re.IGNORECASE | re.UNICODE)
    for kind, words in KINDS.items()
}


def _number(text: str, scale: str | None) -> Decimal | None:
    cleaned = text.replace(" ", "").replace(" ", "")
    # "15,000" is fifteen thousand; "15,5" is fifteen and a half. A comma with
    # exactly three digits after it is a thousands separator, otherwise it is
    # a decimal point -- both conventions turn up in the same feed.
    if re.search(r",\d{3}\b", cleaned):
        cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    cleaned = cleaned.rstrip(".")
    if not cleaned or cleaned.count(".") > 1:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if scale:
        value *= _SCALES.get(scale.lower(), 1)
    # Beyond this it is a token count someone wrote next to a dollar sign, or
    # a typo; either way it is not a dollar figure worth reporting.
    return value if 0 < value < Decimal("1e12") else None


def extract_numbers(text: str) -> dict:
    """Dollar figures, percents and tickers mentioned, as claims -- not facts."""
    usd = []
    for match in _MONEY.finditer(text):
        raw = match.group("lead") or match.group("trail")
        scale = match.group("lead_scale") or match.group("trail_scale")
        value = _number(raw, scale) if raw else None
        if value is not None:
            usd.append(value)
    percents = []
    for match in _PERCENT.finditer(text):
        value = _number(match.group("value"), None)
        if value is not None and value <= 1000:
            percents.append(value)
    tickers = {match.group(1).upper() for match in _TICKER.finditer(text)}
    numbers = {}
    if usd:
        numbers["usd"] = [str(value) for value in usd[:5]]
        numbers["usd_max"] = str(max(usd))
    if percents:
        numbers["percent"] = [str(value) for value in percents[:5]]
    if tickers:
        numbers["tickers"] = sorted(tickers)[:8]
    return numbers


def classify(text: str) -> dict:
    """``{kinds, stance, importance, numbers, confidence}`` for one note.

    ``confidence`` is about the *reading*, not about the claim being true: two
    matching words and a dollar figure is a confident reading of a sentence
    that may still be a lie. Nothing here verifies anything.
    """
    if not isinstance(text, str) or not text.strip():
        return {"kinds": [], "stance": None, "importance": "LOW",
                "numbers": {}, "confidence": Decimal("0")}
    kinds, hits = [], 0
    for kind, pattern in _PATTERNS.items():
        matched = len(pattern.findall(text))
        if matched:
            kinds.append(kind)
            hits += matched
    numbers = extract_numbers(text)
    positive = sum(kind in _POSITIVE for kind in kinds)
    negative = sum(kind in _NEGATIVE for kind in kinds)
    stance = None
    if positive or negative:
        stance = "POSITIVE" if positive > negative else "NEGATIVE" if negative > positive else "MIXED"
    project_claim = any(kind in VERIFIABLE for kind in kinds)
    importance = "HIGH" if project_claim and "usd" in numbers else \
        "MEDIUM" if project_claim or "LISTING" in kinds or "PARTNERSHIP" in kinds else "LOW"
    confidence = Decimal("0")
    if kinds:
        confidence = min(Decimal("0.5") + Decimal("0.1") * hits, Decimal("0.9"))
        if numbers.get("usd"):
            confidence = min(confidence + Decimal("0.1"), Decimal("0.95"))
    return {
        "kinds": sorted(kinds),
        "stance": stance,
        "importance": importance,
        "numbers": numbers,
        "confidence": confidence,
        # Something a later pass can act on: does this claim describe the
        # project doing something the chain would remember?
        "verifiable": project_claim,
    }
