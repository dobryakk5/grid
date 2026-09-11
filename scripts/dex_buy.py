#!/usr/bin/env python3
"""Manual end-to-end swap on Robinhood Chain -- the stage-2 smoke test.

Runs the same code path a worker will, one shot, with the wallet and the limit
given on the command line. Dry-run by default: signing and broadcasting require
an explicit ``--execute``, and even then ``DEX_DRY_RUN=false`` must be set.

    scripts/dex_buy.py --symbol PONSETH --amount 0.001 --limit 0.00022
    scripts/dex_buy.py --symbol PONSETH --amount 0.001 --limit 0.00022 --execute
"""

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.dex.chain import ChainClient  # noqa: E402
from app.dex.dexscreener import DexScreenerClient  # noqa: E402
from app.dex.execution import execute_buy  # noqa: E402
from app.dex.tokens import resolve_pair  # noqa: E402
from app.dex.uniswap import UniswapClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="PONSETH")
    parser.add_argument(
        "--amount", required=True, type=Decimal, help="input amount, in quote units"
    )
    parser.add_argument(
        "--limit", required=True, type=Decimal,
        help="maximum executable price per base token, in quote units",
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
    chain = ChainClient()
    uniswap = UniswapClient()
    market = DexScreenerClient()
    try:
        await chain.ensure_ready()
        print(f"Wallet:  {chain.wallet_address}")
        print(f"Balance: {await chain.native_balance()} ETH")
        print(f"Buying:  {args.amount} {pair.quote_coin} -> {pair.base_coin} "
              f"at <= {args.limit}")

        async with SessionLocal() as session:
            outcome = await execute_buy(
                session,
                symbol=pair.symbol,
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
        ("Gas", outcome.gas_native),
        ("Tx", outcome.tx_hash),
    ):
        if value is not None:
            print(f"{label}: {value}")
    return 0 if outcome.status in {"DRY_RUN", "FILLED", "WAITING", "BLOCKED"} else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
