"""ERC-20 allowances and Permit2 signatures.

Spending an ERC-20 through Uniswap is two permissions, not one:

1. a plain ERC-20 ``approve`` naming **Permit2** as the spender, sent once per
   token per wallet;
2. a per-swap EIP-712 signature that Permit2 checks, carrying the amount, the
   spender (the Universal Router) and an expiry.

The second is why the first is normally unlimited: the standing approval is to
Permit2, and every actual transfer is still gated by a fresh signature that
expires. ``DEX_APPROVE_EXACT`` opts out of that at one extra approval per trade.

Native ETH needs neither, which is why the first execution stage could skip
this module entirely.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import settings
from app.dex.chain import MAX_UINT256, ChainClient, ChainError
from app.dex.tokens import Token

__all__ = [
    "ApprovalPlan",
    "ensure_allowance",
    "permit2_address",
    "revoke_allowance",
    "sign_permit",
]

logger = logging.getLogger(__name__)


def permit2_address() -> str:
    address = (settings.permit2_address or "").strip()
    if not address:
        raise ChainError("PERMIT2_ADDRESS is not configured")
    return address


@dataclass(frozen=True)
class ApprovalPlan:
    """What the allowance check decided, and what came of it."""

    token: str
    spender: str
    required: int
    current: int
    approved_amount: int | None = None
    tx_hash: str | None = None

    @property
    def sufficient(self) -> bool:
        return self.current >= self.required

    @property
    def sent(self) -> bool:
        return self.tx_hash is not None


async def ensure_allowance(
    chain: ChainClient,
    token: Token,
    *,
    amount_wei: int,
    spender: str | None = None,
    dry_run: bool = True,
) -> ApprovalPlan:
    """Make sure Permit2 may move ``amount_wei`` of ``token`` for our wallet.

    A dry run reports what would be sent and sends nothing. A live run waits for
    the approval receipt before returning: the swap that follows is worthless
    until the allowance is actually on chain.
    """
    if token.native:
        raise ChainError(f"{token.symbol} is the native coin and needs no approval")

    target = spender or permit2_address()
    current = await chain.token_allowance(token, target)
    plan = ApprovalPlan(
        token=token.symbol, spender=target, required=amount_wei, current=current
    )
    if plan.sufficient:
        return plan

    amount = amount_wei if settings.dex_approve_exact else MAX_UINT256
    if dry_run:
        return ApprovalPlan(
            token=token.symbol, spender=target, required=amount_wei,
            current=current, approved_amount=amount,
        )

    tx = chain.encode_approve(token, target, amount)
    tx_hash = await _send(chain, tx)
    logger.info(
        "approved %s for Permit2 %s (tx %s)", token.symbol, target, tx_hash
    )
    return ApprovalPlan(
        token=token.symbol, spender=target, required=amount_wei, current=amount,
        approved_amount=amount, tx_hash=tx_hash,
    )


async def revoke_allowance(
    chain: ChainClient, token: Token, *, spender: str | None = None
) -> str:
    """Set the allowance back to zero. Never called automatically."""
    target = spender or permit2_address()
    return await _send(chain, chain.encode_approve(token, target, 0))


async def _send(chain: ChainClient, tx: dict) -> str:
    payload = dict(tx)
    payload["chainId"] = chain.chain_id
    payload["nonce"] = await chain.pending_nonce()
    payload["gas"] = int(Decimal(await chain.estimate_gas(payload)) * Decimal("1.2"))
    payload.update(await chain.fee_fields())

    signed = chain.sign(payload)
    await chain.broadcast(signed)
    receipt = await chain.wait_for_receipt(signed.tx_hash)
    if int(receipt.get("status", 0)) != 1:
        raise ChainError(f"approval transaction {signed.tx_hash} reverted")
    return signed.tx_hash


def sign_permit(chain: ChainClient, permit_data: dict) -> str:
    """Sign the ``permitData`` a quote came back with.

    The permit names its own verifying contract, so it is checked against the
    Permit2 address we approved: if they disagree, the standing approval does
    not cover this signature and the swap would fail on chain -- or worse,
    authorise a contract we never approved.
    """
    domain = dict(permit_data.get("domain") or {})
    types = permit_data.get("types") or {}
    message = permit_data.get("values") or permit_data.get("message") or {}
    if not types or not message:
        raise ChainError(f"permitData is missing types or values: {permit_data}")

    verifying = str(domain.get("verifyingContract") or "").strip()
    expected = permit2_address()
    if verifying and verifying.lower() != expected.lower():
        raise ChainError(
            f"permit names Permit2 at {verifying}, but PERMIT2_ADDRESS is "
            f"{expected}; refusing to sign for a contract we did not approve"
        )

    chain_id = domain.get("chainId")
    if chain_id is not None:
        domain["chainId"] = int(chain_id, 16) if isinstance(chain_id, str) and chain_id.startswith("0x") else int(chain_id)
        if domain["chainId"] != chain.chain_id:
            raise ChainError(
                f"permit is for chain {domain['chainId']}, expected {chain.chain_id}"
            )

    return chain.sign_typed_data(domain=domain, types=types, message=message)
