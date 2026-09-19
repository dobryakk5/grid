"""ERC-20 metadata for tokens we meet on chain but do not have registered.

``app.dex.tokens`` is the registry of instruments we *trade* -- curated, with
addresses and decimals pinned by hand because a wrong decimals value there
silently misprices a real order. This module is the opposite case: tokens a
tracked wallet happened to touch, discovered at scan time, where the only
honest source of ``decimals``/``symbol`` is the contract itself.

Decimals matter here for exactly one reason: without them an amount is a
meaningless integer, and a 6-decimals stablecoin would look 10^12 times
larger than an 18-decimals token. Results are cached in ``chain_tokens`` so a
busy wallet does not re-read the same contract on every pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from web3 import AsyncWeb3

from app.db.models import ChainToken

__all__ = ["TokenMeta", "resolve_token_meta"]

_ERC20_METADATA_ABI = [
    {
        "name": "decimals", "type": "function", "stateMutability": "view",
        "inputs": [], "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol", "type": "function", "stateMutability": "view",
        "inputs": [], "outputs": [{"name": "", "type": "string"}],
    },
]

# A token whose decimals() call fails is almost certainly not a standard
# ERC-20 we can value; 18 is the ERC-20 norm and the least-wrong fallback,
# but the symbol stays None so the UI shows the address rather than a guess.
_DEFAULT_DECIMALS = 18


@dataclass(frozen=True)
class TokenMeta:
    address: str
    symbol: str | None
    decimals: int

    def from_wei(self, amount: int | str) -> Decimal:
        return Decimal(int(amount)).scaleb(-self.decimals)


# An ERC-20 symbol is whatever an arbitrary contract chose to return, and
# "arbitrary" is not a figure of speech: a token on Robinhood Chain answers
# with several hundred digits of pi. It goes into a VARCHAR, so it is cut to
# fit at the boundary where it arrives rather than at the INSERT -- a row that
# cannot be written fails the whole scan pass, and the tape then sits on that
# block forever, re-reading the same token and failing the same way. A coin
# nobody trades must not be able to stop the scan.
#
# Taken from the column so the two cannot drift apart.
_SYMBOL_MAX = ChainToken.symbol.type.length


def _clean_symbol(raw) -> str | None:
    """A symbol short enough to store and printable enough to show."""
    text = "".join(ch for ch in str(raw) if ch.isprintable()).strip()
    return text[:_SYMBOL_MAX] or None


async def _read_from_chain(client, address: str) -> TokenMeta:
    contract = client.w3.eth.contract(
        address=AsyncWeb3.to_checksum_address(address), abi=_ERC20_METADATA_ABI
    )
    try:
        decimals = int(await contract.functions.decimals().call())
    except Exception:
        decimals = _DEFAULT_DECIMALS
    try:
        symbol = _clean_symbol(await contract.functions.symbol().call())
    except Exception:
        # Pre-standard tokens return bytes32 here, and some return nothing at
        # all. An unnamed token is still perfectly tradable, so this is not
        # an error -- the address just has to stand in for the name.
        symbol = None
    return TokenMeta(address=address, symbol=symbol, decimals=decimals)


async def read_cached(session, addresses: list[str], *, chain_id: int) -> dict[str, TokenMeta]:
    wanted = {address.lower() for address in addresses if address}
    if not wanted:
        return {}
    rows = (await session.execute(
        select(ChainToken).where(
            ChainToken.chain_id == chain_id,
            ChainToken.address.in_(sorted(wanted)),
        )
    )).scalars()
    return {row.address.lower(): TokenMeta(row.address, row.symbol, row.decimals) for row in rows}


async def resolve_token_meta(
    client, session_factory, addresses: list[str], *, chain_id: int
) -> dict[str, TokenMeta]:
    """``{lowercase address: TokenMeta}``, reading the chain only for misses.

    The three phases are kept strictly apart -- read the cache, then talk to
    the chain, then write the cache -- so that no RPC call ever happens while
    a database transaction is open. Interleaving them means a stalled RPC
    (which a rate-limited public node does readily) leaves Postgres sitting
    "idle in transaction", holding locks until the process is killed.
    """
    wanted = {address.lower() for address in addresses if address}
    if not wanted:
        return {}

    async with session_factory() as session:
        known = await read_cached(session, sorted(wanted), chain_id=chain_id)

    missing = sorted(wanted - set(known))
    if not missing:
        return known

    fetched = {address: await _read_from_chain(client, address) for address in missing}

    async with session_factory() as session:
        for address, meta in fetched.items():
            statement = insert(ChainToken).values(
                chain_id=chain_id, address=address, symbol=meta.symbol, decimals=meta.decimals,
            )
            statement = statement.on_conflict_do_nothing(
                index_elements=[ChainToken.chain_id, ChainToken.address]
            )
            await session.execute(statement)
        await session.commit()

    return {**known, **fetched}
