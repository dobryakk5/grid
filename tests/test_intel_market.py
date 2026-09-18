from decimal import Decimal

import httpx
import pytest

from unittest.mock import AsyncMock

from app.fomo.tokens import CHAIN_SLUGS, PAIR_CAP
from app.intel.market import parse_market, snapshot_tokens


def pair(**overrides):
    return {
        "chainId": "base", "pairAddress": "0xpair", "dexId": "uniswap",
        "baseToken": {"address": "0xCoIn", "symbol": "COIN", "name": "Coin"},
        "quoteToken": {"address": "0xusdc", "symbol": "USDC"},
        "priceUsd": "0.004", "liquidity": {"usd": 300_000},
        "volume": {"h24": 1_000_000, "h6": 400_000, "h1": 90_000},
        "txns": {"h24": {"buys": 900, "sells": 600}, "h6": {"buys": 300, "sells": 180},
                 "h1": {"buys": 52, "sells": 30}},
        "priceChange": {"m5": 0.4, "h1": 2, "h6": 8, "h24": 12},
        "marketCap": 4_100_000, "fdv": 5_000_000, "pairCreatedAt": 1_700_000_000_000,
        **overrides,
    }


def test_depth_decides_price_while_the_whole_footprint_is_summed():
    thin = pair(pairAddress="0xthin", liquidity={"usd": 1_000}, priceUsd="0.009",
                volume={"h24": 50_000, "h6": 10_000, "h1": 2_000},
                txns={"h24": {"buys": 10, "sells": 5}, "h1": {"buys": 3, "sells": 1}},
                priceChange={"h6": 400}, pairCreatedAt=1_800_000_000_000)
    facts = parse_market([thin, pair()], 8453, "0xcoin")
    # The deep pool speaks for price and momentum; the decoy still counts as
    # part of the coin's liquidity and volume.
    assert facts.price_usd == Decimal("0.004") and facts.change_h6 == Decimal("8")
    assert facts.liquidity_usd == Decimal("301000")
    assert facts.volume_h24_usd == Decimal("1050000")
    assert (facts.buys_h24, facts.sells_h24) == (910, 605)
    assert facts.volume_h1_usd == Decimal("92000")
    # Узкие окна суммируются так же, как суточное: пул без строки за 6 часов
    # просто ничего в неё не вносит, а не обнуляет её для всей монеты.
    assert (facts.buys_h1, facts.sells_h1) == (55, 31)
    assert (facts.buys_h6, facts.sells_h6) == (300, 180)
    assert facts.pools == 2
    # The oldest pool is the coin's age; a newer pool is a migration.
    assert facts.pair_created_at_ms == 1_700_000_000_000


def test_a_pool_that_only_quotes_this_coin_is_not_its_market():
    quoted = pair(baseToken={"address": "0xother", "symbol": "OTHER"},
                  quoteToken={"address": "0xCoIn", "symbol": "COIN"})
    # Inverting that pool's price would put a number on the card that no
    # screener agrees with, so the coin is simply unknown here.
    assert parse_market([quoted], 8453, "0xcoin") is None


def test_another_chains_pool_with_the_same_address_is_ignored():
    assert parse_market([pair(chainId="bsc")], 8453, "0xcoin") is None


def test_a_chain_dexscreener_does_not_index_yields_nothing():
    # Robinhood Chain: app.intel.tape covers it instead.
    assert parse_market([pair()], 4663, "0xcoin") is None


@pytest.mark.parametrize("broken", [
    {"priceUsd": "NaN"}, {"priceUsd": None}, {"priceUsd": "abc"},
])
def test_a_broken_field_is_unknown_not_zero(broken):
    facts = parse_market([pair(**broken)], 8453, "0xcoin")
    assert facts.price_usd is None and facts.liquidity_usd == Decimal("300000")


async def test_a_chain_without_a_screener_is_never_asked_about():
    # 4663 used to be this example and is no longer: DexScreener indexes
    # Robinhood Chain now. The rule is unchanged -- a chain with no slug is not
    # asked about -- so the test names a chain that genuinely has none.
    assert 4663 in CHAIN_SLUGS and 999 not in CHAIN_SLUGS
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"pairs": [pair()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        facts = await snapshot_tokens(http, {(8453, "0xcoin"), (999, "0xnowhere")})
    assert len(seen) == 1 and "0xnowhere" not in seen[0]
    assert (8453, "0xcoin") in facts and (999, "0xnowhere") not in facts


async def test_a_batch_that_does_not_answer_leaves_its_coins_for_next_time():
    async def handle(request):
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        # Not "these coins have no market" -- nothing was learned about them.
        assert await snapshot_tokens(http, {(8453, "0xcoin")}) == {}


async def test_every_coin_gets_its_own_request_so_they_do_not_share_the_cap():
    # Ответ источника ограничен тридцатью пулами на ЗАПРОС, а не на монету.
    # Пока адреса летели пачкой, монета с двадцатью одним пулом записывалась
    # как трёхпуловая, а её ликвидность -- суммой по этим трём.
    seen = []

    def handle(request):
        address = str(request.url).rsplit("/", 1)[-1]
        seen.append(address)
        return httpx.Response(200, json={"pairs": [
            pair(baseToken={"address": address, "symbol": "COIN", "name": "Coin"})]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        facts = await snapshot_tokens(http, {(8453, "0xone"), (8453, "0xtwo")},
                                      sleep=AsyncMock())
    assert sorted(seen) == ["0xone", "0xtwo"]
    assert not any("," in address for address in seen)
    assert len(facts) == 2


async def test_an_answer_at_the_cap_is_marked_as_a_floor():
    def handle(request):
        return httpx.Response(200, json={"pairs": [
            pair(pairAddress=f"0xpair{index}") for index in range(PAIR_CAP)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        facts = await snapshot_tokens(http, {(8453, "0xcoin")}, sleep=AsyncMock())
    fact = facts[(8453, "0xcoin")]
    assert fact.pools == PAIR_CAP and fact.pools_capped is True
    # Тот же ответ, но не упершийся в потолок, ничего про границу не говорит.
    assert parse_market([pair()], 8453, "0xcoin").pools_capped is False
