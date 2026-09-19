from decimal import Decimal

import pytest
from eth_account import Account

from app.core.config import settings
from app.dex.chain import (
    ChainClient,
    ChainError,
    PreflightRevert,
    decode_revert,
    to_int,
)


KEY = "0x" + "11" * 32


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "rh_chain_id", 4663)
    monkeypatch.setattr(settings, "rh_rpc_url", "https://rpc.example")
    monkeypatch.setattr(settings, "rh_private_key", "")


def client(**kwargs) -> ChainClient:
    return ChainClient(rpc_url="https://rpc.example", **kwargs)


@pytest.mark.parametrize(
    "value,expected",
    [(None, None), (5, 5), ("5", 5), ("0x38d7ea4c68000", 10**15), ("", None)],
)
def test_to_int_accepts_every_shape_uniswap_returns(value, expected):
    assert to_int(value) == expected


def test_to_int_rejects_a_type_it_cannot_mean():
    with pytest.raises(TypeError):
        to_int(True)


def test_a_missing_rpc_url_fails_at_construction(monkeypatch):
    monkeypatch.setattr(settings, "rh_rpc_url", "")
    with pytest.raises(ChainError):
        ChainClient()


def test_without_a_key_the_client_reads_but_cannot_sign():
    chain = client()
    assert chain.has_key is False
    with pytest.raises(ChainError):
        chain.wallet_address


def test_signing_refuses_a_transaction_for_another_chain():
    chain = client(private_key=KEY)
    tx = {
        "chainId": 1, "nonce": 0, "to": chain.wallet_address, "value": 0,
        "data": "0x", "gas": 21000, "maxFeePerGas": 10**9,
        "maxPriorityFeePerGas": 10**8,
    }
    with pytest.raises(ChainError) as exc:
        chain.sign(tx)
    assert "chain" in str(exc.value)


def test_the_transaction_hash_is_known_before_broadcast():
    chain = client(private_key=KEY)
    tx = {
        "chainId": 4663, "nonce": 137, "to": chain.wallet_address, "value": 0,
        "data": "0x", "gas": 21000, "maxFeePerGas": 10**9,
        "maxPriorityFeePerGas": 10**8,
    }
    payload = chain.sign(tx)

    # Same hash eth_account computes -- this is the crash-recovery key.
    expected = Account.from_key(KEY).sign_transaction(tx).hash.hex()
    assert payload.tx_hash.removeprefix("0x") == expected.removeprefix("0x")
    assert payload.nonce == 137
    assert payload.wallet_address == chain.wallet_address
    assert payload.raw_hex.startswith("0x")


async def test_native_balance_scales_out_of_wei():
    chain = client(private_key=KEY)

    class FakeEth:
        async def get_balance(self, address):
            return 8_000_000_000_000_000

    chain.w3.eth = FakeEth()

    assert await chain.native_balance() == Decimal("0.008")


async def test_native_received_adds_back_what_we_spent_ourselves():
    chain = client(private_key=KEY)
    balances = {41: 1_000_000_000_000_000_000, 42: 1_022_600_000_000_000_000}

    class FakeEth:
        async def get_balance(self, address, block_identifier=None):
            return balances[block_identifier]

    chain.w3.eth = FakeEth()

    # Balance rose 0.0226 ETH while 0.0004 went to gas: 0.023 actually arrived.
    received = await chain.native_received(
        block_number=42, gas_wei=400_000_000_000_000, value_sent_wei=0
    )

    assert received == 23_000_000_000_000_000


def test_a_dry_run_can_name_a_wallet_without_holding_its_key(monkeypatch):
    monkeypatch.setattr(
        settings, "rh_wallet_address", "0x071a4377479956ffbab52d189b21491c9b895a5b"
    )
    chain = client()

    assert chain.has_key is False
    # Checksummed, so it can be used as a quote swapper and a balance owner.
    assert chain.wallet_address == "0x071A4377479956fFBaB52d189b21491C9B895A5B"


def test_signing_still_refuses_without_a_key(monkeypatch):
    monkeypatch.setattr(
        settings, "rh_wallet_address", "0x071A4377479956fFBaB52d189b21491C9B895A5B"
    )
    with pytest.raises(ChainError):
        client().sign({"chainId": 4663, "nonce": 0, "to": None, "value": 0})


# ---- pre-flight ----------------------------------------------------------


class FakeProvider:
    """Answers ``eth_call`` the way a node does, error object and all."""

    def __init__(self, response):
        self.response = response
        self.requests = []

    async def make_request(self, method, params):
        self.requests.append((method, params))
        return self.response


def preflight_client(response):
    c = client(private_key=KEY)
    c.w3.provider = FakeProvider(response)
    return c


async def test_a_pre_flight_that_the_node_accepts_sends_the_real_calldata():
    c = preflight_client({"jsonrpc": "2.0", "id": 1, "result": "0x"})

    await c.preflight({"to": "0x" + "ab" * 20, "data": "0xdeadbeef", "value": 0})

    method, (payload, block) = c.w3.provider.requests[0]
    assert method == "eth_call" and block == "latest"
    assert payload["data"] == "0xdeadbeef"
    assert payload["from"] == c.wallet_address
    assert payload["value"] == "0x0"


async def test_a_custom_error_comes_back_named_not_as_four_bytes():
    """The whole point of reading ``data`` instead of calling w3.eth.call.

    ``CurrencyNotSettled`` is what a route through a pool the router cannot
    settle reverts with; as "execution reverted" it is indistinguishable from
    a price that merely moved.
    """
    c = preflight_client(
        {"error": {"code": 3, "message": "execution reverted", "data": "0x5212cba1"}}
    )

    with pytest.raises(PreflightRevert) as exc:
        await c.preflight({"to": "0x" + "ab" * 20, "data": "0xdead", "value": 0})

    assert "CurrencyNotSettled" in str(exc.value)


