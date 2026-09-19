"""Reconstruct BUY/SELL fills for tracked wallets from raw chain data.

The main PONS/USDG market is a Uniswap v4 pool (see
``app/db/models.py::DexPriceObservation``), and v4 has no per-pool contract --
every swap settles through a singleton PoolManager with flash accounting. So
"counterparty == the pool address" cannot classify a swap here at all.

Instead, :func:`classify` looks only at what a *wallet* gained or lost in one
transaction, from its ERC-20 ``Transfer`` legs:

* base token in, quote token out -> BUY
* base token out, quote token in -> SELL
* anything else (a plain transfer, an LP add/remove, a zero-net wash) -> not a
  swap, and is not reported as one

This works identically for v2, v3, v4, and Universal Router 2.1.1, without
knowing a single pool address -- it reads the wallet's economic outcome, not
which contract it talked to.

Two scan paths share this module:

* **realtime** -- :func:`fetch_transfers`, token-address-filtered over a
  block range. Cheap regardless of how many wallets are tracked, at the cost
  of reading every Transfer of the watched tokens on the whole chain.
* **backfill** -- :func:`discover_wallet_tx_hashes` (wallet-topic-filtered,
  cheap on a narrow range) then :func:`fetch_transaction_transfers` (one
  receipt per hit) for a single newly-discovered wallet. Used once per
  wallet, over ``settings.fomo_new_wallet_backfill_blocks``, so the trade
  that got a wallet noticed on FOMO is not the trade that goes missing.

Both paths decode into the same plain-dict transfer shape, so
:func:`classify` and :func:`price_swap` don't care which path produced them.
"""

from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from web3 import AsyncWeb3

from app.core.config import settings
from app.db.models import DexPriceObservation, MarketCandle
from app.dex.chain import ChainClient
from app.dex.tokens import DexPair

__all__ = [
    "TRANSFER_TOPIC",
    "AdaptiveBatchSize",
    "RateLimitBackoff",
    "ChainSwapRow",
    "classify",
    "classify_any",
    "fetch_wallet_transfers",
    "discover_wallet_tx_hashes",
    "fetch_transaction_transfers",
    "fetch_transfers",
    "group_by_tx",
    "is_batch_too_large_error",
    "is_rate_limited_error",
    "price_swap",
]

TRANSFER_TOPIC = "0x" + AsyncWeb3.keccak(text="Transfer(address,address,uint256)").hex().removeprefix("0x")

# RPC providers phrase a too-wide-a-range error differently; match on the
# wording seen in practice rather than on one provider's exact code.
_BATCH_TOO_LARGE_NEEDLES = (
    "too many results",
    "query returned more than",
    "limit exceeded",
    "block range",
    "response size exceeded",
    "-32005",
)


@dataclass(frozen=True)
class ChainSwapRow:
    """What :func:`classify` produces -- matches ``ChainSwap`` column for
    column, plus the pricing fields :func:`price_swap` may fill in later."""

    tx_hash: str
    wallet_address: str
    chain_id: int
    block_number: int
    block_time_ms: int
    token_address: str
    symbol: str | None
    side: str  # "BUY" | "SELL"
    token_amount: Decimal
    quote_address: str
    quote_symbol: str | None
    quote_amount: Decimal
    price: Decimal | None
    value_usd: Decimal | None = None
    pricing_source: str = "UNPRICED"


_RATE_LIMITED_NEEDLES = ("429", "too many requests", "rate limit", "-32029")


def is_batch_too_large_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(needle in message for needle in _BATCH_TOO_LARGE_NEEDLES)


def is_rate_limited_error(exc: Exception) -> bool:
    """Did the node refuse this because we asked too often?

    Distinct from a batch being too large, which is about the question; this
    is about the rate of asking, and the same question will be answered a
    moment later.
    """
    message = str(exc).lower()
    if any(needle in message for needle in _RATE_LIMITED_NEEDLES):
        return True
    return getattr(exc, "status", None) == 429


@dataclass
class RateLimitBackoff:
    """How long to wait after a pass the node refused for asking too often.

    Retrying a 429 at the normal cadence is not persistence, it is the thing
    keeping the limit tripped: every refused request still counts against the
    budget that would otherwise have served the next real one. So the wait
    doubles while the node keeps saying no, and is dropped the moment a pass
    gets through -- the tape wants the next block range as soon as it is
    allowed to have it, not a cautious ramp back.
    """

    base: float
    maximum: float
    current: float = 0.0

    def record_success(self) -> None:
        self.current = 0.0

    def record_refusal(self) -> float:
        self.current = min(self.maximum, self.current * 2 if self.current else self.base)
        return self.current


