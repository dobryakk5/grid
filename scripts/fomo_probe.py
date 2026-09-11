"""Stage 0: probe FOMO's internal API with a real session and save fixtures.

Manual, one-off run against a live JWT -- writes anonymized response
fixtures to ``tests/fixtures/fomo/`` so the normalizers in
``app.fomo.schema`` can be tested against real field names instead of
guesses, and so a future schema change shows up as a fixture diff instead
of a surprise in production.

Anonymization keeps structure, drops identity: handles, display names and
avatar URLs become deterministic fakes; ``id``/``evmAddress`` become stable
fake values (same input -> same fake, so relationships between files survive)
built from a truncated hash, never the real value.

Usage::

    FOMO_JWT=... .venv/bin/python scripts/fomo_probe.py
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.dex.tokens import resolve_pair  # noqa: E402
from app.fomo.client import FomoClient, FomoError  # noqa: E402
from app.fomo.schema import normalize_holders, normalize_leaderboard  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures" / "fomo"

_SENSITIVE_STRING_KEYS = {"userHandle", "handle", "displayName", "name", "profilePictureLink", "avatar"}
_ADDRESS_KEYS = {"id", "userId", "evmAddress", "evm_address", "address", "userAddress", "fromAddress", "toAddress"}


def _fake(value: str, *, prefix: str) -> str:
    digest = hashlib.sha256(str(value).encode()).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _scrub(node):
    if isinstance(node, dict):
        scrubbed = {}
        for key, value in node.items():
            if key in _SENSITIVE_STRING_KEYS and isinstance(value, str) and value:
                scrubbed[key] = _fake(value, prefix="name")
            elif key in _ADDRESS_KEYS and isinstance(value, str) and value:
                scrubbed[key] = _fake(value, prefix="0xanon") if value.startswith("0x") else _fake(value, prefix="user")
            else:
                scrubbed[key] = _scrub(value)
        return scrubbed
    if isinstance(node, list):
        return [_scrub(item) for item in node]
    return node


def _save(name: str, payload: object) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / f"{name}.json"
    path.write_text(json.dumps(_scrub(payload), ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {path.relative_to(ROOT)}")


async def main() -> int:
    if not settings.fomo_jwt.strip():
        print("FOMO_JWT is not set; nothing to probe", file=sys.stderr)
        return 1

    fomo = FomoClient()
    try:
        leaderboard = await fomo.leaderboard()
        _save("leaderboard", leaderboard)

        ranks = normalize_leaderboard(leaderboard)
        print(f"leaderboard: {len(ranks)} traders, "
              f"{sum(1 for r in ranks if r.evm_address)} with evmAddress inline")

        symbol = (settings.dex_watch_symbols or "PONSUSDG").split(",")[0].strip()
        pair = resolve_pair(symbol)
        holders = await fomo.holders(pair.base.address, settings.rh_chain_id)
        _save("holders", holders)
        holder_rows = normalize_holders(holders)
        print(f"holders ({symbol}): {len(holder_rows)} rows, "
              f"{sum(1 for h in holder_rows if h.evm_address)} with evmAddress")

        if ranks:
            sample_user_id = next((r.user_id for r in ranks if r.evm_address is None), ranks[0].user_id)
            balances = await fomo.balances(sample_user_id)
            _save("balances", balances)
            trades = await fomo.trades(sample_user_id, limit=10)
            _save("trades", trades)
        else:
            print("leaderboard was empty; skipping per-user balances/trades probes")
    except FomoError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await fomo.close()

    print("\nNext: eyeball the fixtures above against app/fomo/schema.py's _pick() "
          "calls and fix any field names that don't match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
