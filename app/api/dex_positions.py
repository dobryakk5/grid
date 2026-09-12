"""Open positions on Robinhood Chain, and part-sales of them.

A sale here does not touch the chain. It writes the same ``dex_intents`` row a
hand-placed level writes, and ``app.workers.dex`` executes it under the same
risk gates, the same slippage cap and the same ``DEX_DRY_RUN`` switch. Having
one execution path is the point: a second one would be a second place for the
rule "never fill worse than the limit" to be got wrong.
"""

from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from sqlalchemy import func, select

from app.core.auth import require_trading
from app.core.config import settings
from app.db.models import ChainSwap, ChainToken, DexIntent
from app.db.session import SessionLocal
import asyncio

from app.dex.chain import ChainClient
from app.dex.costbasis import Fill, cost_basis
from app.dex.dynamic_tokens import load_dynamic_tokens
from app.dex.intents import TERMINAL_STATUSES, IntentStatus
from app.dex.positions import fraction_amount, limit_from_quote, open_positions
from app.dex.repository import DexIntentRepository
from app.dex.tokens import DexConfigError, dynamic_key, resolve_pair, resolve_token
from app.dex.uniswap import UniswapClient, UniswapError

router = APIRouter(prefix="/api/dex")

QUOTE_SYMBOL = "USDG"
ADDRESS = Path(pattern=r"^0x[0-9a-fA-F]{40}$")


class SellRequest(BaseModel):
    """Sell ``percent`` of the holding, at market or at a price of your own.

    Quarters only for the size. A free-text amount is a different feature with
    a different failure mode; these four are what the page offers.

    ``limit_price`` absent means "at market": the router is quoted and the
    limit is set a slippage cap below that quote, so the order fills now but
    still cannot fill at any price. Present means a real limit order, which
    waits for the market to come up to it and may never fill at all.
    """

    percent: int = Field(json_schema_extra={"enum": [25, 50, 75, 100]})
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=38, decimal_places=18)


class BuyRequest(BaseModel):
    """Spend ``quote_amount`` USDG on this token.

    ``limit_price`` absent means "at market": the router is quoted and the
    limit is set a slippage cap above that quote, so the order fills now but
    still cannot fill at any price. Present means a real limit order, which
    waits for the market to come to it and may never fill at all.
    """

    quote_amount: Decimal = Field(gt=0, max_digits=38, decimal_places=18)
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=38, decimal_places=18)


async def _known_token(address: str) -> ChainToken:
    """The tape-verified token at this address, buyable whether held or not.

    Buying deliberately does not require an existing position -- that is how a
    new one is opened -- so this looks in ``chain_tokens`` rather than in the
    wallet.
    """
    async with SessionLocal() as session:
        token = (await session.execute(select(ChainToken).where(
            ChainToken.chain_id == settings.rh_chain_id,
            ChainToken.address == address.lower(),
        ))).scalar_one_or_none()
    if token is None:
        raise HTTPException(404, "Токен не найден среди проверенных на цепочке")
    return token


def _pair_for(symbol: str | None, address: str):
    """The USDG pair for *this* contract, whatever the registry calls it.

    Two naming schemes have to be tried, because the registry has two. Tokens
    pinned by hand keep their bare ticker (``PONSUSDG``), while discovered ones
    are keyed by ticker *and* address fragment (``CHATGPT-7EC1FFE0USDG``) --
    deliberately, since this chain carries several contracts per ticker. Asking
    for the bare name only would leave every discovered coin unpriceable and
    unsellable, which is exactly what it did before this looked at both.

    Whichever name resolves, the address decides: a pair whose base is not this
    contract is refused rather than traded. The ticker is a lookup key; the
    address is the identity.
    """
    if not symbol:
        raise HTTPException(422, "У токена нет тикера; продажа по адресу пока не поддерживается")
    plain = symbol.strip().upper()
    names = [f"{plain}{QUOTE_SYMBOL}", f"{dynamic_key(plain, address)}{QUOTE_SYMBOL}"]
    mismatched = None
    for name in names:
        try:
            pair = resolve_pair(name)
        except DexConfigError:
            continue
        if (pair.base.address or "").lower() == address.lower():
            return pair
        mismatched = pair
    if mismatched is not None:
        raise HTTPException(
            409,
            f"Тикер {symbol} в реестре указывает на другой контракт "
            f"({mismatched.base.address}), а не на {address}; операция остановлена",
        )
    raise HTTPException(422, f"Пара для {symbol} ({address}) не найдена среди торгуемых")


