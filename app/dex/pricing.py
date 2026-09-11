"""Valuing gas in the currency a position is measured in.

Gas is paid in the chain's native coin, which is neither side of a PONS/USDG
trade. Left like that it lands in ``unconverted_fees`` and quietly disappears
from PnL, so every fill converts it -- while keeping the original figure, so the
conversion can be re-derived later or audited against the block.

The rate comes from pools we already read, not from a price oracle:

* the traded pool gives the quote token's USD price, since DexScreener reports
  the base token both in USD and in quote terms;
* the base token's ETH-quoted pool gives the same for the gas coin.

When the pair is already quoted in the gas coin the rate is 1 and nothing is
looked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.dex.dexscreener import DexScreenerClient, MarketSnapshot
from app.dex.tokens import DexPair, native_pair_for

__all__ = ["GasCost", "PricingError", "convert_gas", "implied_quote_usd"]


class PricingError(RuntimeError):
    """No usable rate between the gas coin and the pair's quote token."""


@dataclass(frozen=True)
class GasCost:
    native: Decimal
    coin: str
    quote: Decimal
    quote_coin: str
    # Quote units per one native coin, kept so the conversion stays auditable.
    rate: Decimal


def implied_quote_usd(snapshot: MarketSnapshot) -> Decimal:
    """USD price of the pair's quote token.

    ``price_usd`` is the base token in dollars and ``price_quote`` the same
    token in quote units, so their ratio is what one quote unit is worth.
    """
    if snapshot.price_quote <= 0 or snapshot.price_usd <= 0:
        raise PricingError(
            f"{snapshot.symbol} has no usable USD/quote prices to imply a rate"
        )
    return snapshot.price_usd / snapshot.price_quote


async def convert_gas(
    market: DexScreenerClient,
    pair: DexPair,
    snapshot: MarketSnapshot,
    gas_native: Decimal,
    *,
    native_coin: str = "ETH",
) -> GasCost:
    """Express ``gas_native`` in the pair's quote currency."""
    if pair.quote.native:
        return GasCost(
            native=gas_native,
            coin=native_coin,
            quote=gas_native,
            quote_coin=pair.quote_coin,
            rate=Decimal("1"),
        )

    quote_usd = implied_quote_usd(snapshot)
    try:
        native_snapshot = await market.snapshot(native_pair_for(pair))
    except Exception as exc:
        raise PricingError(
            f"no {pair.base_coin}/{native_coin} pool to price gas against: {exc}"
        ) from None
    native_usd = implied_quote_usd(native_snapshot)

    rate = native_usd / quote_usd
    return GasCost(
        native=gas_native,
        coin=native_coin,
        quote=gas_native * rate,
        quote_coin=pair.quote_coin,
        rate=rate,
    )
