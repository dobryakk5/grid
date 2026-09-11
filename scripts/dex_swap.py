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

A live run takes three separate acts of intent, because it spends real money:
``DEX_DRY_RUN=false``, ``--execute``, and ``--confirm-live``. Without the last
one it quotes, prints exactly what would be signed -- including the worst fill
the transaction could produce -- and stops.

    scripts/dex_swap.py --symbol PONSETH --side buy --amount 0.0004 \
        --limit 0.00026 --execute                  # preview, signs nothing
    scripts/dex_swap.py --symbol PONSETH --side buy --amount 0.0004 \
        --limit 0.00026 --execute --confirm-live   # sends it
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
from app.exchanges.base import decimal_str  # noqa: E402
from app.dex.dexscreener import DexScreenerClient  # noqa: E402
from app.dex.execution import StorageUnavailable, execute_swap  # noqa: E402
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
        help="intend to trade for real; still previews unless --confirm-live",
    )
    parser.add_argument(
        "--slippage", type=Decimal, default=None,
        help=(
            "slippage tolerance in percent (default DEX_MAX_SLIPPAGE_PCT). "
            "Lower tightens the worst-case fill a limit is judged on, at a "
            "higher chance the swap reverts"
        ),
    )
    parser.add_argument(
        "--confirm-live", action="store_true",
        help="actually sign and broadcast, after reading the preview",
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
        print(f"Balance: {decimal_str(await chain.native_balance())} ETH")
        print(
            f"{args.side.title()}:  {args.amount} {spent} -> {received} "
            f"at {'>=' if selling else '<='} {args.limit}"
        )

        async with SessionLocal() as session:
            # Always quote first without signing. For a live run this is the
            # preview; the money-moving pass re-quotes afterwards, because a
            # quote a human just read is already too old to sign.
            outcome = await execute_swap(
                session,
                symbol=pair.symbol,
                side="Sell" if selling else "Buy",
                amount_in=args.amount,
                limit_price=args.limit,
                chain=chain,
                uniswap=uniswap,
                market=market,
                dry_run=True,
                slippage_pct=args.slippage,
            )
            if args.execute and outcome.status == "DRY_RUN":
                _preview(chain, pair, args, outcome, selling=selling)
                if not args.confirm_live:
                    print("Not sent. Add --confirm-live to sign and broadcast.")
                    return 0
                outcome = await execute_swap(
                    session,
                    symbol=pair.symbol,
                    side="Sell" if selling else "Buy",
                    amount_in=args.amount,
                    limit_price=args.limit,
                    chain=chain,
                    uniswap=uniswap,
                    market=market,
                    dry_run=False,
                    slippage_pct=args.slippage,
                )
    except StorageUnavailable as exc:
        print()
        print(f"Not sent: {exc}")
        print("Nothing was signed or broadcast. Fix DATABASE_URL and re-run;")
        print("an approval already on chain is reused, not repeated.")
        return 3
    finally:
        await chain.close()
        await uniswap.close()
        await market.close()

    print()
    print(f"Status: {outcome.status}")
    if outcome.reason:
        print(f"Reason: {outcome.reason}")
    if outcome.funding_note:
        print(f"Funding: {outcome.funding_note}")
    for label, value in (
        ("Pool price", outcome.market_price),
        ("Executable", outcome.quoted_price),
        ("Worst case", outcome.worst_price),
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


def _preview(chain, pair, args, outcome, *, selling: bool) -> None:
    """What is about to be signed, in the terms it will be signed in."""
    spent = pair.base_coin if selling else pair.quote_coin
    received = pair.quote_coin if selling else pair.base_coin
    unit = f"{pair.quote_coin}/{pair.base_coin}"

    print()
    print("=" * 52)
    print("LIVE ORDER")
    print("=" * 52)
    print(f"  Chain:      {pair.chain} {settings.rh_chain_id}")
    print(f"  Wallet:     {chain.wallet_address}")
    print(f"  Side:       {args.side.upper()}")
    print(f"  Pair:       {pair.base_coin}/{pair.quote_coin}")
    print(f"  Spend max:  {args.amount} {spent}")
    print(f"  Limit:      {args.limit} {unit} "
          f"({'at least' if selling else 'at most'})")
    print()
    print(f"  Quoted:     {outcome.amount_out} {received}")
    if outcome.worst_amount_out is not None:
        print(f"  Worst case: {outcome.worst_amount_out} {received}")
    if outcome.worst_price is not None:
        print(f"  Worst px:   {outcome.worst_price} {unit}")
    if outcome.gas_estimate_native is not None:
        usd = (
            f" / ~${outcome.gas_estimate_usd:.4f}"
            if outcome.gas_estimate_usd is not None else ""
        )
        print(f"  Gas est.:   {outcome.gas_estimate_native} ETH{usd}")
    if "Permit2" in (outcome.reason or ""):
        print("  Permit2:    an approval and a signature are required first")
    print("=" * 52)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
