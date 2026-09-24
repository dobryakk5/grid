"""One swap, end to end, with the crash-safe ordering that matters.

The sequence is the point of this module:

    quote -> check the limit -> build -> sign -> PERSIST -> broadcast -> receipt

The signed payload's hash is known before it reaches a node, so persisting it
under the reserved nonce *before* broadcasting is what makes a crash survivable:
a restarted process finds a row saying "SUBMITTING, hash 0x…, nonce 137" and can
ask the chain what happened, or re-broadcast that same payload. It never gets to
decide to trade again. Anything that dies before the commit never spent a nonce
and never sent anything.

Both directions go through one function. A buy spends the quote token to receive
base, a sell does the reverse, but the limit always compares quote-per-base, so
"fill at my price or better" means the same thing either way.

``DEX_DRY_RUN`` defaults to true, and every path stops before signing while it
is on.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import DexIntent
from app.dex.approvals import ensure_allowance, sign_permit
from app.dex.accounting import execution_values, realised_price
from app.dex.chain import ChainClient, ChainError, PreflightRevert, to_int
from app.dex.dexscreener import DexScreenerClient, MarketSnapshot
from app.dex.intents import IntentStatus, assert_transition
from app.dex.pricing import GasCost, PricingError, convert_gas
from app.dex.receipts import FillReport, ReceiptError, parse_swap_fill
from app.dex.repository import DexIntentRepository
from app.dex.risk import evaluate
from app.dex.tokens import DexConfigError, DexPair, Token, resolve_pair_at
from app.dex.uniswap import UniswapClient, UniswapError
from app.notify.events import dex_kind
from app.notify.outbox import enqueue

__all__ = [
    "StorageUnavailable",
    "SwapOutcome",
    "execute_buy",
    "execute_sell",
    "execute_swap",
    "price_gas",
    "read_fill",
    "record_fill",
]

logger = logging.getLogger(__name__)

# Gas headroom over the router's own estimate; a swap that runs out of gas still
# pays for the attempt.
_GAS_BUFFER = Decimal("1.15")


class StorageUnavailable(RuntimeError):
    """A live swap cannot be recorded, so it must not be sent."""


@dataclass(frozen=True)
class SwapOutcome:
    """Why a level did or did not trade, in terms the caller can log verbatim."""

    status: str
    symbol: str
    side: str = "Buy"
    reason: str = ""
    market_price: Decimal | None = None
    quoted_price: Decimal | None = None
    # The worst the swap can do without reverting -- what the limit is judged on.
    worst_price: Decimal | None = None
    worst_amount_out: Decimal | None = None
    gas_estimate_native: Decimal | None = None
    gas_estimate_usd: Decimal | None = None
    fill_price: Decimal | None = None
    amount_in: Decimal | None = None
    amount_out: Decimal | None = None
    gas_native: Decimal | None = None
    gas_quote: Decimal | None = None
    tx_hash: str | None = None
    approval_tx_hash: str | None = None
    intent_id: int | None = None
    # Set whenever the wallet could not pay for this trade, whatever else
    # happened: finding that out only after retuning the limit wastes a run.
    funding_note: str | None = None
    execution_values: dict | None = None

    @property
    def traded(self) -> bool:
        return self.status == IntentStatus.FILLED


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_sell(side: str) -> bool:
    normalized = side.strip().lower()
    if normalized not in {"buy", "sell"}:
        raise UniswapError(f"unknown side {side!r}")
    return normalized == "sell"


def _direction(pair: DexPair, side: str) -> tuple[Token, Token]:
    """``(spent, received)`` for this direction."""
    return (pair.base, pair.quote) if _is_sell(side) else (pair.quote, pair.base)


def _within_trigger_band(price: Decimal, limit: Decimal, *, selling: bool) -> bool:
    """Is the pool price close enough to the level to be worth a quote?"""
    band = settings.dex_quote_trigger_band_pct / 100
    return price >= limit * (1 - band) if selling else price <= limit * (1 + band)


def _meets_limit(executable: Decimal, limit: Decimal, *, selling: bool) -> bool:
    return executable >= limit if selling else executable <= limit


class _Progress:
    """Walks an existing level through the state machine, if there is one.

    The manual script has no row until the moment of signing; the worker has one
    from the moment the level was armed. Both drive the same code, so every
    transition is expressed once here and simply does nothing when this swap is
    not attached to a level.
    """

    def __init__(
        self,
        repository: DexIntentRepository | None,
        intent: DexIntent | None,
    ) -> None:
        self.repository = repository
        self.intent = intent

    @property
    def active(self) -> bool:
        return self.repository is not None and self.intent is not None

    async def to(self, target: str, **fields) -> None:
        if not self.active or self.intent.status == target:
            return
        await self.repository.transition(self.intent, target, **fields)

    async def block(self, reason: str) -> None:
        if self.active:
            await self.repository.block(self.intent, reason)

    async def missed(self, reason: str) -> None:
        if self.active and self.intent.status != IntentStatus.MISSED:
            await self.repository.missed(self.intent, reason)

    async def stand_down(self, reason: str | None = None) -> None:
        """Price moved away or the quote was not good enough: keep watching.

        A MISSED level keeps its label instead of being walked back to WAITING.
        Surviving the price leaving the band is the entire point of the status:
        that walk-back is what used to erase the evidence, leaving a level that
        looked as though its price had never come.
        """
        if self.active and self.intent.status == IntentStatus.MISSED:
            return
        await self.to(
            IntentStatus.WAITING, **({"last_error": reason[:500]} if reason else {})
        )


async def execute_swap(
    session: AsyncSession | None,
    *,
    symbol: str,
    side: str,
    amount_in: Decimal,
    limit_price: Decimal,
    chain: ChainClient,
    uniswap: UniswapClient,
    market: DexScreenerClient,
    dry_run: bool | None = None,
    slippage_pct: Decimal | None = None,
    profile_id: int | None = None,
    order_link_id: str | None = None,
    repository: DexIntentRepository | None = None,
    intent: DexIntent | None = None,
) -> SwapOutcome:
    """Trade ``amount_in`` of the spent token, at ``limit_price`` or better.

    ``limit_price`` is always quote-per-base and always a bound on the
    *executable* price for this size, never on the pool mid: a buy declines a
    quote above it, a sell declines one below. That is what keeps a synthetic
    limit order from degenerating into a market order.
    """
    progress = _Progress(repository, intent)
    try:
        pair = resolve_pair_at(symbol, intent.token_address if intent is not None else None)
    except DexConfigError as exc:
        # Blocked, not skipped: a level stuck on the wrong contract has to be
        # visible on the history page, not only in the worker's log.
        await progress.block(str(exc))
        return SwapOutcome(
            status=IntentStatus.BLOCKED, symbol=symbol, side=side, reason=str(exc),
        )
    selling = _is_sell(side)
    token_in, token_out = _direction(pair, side)
    dry = settings.dex_dry_run if dry_run is None else dry_run

    # Prove we can record the swap before anything irreversible happens. The
    # recording is what makes a crash survivable, so discovering it is
    # impossible *after* an approval has been sent and a transaction signed is
    # the worst possible moment -- and the most expensive one.
    if not dry:
        await _require_storage(session)

    snapshot = await market.snapshot(pair)
    # A sale is never gated on liquidity. The floors exist to stop us *entering*
    # a market too thin to leave; applying them to the leaving is backwards, and
    # backwards hardest in the case they were written for -- the price reached
    # our level because the coin is dying, which is the moment the gate would
    # fire and lock the position in. For a buy the waiver still has to be asked
    # for, and it travels with the level that was armed with it.
    waived = selling or (intent is not None and bool(intent.ignore_liquidity_gate))
    verdict = evaluate(snapshot, ignore_liquidity=waived)
    if not verdict.ok:
        await progress.block("; ".join(verdict.reasons))
        return SwapOutcome(
            status=IntentStatus.BLOCKED,
            symbol=pair.symbol,
            side=side,
            reason="; ".join(verdict.reasons),
            market_price=snapshot.price_quote,
        )

    # The cheap watcher decides whether a quote is worth spending at all.
    if not _within_trigger_band(snapshot.price_quote, limit_price, selling=selling):
        await progress.stand_down()
        return SwapOutcome(
            status=IntentStatus.WAITING,
            symbol=pair.symbol,
            side=side,
            reason=(
                f"pool price {snapshot.price_quote} is outside the trigger band "
                f"around {limit_price}"
            ),
            market_price=snapshot.price_quote,
        )

    # A new attempt clears the last one's note: the reason a level stood down
    # is worth showing while it is still standing down, and misleading after.
    await progress.to(IntentStatus.TRIGGERED, last_error=None)
    await chain.ensure_ready()
    # A wrong decimals value rescales every amount silently; check before money.
    await chain.verify_token(pair.base)

    amount_in_wei = token_in.to_wei(amount_in)
    balance = (
        await chain.native_balance()
        if token_in.native
        else await chain.token_balance(token_in)
    )
    underfunded = (
        f"wallet holds {balance} {token_in.symbol}, needs {amount_in}"
        if balance < amount_in
        else None
    )
    # A dry run is asking what would happen, so an empty wallet must not hide
    # the quote behind it -- it is reported and the pipeline runs on. A live run
    # stops here rather than quoting something it cannot pay for.
    if underfunded and not dry:
        # Not BLOCKED: nothing is wrong with the market, the wallet simply had
        # nothing to spend when its moment came. The operator can fix this one.
        await progress.missed(underfunded)
        return SwapOutcome(
            status=IntentStatus.MISSED,
            symbol=pair.symbol,
            side=side,
            reason=underfunded,
            market_price=snapshot.price_quote,
        )

    # An ERC-20 input needs a standing approval to Permit2 before a swap can
    # move it; native ETH needs none. A live approval is waited out here, so the
    # swap that follows signs against an allowance that is already on chain.
    approval = None
    if not token_in.native and not underfunded:
        approval = await ensure_allowance(
            chain, token_in, amount_wei=amount_in_wei, dry_run=dry
        )

    quote = await uniswap.quote_exact_in(
        pair=pair,
        side=side,
        amount_in_wei=amount_in_wei,
        swapper=chain.wallet_address,
        slippage_pct=slippage_pct,
    )
    await progress.to(IntentStatus.QUOTED)
    executable = quote.price(pair, side)
    # The limit is judged on the worst fill the transaction can produce, not the
    # expected one: slippage tolerance is the difference between a promise and a
    # hope, and a synthetic limit order has to make a promise.
    guaranteed = quote.worst_price(pair, side)
    if not _meets_limit(guaranteed, limit_price, selling=selling):
        await progress.stand_down()
        return SwapOutcome(
            status=IntentStatus.WAITING,
            symbol=pair.symbol,
            side=side,
            reason=(
                f"worst-case fill {guaranteed} is worse than the limit "
                f"{limit_price} for this size (expected {executable})"
            ),
            market_price=snapshot.price_quote,
            quoted_price=executable,
            worst_price=guaranteed,
            worst_amount_out=token_out.from_wei(
                quote.min_amount_out or quote.amount_out
            ),
            funding_note=underfunded,
            **_gas_estimate(quote),
        )

    expected_out = token_out.from_wei(quote.amount_out)
    guaranteed_out = token_out.from_wei(quote.min_amount_out or quote.amount_out)
    if dry:
        await progress.stand_down()
        notes = ["DEX_DRY_RUN is on; nothing was signed or sent"]
        if underfunded:
            notes.append(f"{underfunded} -- fund it before a live run")
        if approval is not None and not approval.sufficient:
            notes.append(
                f"would first approve {approval.token} for Permit2 {approval.spender}"
            )
        if quote.needs_permit:
            notes.append("quote requires a Permit2 signature")
        return SwapOutcome(
            status="DRY_RUN",
            symbol=pair.symbol,
            side=side,
            reason="; ".join(notes),
            market_price=snapshot.price_quote,
            quoted_price=executable,
            worst_price=guaranteed,
            worst_amount_out=guaranteed_out,
            amount_in=amount_in,
            amount_out=expected_out,
            funding_note=underfunded,
            **_gas_estimate(quote),
        )
    await progress.to(IntentStatus.SIGNING)
    signature = sign_permit(chain, quote.permit_data) if quote.needs_permit else None
    swap = await uniswap.build_swap(quote, signature=signature)
    try:
        tx = await _build_transaction(
            chain, swap, native_input=token_in.native, repository=repository
        )
    except PreflightRevert as exc:
        # Nothing was signed and no nonce was spent: this quote's route does not
        # execute. Keep the level watching -- the next tick quotes again, and
        # may well be routed somewhere that works.
        logger.warning("%s %s: pre-flight refused the swap: %s", pair.symbol, side, exc)
        await progress.stand_down(f"pre-flight: {exc}")
        return SwapOutcome(
            status=IntentStatus.WAITING,
            symbol=pair.symbol,
            side=side,
            reason=f"pre-flight refused the swap: {exc}",
            market_price=snapshot.price_quote,
            quoted_price=executable,
            worst_price=guaranteed,
            worst_amount_out=guaranteed_out,
            amount_in=amount_in,
            amount_out=expected_out,
            funding_note=underfunded,
            **_gas_estimate(quote),
        )
    if token_in.native and int(tx["value"]) != amount_in_wei:
        # The router built something other than what we asked to spend.
        raise UniswapError(
            f"swap value {tx['value']} does not match the quoted input {amount_in_wei}"
        )

    signed = chain.sign(tx)

    # --- everything above is reversible; past this commit it is not ---
    if progress.active:
        await progress.to(
            IntentStatus.SUBMITTING,
            min_amount_out=guaranteed_out,
            baseline_liquidity_usd=snapshot.token_liquidity_usd,
            baseline_volume_h24_usd=snapshot.token_volume_h24,
            wallet_address=signed.wallet_address,
            nonce=signed.nonce,
            tx_hash=signed.tx_hash,
            raw_tx=signed.raw_hex,
            gas_native_coin="ETH",
            approval_tx_hash=approval.tx_hash if approval is not None else None,
        )
        await session.commit()
        return await _broadcast_and_settle(
            session, intent, signed=signed, pair=pair, side=side, tx=tx,
            chain=chain, market=market, snapshot=snapshot, quoted_price=executable,
        )

    intent = DexIntent(
        profile_id=profile_id,
        order_link_id=order_link_id or uuid.uuid4().hex,
        symbol=pair.symbol,
        side="Sell" if selling else "Buy",
        status=IntentStatus.SUBMITTING,
        limit_price=limit_price,
        amount_in=amount_in,
        amount_in_coin=token_in.symbol,
        min_amount_out=guaranteed_out,
        baseline_liquidity_usd=snapshot.token_liquidity_usd,
        baseline_volume_h24_usd=snapshot.token_volume_h24,
        wallet_address=signed.wallet_address,
        nonce=signed.nonce,
        tx_hash=signed.tx_hash,
        raw_tx=signed.raw_hex,
        gas_native_coin="ETH",
        approval_tx_hash=approval.tx_hash if approval is not None else None,
    )
    session.add(intent)
    await session.commit()
    return await _broadcast_and_settle(
        session, intent, signed=signed, pair=pair, side=side, tx=tx,
        chain=chain, market=market, snapshot=snapshot, quoted_price=executable,
    )


async def _broadcast_and_settle(
    session: AsyncSession,
    intent: DexIntent,
    *,
    signed,
    pair: DexPair,
    side: str,
    tx: dict,
    chain: ChainClient,
    market: DexScreenerClient,
    snapshot: MarketSnapshot,
    quoted_price: Decimal,
) -> SwapOutcome:
    """Send a signed, persisted swap and settle whatever the chain says."""
    token_in, token_out = _direction(pair, side)

    try:
        await chain.broadcast(signed)
    except ChainError as exc:
        await _fail(session, intent, str(exc))
        return SwapOutcome(
            status=IntentStatus.FAILED,
            symbol=pair.symbol,
            side=side,
            reason=str(exc),
            tx_hash=signed.tx_hash,
            approval_tx_hash=intent.approval_tx_hash,
            intent_id=intent.id,
        )

    assert_transition(intent.status, IntentStatus.PENDING)
    intent.status = IntentStatus.PENDING
    intent.submitted_at = _now()
    await session.commit()

    try:
        receipt = await chain.wait_for_receipt(signed.tx_hash)
        fill = await read_fill(
            chain,
            receipt,
            wallet=signed.wallet_address,
            token_in=token_in,
            token_out=token_out,
            sent_value_wei=int(tx["value"]),
        )
    except (ChainError, ReceiptError) as exc:
        # The transaction may still be in flight: leave the row PENDING when we
        # merely stopped waiting, and only fail it on a real revert.
        if isinstance(exc, ReceiptError):
            await _fail(session, intent, str(exc))
            status = IntentStatus.FAILED
        else:
            intent.last_error = str(exc)[:500]
            await session.commit()
            status = IntentStatus.PENDING
        return SwapOutcome(
            status=status,
            symbol=pair.symbol,
            side=side,
            reason=str(exc),
            tx_hash=signed.tx_hash,
            approval_tx_hash=intent.approval_tx_hash,
            intent_id=intent.id,
        )

    gas = await price_gas(market, pair, snapshot, fill)
    return await record_fill(
        session, intent, pair=pair, side=side, fill=fill, gas=gas,
        quoted_price=quoted_price,
    )


async def execute_buy(session: AsyncSession | None, **kwargs) -> SwapOutcome:
    return await execute_swap(session, side="Buy", **kwargs)


async def execute_sell(session: AsyncSession | None, **kwargs) -> SwapOutcome:
    return await execute_swap(session, side="Sell", **kwargs)


async def read_fill(
    chain: ChainClient,
    receipt: dict,
    *,
    wallet: str,
    token_in: Token,
    token_out: Token,
    sent_value_wei: int,
) -> FillReport:
    """Parse the receipt, measuring a native output from balances if needed."""
    native_out_wei = None
    if token_out.native:
        gas_wei = int(receipt.get("gasUsed") or 0) * int(
            receipt.get("effectiveGasPrice") or 0
        )
        native_out_wei = await chain.native_received(
            block_number=int(receipt["blockNumber"]),
            gas_wei=gas_wei,
            value_sent_wei=sent_value_wei,
            address=wallet,
        )
    return parse_swap_fill(
        receipt,
        wallet=wallet,
        token_in=token_in,
        token_out=token_out,
        sent_value_wei=sent_value_wei,
        native_out_wei=native_out_wei,
    )


async def price_gas(
    market: DexScreenerClient,
    pair: DexPair,
    snapshot: MarketSnapshot,
    fill: FillReport,
) -> GasCost | None:
    """Value the gas in quote terms; a missing rate must not lose the fill."""
    try:
        return await convert_gas(market, pair, snapshot, fill.gas_native)
    except PricingError as exc:
        logger.warning(
            "%s gas stays unconverted (%s ETH): %s", pair.symbol, fill.gas_native, exc
        )
        return None


async def _build_transaction(
    chain: ChainClient,
    swap: dict,
    *,
    native_input: bool,
    repository: DexIntentRepository | None = None,
) -> dict:
    _check_router(swap["to"])
    tx: dict = {
        "chainId": chain.chain_id,
        "to": swap["to"],
        "data": swap["data"],
        "value": to_int(swap.get("value")) or 0,
    }
    if not native_input and tx["value"]:
        raise UniswapError("ERC-20 input must not carry a native value")

    # Ask the node whether this executes before paying to find out. A reverted
    # swap is charged in full -- the router does not refund the attempt -- and
    # the failures worth catching here are structural: a route through a pool
    # this router cannot settle reverts in every block, not just this one.
    await chain.preflight(tx)

    gas_limit = to_int(swap.get("gasLimit")) or to_int(swap.get("gas"))
    estimated = 0
    try:
        estimated = await chain.estimate_gas(tx)
    except Exception as exc:
        # The pre-flight already passed, so this is the node declining to
        # measure rather than a swap that cannot run. Fall back to the router's
        # own figure; with neither, there is nothing honest to sign.
        if not gas_limit:
            raise
        logger.warning("gas estimate failed, using the router's %s: %s", gas_limit, exc)
    # Never below what the router asked for: the estimate is measured against
    # this block, and a swap that crosses one tick more than it simulated still
    # has to fit.
    tx["gas"] = int(Decimal(max(estimated, gas_limit or 0)) * _GAS_BUFFER)

    # The nonce is reserved last, once the swap is known to be executable. With
    # a repository it is handed out under a row lock, so two workers cannot give
    # the same one to two transactions; without it the chain's pending count is
    # the only source, which is fine for a single manual run.
    chain_nonce = await chain.pending_nonce()
    tx["nonce"] = (
        await repository.reserve_nonce(
            wallet=chain.wallet_address, chain_nonce=chain_nonce
        )
        if repository is not None
        else chain_nonce
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


async def _require_storage(session: AsyncSession | None) -> None:
    """Prove the intent can be written, not merely that a server answers.

    The probe reads from the table the swap will be recorded in: a reachable
    database with no schema in it fails just as late and just as expensively as
    an unreachable one.
    """
    if session is None:
        raise StorageUnavailable("a live swap needs a database session")
    try:
        await session.execute(text("SELECT 1 FROM dex_intents LIMIT 0"))
    except Exception as exc:
        # The session is left in a failed transaction; the worker reuses it.
        try:
            await session.rollback()
        except Exception:  # pragma: no cover - the connection is already gone
            pass
        detail = str(exc)
        if "does not exist" in detail or "UndefinedTable" in detail:
            raise StorageUnavailable(
                "the database has no dex_intents table, so a live swap could "
                "not be recorded before it is broadcast; create the schema "
                "with `make db-init`"
            ) from None
        raise StorageUnavailable(
            "a live swap has to be recorded before it is broadcast, and the "
            f"database is not reachable: {exc}"
        ) from None


def _gas_estimate(quote) -> dict:
    """The router's own gas estimate, for a preview to show before signing."""
    return {
        "gas_estimate_native": (
            Decimal(quote.gas_fee_native_wei).scaleb(-18)
            if quote.gas_fee_native_wei
            else None
        ),
        "gas_estimate_usd": quote.gas_fee_usd,
    }


