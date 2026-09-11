"""Manual wallet seed -- the registry's path that needs no FOMO session.

FOMO's API is the convenient way to learn "which wallet belongs to which
ranked trader", but it must not be a dependency the trade tape inherits: a
Privy session expires within the hour, the endpoints are undocumented and
move, and FOMO's Terms discourage automated collection. A hand-curated seed
file keeps the tape running on wallets gathered from public profiles and the
public chain -- the same approach fomopulse takes with its own
``config/wallets.json``.

Seeded rows carry ``source="manual"`` and, like any newly discovered wallet,
leave ``backfilled_from_block`` NULL, so ``app.workers.chain_tape`` backfills
their recent history before advancing its cursor.

Parsing is deliberately separate from writing: ``parse_seed`` is pure and
fully tested, so a malformed address is caught before anything reaches the
database.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import FomoTrader

__all__ = ["SeedError", "SeedWallet", "apply_seed", "load_seed_file", "parse_seed"]

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class SeedError(RuntimeError):
    """The seed file is malformed or carries an address we refuse to store."""


@dataclass(frozen=True)
class SeedWallet:
    fomo_user_id: str
    evm_address: str
    user_handle: str | None
    display_name: str | None


def _synthetic_id(address: str) -> str:
    """Stable id for a wallet we know by address but not by FOMO identity.

    Prefixed so its origin is obvious in the table, and derived from the
    address so re-seeding the same wallet updates its row instead of adding
    a second one.
    """
    return f"manual:{address.lower()}"


def parse_seed(payload: object) -> list[SeedWallet]:
    """Validate seed JSON into rows. Pure -- no I/O, no database."""
    if isinstance(payload, dict):
        rows = payload.get("wallets")
    elif isinstance(payload, list):
        rows = payload
    else:
        raise SeedError("seed must be a JSON object with a 'wallets' array, or a bare array")
    if not isinstance(rows, list):
        raise SeedError("'wallets' must be an array")

    seen: set[str] = set()
    result: list[SeedWallet] = []
    for index, row in enumerate(rows):
        if isinstance(row, str):
            row = {"address": row}
        if not isinstance(row, dict):
            raise SeedError(f"wallets[{index}] must be an object or an address string")

        address = str(row.get("address") or "").strip()
        if not _ADDRESS_RE.match(address):
            raise SeedError(f"wallets[{index}].address is not a 0x-prefixed 20-byte address: {address!r}")
        if address.lower() in seen:
            raise SeedError(f"wallets[{index}] repeats address {address}")
        seen.add(address.lower())

        fomo_user_id = str(row.get("fomo_user_id") or "").strip() or _synthetic_id(address)
        handle = str(row.get("handle") or "").strip() or None
        display_name = str(row.get("display_name") or "").strip() or None
        result.append(SeedWallet(
            fomo_user_id=fomo_user_id,
            evm_address=address,
            user_handle=handle,
            display_name=display_name,
        ))
    return result


def load_seed_file(path: str | Path) -> list[SeedWallet]:
    file_path = Path(path)
    if not file_path.exists():
        raise SeedError(f"seed file not found: {file_path}")
    try:
        payload = json.loads(file_path.read_text())
    except ValueError as exc:
        raise SeedError(f"seed file is not valid JSON: {exc}") from None
    return parse_seed(payload)


async def apply_seed(session, wallets: list[SeedWallet]) -> dict[str, int]:
    """Upsert seeded wallets, leaving backfill state alone.

    Re-running is safe and is expected: identity fields are refreshed, but
    ``backfilled_from_block`` is never reset, so an already-backfilled wallet
    is not re-scanned on every seed run.
    """
    inserted = 0
    for wallet in wallets:
        statement = insert(FomoTrader).values(
            fomo_user_id=wallet.fomo_user_id,
            evm_address=wallet.evm_address,
            user_handle=wallet.user_handle,
            display_name=wallet.display_name,
            source="manual",
        )
        statement = statement.on_conflict_do_update(
            index_elements=[FomoTrader.fomo_user_id],
            set_={
                "evm_address": statement.excluded.evm_address,
                "user_handle": statement.excluded.user_handle,
                "display_name": statement.excluded.display_name,
            },
        )
        result = await session.execute(statement)
        inserted += result.rowcount or 0
    return {"seeded": len(wallets), "written": inserted}