async def test_a_node_that_cannot_answer_is_not_reported_as_a_bad_swap():
    """An outage must not read as "this route reverts": the two are acted on
    differently, and only one of them says anything about the trade."""

    class DeadProvider:
        async def make_request(self, method, params):
            raise OSError("connection refused")

    c = client(private_key=KEY)
    c.w3.provider = DeadProvider()

    with pytest.raises(ChainError) as exc:
        await c.preflight({"to": "0x" + "ab" * 20, "data": "0xdead", "value": 0})

    assert not isinstance(exc.value, PreflightRevert)
    assert "pre-flight call failed" in str(exc.value)


@pytest.mark.parametrize(
    "data,expected",
    [
        ("0x5212cba1", "CurrencyNotSettled"),
        ("0x8b063d73" + "00" * 64, "V4TooLittleReceived"),
        ("0x5bf6f916", "TransactionDeadlinePassed"),
        ("0xd81b2f2e" + "00" * 32, "AllowanceExpired"),
        ("0x1234abcd", "unknown error 0x1234abcd"),
        ("0x", "reverted without a reason"),
        (None, "reverted without a reason"),
    ],
)
def test_revert_data_is_decoded_as_far_as_it_can_be_read(data, expected):
    assert expected in decode_revert(data)


def test_a_revert_string_is_read_out_of_its_abi_encoding():
    reason = "STF"
    payload = (
        "0x08c379a0"
        + f"{32:064x}"
        + f"{len(reason):064x}"
        + reason.encode().hex().ljust(64, "0")
    )

    assert decode_revert(payload) == reason


# ---- answers asked once --------------------------------------------------


class FakeEth:
    def __init__(self):
        self.chain_id_reads = 0

    @property
    async def chain_id(self):
        self.chain_id_reads += 1
        return 4663


async def test_the_chain_id_is_confirmed_once_not_every_tick():
    """Robinhood's public RPC rate-limits; a question whose answer cannot
    change must not be re-asked for every level on every pass."""
    c = client(private_key=KEY)
    eth = FakeEth()
    c.w3.eth = eth

    for _ in range(5):
        await c.ensure_ready()

    assert eth.chain_id_reads == 1


async def test_a_wrong_chain_is_still_refused_and_not_remembered():
    class WrongChain(FakeEth):
        @property
        async def chain_id(self):
            self.chain_id_reads += 1
            return 1

    c = client(private_key=KEY)
    c.w3.eth = WrongChain()

    for _ in range(2):
        with pytest.raises(ChainError):
            await c.ensure_ready()

    assert c.w3.eth.chain_id_reads == 2


def _counting_decimals(c, value=6):
    reads = []

    async def token_decimals(token):
        reads.append(token.address)
        return value

    c.token_decimals = token_decimals
    return reads


async def test_token_decimals_are_read_once_per_address():
    from app.dex.tokens import Token

    c = client(private_key=KEY)
    reads = _counting_decimals(c)
    usdg = Token(symbol="USDG", address="0x" + "ab" * 20, decimals=6)

    for _ in range(4):
        await c.verify_token(usdg)

    assert len(reads) == 1


async def test_a_symbol_repointed_at_another_contract_is_checked_again():
    """The guard is against a wrong registry entry, so what is remembered is
    the address -- not the symbol, which dynamic tokens can move."""
    from app.dex.tokens import Token

    c = client(private_key=KEY)
    reads = _counting_decimals(c)

    await c.verify_token(Token(symbol="CASHCAT", address="0x" + "ab" * 20, decimals=6))
    await c.verify_token(Token(symbol="CASHCAT", address="0x" + "cd" * 20, decimals=6))

    assert len(reads) == 2


async def test_a_registry_that_now_claims_other_decimals_is_checked_again():
    from app.dex.tokens import Token

    c = client(private_key=KEY)
    reads = _counting_decimals(c, value=6)
    address = "0x" + "ab" * 20

    await c.verify_token(Token(symbol="USDG", address=address, decimals=6))
    with pytest.raises(ChainError):
        await c.verify_token(Token(symbol="USDG", address=address, decimals=18))

    assert len(reads) == 2


# ---- two endpoints for one chain -----------------------------------------


def test_a_blank_tape_endpoint_falls_back_to_the_trading_one(monkeypatch):
    """Blank is the single-endpoint setup, and must stay the default."""
    monkeypatch.setattr(settings, "rh_rpc_url", "https://public.example")
    monkeypatch.setattr(settings, "chain_tape_rpc_url", "")

    c = ChainClient(rpc_url=settings.chain_tape_rpc_url or None)

    assert c.w3.provider.endpoint_uri == "https://public.example"


def test_the_tape_reads_through_its_own_endpoint_when_one_is_set(monkeypatch):
    """A rate limit the tape walks into must not be one a swap stands behind."""
    monkeypatch.setattr(settings, "rh_rpc_url", "https://metered.example/key")
    monkeypatch.setattr(settings, "chain_tape_rpc_url", "https://public.example")

    tape = ChainClient(rpc_url=settings.chain_tape_rpc_url or None)
    trading = ChainClient()

    assert tape.w3.provider.endpoint_uri == "https://public.example"
    assert trading.w3.provider.endpoint_uri == "https://metered.example/key"
