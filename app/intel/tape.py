"""Market facts for the chain no screener indexes, read from our own tape.

DexScreener does not list Robinhood Chain, so for chain 4663 the only market
data that exists here is the one this project produces itself: ``chain_swaps``,
reconstructed from ``Transfer`` logs of the wallets the tape tracks.

That is a narrower thing than a market, and the difference is not cosmetic:
volume and trade counts cover **tracked wallets only**, and liquidity and
market cap are simply not knowable this way. Every fact produced here is
labelled ``source="tape"`` so a score can weigh it as what it is instead of
mistaking a quiet tape for a quiet market.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.db.models import ChainSwap
from app.intel.market import MarketFacts

__all__ = ["aggregate_tape", "tape_facts"]

HOUR_MS = 3600_000


def _usd_price(row) -> Decimal | None:
    """USD per token from a priced swap, or nothing.

    ``value_usd`` and ``token_amount`` are the two figures the tape actually
    measured; ``price`` is denominated in the swap's quote token, which is only
    dollars when that quote happens to be a stablecoin.
    """
    if row.value_usd is None or not row.token_amount:
        return None
    try:
        return Decimal(row.value_usd) / Decimal(row.token_amount)
    except (ArithmeticError, TypeError):
        return None


def aggregate_tape(rows, *, chain_id: int, now_ms: int, hours: int = 24) -> dict:
    """``{(chain_id, address): MarketFacts}`` from raw swap rows.

    Pure so the arithmetic can be tested without a database. ``change_h24`` is
    first-versus-last *priced* swap inside the window -- an honest measure of
    where the tape saw the coin trade, and unknown when it saw fewer than two.
    """
    window_start = now_ms - hours * HOUR_MS
    six_hours_ago = now_ms - 6 * HOUR_MS
    coins: dict[str, dict] = {}
    for row in rows:
        if row.block_time_ms < window_start:
            continue
        coin = coins.setdefault(row.token_address.lower(), {
            "buys": 0, "sells": 0, "volume": Decimal(0), "volume_6h": Decimal(0),
            "priced": [], "symbol": None, "unpriced": 0,
        })
        coin["symbol"] = coin["symbol"] or row.symbol
        coin["buys" if row.side == "BUY" else "sells"] += 1
        if row.value_usd is None:
            coin["unpriced"] += 1
        else:
            coin["volume"] += abs(Decimal(row.value_usd))
            if row.block_time_ms >= six_hours_ago:
                coin["volume_6h"] += abs(Decimal(row.value_usd))
        price = _usd_price(row)
        if price is not None and price > 0:
            coin["priced"].append((row.block_time_ms, price))

    facts = {}
    for address, coin in coins.items():
        priced = sorted(coin["priced"])
        change = None
        if len(priced) >= 2 and priced[0][1] > 0:
            change = (priced[-1][1] - priced[0][1]) / priced[0][1] * 100
        facts[(chain_id, address)] = MarketFacts(
            chain_id=chain_id,
            token_address=address,
            source="tape",
            price_usd=priced[-1][1] if priced else None,
            # Liquidity, market cap and FDV stay unknown rather than zero: the
            # tape watches wallets, not pools or supply.
            volume_h24_usd=coin["volume"] or None,
            volume_h6_usd=coin["volume_6h"] or None,
            buys_h24=coin["buys"],
            sells_h24=coin["sells"],
            change_h24=change,
            symbol=coin["symbol"],
        )
    return facts


async def tape_facts(session, keys, *, now_ms: int, hours: int = 24) -> dict:
    """The same aggregate, for the coins in ``keys`` that live on one chain."""
    addresses = {address.lower() for chain_id, address in keys}
    chains = {chain_id for chain_id, _ in keys}
    if not addresses or len(chains) != 1:
        return {}
    chain_id = chains.pop()
    rows = (await session.execute(select(ChainSwap).where(
        ChainSwap.chain_id == chain_id,
        ChainSwap.block_time_ms >= now_ms - hours * HOUR_MS,
        func.lower(ChainSwap.token_address).in_(addresses),
    ))).scalars()
    return aggregate_tape(rows, chain_id=chain_id, now_ms=now_ms, hours=hours)
