from decimal import Decimal

import httpx

from app.intel.security import check_tokens, dangers, parse_evm, parse_solana

EVM = {
    "is_open_source": "1", "is_proxy": "0", "is_mintable": "0", "is_honeypot": "0",
    "buy_tax": "", "sell_tax": "0.05", "owner_percent": "0.0", "creator_percent": "0.01",
    "holder_count": "903280", "transfer_pausable": "0", "is_blacklisted": "0",
    "token_symbol": "BRETT", "token_name": "Brett",
    "holders": [
        {"address": "0xwhale", "percent": "0.114", "is_locked": 0, "tag": ""},
        {"address": "0xpool", "percent": "0.300", "is_locked": 0, "tag": "Uniswap V3 pool"},
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.050", "is_locked": 0, "tag": ""},
    ],
    "lp_holders": [{"address": "0xlp", "percent": "0.9", "is_locked": 1, "tag": ""}],
}

SOLANA = {
    "mintable": {"status": "1", "authority": [{"address": "auth"}]},
    "freezable": {"status": "0", "authority": []},
    "closable": {"status": "0"}, "metadata_mutable": {"status": "1"},
    "balance_mutable_authority": {"status": "0"}, "non_transferable": "0",
    "transfer_fee": {}, "transfer_hook": [], "holder_count": "8740",
    "metadata": {"symbol": "WIF", "name": "dogwifhat"},
    "holders": [{"address": "whale", "percent": "0.2", "is_locked": 0, "tag": ""}],
    "dex": [{"burn_percent": 0.34}, {"burn_percent": 35.6}],
}


def test_an_empty_answer_is_unknown_and_never_a_clean_bill_of_health():
    facts = parse_evm(EVM)
    # buy_tax came back "" -- that is "could not determine", and reading it as
    # zero is how a honeypot passes a gate.
    assert facts["buy_tax_pct"] is None
    assert facts["sell_tax_pct"] == Decimal("5.000")
    assert facts["open_source"] is True and facts["mintable"] is False


def test_pools_and_burn_addresses_do_not_count_as_someone_who_can_sell():
    facts = parse_evm(EVM)
    assert facts["top10_percent"] == Decimal("46.400")
    # Only the whale can actually dump: the pool and the burn address cannot.
    assert facts["top10_percent_free"] == Decimal("11.400")
    assert facts["lp_locked_percent"] == Decimal("90.0")


def test_solana_authorities_are_read_as_the_same_question_evm_asks():
    facts = parse_solana(SOLANA)
    assert facts["mintable"] is True and facts["freezable"] is False
    assert facts["symbol"] == "WIF"
    # Per-pool figure, already a percent, best pool wins, never above 100.
    assert facts["lp_burn_percent"] == Decimal("35.6")
    assert "выпуск новых монет не закрыт" in dangers(facts)


def test_only_what_is_known_to_be_true_is_reported_as_a_danger():
    quiet = dangers(parse_evm({"is_honeypot": "", "is_mintable": ""}))
    assert quiet == []


def test_each_chain_is_asked_on_its_own_endpoint_and_an_outage_yields_nothing():
    seen = []

    def handle(request):
        seen.append(str(request.url))
        if "solana" in str(request.url):
            return httpx.Response(200, json={"result": {"SoLmInT": SOLANA}})
        if "/4663" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json={"result": {"0xcoin": EVM}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            return await check_tokens(http, [
                (8453, "0xCoin"), (1399811149, "SoLmInT"), (4663, "0xrh"),
            ], sleep=_no_sleep)

    import asyncio
    results = asyncio.run(run())
    assert (8453, "0xCoin") in results          # GoPlus lowercases EVM keys
    assert (1399811149, "SoLmInT") in results   # Solana mints keep their case
    assert (4663, "0xrh") not in results        # unchecked, not clean
    assert any("solana/token_security" in url for url in seen)


async def _no_sleep(_seconds):
    return None


async def test_one_address_from_the_wrong_chain_does_not_cost_the_whole_batch():
    asked = []

    def handle(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"result": {"SoLmInT": SOLANA}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        results = await check_tokens(http, [
            (1399811149, "SoLmInT"), (1399811149, "0xdefinitelyEvm"),
        ], sleep=_no_sleep)
    # GoPlus answers about nobody when it cannot parse one of the addresses, so
    # the impostor never reaches the request.
    assert "0xdefinitelyEvm" not in asked[0]
    assert list(results) == [(1399811149, "SoLmInT")]
