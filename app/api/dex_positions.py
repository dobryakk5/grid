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

from sqlalchemy import select

from app.core.auth import require_trading
from app.core.config import settings
from app.db.models import ChainToken, DexIntent
from app.db.session import SessionLocal
from app.dex.chain import ChainClient
from app.dex.dynamic_tokens import load_dynamic_tokens
from app.dex.intents import TERMINAL_STATUSES, IntentStatus
from app.dex.positions import fraction_amount, limit_from_quote, open_positions
from app.dex.repository import DexIntentRepository
from app.dex.tokens import DexConfigError, resolve_pair, resolve_token
from app.dex.uniswap import UniswapClient, UniswapError

router = APIRouter(prefix="/api/dex")

QUOTE_SYMBOL = "USDG"
ADDRESS = Path(pattern=r"^0x[0-9a-fA-F]{40}$")


class SellRequest(BaseModel):
    # Quarters only. A free-text amount is a different feature with a
    # different failure mode; these four are what the page offers.
    percent: int = Field(json_schema_extra={"enum": [25, 50, 75, 100]})


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
    """The ``<SYMBOL>USDG`` pair, checked to be about *this* contract.

    Symbols are not unique -- the chain is full of tokens that call themselves
    USDC -- so resolving by ticker and trusting the result is how a sale ends
    up spending a different coin than the row the operator clicked. The
    address is the identity; the ticker is only a lookup key.
    """
    if not symbol:
        raise HTTPException(422, "У токена нет тикера; продажа по адресу пока не поддерживается")
    try:
        pair = resolve_pair(f"{symbol.strip().upper()}{QUOTE_SYMBOL}")
    except DexConfigError as exc:
        raise HTTPException(422, str(exc)) from None
    if (pair.base.address or "").lower() != address.lower():
        raise HTTPException(
            409,
            f"Тикер {symbol} в реестре указывает на другой контракт "
            f"({pair.base.address}), а не на {address}; продажа остановлена",
        )
    return pair


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

    quote_address = (resolve_token(QUOTE_SYMBOL).address or "").lower()
    cash, rows = None, []
    for position in held:
        entry = {
            "token_address": position.address, "symbol": position.symbol,
            "decimals": position.decimals, "amount": str(position.amount),
        }
        if position.address.lower() == quote_address:
            cash = entry
        else:
            rows.append(entry)
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

    # The router priced the gas for this exact swap; compare the wallet against
    # that, not against a guess. Refusing here costs a click, while arming a
    # level the wallet can never broadcast wastes the price it was armed at.
    gas_wei = quote.gas_fee_native_wei
    native_wei = int(native_balance.scaleb(18))
    if gas_wei and native_wei < gas_wei:
        raise HTTPException(
            422,
            f"Не хватает нативного ETH на газ: нужно примерно "
            f"{Decimal(gas_wei).scaleb(-18)}, на кошельке {native_balance}",
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
        "percent": payload.percent, "amount": str(amount),
        "amount_coin": pair.base_coin,
        "quoted_proceeds": str(proceeds), "limit_price": str(limit),
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
