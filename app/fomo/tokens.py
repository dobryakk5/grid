"""Names for the token ids that FOMO swaps arrive with.

``/v2/users/{id}/swaps`` carries addresses and no symbol -- probed against the
live endpoint, the row keys are ``inTokenAddress``/``outTokenAddress`` with no
``*TokenSymbol`` anywhere -- so a coin is a bare address until something else
names it. DexScreener knows most of them and is already a dependency here.

A name is display only. Nothing keys off it: two chains may well both host a
"USDC", and the pair ``(chain_id, token_address)`` stays the identity.
"""

from __future__ import annotations

import asyncio

import httpx

from app.core.config import settings

# FOMO numbers chains; DexScreener names them. A wrong slug here would silently
# attach another chain's symbol to its tokens, so a chain is added only once its
# slug has been checked against a contract we already know by another route.
#
# Robinhood Chain (4663) was deliberately absent until DexScreener started
# indexing it -- checked against the official PONS contract from
# ``app.dex.tokens``, which answers on slug ``robinhood`` with its real market.
# The tape still covers what the screener has not listed there; it is now a
# fallback rather than the only source (see ``app.intel.refresh.collect``).
#
# ``arc`` is Circle's Arc (chain id 5042). FOMO does not serve it, so the id is
# ours alone: it keys our rows and picks the GoPlus endpoint, and nothing else.
CHAIN_SLUGS = {
    1: "ethereum",
    56: "bsc",
    143: "monad",
    4663: "robinhood",
    5042: "arc",
    8453: "base",
    1399811149: "solana",
}

# DexScreener's documented ceiling for the comma-joined token endpoint.
BATCH = 30
#: Pools one response may carry, for the whole request rather than per coin.
#: An answer holding exactly this many was cut off somewhere, and the endpoint
#: does not say where -- see ``app.intel.market.snapshot_tokens``.
PAIR_CAP = 30


def same_address(left: str, right: str) -> bool:
    """EVM addresses are case-insensitive; Solana mints are not."""
    if left.startswith("0x") and right.startswith("0x"):
        return left.lower() == right.lower()
    return left == right


def pick_name(pairs, chain_id: int, address: str) -> tuple[str | None, str | None]:
    """Symbol and name from the deepest pool that really is this token.

    Deepest, not first: DexScreener returns every pool, including honeypot
    clones that share a ticker, and liquidity is the one field that separates
    the real market from a decoy.
    """
    slug = CHAIN_SLUGS.get(chain_id)
    best, best_liquidity = None, None
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != slug:
            continue
        token = pair.get("baseToken") or {}
        if not isinstance(token, dict):
            continue
        token_address = token.get("address")
        if not isinstance(token_address, str) or not same_address(token_address, address):
            token = pair.get("quoteToken") or {}
            token_address = token.get("address") if isinstance(token, dict) else None
            if not isinstance(token_address, str) or not same_address(token_address, address):
                continue
        try:
            liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            liquidity = 0.0
        if best_liquidity is None or liquidity > best_liquidity:
            symbol = token.get("symbol")
            name = token.get("name")
            best_liquidity = liquidity
            best = (symbol[:64] if isinstance(symbol, str) and symbol.strip() else None,
                    name[:160] if isinstance(name, str) and name.strip() else None)
    return best or (None, None)


async def resolve(http, wanted, *, sleep=asyncio.sleep):
    """``{(chain_id, address): (symbol, name)}`` for every pair we got an answer about.

    A batch that answered contributes all of its keys, ``(None, None)``
    included: "asked, nobody lists it" is an answer worth storing, so an
    unlisted token is asked about once instead of on every import. A batch that
    did *not* answer -- rate limit, timeout, outage -- contributes nothing, so
    those tokens stay unknown and get another chance next time rather than
    being recorded as nameless forever. A name is a nicety either way; losing
    one must never fail an import that has already walked FOMO's whole history.
    """
    by_address: dict[str, list[int]] = {}
    for key in wanted:
        if isinstance(key, tuple) and key[0] in CHAIN_SLUGS:
            by_address.setdefault(key[1], []).append(key[0])
    addresses = sorted(by_address)
    base = settings.dexscreener_base_url.rstrip("/")
    resolved: dict[tuple[int, str], tuple[str | None, str | None]] = {}
    for start in range(0, len(addresses), BATCH):
        chunk = addresses[start:start + BATCH]
        if start:
            # DexScreener allows 300 requests a minute on this endpoint; a full
            # history can ask about thousands of tokens.
            await sleep(0.25)
        try:
            response = await http.get(f"{base}/latest/dex/tokens/{','.join(chunk)}")
            payload = response.json() if response.status_code < 400 else None
        except (httpx.HTTPError, ValueError):
            continue
        pairs = payload.get("pairs") if isinstance(payload, dict) else None
        if not isinstance(pairs, list):
            continue
        # An answer at the cap was cut off, and the endpoint does not say
        # where. A coin missing from it was not necessarily unlisted -- it may
        # simply not have fitted -- and "asked, nobody lists it" is stored
        # forever, so a truncated batch may only report what it did find.
        cut_off = len(pairs) >= PAIR_CAP
        for address in chunk:
            for chain_id in by_address[address]:
                found = pick_name(pairs, chain_id, address)
                if found != (None, None) or not cut_off:
                    resolved[(chain_id, address)] = found
    return resolved
