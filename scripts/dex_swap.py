#!/usr/bin/env python3
"""Manual end-to-end swap on Robinhood Chain -- the execution smoke test.

Runs the same code path a worker will, one shot, with the side, size and limit
given on the command line. Dry-run by default: signing and broadcasting require
an explicit ``--execute``, and even then ``DEX_DRY_RUN=false`` must be set.

The limit is always quote-per-base. A buy fills at that price or lower, a sell
at that price or higher; a quote on the wrong side of it is declined rather than
filled at market.

    scripts/dex_swap.py --symbol PONSETH --side buy  --amount 0.001 --limit 0.00022
    scripts/dex_swap.py --symbol PONSETH --side sell --amount 100   --limit 0.00025
    scripts/dex_swap.py --symbol PONSETH --side buy  --amount 0.001 --limit 0.00022 --execute
"""

import argparse
import asyncio
import logging
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.dex.chain import ChainClient  # noqa: E402
from app.dex.dexscreener import DexScreenerClient  # noqa: E402
from app.dex.execution import execute_swap  # noqa: E402
from app.dex.tokens import resolve_pair  # noqa: E402
from app.dex.uniswap import UniswapClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="PONSETH")
    parser.add_argument("--side", choices=("buy", "sell"), default="buy")
    parser.add_argument(
        "--amount", required=True, type=Decimal,
        help="amount of the token being spent (quote for a buy, base for a sell)",
    )
    parser.add_argument(
        "--limit", required=True, type=Decimal,
        help="price per base token, in quote units: a ceiling to buy, a floor to sell",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="sign and broadcast; without it nothing leaves the machine",
    )
    args = parser.parse_args()

    if args.execute and settings.dex_dry_run:
        print("Refusing: --execute was passed but DEX_DRY_RUN is still true.")
        return 2

    pair = resolve_pair(args.symbol)
    selling = args.side == "sell"
    spent = pair.base_coin if selling else pair.quote_coin
    received = pair.quote_coin if selling else pair.base_coin

    chain = ChainClient()
    uniswap = UniswapClient()
    market = DexScreenerClient()
    try:
        await chain.ensure_ready()
        print(f"Wallet:  {chain.wallet_address}")
        print(f"Balance: {await chain.native_balance()} ETH")
        print(
            f"{args.side.title()}:  {args.amount} {spent} -> {received} "
            f"at {'>=' if selling else '<='} {args.limit}"
        )

        async with SessionLocal() as session:
            outcome = await execute_swap(
                session,
                symbol=pair.symbol,
                side="Sell" if selling else "Buy",
                amount_in=args.amount,
                limit_price=args.limit,
                chain=chain,
                uniswap=uniswap,
                market=market,
                dry_run=not args.execute,
            )
    finally:
        await chain.close()
        await uniswap.close()
        await market.close()

    print()
    print(f"Status: {outcome.status}")
    if outcome.reason:
        print(f"Reason: {outcome.reason}")
    for label, value in (
        ("Pool price", outcome.market_price),
        ("Executable", outcome.quoted_price),
        ("Filled at", outcome.fill_price),
        ("Spent", outcome.amount_in),
        ("Received", outcome.amount_out),
        ("Gas (ETH)", outcome.gas_native),
        (f"Gas ({pair.quote_coin})", outcome.gas_quote),
        ("Approval tx", outcome.approval_tx_hash),
        ("Tx", outcome.tx_hash),
    ):
        if value is not None:
            print(f"{label}: {value}")
    return 0 if outcome.status in {"DRY_RUN", "FILLED", "WAITING", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