async def _fills_by_address(wallet: str) -> dict[str, list[Fill]]:
    """Confirmed swaps per token contract, oldest first.

    Two records answer this and neither answers it alone. ``dex_intents`` is
    what this system executed -- the only source that knows what the gas cost.
    ``chain_swaps`` is what the chain says the wallet did, which includes every
    coin bought by hand in the Robinhood app that no intent ever described; for
    those holdings the intents table is simply empty, which is why the page
    reported "средняя цена неизвестна" for a coin that plainly cost 100 USDG.

    Keyed by contract address rather than by ticker on purpose: this chain
    carries several contracts per ticker, and a cost basis attached to the
    wrong one is worse than none.

    A swap the bot made appears in *both* tables once the wallet has been
    imported, so an intent's ``tx_hash`` suppresses the matching chain row.
    Counting it twice would double the recorded cost of every coin the bot
    itself bought.
    """
    async with SessionLocal() as session:
        intents = list((await session.execute(
            select(DexIntent).where(DexIntent.status == IntentStatus.FILLED)
            .order_by(DexIntent.id)
        )).scalars())
        swaps = list((await session.execute(
            select(ChainSwap)
            .where(func.lower(ChainSwap.wallet_address) == wallet.lower())
            .order_by(ChainSwap.block_time_ms)
        )).scalars())

    # (sort key, address, Fill) -- gathered from both sources, then merged into
    # one chronological series per token, because FIFO is only meaningful in
    # the order the trades actually happened.
    gathered: list[tuple[tuple, str, Fill]] = []
    ours: set[str] = set()

    for row in intents:
        if row.filled_amount_in is None or row.filled_amount_out is None:
            continue
        try:
            pair = resolve_pair(row.symbol)
        except DexConfigError:
            # Nothing to attach the fill to: a pair we no longer recognise
            # cannot be matched to a holding without guessing at the contract.
            continue
        address = (pair.base.address or "").lower()
        if not address:
            continue
        if row.tx_hash:
            ours.add(row.tx_hash.lower())
        base, quote = (
            (row.filled_amount_out, row.filled_amount_in) if row.side == "Buy"
            else (row.filled_amount_in, row.filled_amount_out)
        )
        when = row.submitted_at or row.created_at
        gathered.append((
            (when.timestamp() if when else 0.0, row.id),
            address,
            Fill(side=row.side, base_qty=base, quote_qty=quote,
                 gas_quote=row.gas_quote or Decimal(0)),
        ))

    for swap in swaps:
        if swap.tx_hash and swap.tx_hash.lower() in ours:
            continue
        side = "Buy" if swap.side.upper() == "BUY" else "Sell"
        # An unpriced buy has no cost to carry, and inventing a zero-cost lot
        # would report the whole holding as profit -- it is left out, so the
        # quantity surfaces as uncovered instead. An unpriced *sell* is still
        # recorded: FIFO only needs the quantity to retire the lots it ate.
        if side == "Buy" and swap.quote_amount is None:
            continue
        gathered.append((
            (swap.block_time_ms / 1000, 0),
            swap.token_address.lower(),
            Fill(side=side, base_qty=swap.token_amount,
                 quote_qty=swap.quote_amount or Decimal(0)),
        ))

    fills: dict[str, list[Fill]] = {}
    for _key, address, fill in sorted(gathered, key=lambda item: item[0]):
        fills.setdefault(address, []).append(fill)
    return fills


# The router refuses a burst far more readily than a trickle: asking it for
# every holding at once is what turns a page of quotes into a page of blanks.
# Three at a time is slower to paint and far likelier to paint completely.
QUOTE_CONCURRENCY = 3
QUOTE_ATTEMPTS = 3

# Failures the router itself describes as worth retrying, plus the transport
# errors that never mean "this token has no market".
_TRANSIENT_QUOTE_ERRORS = (
    "UpstreamTimeoutError", "request failed", "non-JSON response",
    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
)


def _transient(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _TRANSIENT_QUOTE_ERRORS)


