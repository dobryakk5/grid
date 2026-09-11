"""One swap, end to end, with the crash-safe ordering that matters.

The sequence is the point of this module:

    quote -> check the limit -> build -> sign -> PERSIST -> broadcast -> receipt

The signed payload's hash is known before it reaches a node, so persisting it
under the reserved nonce *before* broadcasting is what makes a crash survivable:
a restarted process finds a row saying "SUBMITTING, hash 0x…, nonce 137" and can
ask the chain what happened, or re-broadcast that same payload. It never gets to
decide to buy again. Anything that dies before the commit never spent a nonce
and never sent anything.

``DEX_DRY_RUN`` defaults to true, and every path stops before signing while it
is on.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DexIntent
from app.dex.chain import ChainClient, ChainError, to_int
from app.dex.dexscreener import DexScreenerClient
from app.dex.intents import IntentStatus, assert_transition
from app.dex.receipts import FillReport, ReceiptError, parse_swap_fill
from app.dex.risk import evaluate
from app.dex.tokens import resolve_pair
from app.dex.uniswap import UniswapClient, UniswapError

__all__ = ["SwapOutcome", "execute_buy"]

logger = logging.getLogger(__name__)

# Gas headroom over the router's own estimate; a swap that runs out of gas still
# pays for the attempt.
_GAS_BUFFER = Decimal("1.15")


@dataclass(frozen=True)
class SwapOutcome:
    """Why a level did or did not trade, in terms the caller can log verbatim."""

    status: str
    symbol: str
    reason: str = ""
    market_price: Decimal | None = None
    quoted_price: Decimal | None = None
    fill_price: Decimal | None = None
    amount_in: Decimal | None = None
    amount_out: Decimal | None = None
    gas_native: Decimal | None = None
    tx_hash: str | None = None
    intent_id: int | None = None

    @property
    def traded(self) -> bool:
        return self.status == IntentStatus.FILLED


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def execute_buy(
    session: AsyncSession | None,
    *,
    symbol: str,
    amount_in: Decimal,
    limit_price: Decimal,
    chain: ChainClient,
    uniswap: UniswapClient,
    market: DexScreenerClient,
    dry_run: bool | None = None,
    profile_id: int | None = None,
    order_link_id: str | None = None,
) -> SwapOutcome:
    """Buy the pair's base token with ``amount_in`` of its quote token.

    ``limit_price`` is a ceiling on the *executable* price, not on the pool mid:
    a quote worse than it is declined, which is what keeps a synthetic limit
    order from degenerating into a market order.
    """
    pair = resolve_pair(symbol)
    dry = settings.dex_dry_run if dry_run is None else dry_run

    snapshot = await market.snapshot(pair)
    verdict = evaluate(snapshot)
    if not verdict.ok:
        return SwapOutcome(
            status=IntentStatus.BLOCKED,
            symbol=pair.symbol,
            reason="; ".join(verdict.reasons),
            market_price=snapshot.price_quote,
        )

    # The cheap watcher decides whether a quote is worth spending at all.
    band = limit_price * (1 + settings.dex_quote_trigger_band_pct / 100)
    if snapshot.price_quote > band:
        return SwapOutcome(
            status=IntentStatus.WAITING,
            symbol=pair.symbol,
            reason=f"pool price {snapshot.price_quote} is outside the trigger band {band}",
            market_price=snapshot.price_quote,
        )

    await chain.ensure_ready()
    # A wrong decimals value rescales every amount silently; check before money.
    await chain.verify_token(pair.base)

    amount_in_wei = pair.quote.to_wei(amount_in)
    balance = (
        await chain.native_balance()
        if pair.quote.native
        else await chain.token_balance(pair.quote)
    )
    if balance < amount_in:
        return SwapOutcome(
            status=IntentStatus.BLOCKED,
            symbol=pair.symbol,
            reason=f"wallet holds {balance} {pair.quote_coin}, needs {amount_in}",
            market_price=snapshot.price_quote,
        )

    quote = await uniswap.quote_exact_in(
        pair=pair,
        side="Buy",
        amount_in_wei=amount_in_wei,
        swapper=chain.wallet_address,
    )
    executable = quote.price(pair)
    if executable > limit_price:
        return SwapOutcome(
            status=IntentStatus.WAITING,
            symbol=pair.symbol,
            reason=(
                f"executable price {executable} is worse than the limit "
                f"{limit_price} for this size"
            ),
            market_price=snapshot.price_quote,
            quoted_price=executable,
        )

    expected_out = pair.base.from_wei(quote.amount_out)
    if dry:
        return SwapOutcome(
            status="DRY_RUN",
            symbol=pair.symbol,
            reason="DEX_DRY_RUN is on; nothing was signed or sent",
            market_price=snapshot.price_quote,
            quoted_price=executable,
            amount_in=amount_in,
            amount_out=expected_out,
        )
    if session is None:
        raise ChainError("a live swap needs a database session to record intent")

    swap = await uniswap.build_swap(quote)
    tx = await _build_transaction(chain, swap, pair_is_native_in=pair.quote.native)
    if pair.quote.native and int(tx["value"]) != amount_in_wei:
        # The router built something other than what we asked to spend.
        raise UniswapError(
            f"swap value {tx['value']} does not match the quoted input {amount_in_wei}"
        )

    signed = chain.sign(tx)

    # --- everything above is reversible; past this commit it is not ---
    intent = DexIntent(
        profile_id=profile_id,
        order_link_id=order_link_id or uuid.uuid4().hex,
        symbol=pair.symbol,
        side="Buy",
        status=IntentStatus.SUBMITTING,
        limit_price=limit_price,
        amount_in=amount_in,
        amount_in_coin=pair.quote_coin,
        min_amount_out=expected_out,
        baseline_liquidity_usd=snapshot.token_liquidity_usd,
        baseline_volume_h24_usd=snapshot.token_volume_h24,
        wallet_address=signed.wallet_address,
        nonce=signed.nonce,
        tx_hash=signed.tx_hash,
        raw_tx=signed.raw_hex,
        gas_native_coin="ETH",
    )
    session.add(intent)
    await session.commit()

    try:
        await chain.broadcast(signed)
    except ChainError as exc:
        await _fail(session, intent, str(exc))
        return SwapOutcome(
            status=IntentStatus.FAILED,
            symbol=pair.symbol,
            reason=str(exc),
            tx_hash=signed.tx_hash,
            intent_id=intent.id,
        )

    assert_transition(intent.status, IntentStatus.PENDING)
    intent.status = IntentStatus.PENDING
    intent.submitted_at = _now()
    await session.commit()

    try:
        receipt = await chain.wait_for_receipt(signed.tx_hash)
        fill = parse_swap_fill(
            receipt,
            wallet=signed.wallet_address,
            token_in=pair.quote,
            token_out=pair.base,
            sent_value_wei=int(tx["value"]),
        )
    except (ChainError, ReceiptError) as exc:
        # The transaction may still be in flight: leave the row PENDING when we
        # merely stopped waiting, and only fail it on a real revert.
        if isinstance(exc, ReceiptError):
            await _fail(session, intent, str(exc))
            status = IntentStatus.FAILED
        else:
            intent.last_error = str(exc)
            await session.commit()
            status = IntentStatus.PENDING
        return SwapOutcome(
            status=status,
            symbol=pair.symbol,
            reason=str(exc),
            tx_hash=signed.tx_hash,
            intent_id=intent.id,
        )

    return await _record_fill(
        session, intent, pair=pair, fill=fill, quoted_price=executable
    )


async def _build_transaction(
    chain: ChainClient, swap: dict, *, pair_is_native_in: bool
) -> dict:
    tx: dict = {
        "chainId": chain.chain_id,
        "nonce": await chain.pending_nonce(),
        "to": swap["to"],
        "data": swap["data"],
        "value": to_int(swap.get("value")) or 0,
    }
    if not pair_is_native_in and tx["value"]:
        raise UniswapError("ERC-20 input must not carry a native value")

    gas_limit = to_int(swap.get("gasLimit")) or to_int(swap.get("gas"))
    tx["gas"] = (
        int(Decimal(gas_limit) * _GAS_BUFFER)
        if gas_limit
        else int(Decimal(await chain.estimate_gas(tx)) * _GAS_BUFFER)
    )

    max_fee = to_int(swap.get("maxFeePerGas"))
    if max_fee is not None:
        tx["maxFeePerGas"] = max_fee
        tx["maxPriorityFeePerGas"] = to_int(swap.get("maxPriorityFeePerGas")) or 0
    elif swap.get("gasPrice") is not None:
        tx["gasPrice"] = to_int(swap["gasPrice"])
    else:
        tx.update(await chain.fee_fields())
    return tx


async def _fail(session: AsyncSession, intent: DexIntent, reason: str) -> None:
    assert_transition(intent.status, IntentStatus.FAILED)
    intent.status = IntentStatus.FAILED
    intent.last_error = reason[:500]
    await session.commit()


async def _record_fill(
    session: AsyncSession,
    intent: DexIntent,
    *,
    pair,
    fill: FillReport,
    quoted_price: Decimal,
) -> SwapOutcome:
    amount_in = fill.amount_in(pair.quote)
    amount_out = fill.amount_out(pair.base)
    price = fill.price(token_in=pair.quote, token_out=pair.base)

    assert_transition(intent.status, IntentStatus.FILLED)
    intent.status = IntentStatus.FILLED
    intent.filled_amount_in = amount_in
    intent.filled_amount_out = amount_out
    intent.fill_price = price
    intent.gas_native = fill.gas_native
    intent.confirmed_block = fill.block_number
    intent.block_hash = fill.block_hash
    # The payload only exists to make a re-broadcast possible; it is confirmed.
    intent.raw_tx = None
    await session.commit()

    logger.info(
        "%s filled: %s %s -> %s %s at %s (gas %s ETH, tx %s)",
        intent.symbol, amount_in, pair.quote_coin, amount_out, pair.base_coin,
        price, fill.gas_native, fill.tx_hash,
    )
    return SwapOutcome(
        status=IntentStatus.FILLED,
        symbol=intent.symbol,
        quoted_price=quoted_price,
        fill_price=price,
        amount_in=amount_in,
        amount_out=amount_out,
        gas_native=fill.gas_native,
        tx_hash=fill.tx_hash,
        intent_id=intent.id,
    )
