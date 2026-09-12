"""Contract safety, from GoPlus -- free, keyless, and covering every chain here.

GoPlus answers for Robinhood Chain (4663) as well as Ethereum, BSC, Base and
Monad, with a separate endpoint for Solana. That matters: it is the only
external source in this project that covers 4663 at all.

The one rule that shapes this module: **an empty answer is unknown, never
"no"**. GoPlus returns ``""`` or omits a field it could not determine, and
reading that as a clean bill of health is precisely how a honeypot walks
through a risk gate. So every fact here is tri-state -- ``True``, ``False`` or
``None`` -- and the scoring treats unknown as unknown.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import settings

__all__ = ["SOLANA_CHAIN_ID", "check_tokens", "parse_evm", "parse_solana"]

SOLANA_CHAIN_ID = 1399811149
# GoPlus takes a comma-joined list; this is the documented ceiling per call.
BATCH = 30

# Burn and lock destinations: tokens parked here are not someone's position.
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "11111111111111111111111111111111",
}


def _decimal(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _flag(value) -> bool | None:
    """``"1"``/``"0"`` to a bool; anything else -- including ``""`` -- unknown."""
    if value in ("1", 1, True):
        return True
    if value in ("0", 0, False):
        return False
    return None


def _status(node) -> bool | None:
    """Solana states the same thing as ``{"status": "1", "authority": [...]}``."""
    if isinstance(node, dict):
        return _flag(node.get("status"))
    return _flag(node)


def _percent(value) -> Decimal | None:
    """GoPlus reports shares as fractions (0.1146 = 11.46%); we store percent."""
    number = _decimal(value)
    return number * 100 if number is not None else None


def _top_holders(rows, *, limit: int = 10) -> tuple[Decimal | None, Decimal | None]:
    """``(top N percent, top N percent excluding locked/burned/pools)``.

    Both, because they answer different questions. The raw figure is what every
    screener shows; the second is the one that matters for "can a handful of
    people dump this", since liquidity pools and burn addresses cannot sell.
    """
    if not isinstance(rows, list) or not rows:
        return None, None
    shares, free = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        percent = _percent(row.get("percent"))
        if percent is None:
            continue
        shares.append(percent)
        address = str(row.get("address") or "").lower()
        tag = str(row.get("tag") or "").lower()
        locked = _flag(row.get("is_locked")) is True
        parked = locked or address in BURN_ADDRESSES or any(
            word in tag for word in ("burn", "lock", "pool", "lp", "dead")
        )
        if not parked:
            free.append(percent)
    if not shares:
        return None, None
    top = sum(sorted(shares, reverse=True)[:limit])
    top_free = sum(sorted(free, reverse=True)[:limit]) if free else Decimal(0)
    return top, top_free


def parse_evm(row: dict) -> dict:
    """Normalised EVM facts. Keys missing from the answer stay ``None``."""
    if not isinstance(row, dict):
        return {}
    top10, top10_free = _top_holders(row.get("holders"))
    lp_locked = None
    lp_rows = row.get("lp_holders")
    if isinstance(lp_rows, list) and lp_rows:
        parked = [
            _percent(item.get("percent")) or Decimal(0)
            for item in lp_rows
            if isinstance(item, dict) and (
                _flag(item.get("is_locked")) is True
                or str(item.get("address") or "").lower() in BURN_ADDRESSES
                or "burn" in str(item.get("tag") or "").lower()
            )
        ]
        lp_locked = sum(parked) if parked else Decimal(0)
    return {
        "open_source": _flag(row.get("is_open_source")),
        "proxy": _flag(row.get("is_proxy")),
        "mintable": _flag(row.get("is_mintable")),
        "honeypot": _flag(row.get("is_honeypot")),
        "cannot_sell_all": _flag(row.get("cannot_sell_all")),
        "cannot_buy": _flag(row.get("cannot_buy")),
        "transfer_pausable": _flag(row.get("transfer_pausable")),
        "blacklisted": _flag(row.get("is_blacklisted")),
        "slippage_modifiable": _flag(row.get("slippage_modifiable")),
        "can_take_back_ownership": _flag(row.get("can_take_back_ownership")),
        "hidden_owner": _flag(row.get("hidden_owner")),
        "selfdestruct": _flag(row.get("selfdestruct")),
        "in_dex": _flag(row.get("is_in_dex")),
        "buy_tax_pct": _percent(row.get("buy_tax")),
        "sell_tax_pct": _percent(row.get("sell_tax")),
        "owner_percent": _percent(row.get("owner_percent")),
        "creator_percent": _percent(row.get("creator_percent")),
        "holder_count": int(_decimal(row.get("holder_count")) or 0) or None,
        "top10_percent": top10,
        "top10_percent_free": top10_free,
        "lp_locked_percent": lp_locked,
        "symbol": row.get("token_symbol") or None,
        "name": row.get("token_name") or None,
    }


def parse_solana(row: dict) -> dict:
    """Normalised Solana facts, named like the EVM ones where they mean the same.

    Different authorities, same question: can someone change the rules after
    you bought. ``mintable``/``freezable``/``closable`` are the ones that end a
    position without asking.
    """
    if not isinstance(row, dict):
        return {}
    top10, top10_free = _top_holders(row.get("holders"))
    burn = [
        _decimal(pool.get("burn_percent"))
        for pool in (row.get("dex") or [])
        if isinstance(pool, dict) and pool.get("burn_percent") is not None
    ]
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    transfer_fee = row.get("transfer_fee")
    return {
        "mintable": _status(row.get("mintable")),
        "freezable": _status(row.get("freezable")),
        "closable": _status(row.get("closable")),
        "balance_mutable": _status(row.get("balance_mutable_authority")),
        "metadata_mutable": _status(row.get("metadata_mutable")),
        "transfer_hook": bool(row.get("transfer_hook")) if row.get("transfer_hook") is not None else None,
        "has_transfer_fee": bool(transfer_fee) if transfer_fee is not None else None,
        "non_transferable": _flag(row.get("non_transferable")),
        "trusted_token": _flag(row.get("trusted_token")),
        "holder_count": int(_decimal(row.get("holder_count")) or 0) or None,
        "top10_percent": top10,
        "top10_percent_free": top10_free,
        # Already a percent in the answer (0.34 = 0.34%), unlike the fractions
        # EVM uses -- and it is per pool, so the best pool is the most generous
        # reading there is.
        "lp_burn_percent": min(max([value for value in burn if value is not None],
                                   default=None) or Decimal(0), Decimal(100)) if burn else None,
        "symbol": metadata.get("symbol") or None,
        "name": metadata.get("name") or None,
    }


#: Facts that are dangerous when true, with the wording a human should read.
DANGERS = {
    "honeypot": "не даёт продавать (honeypot)",
    "cannot_sell_all": "нельзя продать весь объём",
    "cannot_buy": "покупка заблокирована",
    "mintable": "выпуск новых монет не закрыт",
    "freezable": "эмитент может заморозить счёт",
    "closable": "эмитент может закрыть счёт",
    "balance_mutable": "эмитент может менять баланс",
    "proxy": "прокси-контракт: логику можно подменить",
    "can_take_back_ownership": "владение можно вернуть себе",
    "hidden_owner": "скрытый владелец",
    "selfdestruct": "контракт может самоуничтожиться",
    "transfer_pausable": "переводы можно остановить",
    "blacklisted": "есть чёрный список адресов",
    "slippage_modifiable": "комиссию/проскальзывание можно менять",
    "non_transferable": "токен нельзя переводить",
    "has_transfer_fee": "комиссия за перевод",
    "metadata_mutable": "метаданные можно переписать",
}


def dangers(facts: dict) -> list[str]:
    """Only what is known to be true. Unknown says nothing, here or anywhere."""
    found = [text for key, text in DANGERS.items() if facts.get(key) is True]
    for key, label in (("buy_tax_pct", "налог на покупку"), ("sell_tax_pct", "налог на продажу")):
        tax = facts.get(key)
        if tax is not None and tax > 0:
            found.append(f"{label} {tax:.1f}%")
    if facts.get("open_source") is False:
        found.append("исходник контракта не опубликован")
    return found


def plausible(chain_id: int, address: str) -> bool:
    """Does this address even belong to this chain's family?

    One address the endpoint cannot parse takes its whole batch down with it --
    GoPlus answers about none of them -- so a Solana mint sent to an EVM chain
    (or the reverse) is dropped before it can cost thirty other coins their
    check. A wrong pairing like that is a bug upstream, not a coin.
    """
    if not isinstance(address, str) or not address.strip():
        return False
    # The family, not the exact length: a Solana mint and an EVM address are
    # never confusable by their prefix, and inventing per-chain length rules
    # would risk dropping addresses from a chain we have not seen yet.
    return address.startswith("0x") != (chain_id == SOLANA_CHAIN_ID)


def _url(chain_id: int, addresses: list[str]) -> str:
    base = settings.goplus_base_url.rstrip("/")
    joined = ",".join(addresses)
    if chain_id == SOLANA_CHAIN_ID:
        return f"{base}/api/v1/solana/token_security?contract_addresses={joined}"
    return f"{base}/api/v1/token_security/{chain_id}?contract_addresses={joined}"


async def check_tokens(http, wanted, *, sleep=asyncio.sleep) -> dict:
    """``{(chain_id, address): (facts, raw)}`` for everything that answered.

    One request per chain per batch. A chain GoPlus does not serve, a timeout
    or a malformed answer simply yields nothing for those coins -- they stay
    unchecked and get asked again next pass, which is different from, and much
    safer than, being recorded as clean.
    """
    by_chain: dict[int, list[str]] = {}
    for chain_id, address in wanted:
        if plausible(chain_id, address):
            by_chain.setdefault(chain_id, []).append(address)
    results: dict[tuple[int, str], tuple[dict, dict]] = {}
    first = True
    for chain_id, addresses in by_chain.items():
        for start in range(0, len(addresses), BATCH):
            chunk = addresses[start:start + BATCH]
            if not first:
                # GoPlus throttles anonymous callers; this keeps a 120-coin
                # refresh comfortably inside it.
                await sleep(1.0)
            first = False
            try:
                response = await http.get(_url(chain_id, chunk))
                payload = response.json() if response.status_code < 400 else None
            except (httpx.HTTPError, ValueError):
                continue
            rows = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rows, dict):
                continue
            # GoPlus lowercases EVM keys and keeps Solana mints as they are.
            lowered = {str(key).lower(): value for key, value in rows.items()}
            parse = parse_solana if chain_id == SOLANA_CHAIN_ID else parse_evm
            for address in chunk:
                raw = rows.get(address) or lowered.get(address.lower())
                if isinstance(raw, dict):
                    results[(chain_id, address)] = (parse(raw), raw)
    return results