async def _exit_value(
    pair, position, wallet, uniswap: UniswapClient, gate: asyncio.Semaphore,
) -> tuple[Decimal | None, str | None]:
    """What selling the whole holding would fetch, and why not if it cannot.

    A quote, not a mid price: it already carries the price impact of this size,
    which is the number a position is actually worth to us.

    Returns the reason alongside the value because the two failures are not the
    same fact. "No pool" is a property of the token and will still be true on
    the next load; "the router timed out" is a property of this second, and
    reporting it as an absent price -- which is what this did before -- tells
    the operator a liquid coin is unsellable.
    """
    last: Exception | None = None
    for attempt in range(QUOTE_ATTEMPTS):
        try:
            async with gate:
                quote = await uniswap.quote_exact_in(
                    pair=pair, side="Sell",
                    amount_in_wei=int(position.amount.scaleb(position.decimals)),
                    swapper=wallet,
                )
            value = Decimal(quote.amount_out).scaleb(-resolve_token(QUOTE_SYMBOL).decimals)
            return value, None
        except (UniswapError, HTTPException, ValueError) as exc:
            last = exc
            if not _transient(exc):
                break
            if attempt + 1 < QUOTE_ATTEMPTS:
                await asyncio.sleep(0.4 * 2 ** attempt)
    return None, ("timeout" if last is not None and _transient(last) else "unavailable")


@router.get("/positions")
async def positions() -> dict:
    """Everything the wallet holds, with USDG cash reported apart from it."""
    await load_dynamic_tokens(SessionLocal)
    chain = ChainClient()
    try:
        held = await open_positions(SessionLocal, chain)
        native = await chain.native_balance()
        wallet = chain.wallet_address
    finally:
        await chain.close()

    fills = await _fills_by_address(wallet)
    quote_address = (resolve_token(QUOTE_SYMBOL).address or "").lower()
    cash, rows, priced = None, [], []
    for position in held:
        entry = {
            "token_address": position.address, "symbol": position.symbol,
            "decimals": position.decimals, "amount": str(position.amount),
        }
        if position.address.lower() == quote_address:
            cash = entry
            continue
        pair = None
        if position.symbol:
            try:
                pair = _pair_for(position.symbol, position.address)
            except HTTPException:
                # Unknown or mismatched ticker: still list the holding, just
                # without a price or a cost we would have to guess at.
                pair = None
        # Keyed by contract, so a holding whose ticker no longer resolves to a
        # tradable pair still gets the cost of what we paid for it.
        basis = cost_basis(fills.get(position.address.lower(), []), position.amount)
        entry.update({
            "pair": pair.symbol if pair else None,
            "covered_qty": str(basis.covered_qty),
            "uncovered_qty": str(basis.uncovered_qty),
            "unexplained_outflow": str(basis.unexplained_outflow),
            "cost_quote": str(basis.cost_quote) if basis.covered_qty > 0 else None,
            "average_price": str(basis.average_price) if basis.average_price is not None else None,
            "basis_complete": basis.complete,
        })
        rows.append(entry)
        if pair is not None:
            priced.append((entry, pair, position, basis))

    # One client and one gate for the whole page: a client per position was
    # nine connections opening at once, which is the burst the router minds.
    uniswap = UniswapClient()
    gate = asyncio.Semaphore(QUOTE_CONCURRENCY)
    try:
        quotes = await asyncio.gather(*(
            _exit_value(p, pos, wallet, uniswap, gate) for _e, p, pos, _b in priced
        ))
    finally:
        await uniswap.close()

    for (entry, _pair, position, basis), (value, failure) in zip(priced, quotes):
        entry["value_quote"] = str(value) if value is not None else None
        entry["quote_error"] = failure
        if value is None or basis.covered_qty <= 0:
            entry["pnl_quote"] = entry["pnl_pct"] = None
            continue
        # Compare like with like: only the part of the holding we know the
        # cost of, valued at the same price the whole holding was quoted at.
        covered_value = value * basis.covered_qty / position.amount
        pnl = covered_value - basis.cost_quote
        entry["pnl_quote"] = str(pnl)
        entry["pnl_pct"] = str(pnl / basis.cost_quote * 100) if basis.cost_quote > 0 else None
    return {
        "wallet": wallet,
        # Gas is paid in native ETH. Whether this is *enough* is not knowable
        # without a quote, so the number is reported and the judgement is made
        # at sell time against the router's own gas estimate -- rather than
        # against a threshold invented here.
        "native_balance": str(native),
        "cash": cash,
        "positions": rows,
        "dry_run": settings.dex_dry_run,
        "quote_symbol": QUOTE_SYMBOL,
    }


