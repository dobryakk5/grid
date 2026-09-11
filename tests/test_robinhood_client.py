from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.dexscreener import DexScreenerClient
from app.exchanges import SUPPORTED_EXCHANGES, make_exchange
from app.exchanges.robinhood import DexNotImplementedError, RobinhoodClient, RobinhoodError


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, url, params=None):
        return FakeResponse(self.payload)


POOL = {
    "pairAddress": "0xpair",
    "dexId": "uniswap",
    "baseToken": {
        "address": "0x39dbed3a2bd333467115de45665cc57f813c4571",
        "symbol": "PONS",
    },
    "quoteToken": {"address": "0x" + "11" * 20, "symbol": "WETH"},
    "priceNative": "0.00022",
    "priceUsd": "0.55",
    "liquidity": {"usd": "7000000"},
    "volume": {"h24": "6000000"},
}


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


def client() -> RobinhoodClient:
    return RobinhoodClient(market=DexScreenerClient(http=FakeHttpClient([POOL])))


def test_registry_builds_the_venue_by_name():
    assert "robinhood" in SUPPORTED_EXCHANGES
    exchange = make_exchange("robinhood")
    assert isinstance(exchange, RobinhoodClient)
    assert exchange.name == "robinhood"


async def test_last_price_is_quoted_in_the_pair_quote_token():
    assert await client().last_price("PONSETH") == Decimal("0.00022")


async def test_instrument_info_takes_size_granularity_from_erc20_decimals():
    info = await client().instrument_info("PONSETH")

    assert info.base_coin == "PONS"
    assert info.quote_coin == "ETH"
    assert info.base_precision == Decimal("1e-18")
    assert info.min_order_amt == Decimal("10")


async def test_an_unknown_pair_is_an_exchange_error():
    with pytest.raises(RobinhoodError):
        await client().last_price("BTCUSDT")


async def test_unsampled_intervals_are_refused():
    with pytest.raises(RobinhoodError):
        await client().klines("PONSETH", interval="240")


async def test_api_key_info_reports_config_without_key_material():
    result = (await client().api_key_info())["result"]

    assert result["chainId"] == settings.rh_chain_id
    assert result["universalRouterVersion"] == "2.1.1"
    assert "PONSETH" in result["pairs"]
    assert not any("key" in str(value).lower() for value in result.values())


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.place_limit_order(
            symbol="PONSETH", side="Buy", qty=Decimal("1"),
            price=Decimal("1"), order_link_id="x",
        ),
        lambda c: c.place_market_order(
            symbol="PONSETH", side="Buy", qty=Decimal("1"), order_link_id="x",
        ),
        lambda c: c.cancel_order(order_id="1", symbol="PONSETH"),
        lambda c: c.get_order(order_id="1", symbol="PONSETH"),
        lambda c: c.get_executions(order_id="1", symbol="PONSETH"),
    ],
)
async def test_fund_moving_calls_refuse_loudly_and_name_their_stage(call):
    with pytest.raises(DexNotImplementedError) as exc:
        await call(client())
    assert "stage" in str(exc.value)


class FakeChain:
    def __init__(self):
        self.wallet_address = "0x" + "99" * 20

    async def native_balance(self, address=None):
        return Decimal("0.008")

    async def token_balance(self, token, address=None):
        return Decimal("1250")

    async def close(self):
        pass


async def test_balances_need_an_rpc_and_say_so(monkeypatch):
    monkeypatch.setattr(settings, "rh_rpc_url", "")

    with pytest.raises(RobinhoodError) as exc:
        await client().available_balance("ETH")
    assert "RH_RPC_URL" in str(exc.value)


async def test_native_and_token_balances_come_from_the_chain(monkeypatch):
    monkeypatch.setattr(
        settings, "dex_tokens", '{"USDG": {"address": "0x' + "ab" * 20 + '", "decimals": 6}}'
    )
    exchange = RobinhoodClient(
        market=DexScreenerClient(http=FakeHttpClient([POOL])), chain=FakeChain()
    )

    assert await exchange.available_balance("ETH") == Decimal("0.008")
    assert await exchange.available_balance("USDG") == Decimal("1250")


async def test_wallet_balance_reports_per_coin_errors_instead_of_failing(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    exchange = RobinhoodClient(
        market=DexScreenerClient(http=FakeHttpClient([POOL])), chain=FakeChain()
    )

    result = (await exchange.wallet_balance("ETH,USDG"))["result"]

    by_coin = {row["coin"]: row for row in result["balances"]}
    assert by_coin["ETH"]["walletBalance"] == "0.008"
    # USDG has no address configured; that is reported, not raised.
    assert "DEX_TOKENS" in by_coin["USDG"]["error"]
