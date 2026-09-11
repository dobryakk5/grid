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
    "dynamic_key",
    "dynamic_token_by_address",
    "dynamic_tokens",
    "register_dynamic_token",
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
# Addresses below are Robinhood Chain (4663) deployments, from Robinhood's own
# contract list. They are not valid on any other chain: point DEX_CHAIN_SLUG
# somewhere else and every one of them must be overridden through DEX_TOKENS.
_BUILTIN_TOKENS: dict[str, Token] = {
    "ETH": Token(symbol="ETH", address=NATIVE_ADDRESS, decimals=18, native=True),
    # Pools hold WETH, never native ETH, so ETH-quoted pairs match through this.
    "WETH": Token(
        symbol="WETH",
        address="0x0bd7d308f8e1639fab988df18a8011f41eacad73",
        decimals=18,
    ),
    # The stablecoin Robinhood Chain actually publishes. USDC is a bridge-in
    # transport on other chains, not the capital we hold here, so it is
    # deliberately not a pair.
    "USDG": Token(
        symbol="USDG",
        address="0x5fc5360d0400a0fd4f2af552add042d716f1d168",
        decimals=6,
    ),
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


# Tokens learned from the chain rather than pinned here by hand. The registry
# above exists because a wrong `decimals` misprices a real order -- but a value
# read from the token's own `decimals()` is not a guess, so a token verified
# that way is just as safe to trade as one typed in. `app.dex.dynamic_tokens`
# fills this from the `chain_tokens` cache.
_DYNAMIC_TOKENS: dict[str, Token] = {}

# Quote assets a dynamic pair may be denominated in, longest suffix first so
# "USDG" wins over a hypothetical "G".
_DYNAMIC_QUOTES: tuple[str, ...] = ("USDG", "WETH", "ETH")


def dynamic_key(symbol: str, address: str) -> str:
    """Name a discovered token by symbol *and* address.

    Symbols are not identities on a permissionless chain: this one carries
    four different DOGGO contracts and three calling themselves USDG. Keying
    a tradable pair on the bare symbol means a limit order can resolve to
    whichever contract registered last, which is precisely how you buy a
    copy of the thing you meant to buy. The address suffix makes the pair
    name unambiguous while still reading like the token.
    """
    clean = "".join(ch for ch in symbol.strip().upper() if ch.isalnum())[:12] or "TOKEN"
    # Upper-cased on purpose: resolve_pair() upper-cases the symbol it is
    # given, so a lower-case hex suffix here would never match its own key.
    return f"{clean}-{address.upper().removeprefix('0X')[:8]}"


def register_dynamic_token(symbol: str, address: str, decimals: int) -> str | None:
    """Make a chain-verified token tradable without editing this file.

    Returns the key it was registered under, or ``None`` if the address was
    unusable. Refuses to shadow a builtin symbol with a different address:
    a token is free to call itself PONS, and resolving that name to someone
    else's contract is how a limit order buys the wrong thing.
    """
    plain = symbol.strip().upper()
    if not plain or not _ADDRESS_RE.match(address):
        return None
    builtin = _BUILTIN_TOKENS.get(plain)
    if builtin is not None and builtin.address.lower() != address.lower():
        raise DexConfigError(
            f"refusing to register {plain} at {address}: the registry already "
            f"pins {plain} to {builtin.address}"
        )
    key = dynamic_key(plain, address)
    _DYNAMIC_TOKENS[key] = Token(symbol=key, address=address.lower(), decimals=int(decimals))
    return key


def dynamic_tokens() -> tuple[str, ...]:
    return tuple(sorted(_DYNAMIC_TOKENS))


def dynamic_token_by_address(address: str) -> Token | None:
    wanted = address.lower()
    for token in _DYNAMIC_TOKENS.values():
        if token.address == wanted:
            return token
    return None


def _split_dynamic(key: str) -> tuple[str, str] | None:
    for quote in _DYNAMIC_QUOTES:
        if key.endswith(quote) and len(key) > len(quote):
            return key[: -len(quote)], quote
    return None


def resolve_pair(symbol: str) -> DexPair:
    key = symbol.strip().upper()
    spec = _BUILTIN_PAIRS.get(key)
    if spec is None:
        dynamic = _split_dynamic(key)
        base = _DYNAMIC_TOKENS.get(dynamic[0]) if dynamic else None
        if base is not None:
            return DexPair(
                symbol=key,
                base=base,
                quote=resolve_token(dynamic[1]),
                chain=settings.dex_chain_slug,
                # A hand-registered pair carries a tick size chosen for its
                # price range; a discovered one has no such knowledge, so it
                # gets the finest granularity rather than a made-up rounding.
                tick_size=Decimal(1).scaleb(-18),
                min_order_quote=settings.dex_min_order_quote,
            )
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