def _check_router(target: str) -> None:
    """Refuse calldata aimed at anything but a router we trust.

    Plural because the chain has more than one live Universal Router and the
    Trading API picks which one it builds for; a single pin turns the API
    moving to another deployment into every order being refused. The guard is
    unchanged in what it is for: an address nobody put on the list is still
    something we will not sign.
    """
    allowed = {
        item.strip().lower()
        for item in (settings.rh_universal_router_address or "").split(",")
        if item.strip()
    }
    if allowed and target.strip().lower() not in allowed:
        raise UniswapError(
            f"swap targets {target}, which is not one of the configured "
            f"Universal Routers ({', '.join(sorted(allowed))}); refusing to sign it"
        )


async def _fail(session: AsyncSession, intent: DexIntent, reason: str) -> None:
    assert_transition(intent.status, IntentStatus.FAILED)
    intent.status = IntentStatus.FAILED
    intent.last_error = reason[:500]
    await session.commit()


async def record_fill(
    session: AsyncSession,
    intent: DexIntent,
    *,
    pair: DexPair,
    side: str,
    fill: FillReport,
    gas: GasCost | None,
    quoted_price: Decimal,
) -> SwapOutcome:
    token_in, token_out = _direction(pair, side)
    amount_in = fill.amount_in(token_in)
    amount_out = fill.amount_out(token_out)
    price = realised_price(fill, pair, side)

    assert_transition(intent.status, IntentStatus.FILLED)
    intent.status = IntentStatus.FILLED
    intent.filled_amount_in = amount_in
    intent.filled_amount_out = amount_out
    intent.fill_price = price
    intent.gas_native = fill.gas_native
    intent.gas_quote = gas.quote if gas is not None else None
    intent.gas_quote_coin = gas.quote_coin if gas is not None else None
    intent.native_quote_rate = gas.rate if gas is not None else None
    intent.confirmed_block = fill.block_number
    intent.block_hash = fill.block_hash
    # The payload only exists to make a re-broadcast possible; it is confirmed.
    intent.raw_tx = None
    # Queued before the commit that makes the fill real, so the message and the
    # fill are the same decision: no commit, no message. The realised figures
    # are read back from this row at send time, so only the status has to be
    # carried here.
    enqueue(session, dex_kind(IntentStatus.FILLED), {
        "intent_id": intent.id,
        "profile_id": intent.profile_id,
        "symbol": intent.symbol,
        "side": intent.side,
        "status": IntentStatus.FILLED,
    })
    await session.commit()

    logger.info(
        "%s %s filled: %s %s -> %s %s at %s (gas %s ETH%s, tx %s)",
        intent.symbol, intent.side, amount_in, token_in.symbol,
        amount_out, token_out.symbol, price, fill.gas_native,
        f" = {gas.quote} {gas.quote_coin}" if gas is not None else " unconverted",
        fill.tx_hash,
    )
    return SwapOutcome(
        status=IntentStatus.FILLED,
        symbol=intent.symbol,
        side=intent.side,
        quoted_price=quoted_price,
        fill_price=price,
        amount_in=amount_in,
        amount_out=amount_out,
        gas_native=fill.gas_native,
        gas_quote=gas.quote if gas is not None else None,
        tx_hash=fill.tx_hash,
        approval_tx_hash=intent.approval_tx_hash,
        intent_id=intent.id,
        execution_values=execution_values(
            pair=pair, side=side, fill=fill, gas=gas,
            exec_time_ms=int(intent.submitted_at.timestamp() * 1000)
            if intent.submitted_at else None,
        ),
    )
