#!/usr/bin/env python3
"""Inspect or revoke a token's Permit2 allowance.

The standing approval a swap needs is unlimited by default -- that is Permit2's
design, since each transfer is still gated by a signed, expiring permit. This is
the tool for looking at that approval and taking it back.

    scripts/dex_allowance.py --token USDG
    scripts/dex_allowance.py --token USDG --revoke
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dex.approvals import permit2_address, revoke_allowance  # noqa: E402
from app.dex.chain import MAX_UINT256, ChainClient  # noqa: E402
from app.dex.tokens import resolve_token  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--revoke", action="store_true", help="set the allowance back to zero"
    )
    args = parser.parse_args()

    token = resolve_token(args.token)
    if token.native:
        print(f"{token.symbol} is the native coin; it has no allowance.")
        return 0

    spender = permit2_address()
    chain = ChainClient()
    try:
        await chain.ensure_ready()
        raw = await chain.token_allowance(token, spender)
        print(f"Wallet:  {chain.wallet_address}")
        print(f"Permit2: {spender}")
        print(
            f"Allowed: {'unlimited' if raw == MAX_UINT256 else token.from_wei(raw)} "
            f"{token.symbol}"
        )
        if args.revoke:
            tx_hash = await revoke_allowance(chain, token)
            print(f"Revoked in {tx_hash}")
    finally:
        await chain.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
