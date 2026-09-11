from decimal import Decimal

import pytest
from eth_account import Account

from app.core.config import settings
from app.dex.chain import ChainClient, ChainError, to_int


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
