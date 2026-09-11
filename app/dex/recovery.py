"""Picking up intents that were already signed when something went wrong.

Every row this module touches has a nonce and a transaction hash, so none of the
choices here involve deciding whether to trade -- that was decided before the
commit. The only questions are:

* did it land? Settle it against the receipt.
* was it never actually broadcast? Send the same payload again; a node that
  already has it says so, which is success, not an error.
* has it been unmined for too long? Free the nonce with a replacement at the
  same nonce and higher fees, and let the level re-arm as a new attempt.

A stuck swap is deliberately *not* re-signed with fresh calldata: the quote it
was built on is long stale, and a bumped swap would execute at a price nobody
checked. Replacing it with a no-op and re-quoting is the honest path.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DexIntent
from app.dex.chain import ChainClient, ChainError, SignedPayload
from app.dex.dexscreener import DexScreenerClient
from app.dex.execution import SwapOutcome, price_gas, read_fill, record_fill
from app.dex.intents import IntentStatus
from app.dex.receipts import ReceiptError
from app.dex.repository import DexIntentRepository
from app.dex.tokens import DexPair, resolve_pair

__all__ = ["rebroadcast", "replace_stuck", "settle"]

logger = logging.getLogger(__name__)


def _direction(pair: DexPair, side: str):
    return (pair.base, pair.quote) if side.strip().lower() == "sell" else (pair.quote, pair.base)


def _sent_value_wei(pair: DexPair, intent: DexIntent) -> int:
    token_in, _ = _direction(pair, intent.side)
    return token_in.to_wei(Decimal(intent.amount_in)) if token_in.native else 0


async def settle(
    session: AsyncSession,
    repository: DexIntentRepository,
    intent: DexIntent,
    *,
    chain: ChainClient,
    market: DexScreenerClient,
) -> SwapOutcome | None:
    """Finish an intent if its transaction has a receipt; otherwise leave it.

    ``None`` means still pending -- which is not the same as missing, and must
    never be treated as permission to trade again.
    """
    receipt = await chain.receipt(intent.tx_hash)
    if receipt is None:
        return None

    pair = resolve_pair(intent.symbol)
    token_in, token_out = _direction(pair, intent.side)

    if intent.status == IntentStatus.SUBMITTING:
        # It was broadcast after all, we just never saw it happen.
        await repository.transition(intent, IntentStatus.PENDING)
        await session.commit()

    try:
        fill = await read_fill(
            chain,
            receipt,
            wallet=intent.wallet_address,
            token_in=token_in,
            token_out=token_out,
            sent_value_wei=_sent_value_wei(pair, intent),
        )
    except ReceiptError as exc:
        await repository.fail(intent, str(exc))
        await session.commit()
        logger.warning("%s reverted: %s", intent.tx_hash, exc)
        return SwapOutcome(
            status=IntentStatus.FAILED,
            symbol=intent.symbol,
            side=intent.side,
            reason=str(exc),
            tx_hash=intent.tx_hash,
            intent_id=intent.id,
        )

    snapshot = await market.snapshot(pair)
    gas = await price_gas(market, pair, snapshot, fill)
    return await record_fill(
        session, intent, pair=pair, side=intent.side, fill=fill, gas=gas,
        quoted_price=Decimal(intent.limit_price),
    )


async def rebroadcast(chain: ChainClient, intent: DexIntent) -> str:
    """Send the stored payload again, byte for byte."""
    if not intent.raw_tx:
        raise ChainError(
            f"intent {intent.id} is {intent.status} with no payload to re-send"
        )
    payload = SignedPayload(
        tx_hash=intent.tx_hash,
        raw=bytes.fromhex(intent.raw_tx.removeprefix("0x")),
        nonce=int(intent.nonce),
        wallet_address=intent.wallet_address,
    )
    return await chain.broadcast(payload)


async def replace_stuck(
    session: AsyncSession,
    repository: DexIntentRepository,
    intent: DexIntent,
    *,
    chain: ChainClient,
) -> DexIntent | None:
    """Cancel a transaction that will not mine, and re-arm the level.

    The replacement is a zero-value send to ourselves at the same nonce with
    higher fees: whichever of the two lands, the nonce is spent exactly once and
    no swap executes at a price we never re-checked.
    """
    bump = 1 + settings.dex_gas_bump_pct / 100
    fees = await chain.fee_fields()
    for key in ("maxFeePerGas", "maxPriorityFeePerGas", "gasPrice"):
        if key in fees:
            fees[key] = int(Decimal(fees[key]) * bump)

    cancel = {
        "chainId": chain.chain_id,
        "nonce": int(intent.nonce),
        "to": chain.wallet_address,
        "value": 0,
        "data": "0x",
        "gas": 21_000,
        **fees,
    }
    signed = chain.sign(cancel)
    await chain.broadcast(signed)
    logger.warning(
        "replaced stuck %s at nonce %s with %s",
        intent.tx_hash, intent.nonce, signed.tx_hash,
    )

    await repository.fail(
        intent, f"unmined for too long; replaced at nonce {intent.nonce} by {signed.tx_hash}"
    )
    retry = await repository.retry_level(intent)
    await session.commit()
    return retry