@router.post("/positions/{address}/sell", dependencies=[Depends(require_trading)])
async def sell(payload: SellRequest, address: str = ADDRESS) -> dict:
    if payload.percent not in (25, 50, 75, 100):
        raise HTTPException(422, "Доля продажи: 25, 50, 75 или 100 процентов")
    await load_dynamic_tokens(SessionLocal)
    chain = ChainClient()
    try:
        held = {p.address.lower(): p for p in await open_positions(SessionLocal, chain)}
        native_balance = await chain.native_balance()
        wallet = chain.wallet_address
    finally:
        await chain.close()

    position = held.get(address.lower())
    if position is None:
        raise HTTPException(404, "Такой позиции в кошельке нет")
    pair = _pair_for(position.symbol, position.address)

    amount = fraction_amount(position.amount, payload.percent, position.decimals)
    if amount <= 0:
        raise HTTPException(422, "Доля меньше одной единицы токена")

    limit, proceeds = payload.limit_price, None
    if limit is None:
        uniswap = UniswapClient()
        try:
            quote = await uniswap.quote_exact_in(
                pair=pair, side="Sell",
                amount_in_wei=int(amount.scaleb(position.decimals)),
                swapper=wallet,
            )
        except UniswapError as exc:
            raise HTTPException(502, f"Не удалось получить котировку: {exc}") from None
        finally:
            await uniswap.close()

        proceeds = Decimal(quote.amount_out).scaleb(-resolve_token(QUOTE_SYMBOL).decimals)
        if proceeds < settings.dex_min_order_quote:
            raise HTTPException(
                422,
                f"Минимальный ордер — {settings.dex_min_order_quote} {QUOTE_SYMBOL}; "
                f"эта доля стоит около {proceeds} {QUOTE_SYMBOL}",
            )
        limit = limit_from_quote(amount, proceeds, settings.dex_max_slippage_pct, side="Sell")

        # The router priced the gas for this exact swap; compare the wallet
        # against that, not against a guess. Refusing here costs a click, while
        # arming a level the wallet can never broadcast wastes the price it was
        # armed at.
        gas_wei = quote.gas_fee_native_wei
        native_wei = int(native_balance.scaleb(18))
        if gas_wei and native_wei < gas_wei:
            raise HTTPException(
                422,
                f"Не хватает нативного ETH на газ: нужно примерно "
                f"{Decimal(gas_wei).scaleb(-18)}, на кошельке {native_balance}",
            )
    else:
        # A limit order is deliberately not quoted or gas-checked here: it may
        # wait days, and both facts will have changed by the time it triggers.
        # The worker re-checks them at execution. What the order is worth *if*
        # it fills is known without the router, though, so the minimum still
        # applies -- a level too small to be executable is not worth arming.
        asked = amount * limit
        if asked < settings.dex_min_order_quote:
            raise HTTPException(
                422,
                f"Минимальный ордер — {settings.dex_min_order_quote} {QUOTE_SYMBOL}; "
                f"эта доля по такой цене даёт около {asked} {QUOTE_SYMBOL}",
            )

    async with SessionLocal() as session:
        intent = await DexIntentRepository(session).create_level(
            symbol=pair.symbol, side="Sell", limit_price=limit,
            amount_in=amount, amount_in_coin=pair.base_coin,
            order_link_id=str(uuid4()),
        )
        await session.commit()
        intent_id = intent.id

    return {
        "intent_id": intent_id, "symbol": pair.symbol, "side": "Sell",
        "order_type": "market" if payload.limit_price is None else "limit",
        "percent": payload.percent, "amount": str(amount),
        "amount_coin": pair.base_coin,
        "quoted_proceeds": str(proceeds) if proceeds is not None else None,
        "limit_price": str(limit),
        "slippage_pct": str(settings.dex_max_slippage_pct),
        "status": "WAITING", "dry_run": settings.dex_dry_run,
        "note": (
            "DEX_DRY_RUN включён: уровень наблюдается и котируется, но ничего не подписывается"
            if settings.dex_dry_run else
            "DEX_DRY_RUN выключен: воркер подпишет и отправит сделку, когда цена сойдётся"
        ),
    }


