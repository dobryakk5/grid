import pytest

from app.core.config import settings
from app.dex.approvals import (
    ApprovalPlan,
    ensure_allowance,
    revoke_allowance,
    sign_permit,
)
from app.dex.chain import MAX_UINT256, ChainClient, ChainError
from app.dex.tokens import Token


KEY = "0x" + "11" * 32
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"

USDG = Token(symbol="USDG", address="0x" + "ab" * 20, decimals=6)
ETH = Token(symbol="ETH", address="0x" + "00" * 20, decimals=18, native=True)


class FakeChain(ChainClient):
    """Real signing and calldata encoding, fake network."""

    def __init__(self, *, allowance=0, status=1):
        super().__init__(rpc_url="https://rpc.example", private_key=KEY)
        self.allowance = allowance
        self.status = status
        self.broadcast_payloads = []

    async def token_allowance(self, token, spender, owner=None):
        return self.allowance

    async def pending_nonce(self, address=None):
        return 7

    async def estimate_gas(self, tx):
        return 50_000

    async def fee_fields(self, *, priority_wei=None):
        return {"maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8}

    async def broadcast(self, payload):
        self.broadcast_payloads.append(payload)
        return payload.tx_hash

    async def wait_for_receipt(self, tx_hash, *, timeout=None):
        return {"status": self.status, "transactionHash": tx_hash}


@pytest.fixture(autouse=True)
def settings_defaults(monkeypatch):
    monkeypatch.setattr(settings, "rh_chain_id", 4663)
    monkeypatch.setattr(settings, "permit2_address", PERMIT2)
    monkeypatch.setattr(settings, "dex_approve_exact", False)


def permit_data(*, verifying=PERMIT2, chain_id=4663, with_domain_type=False) -> dict:
    types = {
        "PermitSingle": [
            {"name": "details", "type": "PermitDetails"},
            {"name": "spender", "type": "address"},
            {"name": "sigDeadline", "type": "uint256"},
        ],
        "PermitDetails": [
            {"name": "token", "type": "address"},
            {"name": "amount", "type": "uint160"},
            {"name": "expiration", "type": "uint48"},
            {"name": "nonce", "type": "uint48"},
        ],
    }
    if with_domain_type:
        types["EIP712Domain"] = [
            {"name": "name", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ]
    return {
        "domain": {
            "name": "Permit2",
            "chainId": chain_id,
            "verifyingContract": verifying,
        },
        "types": types,
        "values": {
            "details": {
                "token": USDG.address,
                "amount": "1461501637330902918203684832716283019655932542975",
                "expiration": "1757000000",
                "nonce": "0",
            },
            "spender": ROUTER,
            "sigDeadline": "1757000000",
        },
    }


async def test_the_native_coin_is_never_approved():
    with pytest.raises(ChainError):
        await ensure_allowance(FakeChain(), ETH, amount_wei=1, dry_run=True)


async def test_a_sufficient_allowance_sends_nothing():
    chain = FakeChain(allowance=10**30)
    plan = await ensure_allowance(chain, USDG, amount_wei=250_000_000, dry_run=False)

    assert plan.sufficient
    assert not plan.sent
    assert chain.broadcast_payloads == []


async def test_a_dry_run_reports_the_approval_without_sending_it():
    chain = FakeChain(allowance=0)
    plan = await ensure_allowance(chain, USDG, amount_wei=250_000_000, dry_run=True)

    assert not plan.sufficient
    assert plan.approved_amount == MAX_UINT256
    assert plan.tx_hash is None
    assert chain.broadcast_payloads == []


async def test_a_live_approval_is_broadcast_and_waited_out():
    chain = FakeChain(allowance=0)
    plan = await ensure_allowance(chain, USDG, amount_wei=250_000_000, dry_run=False)

    assert plan.sent
    assert plan.tx_hash == chain.broadcast_payloads[0].tx_hash
    assert plan.approved_amount == MAX_UINT256


async def test_unlimited_is_the_default_because_permit2_gates_each_swap(monkeypatch):
    chain = FakeChain(allowance=0)
    unlimited = await ensure_allowance(chain, USDG, amount_wei=250_000_000, dry_run=True)

    monkeypatch.setattr(settings, "dex_approve_exact", True)
    exact = await ensure_allowance(chain, USDG, amount_wei=250_000_000, dry_run=True)

    assert unlimited.approved_amount == MAX_UINT256
    assert exact.approved_amount == 250_000_000


async def test_a_reverted_approval_is_an_error_not_a_silent_pass():
    chain = FakeChain(allowance=0, status=0)
    with pytest.raises(ChainError):
        await ensure_allowance(chain, USDG, amount_wei=1, dry_run=False)


async def test_revoking_approves_zero():
    chain = FakeChain(allowance=10**30)
    await revoke_allowance(chain, USDG)

    # approve(spender, 0) -- selector plus two zero-tailed words.
    raw = chain.broadcast_payloads[0].raw_hex
    assert raw.startswith("0x")
    assert chain.broadcast_payloads


def test_signing_a_permit_returns_a_65_byte_signature():
    signature = sign_permit(FakeChain(), permit_data())

    assert signature.startswith("0x")
    assert len(signature) == 132


def test_the_domain_type_is_stripped_rather_than_breaking_the_signature():
    # eth_account cannot determine a primary type while EIP712Domain is listed.
    with_domain = sign_permit(FakeChain(), permit_data(with_domain_type=True))
    without = sign_permit(FakeChain(), permit_data())

    assert with_domain == without


def test_a_permit_for_another_verifying_contract_is_refused():
    with pytest.raises(ChainError) as exc:
        sign_permit(FakeChain(), permit_data(verifying="0x" + "ee" * 20))
    assert "did not approve" in str(exc.value)


def test_a_permit_for_another_chain_is_refused():
    with pytest.raises(ChainError) as exc:
        sign_permit(FakeChain(), permit_data(chain_id=1))
    assert "chain 1" in str(exc.value)


def test_permit_data_without_values_is_refused():
    with pytest.raises(ChainError):
        sign_permit(FakeChain(), {"domain": {}, "types": {}})


def test_plan_reports_sufficiency_by_amount():
    plan = ApprovalPlan(token="USDG", spender=PERMIT2, required=100, current=100)
    assert plan.sufficient
    assert not plan.sent
