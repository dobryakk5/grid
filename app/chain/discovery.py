"""Find wallets worth tracking, from chain activity alone.

The registry has to grow by itself: a list of wallets curated once goes stale
the moment a new trader shows up, and the tape only ever sees wallets it
already knows. This module answers "who is actually moving size in this
market right now" without needing FOMO's leaderboard -- by classifying every
swap in a recent window and ranking the addresses behind them.

Two rules keep the answer useful:

* only externally-owned accounts. Net-delta classification happily reports a
  router, an aggregator or a market-maker contract as a "trader" -- tokens
  really do move in and out of them -- but their positioning means nothing,
  and scanning one for every token it touches costs thousands of transfers
  per block range;
* ranked by traded volume, not trade count, so a hundred dust trades do not
  outrank real size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from web3 import AsyncWeb3

from app.chain.tape import ChainSwapRow, classify, fetch_transfers, group_by_tx
from app.dex.chain import ChainClient
from app.dex.tokens import DexPair

__all__ = ["Candidate", "aggregate_candidates", "discover_wallets", "keep_externally_owned"]


@dataclass(frozen=True)
class Candidate:
    address: str
    buys: int
    sells: int
    bought_usd: Decimal
    sold_usd: Decimal

    @property
    def volume_usd(self) -> Decimal:
        return self.bought_usd + self.sold_usd

    @property
    def trades(self) -> int:
        return self.buys + self.sells


def aggregate_candidates(rows: Iterable[ChainSwapRow]) -> list[Candidate]:
    """Fold classified swaps into per-wallet totals, biggest volume first.

    Pure: the ranking rule is the part worth testing, and it needs no chain.
    """
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(
            row.wallet_address,
            {"buys": 0, "sells": 0, "bought": Decimal(0), "sold": Decimal(0)},
        )
        usd = row.value_usd or Decimal(0)
        if row.side == "BUY":
            entry["buys"] += 1
            entry["bought"] += usd
        else:
            entry["sells"] += 1
            entry["sold"] += usd

    candidates = [
        Candidate(
            address=address,
            buys=entry["buys"],
            sells=entry["sells"],
            bought_usd=entry["bought"],
            sold_usd=entry["sold"],
        )
        for address, entry in totals.items()
    ]
    candidates.sort(key=lambda c: (-c.volume_usd, -c.trades, c.address))
    return candidates


async def keep_externally_owned(
    client: ChainClient, candidates: list[Candidate], *, limit: int
) -> list[Candidate]:
    """Take the first ``limit`` candidates that are wallets, not contracts."""
    kept: list[Candidate] = []
    for candidate in candidates:
        if len(kept) >= limit:
            break
        code = await client.w3.eth.get_code(AsyncWeb3.to_checksum_address(candidate.address))
        if len(bytes(code)) > 0:
            continue
        kept.append(candidate)
    return kept


async def discover_wallets(
    client: ChainClient, pair: DexPair, *, blocks: int, top: int, chain_id: int
) -> list[Candidate]:
    """Rank real wallets trading ``pair`` over the last ``blocks`` blocks."""
    head = await client.w3.eth.block_number
    from_block = max(head - blocks, 0)

    transfers = await fetch_transfers(
        client, [pair.base.address, pair.quote.address], from_block, head
    )
    grouped = group_by_tx(transfers)

    # Everyone who appears is a candidate; classify() only reports wallets it
    # is told about, so the whole cast has to be handed to it here.
    everyone = {
        address
        for tx_transfers in grouped.values()
        for transfer in tx_transfers
        for address in (transfer["from_address"], transfer["to_address"])
    }

    rows: list[ChainSwapRow] = []
    for tx_transfers in grouped.values():
        rows.extend(classify(tx_transfers, everyone, pair, chain_id=chain_id))

    return await keep_externally_owned(client, aggregate_candidates(rows), limit=top)