@dataclass
class AdaptiveBatchSize:
    """Block-range size that shrinks hard on failure and recovers slowly.

    A fixed batch size is a bet on an RPC provider we don't control; this
    halves on ``"too many results"`` down to ``minimum``, and only grows back
    after a few consecutive clean passes -- recovering fast is not worth
    flapping back into the same error.
    """

    current: int
    minimum: int
    maximum: int
    _clean_passes: int = 0

    def shrink(self) -> int:
        self.current = max(self.minimum, self.current // 2)
        self._clean_passes = 0
        return self.current

    def record_success(self) -> int:
        self._clean_passes += 1
        if self._clean_passes >= 3 and self.current < self.maximum:
            self.current = min(self.maximum, self.current * 2)
            self._clean_passes = 0
        return self.current


# ---- log decoding ---------------------------------------------------------


def _hex(value) -> str:
    if isinstance(value, str):
        return value
    return "0x" + bytes(value).hex()


def _to_int(value) -> int:
    if isinstance(value, bool):
        raise TypeError(f"unsupported numeric value: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16)
    return int.from_bytes(bytes(value), byteorder="big")


def _topic_to_address(topic) -> str:
    raw = bytes(topic) if not isinstance(topic, str) else bytes.fromhex(topic.removeprefix("0x"))
    return "0x" + raw[-20:].hex()


def _decode_log(log) -> dict:
    topics = log["topics"]
    return {
        "tx_hash": _hex(log["transactionHash"]),
        "log_index": int(log["logIndex"]),
        "token_address": AsyncWeb3.to_checksum_address(_hex(log["address"])),
        "from_address": AsyncWeb3.to_checksum_address(_topic_to_address(topics[1])),
        "to_address": AsyncWeb3.to_checksum_address(_topic_to_address(topics[2])),
        "amount": _to_int(log["data"]),
        "block_number": int(log["blockNumber"]),
    }


def _pad_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


# ---- RPC reads --------------------------------------------------------


async def _block_times(client: ChainClient, block_numbers: list[int], *, concurrency: int = 8) -> dict[int, int]:
    if not block_numbers:
        return {}
    semaphore = asyncio.Semaphore(concurrency)

    async def one(number: int) -> tuple[int, int]:
        async with semaphore:
            block = await client.w3.eth.get_block(number)
            return number, int(block["timestamp"]) * 1000

    results = await asyncio.gather(*(one(n) for n in block_numbers))
    return dict(results)


async def fetch_transfers(
    client: ChainClient, token_addresses: list[str], from_block: int, to_block: int
) -> list[dict]:
    """Every ``Transfer`` of ``token_addresses`` in ``[from_block, to_block]``.

    One ``eth_getLogs`` call, filtered by token address rather than by
    wallet: there are a handful of watched tokens and potentially hundreds of
    tracked wallets, so this is the cheaper axis to filter on. As a side
    effect every leg of any transaction touching our tokens comes back in one
    shot, which is what lets ``chain_transactions`` store complete raw
    receipts without extra RPC calls.
    """
    checksum = [AsyncWeb3.to_checksum_address(a) for a in token_addresses]
    logs = await client.w3.eth.get_logs({
        "address": checksum,
        "topics": [TRANSFER_TOPIC],
        "fromBlock": from_block,
        "toBlock": to_block,
    })
    decoded = [_decode_log(log) for log in logs]
    times = await _block_times(client, sorted({item["block_number"] for item in decoded}))
    for item in decoded:
        item["block_time_ms"] = times.get(item["block_number"])
    return decoded


async def discover_wallet_tx_hashes(
    client: ChainClient, wallet_address: str, token_addresses: list[str], from_block: int, to_block: int
) -> set[str]:
    """Transaction hashes where ``wallet_address`` sent or received one of
    ``token_addresses``, over a (narrow, backfill-sized) block range.

    Wallet-topic-filtered rather than token-address-filtered: for one wallet
    over one day of blocks this is cheap, unlike scanning every holder of the
    token for that same window.
    """
    checksum_tokens = [AsyncWeb3.to_checksum_address(a) for a in token_addresses]
    padded = _pad_topic(wallet_address)
    hashes: set[str] = set()
    for topics in ([TRANSFER_TOPIC, padded, None], [TRANSFER_TOPIC, None, padded]):
        logs = await client.w3.eth.get_logs({
            "address": checksum_tokens,
            "topics": topics,
            "fromBlock": from_block,
            "toBlock": to_block,
        })
        hashes.update(_hex(log["transactionHash"]) for log in logs)
    return hashes


async def fetch_transaction_transfers(
    client: ChainClient,
    tx_hash: str,
    token_addresses: list[str],
    *,
    block_time_cache: dict[int, int] | None = None,
) -> list[dict]:
    """All ``Transfer`` legs of an already-known transaction, via its receipt.

    Used by backfill so a discovered wallet's history carries the same
    complete-transaction raw data as the realtime path -- not just the legs
    that happen to touch that one wallet.

    ``block_time_cache`` is shared across a backfill run on purpose: a busy
    market puts many transactions in the same block, and on a rate-limited
    public RPC an extra ``eth_getBlockByNumber`` per transaction is the
    difference between a backfill that finishes and one that crawls.
    """
    receipt = await client.w3.eth.get_transaction_receipt(tx_hash)
    token_set = {a.lower() for a in token_addresses}
    decoded = [
        _decode_log(log)
        for log in receipt["logs"]
        if len(log["topics"]) == 3
        and _hex(log["topics"][0]).lower() == TRANSFER_TOPIC.lower()
        and _hex(log["address"]).lower() in token_set
    ]
    if not decoded:
        return []

    cache = block_time_cache if block_time_cache is not None else {}
    block_number = decoded[0]["block_number"]
    if block_number not in cache:
        block = await client.w3.eth.get_block(block_number)
        cache[block_number] = int(block["timestamp"]) * 1000
    for item in decoded:
        item["block_time_ms"] = cache[block_number]
    return decoded


async def fetch_wallet_transfers(
    client: ChainClient, wallets: list[str], from_block: int, to_block: int
) -> list[dict]:
    """Every ERC-20 ``Transfer`` touching any of ``wallets`` -- any token.

    Deliberately *not* filtered by token address: the point is to see
    everything these wallets bought and sold, not only the pair we happen to
    trade ourselves.

    Cost does not grow with the number of wallets, because ``eth_getLogs``
    accepts a set of values per topic position: all wallets go into one
    indexed-``from`` query and one indexed-``to`` query, so this is two calls
    whether we track four wallets or four hundred.
    """
    if not wallets:
        return []
    padded = [_pad_topic(wallet) for wallet in wallets]

    seen: set[tuple[str, int]] = set()
    decoded: list[dict] = []
    for topics in ([TRANSFER_TOPIC, padded, None], [TRANSFER_TOPIC, None, padded]):
        logs = await client.w3.eth.get_logs({
            "topics": topics,
            "fromBlock": from_block,
            "toBlock": to_block,
        })
        for log in logs:
            item = _decode_log(log)
            # A wallet-to-wallet transfer between two tracked wallets comes
            # back from both queries; keep one copy.
            key = (item["tx_hash"], item["log_index"])
            if key in seen:
                continue
            seen.add(key)
            decoded.append(item)

    times = await _block_times(client, sorted({item["block_number"] for item in decoded}))
    for item in decoded:
        item["block_time_ms"] = times.get(item["block_number"])
    return decoded


def classify_any(
    tx_transfers: list[dict],
    tracked_wallets: set[str],
    *,
    token_meta: dict,
    quote_assets: dict[str, str],
    chain_id: int,
    usd_quote_symbols: frozenset[str] | None = None,
) -> list[ChainSwapRow]:
    """Net-delta classification for arbitrary tokens, not a registered pair.

    Same principle as :func:`classify` -- what did the wallet gain and lose
    in this transaction -- generalised so any token can be the base, with
    ``quote_assets`` (address -> symbol) naming the assets that count as the
    money side.

    Two shapes are reported: one non-quote token against one quote asset
    (the ordinary case, priced off the money leg), and one token straight for
    another with no quote asset involved, which yields two rows -- the sale
    and the purchase -- both left UNPRICED, since nothing in the transaction
    says what either was worth in dollars.

    Anything busier (a swap landing in three tokens, an LP action) is skipped
    rather than split by some invented rule: a missing row is honest, a
    fabricated one is not.
    """
    if not tx_transfers:
        return []

    if usd_quote_symbols is None:
        usd_quote_symbols = frozenset(
            item.strip().upper() for item in (settings.usd_quote_symbols or "").split(",") if item.strip()
        )

    tracked_lower = {wallet.lower() for wallet in tracked_wallets}
    quote_lower = {address.lower(): symbol for address, symbol in quote_assets.items()}

    wallets_seen: set[str] = set()
    for transfer in tx_transfers:
        for side in ("from_address", "to_address"):
            if transfer[side].lower() in tracked_lower:
                wallets_seen.add(transfer[side].lower())

    tx_hash = tx_transfers[0]["tx_hash"]
    block_number = tx_transfers[0]["block_number"]
    block_time_ms = tx_transfers[0]["block_time_ms"]

    rows: list[ChainSwapRow] = []
    for wallet in sorted(wallets_seen):
        deltas: dict[str, Decimal] = {}
        for transfer in tx_transfers:
            token = transfer["token_address"].lower()
            meta = token_meta.get(token)
            if meta is None:
                continue
            if transfer["to_address"].lower() == wallet:
                sign = Decimal(1)
            elif transfer["from_address"].lower() == wallet:
                sign = Decimal(-1)
            else:
                continue
            deltas[token] = deltas.get(token, Decimal(0)) + sign * meta.from_wei(transfer["amount"])

        moved = {token: delta for token, delta in deltas.items() if delta != 0}
        quote_side = [token for token in moved if token in quote_lower]
        base_side = [token for token in moved if token not in quote_lower]

        pairs: list[tuple[str, str]] = []
        if len(quote_side) == 1 and len(base_side) == 1:
            pairs.append((base_side[0], quote_side[0]))
        elif len(quote_side) == 0 and len(base_side) == 2:
            # Token for token, with no money leg. Still two real trades --
            # one position closed, another opened -- so both are recorded,
            # each priced in the other and left UNPRICED in dollars rather
            # than valued at some invented rate.
            first, second = base_side
            if (moved[first] > 0) == (moved[second] > 0):
                continue
            pairs.extend(((first, second), (second, first)))
        else:
            continue

        for base_token, quote_token in pairs:
            base_delta, quote_delta = moved[base_token], moved[quote_token]
            if base_delta > 0 and quote_delta < 0:
                side = "BUY"
            elif base_delta < 0 and quote_delta > 0:
                side = "SELL"
            else:
                continue

            token_amount, quote_amount = abs(base_delta), abs(quote_delta)
            if token_amount == 0 or quote_amount == 0:
                continue

            base_meta = token_meta[base_token]
            quote_symbol = quote_lower.get(quote_token) or (
                token_meta[quote_token].symbol if quote_token in token_meta else None
            )
            pricing_source, value_usd = "UNPRICED", None
            if quote_symbol and quote_symbol.upper() in usd_quote_symbols:
                pricing_source, value_usd = "QUOTE_LEG", quote_amount

            rows.append(ChainSwapRow(
                tx_hash=tx_hash,
                wallet_address=AsyncWeb3.to_checksum_address(wallet),
                chain_id=chain_id,
                block_number=block_number,
                block_time_ms=block_time_ms,
                token_address=base_meta.address,
                symbol=base_meta.symbol,
                side=side,
                token_amount=token_amount,
                quote_address=quote_token,
                quote_symbol=quote_symbol,
                quote_amount=quote_amount,
                price=quote_amount / token_amount,
                value_usd=value_usd,
                pricing_source=pricing_source,
            ))
    return rows


def group_by_tx(transfers: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for transfer in transfers:
        grouped.setdefault(transfer["tx_hash"], []).append(transfer)
    return grouped


# ---- classification ---------------------------------------------------


def classify(
    tx_transfers: list[dict],
    tracked_wallets: set[str],
    pair: DexPair,
    *,
    chain_id: int,
    usd_quote_symbols: frozenset[str] | None = None,
) -> list[ChainSwapRow]:
    """Net-delta classification for every tracked wallet in one transaction.

    Pure and synchronous by design: the whole point is that this can be
    re-run against ``chain_transactions`` (via
    ``scripts/rebuild-chain-swaps.py``) with no RPC access at all when the
    logic here changes.
    """
    if not tx_transfers:
        return []

    if usd_quote_symbols is None:
        usd_quote_symbols = frozenset(
            item.strip().upper() for item in (settings.usd_quote_symbols or "").split(",") if item.strip()
        )

    tracked_lower = {wallet.lower() for wallet in tracked_wallets}
    base_addr = pair.base.address.lower()
    quote_addr = pair.quote.address.lower()

    wallets_seen: set[str] = set()
    for transfer in tx_transfers:
        if transfer["from_address"].lower() in tracked_lower:
            wallets_seen.add(transfer["from_address"].lower())
        if transfer["to_address"].lower() in tracked_lower:
            wallets_seen.add(transfer["to_address"].lower())

    tx_hash = tx_transfers[0]["tx_hash"]
    block_number = tx_transfers[0]["block_number"]
    block_time_ms = tx_transfers[0]["block_time_ms"]

    rows: list[ChainSwapRow] = []
    for wallet in sorted(wallets_seen):
        delta_base = Decimal(0)
        delta_quote = Decimal(0)
        for transfer in tx_transfers:
            token = transfer["token_address"].lower()
            if token not in (base_addr, quote_addr):
                continue
            unit = pair.base if token == base_addr else pair.quote
            amount = unit.from_wei(transfer["amount"])
            if transfer["to_address"].lower() == wallet:
                delta = amount
            elif transfer["from_address"].lower() == wallet:
                delta = -amount
            else:
                continue
            if token == base_addr:
                delta_base += delta
            else:
                delta_quote += delta

        if delta_base > 0 and delta_quote < 0:
            side = "BUY"
        elif delta_base < 0 and delta_quote > 0:
            side = "SELL"
        else:
            continue

        token_amount = abs(delta_base)
        quote_amount = abs(delta_quote)
        if token_amount == 0 or quote_amount == 0:
            continue

        pricing_source = "UNPRICED"
        value_usd = None
        if pair.quote_coin.upper() in usd_quote_symbols:
            pricing_source = "QUOTE_LEG"
            value_usd = quote_amount

        rows.append(ChainSwapRow(
            tx_hash=tx_hash,
            wallet_address=AsyncWeb3.to_checksum_address(wallet),
            chain_id=chain_id,
            block_number=block_number,
            block_time_ms=block_time_ms,
            token_address=pair.base.address,
            symbol=pair.base_coin,
            side=side,
            token_amount=token_amount,
            quote_address=pair.quote.address,
            quote_symbol=pair.quote_coin,
            quote_amount=quote_amount,
            price=quote_amount / token_amount,
            value_usd=value_usd,
            pricing_source=pricing_source,
        ))
    return rows


# ---- pricing fallback ---------------------------------------------------


async def _nearest_market_price(session: AsyncSession, symbol: str, block_time_ms: int) -> Decimal | None:
    """Latest known-good price at or before ``block_time_ms``.

    Tried in the same order the DEX sampler produces data: the raw
    observation series first (finer-grained), then folded candles.
    """
    result = await session.execute(
        select(DexPriceObservation.price_usd)
        .where(
            DexPriceObservation.symbol == symbol,
            DexPriceObservation.timestamp_ms <= block_time_ms,
            DexPriceObservation.price_usd.is_not(None),
        )
        .order_by(DexPriceObservation.timestamp_ms.desc())
        .limit(1)
    )
    price = result.scalar_one_or_none()
    if price is not None:
        return Decimal(price)

    result = await session.execute(
        select(MarketCandle.close)
        .where(MarketCandle.symbol == symbol, MarketCandle.interval == "1", MarketCandle.timestamp_ms <= block_time_ms)
        .order_by(MarketCandle.timestamp_ms.desc())
        .limit(1)
    )
    close = result.scalar_one_or_none()
    return Decimal(close) if close is not None else None


async def price_swap(row: ChainSwapRow, session: AsyncSession) -> ChainSwapRow:
    """Fill ``value_usd``/``pricing_source`` for a row the cash leg couldn't price.

    A ``QUOTE_LEG`` row is left untouched -- the swap's own dollar-pegged leg
    is already a better number than any nearby candle. Rows with no price
    available anywhere stay ``UNPRICED`` rather than raising: a report with a
    gap is still useful, an exception mid-batch is not.
    """
    if row.pricing_source != "UNPRICED" or row.symbol is None:
        return row
    price = await _nearest_market_price(session, row.symbol, row.block_time_ms)
    if price is None:
        return row
    return dataclasses.replace(row, value_usd=row.token_amount * price, pricing_source="MARKET_PRICE")
