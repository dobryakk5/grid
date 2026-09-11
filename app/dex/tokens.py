"""Token and pair registry for on-chain venues.

The rest of the app addresses instruments by symbol (``"PONSETH"``), exactly as
it does for Bybit and MEXC. On a DEX a symbol is not enough: every call needs
the two ERC-20 addresses and their decimals. This module is the single place
that turns one into the other.

Addresses that are not public knowledge yet are left empty on purpose and must
be supplied through the ``DEX_TOKENS`` env override -- the registry raises a
named error rather than guessing an address that would send funds nowhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal

from app.core.config import settings

__all__ = [
    "DexConfigError",
    "NATIVE_ADDRESS",
    "Token",
    "DexPair",
    "list_pairs",
    "native_pair_for",
    "resolve_pair",
    "resolve_token",
]


class DexConfigError(RuntimeError):
    """Raised when a token/pair is referenced before it has been configured."""


# Uniswap addresses native ETH with the zero address in quote/swap payloads.
NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Symbols that mean "the chain's gas token" when matching pools, where the pool
# itself is always denominated in the wrapped version.
_NATIVE_ALIASES = frozenset({"ETH", "WETH"})


@dataclass(frozen=True)
class Token:
    symbol: str
    address: str
    decimals: int
    native: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.address) and _ADDRESS_RE.match(self.address) is not None

    @property
    def unit(self) -> Decimal:
        """Smallest representable amount, e.g. ``1e-18`` for an 18-decimals token."""
        return Decimal(1).scaleb(-self.decimals)

    def to_wei(self, amount: Decimal) -> int:
        return int((Decimal(amount).scaleb(self.decimals)).to_integral_value())

    def from_wei(self, amount: int | str) -> Decimal:
        return Decimal(int(amount)).scaleb(-self.decimals)


@dataclass(frozen=True)
class DexPair:
    """One tradable instrument: ``base`` priced in ``quote`` on ``chain``."""

    symbol: str
    base: Token
    quote: Token
    chain: str
    tick_size: Decimal
    min_order_quote: Decimal

    @property
    def base_coin(self) -> str:
        return self.base.symbol

    @property
    def quote_coin(self) -> str:
        return self.quote.symbol


# decimals for tokens we have not read on chain yet default to 18 (the ERC-20
# norm). Stage 2 verifies each against the contract's own ``decimals()`` before
# the first signed transaction.
_BUILTIN_TOKENS: dict[str, Token] = {
    "ETH": Token(symbol="ETH", address=NATIVE_ADDRESS, decimals=18, native=True),
    # Wrapped ETH on Robinhood Chain -- needed to recognise ETH-quoted pools.
    "WETH": Token(symbol="WETH", address="", decimals=18),
    "USDG": Token(symbol="USDG", address="", decimals=6),
    # Official PONS contract (same address on CoinGecko and the main PONS market).
    "PONS": Token(
        symbol="PONS",
        address="0x39dbed3a2bd333467115de45665cc57f813c4571",
        decimals=18,
    ),
    "CASHCAT": Token(symbol="CASHCAT", address="", decimals=18),
}

_BUILTIN_PAIRS: dict[str, tuple[str, str, Decimal]] = {
    # symbol -> (base, quote, tick_size)
    "PONSETH": ("PONS", "ETH", Decimal("0.0000000001")),
    "PONSUSDG": ("PONS", "USDG", Decimal("0.0001")),
    "CASHCATETH": ("CASHCAT", "ETH", Decimal("0.0000000001")),
    "CASHCATUSDG": ("CASHCAT", "USDG", Decimal("0.000001")),
}


def _overrides() -> dict[str, dict]:
    """``DEX_TOKENS`` JSON, e.g. ``{"USDG": {"address": "0x...", "decimals": 6}}``."""
    raw = (settings.dex_tokens or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise DexConfigError(f"DEX_TOKENS is not valid JSON: {exc}") from None
    if not isinstance(parsed, dict):
        raise DexConfigError("DEX_TOKENS must be a JSON object keyed by symbol")
    return parsed


def _registry() -> dict[str, Token]:
    tokens = dict(_BUILTIN_TOKENS)
    for symbol, patch in _overrides().items():
        key = symbol.strip().upper()
        if not isinstance(patch, dict):
            raise DexConfigError(f"DEX_TOKENS[{key}] must be an object")
        base = tokens.get(key, Token(symbol=key, address="", decimals=18))
        address = str(patch.get("address", base.address) or "").strip().lower()
        if address and not _ADDRESS_RE.match(address):
            raise DexConfigError(f"DEX_TOKENS[{key}].address is not a 0x address")
        tokens[key] = replace(
            base,
            address=address,
            decimals=int(patch.get("decimals", base.decimals)),
        )
    return tokens


def resolve_token(symbol: str) -> Token:
    token = _registry().get(symbol.strip().upper())
    if token is None:
        raise DexConfigError(f"unknown token {symbol!r}")
    if not token.configured and not token.native:
        raise DexConfigError(
            f"token {token.symbol} has no address; set it via "
            f'DEX_TOKENS={{"{token.symbol}": {{"address": "0x..."}}}}'
        )
    return token


def native_alias_addresses() -> frozenset[str]:
    """Addresses that stand in for the gas token when matching pools."""
    tokens = _registry()
    addresses = {NATIVE_ADDRESS}
    weth = tokens.get("WETH")
    if weth is not None and weth.configured:
        addresses.add(weth.address)
    return frozenset(addresses)


def is_native_symbol(symbol: str) -> bool:
    return symbol.strip().upper() in _NATIVE_ALIASES


def list_pairs() -> tuple[str, ...]:
    return tuple(sorted(_BUILTIN_PAIRS))


def native_pair_for(pair: DexPair) -> DexPair:
    """The same base token priced in the gas coin.

    Used to value gas in the pair's quote currency: DexScreener knows the base
    token's USD price, and its ETH-quoted pool turns that into an ETH price.
    """
    return DexPair(
        symbol=f"{pair.base.symbol}ETH",
        base=pair.base,
        quote=resolve_token("ETH"),
        chain=pair.chain,
        tick_size=pair.tick_size,
        min_order_quote=pair.min_order_quote,
    )


def resolve_pair(symbol: str) -> DexPair:
    key = symbol.strip().upper()
    spec = _BUILTIN_PAIRS.get(key)
    if spec is None:
        raise DexConfigError(
            f"unknown DEX pair {key!r}; known pairs: {', '.join(list_pairs())}"
        )
    base_symbol, quote_symbol, tick_size = spec
    return DexPair(
        symbol=key,
        base=resolve_token(base_symbol),
        quote=resolve_token(quote_symbol),
        chain=settings.dex_chain_slug,
        tick_size=tick_size,
        min_order_quote=settings.dex_min_order_quote,
    )