@router.post("/positions/{address}/buy", dependencies=[Depends(require_trading)])
async def buy(payload: BuyRequest, address: str = ADDRESS) -> dict:
    """Spend USDG on this token, at market or at a price of your choosing."""
    if payload.quote_amount < settings.dex_min_order_quote:
        raise HTTPException(
            422,
            f"Минимальный ордер — {settings.dex_min_order_quote} {QUOTE_SYMBOL}",
        )
    await load_dynamic_tokens(SessionLocal)
    token = await _known_token(address)
    pair = _pair_for(token.symbol, address)
    quote_token = resolve_token(QUOTE_SYMBOL)

    chain = ChainClient()
    try:
        cash = await chain.token_balance(quote_token)
        native_balance = await chain.native_balance()
        wallet = chain.wallet_address
    finally:
        await chain.close()
    if cash < payload.quote_amount:
        raise HTTPException(
            422,
            f"На кошельке {cash} {QUOTE_SYMBOL}, а ордер на {payload.quote_amount}",
        )

    limit, quoted_receive = payload.limit_price, None
    if limit is None:
        # Market-style: ask what this buys right now, then cap the damage.
        uniswap = UniswapClient()
        try:
            quote = await uniswap.quote_exact_in(
                pair=pair, side="Buy",
                amount_in_wei=int(payload.quote_amount.scaleb(quote_token.decimals)),
                swapper=wallet,
            )
        except UniswapError as exc:
            raise HTTPException(502, f"Не удалось получить котировку: {exc}") from None
        finally:
            await uniswap.close()
        quoted_receive = Decimal(quote.amount_out).scaleb(-token.decimals)
        limit = limit_from_quote(
            payload.quote_amount, quoted_receive, settings.dex_max_slippage_pct, side="Buy",
        )
        gas_wei = quote.gas_fee_native_wei
        if gas_wei and int(native_balance.scaleb(18)) < gas_wei:
            raise HTTPException(
                422,
                f"Не хватает нативного ETH на газ: нужно примерно "
                f"{Decimal(gas_wei).scaleb(-18)}, на кошельке {native_balance}",
            )
    # A limit order is deliberately not quoted or gas-checked here: it may wait
    # days, and both facts will have changed by the time it triggers. The
    # worker re-checks them at execution, which is the only moment they mean
    # anything.

    async with SessionLocal() as session:
        intent = await DexIntentRepository(session).create_level(
            symbol=pair.symbol, side="Buy", limit_price=limit,
            amount_in=payload.quote_amount, amount_in_coin=pair.quote_coin,
            order_link_id=str(uuid4()),
        )
        await session.commit()
        intent_id = intent.id

    return {
        "intent_id": intent_id, "symbol": pair.symbol, "side": "Buy",
        "order_type": "market" if payload.limit_price is None else "limit",
        "amount": str(payload.quote_amount), "amount_coin": pair.quote_coin,
        "limit_price": str(limit),
        "quoted_receive": str(quoted_receive) if quoted_receive is not None else None,
        "status": "WAITING", "dry_run": settings.dex_dry_run,
        "note": (
            "DEX_DRY_RUN включён: уровень наблюдается и котируется, но ничего не подписывается"
            if settings.dex_dry_run else
            "DEX_DRY_RUN выключен: воркер подпишет и отправит сделку, когда цена сойдётся"
        ),
    }


@router.get("/orders")
async def orders(status: str = "all", limit: int = 200) -> dict:
    """Buys and sells, newest first -- history and open levels in one list.

    There is no separate trade log to read: ``dex_intents`` already records
    what was asked for and what the chain did about it, so a filled order and
    a level still waiting are the same row at different points in its life.
    That is also why the grid's own orders appear here next to hand-placed
    ones; ``profile_id`` is what tells them apart.
    """
    if status not in {"all", "open", "closed", "filled"}:
        raise HTTPException(422, "status: all, open, closed или filled")
    limit = max(1, min(limit, 1000))
    query = select(DexIntent).order_by(DexIntent.id.desc()).limit(limit)
    if status == "open":
        query = query.where(DexIntent.status.not_in(TERMINAL_STATUSES))
    elif status == "closed":
        query = query.where(DexIntent.status.in_(TERMINAL_STATUSES))
    elif status == "filled":
        query = query.where(DexIntent.status == IntentStatus.FILLED)

    async with SessionLocal() as session:
        rows = list((await session.execute(query)).scalars())

    def money(value):
        return None if value is None else str(value)

    return {"orders": [{
        "id": row.id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "symbol": row.symbol,
        "side": row.side,
        "status": row.status,
        "open": row.status not in TERMINAL_STATUSES,
        # A level with no profile was placed by hand, from a page like this one.
        "source": "сетка" if row.profile_id else "вручную",
        "limit_price": money(row.limit_price),
        "amount_in": money(row.amount_in),
        "amount_in_coin": row.amount_in_coin,
        "filled_amount_in": money(row.filled_amount_in),
        "filled_amount_out": money(row.filled_amount_out),
        "fill_price": money(row.fill_price),
        "gas_quote": money(row.gas_quote),
        "gas_quote_coin": row.gas_quote_coin,
        "tx_hash": row.tx_hash,
        "reason": row.last_error or row.blocked_reason,
    } for row in rows]}
