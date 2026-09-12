"""What the trading wallet actually holds on Robinhood Chain, and what a
part-sale of one of those holdings would look like.

Finding the holdings is the awkward half. The tape indexes FOMO traders'
wallets, not ours, so ``chain_transactions`` has nothing about us; a filtered
log scan over the chain's 61M blocks times out on the RPC; and the Blockscout
API sits behind a bot-check. What is left is to ask every token we know about
whether it owes us anything -- which is only affordable through multicall3,
where 1385 ``balanceOf`` calls cost four ``eth_call``s instead of 1385.

The consequence worth knowing: a position is visible here only if its token is
in ``chain_tokens``. That table is filled by the tape, so a coin nobody on the
leaderboard has ever touched could sit in the wallet unseen. It is a real gap,
not a rounding error, and it is the price of not having an indexer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select

from app.core.config import settings
from app.db.models import ChainToken

__all__ = [
    "MULTICALL3", "Position", "fraction_amount", "limit_from_quote",
    "read_balances", "open_positions",
]

# Deterministic-deployment address, identical on every chain that has it.
# Read out of FOMO's own chain definition for 4663 and confirmed deployed.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_BALANCE_OF = "70a08231"
# 400 calls per batch answered in ~2.3s against this RPC; larger batches start
# to risk the same timeout that killed the log scan.
_BATCH = 400

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

    async def sweep(chunk: list[str]) -> list[tuple[str, int]]:
        calls = [(client.w3.to_checksum_address(a), True, data) for a in chunk]
        results = await contract.functions.aggregate3(calls).call()
        found = []
        for address, (ok, returned) in zip(chunk, results):
            if ok and len(returned) >= 32:
                found.append((address, int.from_bytes(returned[:32], "big")))
        return found

    balances: dict[str, int] = {}
    for part in await asyncio.gather(*(sweep(chunk) for chunk in chunks)):
        balances.update(part)
    return balances


async def open_positions(session_factory, client, *, chain_id: int | None = None) -> list[Position]:
    """Every known token the wallet holds a non-zero amount of, largest first."""
    chain = chain_id if chain_id is not None else settings.rh_chain_id
    async with session_factory() as session:
        tokens = list((await session.execute(
            select(ChainToken).where(ChainToken.chain_id == chain)
        )).scalars())
    known = {token.address.lower(): token for token in tokens}
    balances = await read_balances(client, sorted(known), client.wallet_address)
    positions = [
        Position(address=address, symbol=known[address].symbol,
                 decimals=known[address].decimals, raw=raw)
        for address, raw in balances.items() if raw > 0 and address in known
    ]
    return sorted(positions, key=lambda p: p.amount, reverse=True)
