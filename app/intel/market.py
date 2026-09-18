"""Market facts for a coin, from whichever source actually covers its chain.

DexScreener indexes every chain here and is free and keyless, so it is the
source wherever it answers -- Robinhood Chain included, which it did not index
when this module was written. What it has not listed yet still falls back to
our own tape (``app.intel.tape``), and such a fact says so in ``source``.

Every function here is pure except the one that fetches. The parse is exercised
against recorded response shapes in ``tests/test_intel_market.py``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings
from app.fomo.tokens import CHAIN_SLUGS, PAIR_CAP, same_address

__all__ = ["MarketFacts", "circulating_pct", "parse_market", "snapshot_tokens"]


@dataclass(frozen=True)
class MarketFacts:
    """What one coin's market looks like right now.

    ``source`` is part of the fact, not decoration: ``tape`` numbers describe
    the wallets we watch, ``dexscreener`` numbers describe the whole market,
    and nothing may average the two.
    """

    chain_id: int
    token_address: str
    source: str
    price_usd: Decimal | None = None
    market_cap_usd: Decimal | None = None
    fdv_usd: Decimal | None = None
    liquidity_usd: Decimal | None = None
    volume_h24_usd: Decimal | None = None
    volume_h6_usd: Decimal | None = None
    volume_h1_usd: Decimal | None = None
    buys_h24: int | None = None
    sells_h24: int | None = None
    buys_h6: int | None = None
    sells_h6: int | None = None
    buys_h1: int | None = None
    sells_h1: int | None = None
    change_m5: Decimal | None = None
    change_h1: Decimal | None = None
    change_h6: Decimal | None = None
    change_h24: Decimal | None = None
    pair_created_at_ms: int | None = None
    pools: int | None = None
    # Источник отдаёт не больше ``PAIR_CAP`` пулов за ответ. Когда их ровно
    # столько, сумма по пулам — это нижняя граница, а не итог, и строка обязана
    # уметь про это сказать: «ликвидность $1.7M» и «ликвидность не меньше
    # $1.7M» — разные утверждения.
    pools_capped: bool | None = None
    symbol: str | None = None
    name: str | None = None

    def row(self, observed_at_ms: int) -> dict:
        """The snapshot row this fact stores as."""
        data = asdict(self)
        data.pop("symbol", None)
        data.pop("name", None)
        return {**data, "observed_at_ms": observed_at_ms}


def circulating_pct(market) -> Decimal | None:
    """Доля обращения от FDV, и только когда две цифры не спорят друг с другом.

    Тот же отбор, что и в ``scoring.quality``: DexScreener считает FDV по пулу,
    а капитализацию по токену, поэтому пара может дать «в обращении 130%». Это
    расхождение двух чисел, а не факт о предложении, и показывать его нельзя —
    иначе экран и оценка сказали бы разное об одной монете.

    Живёт рядом с фактами, а не на странице: тот же вопрос задают и карточка, и
    суточная таблица, а две реализации одного отбора рано или поздно разойдутся.
    """
    if market is None:
        return None
    cap, fdv = market.get("market_cap_usd"), market.get("fdv_usd")
    if not cap or not fdv or Decimal(fdv) <= 0:
        return None
    cap, fdv = Decimal(cap), Decimal(fdv)
    if cap > fdv * Decimal("1.05"):
        return None
    return min(cap / fdv, Decimal(1)) * 100


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    # A non-finite or absurd figure is a broken field, not a big number.
    return result if result.is_finite() and abs(result) < Decimal("1e24") else None


def _int(value) -> int | None:
    number = _decimal(value)
    return int(number) if number is not None and number >= 0 else None


def _side(pair: dict, key: str) -> dict:
    side = pair.get(key)
    return side if isinstance(side, dict) else {}


def parse_market(pairs, chain_id: int, address: str, *,
                 capped: bool | None = None) -> MarketFacts | None:
    """Aggregate every pool where this coin is the base token.

    Base side only. A pool that holds the coin as its *quote* prices the other
    token, and inverting it here would put a number in ``price_usd`` that no
    screen anywhere agrees with. Depth decides which pool speaks for price,
    market cap and momentum -- a fake pool with a copied ticker has volume but
    no liquidity -- while liquidity, volume and trade counts are summed over
    all of them, because that is the coin's whole footprint.
    """
    slug = CHAIN_SLUGS.get(chain_id)
    if slug is None:
        return None
    pools = []
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != slug:
            continue
        base = _side(pair, "baseToken")
        base_address = base.get("address")
        if not isinstance(base_address, str) or not same_address(base_address, address):
            continue
        pools.append(pair)
    if not pools:
        return None

    deepest = max(pools, key=lambda pair: _decimal(_side(pair, "liquidity").get("usd")) or Decimal(0))
    base = _side(deepest, "baseToken")
    liquidity = [_decimal(_side(pair, "liquidity").get("usd")) for pair in pools]
    volume_24 = [_decimal(_side(pair, "volume").get("h24")) for pair in pools]
    volume_6 = [_decimal(_side(pair, "volume").get("h6")) for pair in pools]
    volume_1 = [_decimal(_side(pair, "volume").get("h1")) for pair in pools]
    created = [_int(pair.get("pairCreatedAt")) for pair in pools]
    change = _side(deepest, "priceChange")
    symbol = base.get("symbol")
    name = base.get("name")

    def total(values):
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    def trades(window: str, side: str):
        """Trade counts for one window, summed over every pool.

        DexScreener reports counts per window but no per-side *volume*: there
        is no `buy_usd`/`sell_usd` in the feed. So the market's pressure is
        readable only as a count ratio here, and the dollar split exists just
        for the cohort, whose swaps this project measures itself.
        """
        return total([_int(_side(_side(pair, "txns"), window).get(side)) for pair in pools])

    return MarketFacts(
        chain_id=chain_id,
        token_address=address,
        source="dexscreener",
        price_usd=_decimal(deepest.get("priceUsd")),
        market_cap_usd=_decimal(deepest.get("marketCap")),
        fdv_usd=_decimal(deepest.get("fdv")),
        liquidity_usd=total(liquidity),
        volume_h24_usd=total(volume_24),
        volume_h6_usd=total(volume_6),
        volume_h1_usd=total(volume_1),
        buys_h24=trades("h24", "buys"),
        sells_h24=trades("h24", "sells"),
        buys_h6=trades("h6", "buys"),
        sells_h6=trades("h6", "sells"),
        buys_h1=trades("h1", "buys"),
        sells_h1=trades("h1", "sells"),
        change_m5=_decimal(change.get("m5")),
        change_h1=_decimal(change.get("h1")),
        change_h6=_decimal(change.get("h6")),
        change_h24=_decimal(change.get("h24")),
        # The oldest pool is the coin's real age; a new pool for an old coin is
        # a migration, not a launch.
        pair_created_at_ms=min([value for value in created if value], default=None),
        pools=len(pools),
        pools_capped=bool(capped) if capped is not None else len(pairs) >= PAIR_CAP,
        symbol=symbol[:64] if isinstance(symbol, str) and symbol.strip() else None,
        name=name[:160] if isinstance(name, str) and name.strip() else None,
    )


async def snapshot_tokens(http, wanted, *, sleep=asyncio.sleep) -> dict:
    """``{(chain_id, address): MarketFacts}`` for the coins DexScreener knows.

    **One address per request, on purpose.** The endpoint takes a comma-joined
    list, but it answers with at most ``PAIR_CAP`` pools *for the whole
    request*, and it does not say that it truncated. Asking about thirty coins
    at once therefore made them share thirty pools between them: a coin with
    twenty-one pools was recorded as having three, its liquidity and volume
    summed over those three, and a coin whose pools did not fit at all was
    recorded as having no market. Sums across pools are the entire point of
    this module, so the trade is made the other way -- one request per coin,
    paced under the same 300/minute ceiling.

    A request that does not answer contributes nothing and leaves that coin for
    the next pass -- an outage must not be recorded as "this coin has no
    market". Coins on chains DexScreener does not index are skipped here in
    silence; ``app.intel.tape`` is what covers them.
    """
    by_address: dict[str, list[int]] = {}
    for key in wanted:
        if isinstance(key, tuple) and key[0] in CHAIN_SLUGS:
            by_address.setdefault(key[1], []).append(key[0])
    addresses = sorted(by_address)
    base = settings.dexscreener_base_url.rstrip("/")
    facts: dict[tuple[int, str], MarketFacts] = {}
    for index, address in enumerate(addresses):
        if index:
            # Same shared ceiling the name lookup respects: 300 requests/minute.
            await sleep(0.25)
        try:
            response = await http.get(f"{base}/latest/dex/tokens/{address}")
            payload = response.json() if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            continue
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(pairs, list):
            continue
        for chain_id in by_address[address]:
            parsed = parse_market(pairs, chain_id, address)
            if parsed is not None:
                facts[(chain_id, address)] = parsed
    return facts


def observed_now_ms() -> int:
    return int(time.time() * 1000)
