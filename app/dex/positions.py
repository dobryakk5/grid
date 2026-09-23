"""What the trading wallet actually holds on Robinhood Chain, and what a
part-sale of one of those holdings would look like.

Finding the holdings is the awkward half. The tape indexes FOMO traders'
wallets, not ours, so ``chain_transactions`` has nothing about us; a filtered
log scan over the chain's 61M blocks times out on the RPC; and the Blockscout
API sits behind a bot-check. What is left is to ask a list of tokens whether
they owe us anything -- which is only affordable through multicall3, where
hundreds of ``balanceOf`` calls cost one ``eth_call`` instead of hundreds.

Which list is the whole question. Asking ``chain_tokens`` -- everything the
tape has ever met -- was affordable at 1385 rows and is not at eleven
thousand: other people's trading grows that table by thousands a day, and a
page load turned into dozens of Multicalls and then into a gateway timeout.
So the request path asks ``dex_wallet_tokens`` instead, the few dozen
contracts this wallet has touched, and the wider sweep moved to a background
pass that feeds that list (see ``app.dex.wallet_tokens``).

The consequence worth knowing is unchanged in kind, only in who bears it: a
holding is visible once something has put its contract on one of those two
lists. A coin airdropped to us and never traded by anyone is found by the
background sweep, not by the page. It is a real gap, not a rounding error,
and it is the price of not having an indexer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select

from app.core.config import settings
from app.db.models import ChainToken, DexWalletToken

__all__ = [
    "MULTICALL3", "Position", "PositionReadError", "fraction_amount", "limit_from_quote",
    "read_balances", "open_positions",
]

# Deterministic-deployment address, identical on every chain that has it.
# Read out of FOMO's own chain definition for 4663 and confirmed deployed.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_BALANCE_OF = "70a08231"
# 400 calls per batch answered in ~2.3s against this RPC; larger batches start
# to risk the same timeout that killed the log scan.
_BATCH = 400
# Thousands of discovered tokens turn those batches into a burst large enough
# to make the public RPC rate-limit every request. Keep a little parallelism,
# but never fan the whole token table out at once. The total deadline stays
# below nginx's default 60-second upstream timeout, so callers get a useful
# application error rather than a gateway timeout.
_BATCH_CONCURRENCY = 8
_BATCH_ATTEMPT_TIMEOUT = 10.0
_BATCH_ATTEMPTS = 3
_BATCH_RETRY_DELAY = 0.5
_BALANCE_SCAN_TIMEOUT = 45.0

_AGGREGATE3_ABI = [{
    "inputs": [{"components": [
        {"name": "target", "type": "address"},
        {"name": "allowFailure", "type": "bool"},
        {"name": "callData", "type": "bytes"},
    ], "name": "calls", "type": "tuple[]"}],
    "name": "aggregate3",
    "outputs": [{"components": [
        {"name": "success", "type": "bool"},
        {"name": "returnData", "type": "bytes"},
    ], "name": "returnData", "type": "tuple[]"}],
    "stateMutability": "payable",
    "type": "function",
}]


@dataclass(frozen=True)
class Position:
    address: str
    symbol: str | None
    decimals: int
    raw: int

    @property
    def amount(self) -> Decimal:
        return Decimal(self.raw).scaleb(-self.decimals)


class PositionReadError(RuntimeError):
    """The wallet balance scan could not finish against the chain RPC."""


def fraction_amount(balance: Decimal, percent: int, decimals: int) -> Decimal:
    """``percent`` of ``balance``, rounded *down* to the token's precision.

    Down, never nearest: rounding up would ask the wallet to hand over a
    fraction of a unit it does not own, and at 100% the difference between
    "all of it" and "a hair more than all of it" is a reverted transaction.
    """
    if percent not in (25, 50, 75, 100):
        raise ValueError("percent must be 25, 50, 75 or 100")
    if percent == 100:
        exact = balance
    else:
        exact = balance * Decimal(percent) / Decimal(100)
    step = Decimal(1).scaleb(-decimals)
    return exact.quantize(step, rounding=ROUND_DOWN)


LADDER_STEPS = 5


def ladder_levels(
    amount: Decimal, price: Decimal, step_pct: Decimal, decimals: int
) -> list[tuple[Decimal, Decimal]]:
    """``(limit_price, amount)`` for five levels centred on ``price``.

    Two steps below, the price itself, two above -- so the chosen price is the
    average of the ladder. Equal slices rounded down to the token precision;
    the rounding dust goes to the top level so the slices add up to exactly
    ``amount`` and a 100% ladder still empties the wallet.
    """
    if step_pct <= 0 or step_pct * 2 >= 100:
        raise ValueError("step must be between 0 and 50 percent")
    unit = Decimal(1).scaleb(-decimals)
    slice_ = (amount / LADDER_STEPS).quantize(unit, rounding=ROUND_DOWN)
    if slice_ <= 0:
        raise ValueError("amount is too small to split into five levels")
    half = LADDER_STEPS // 2
    levels = []
    for k in range(-half, half + 1):
        level_price = (price * (1 + step_pct * k / 100)).quantize(
            Decimal(1).scaleb(-18), rounding=ROUND_DOWN
        )
        levels.append((level_price, slice_))
    top_price, _ = levels[-1]
    levels[-1] = (top_price, amount - slice_ * (LADDER_STEPS - 1))
    return levels


def limit_from_quote(
    amount_in: Decimal, amount_out: Decimal, slippage_pct: Decimal, *, side: str = "Sell"
) -> Decimal:
    """Worst price, in quote per base, that a market-style order may fill at.

    The quote is what the router offers right now; the limit is that offer
    moved by the configured slippage cap -- and the direction of "worse"
    depends on the side. A seller is hurt by a *lower* price and a buyer by a
    *higher* one, so the cap is subtracted for one and added for the other.
    Getting this backwards would produce a limit that can never be met, or one
    that permits any price at all; hence the explicit side.

    ``amount_in``/``amount_out`` are in the trade's own direction: a sell hands
    over base and receives quote, a buy the other way round.
    """
    if amount_in <= 0 or amount_out <= 0:
        raise ValueError("amounts must be positive")
    if side == "Sell":
        price = amount_out / amount_in
        return price * (Decimal(100) - slippage_pct) / Decimal(100)
    if side == "Buy":
        price = amount_in / amount_out
        return price * (Decimal(100) + slippage_pct) / Decimal(100)
    raise ValueError("side must be Buy or Sell")


def _call_data(owner: str) -> bytes:
    return bytes.fromhex(_BALANCE_OF + owner.lower().removeprefix("0x").rjust(64, "0"))


async def read_balances(client, addresses: list[str], owner: str) -> dict[str, int]:
    """``{address: raw balance}`` for every address, in a handful of calls.

    ``allowFailure`` is on: one token whose ``balanceOf`` reverts (a
    self-destructed contract, a honeypot with a modified ABI) must not take the
    whole sweep down with it.
    """
    if not addresses:
        return {}
    contract = client.w3.eth.contract(
        address=client.w3.to_checksum_address(MULTICALL3), abi=_AGGREGATE3_ABI,
    )
    data = _call_data(owner)
    chunks = [addresses[i:i + _BATCH] for i in range(0, len(addresses), _BATCH)]
    gate = asyncio.Semaphore(_BATCH_CONCURRENCY)

    async def sweep(chunk: list[str]) -> list[tuple[str, int]]:
        calls = [(client.w3.to_checksum_address(a), True, data) for a in chunk]
        last_error: Exception | None = None
        for attempt in range(_BATCH_ATTEMPTS):
            try:
                async with gate:
                    async with asyncio.timeout(_BATCH_ATTEMPT_TIMEOUT):
                        results = await contract.functions.aggregate3(calls).call()
                break
            except Exception as exc:
                last_error = exc
                if attempt + 1 < _BATCH_ATTEMPTS:
                    await asyncio.sleep(_BATCH_RETRY_DELAY * 2 ** attempt)
        else:
            raise PositionReadError(
                f"balance batch failed after {_BATCH_ATTEMPTS} attempts: {last_error}"
            ) from last_error
        found = []
        for address, (ok, returned) in zip(chunk, results):
            if ok and len(returned) >= 32:
                found.append((address, int.from_bytes(returned[:32], "big")))
        return found

    balances: dict[str, int] = {}
    try:
        async with asyncio.timeout(_BALANCE_SCAN_TIMEOUT):
            for part in await asyncio.gather(*(sweep(chunk) for chunk in chunks)):
                balances.update(part)
    except TimeoutError as exc:
        raise PositionReadError(
            f"wallet balance scan exceeded {_BALANCE_SCAN_TIMEOUT:g}s"
        ) from exc
    except Exception as exc:
        raise PositionReadError(f"wallet balance scan failed: {exc}") from exc
    return balances


async def open_positions(
    session_factory, client, *, chain_id: int | None = None, universe: str = "wallet",
) -> list[Position]:
    """Every known token the wallet holds a non-zero amount of, largest first.

    ``universe`` picks which list of contracts to ask about, and the choice is
    the difference between a page that paints and one that times out:

    ``"wallet"`` -- the few dozen in ``dex_wallet_tokens``, the ones this
    wallet has actually touched. One Multicall. This is what a request path
    should ever use.

    ``"chain"`` -- every token the tape has ever met, which is what this did
    unconditionally until the table passed ten thousand rows. Kept for the
    background sweep in ``app.workers.dex``, whose whole job is to discover
    holdings the wallet never traded for -- an airdrop, or a coin bought
    outside the bot -- and which can afford to take a minute over it.
    """
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    model = ChainToken if universe == "chain" else DexWalletToken
    async with session_factory() as session:
        tokens = list((await session.execute(
            select(model).where(model.chain_id == chain)
        )).scalars())
    known = {token.address.lower(): token for token in tokens}
    balances = await read_balances(client, sorted(known), client.wallet_address)
    positions = [
        Position(address=address, symbol=known[address].symbol,
                 decimals=known[address].decimals, raw=raw)
        for address, raw in balances.items() if raw > 0 and address in known
    ]
    return sorted(positions, key=lambda p: p.amount, reverse=True)
