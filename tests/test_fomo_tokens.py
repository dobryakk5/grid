"""Naming a token id, without letting a decoy pool do the naming."""

from unittest.mock import AsyncMock

import httpx

from app.fomo.tokens import CHAIN_SLUGS, pick_name, resolve, same_address

SOL = 1399811149
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def pair(chain, address, symbol, name, liquidity, *, quote=False):
    side = "quoteToken" if quote else "baseToken"
    return {"chainId": chain, "liquidity": {"usd": liquidity},
            side: {"address": address, "symbol": symbol, "name": name}}


def test_evm_case_is_ignored_and_solana_case_is_not():
    assert same_address("0xAbC", "0xabc")
    assert not same_address("SoLaNa", "solana")


def test_deepest_matching_pool_names_the_token():
    pairs = [
        pair("solana", MINT, "USDC-FAKE", "Impostor", 12),
        pair("solana", MINT, "USDC", "USD Coin", 9_000_000),
        pair("ethereum", MINT, "WRONGCHAIN", "Wrong Chain", 99_000_000),
    ]
    assert pick_name(pairs, SOL, MINT) == ("USDC", "USD Coin")


def test_a_token_only_ever_quoted_against_is_still_named():
    assert pick_name([pair("solana", MINT, "USDC", "USD Coin", 5, quote=True)],
                     SOL, MINT) == ("USDC", "USD Coin")


def test_unknown_chain_and_unlisted_token_name_nothing():
    assert pick_name([pair("solana", MINT, "USDC", "USD Coin", 5)], 4663, MINT) == (None, None)
    assert pick_name([pair("solana", "other", "X", "X", 5)], SOL, MINT) == (None, None)


async def test_resolve_batches_and_records_the_ones_nobody_lists():
    seen = []

    async def handle(request):
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"pairs": [pair("solana", MINT, "USDC", "USD Coin", 5)]})

    wanted = {(SOL, MINT), (SOL, "unlisted"), (999, "0xnowhere")}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        found = await resolve(http, wanted, sleep=AsyncMock())
    assert found[(SOL, MINT)] == ("USDC", "USD Coin")
    # Asked and unknown is a stored answer, not a missing key to ask again.
    assert found[(SOL, "unlisted")] == (None, None)
    # A chain DexScreener does not index is never asked about at all. Robinhood
    # Chain was this example until DexScreener started indexing it.
    assert (999, "0xnowhere") not in found and 999 not in CHAIN_SLUGS
    assert len(seen) == 1 and seen[0].count(",") == 1


async def test_an_outage_leaves_the_token_unknown_rather_than_nameless_forever():
    async def down(request):
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        # Empty, not {(SOL, MINT): (None, None)}: nothing is stored, so the
        # next import asks again instead of trusting a failure as an answer.
        assert await resolve(http, {(SOL, MINT)}, sleep=AsyncMock()) == {}

    async def throttled(request):
        return httpx.Response(429, json={"error": "rate limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(throttled)) as http:
        assert await resolve(http, {(SOL, MINT)}, sleep=AsyncMock()) == {}
