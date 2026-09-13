"""Pre-trade health gate for on-chain pairs.

A limit order on a CEX is matched by an exchange that keeps existing. On a DEX
the pool can be drained between the moment a level is armed and the moment the
price touches it, and the two events are usually the same event: price reached
our level *because* the token is dying.

So every gate is checked twice -- absolute floors, and a comparison against the
snapshot taken when the level was armed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.config import settings
from app.dex.dexscreener import MarketSnapshot

__all__ = ["RiskLimits", "RiskVerdict", "evaluate"]


@dataclass(frozen=True)
class RiskLimits:
    min_liquidity_usd: Decimal
    min_volume_h24_usd: Decimal
    max_liquidity_drop_pct: Decimal
    max_volume_drop_pct: Decimal

    @classmethod
    def from_settings(cls) -> "RiskLimits":
        return cls(
            min_liquidity_usd=settings.dex_min_liquidity_usd,
            min_volume_h24_usd=settings.dex_min_volume_h24_usd,
            max_liquidity_drop_pct=settings.dex_max_liquidity_drop_pct,
            max_volume_drop_pct=settings.dex_max_volume_drop_pct,
        )


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reasons": list(self.reasons)}


def _drop_pct(baseline: Decimal, current: Decimal) -> Decimal:
    if baseline <= 0:
        return Decimal("0")
    return (baseline - current) / baseline * Decimal("100")


def evaluate(
    snapshot: MarketSnapshot,
    limits: RiskLimits | None = None,
    *,
    baseline: MarketSnapshot | None = None,
    ignore_liquidity: bool = False,
) -> RiskVerdict:
    """Absolute floors plus collapse-relative-to-baseline checks.

    ``baseline`` is the snapshot stored when the level was armed. Omit it and
    only the absolute floors apply.

    ``ignore_liquidity`` drops every liquidity and volume check -- the floors
    and the collapse comparisons alike -- for an order whose owner has said,
    in as many words, that they are buying a thin coin on purpose. It does not
    drop the price check: a pool that cannot quote a price is not a market with
    poor liquidity, it is not a market, and no order can execute against it.
    """
    limits = limits or RiskLimits.from_settings()
    reasons: list[str] = []

    if not ignore_liquidity:
        if snapshot.token_liquidity_usd < limits.min_liquidity_usd:
            reasons.append(
                f"liquidity ${snapshot.token_liquidity_usd:,.0f} "
                f"< floor ${limits.min_liquidity_usd:,.0f}"
            )
        if snapshot.token_volume_h24 < limits.min_volume_h24_usd:
            reasons.append(
                f"volume24h ${snapshot.token_volume_h24:,.0f} "
                f"< floor ${limits.min_volume_h24_usd:,.0f}"
            )
    if snapshot.price_quote <= 0:
        reasons.append("pool reports a non-positive price")

    if baseline is not None and not ignore_liquidity:
        liquidity_drop = _drop_pct(
            baseline.token_liquidity_usd, snapshot.token_liquidity_usd
        )
        if liquidity_drop > limits.max_liquidity_drop_pct:
            reasons.append(
                f"liquidity fell {liquidity_drop:.1f}% since the level was armed "
                f"(limit {limits.max_liquidity_drop_pct}%)"
            )
        volume_drop = _drop_pct(baseline.token_volume_h24, snapshot.token_volume_h24)
        if volume_drop > limits.max_volume_drop_pct:
            reasons.append(
                f"volume24h fell {volume_drop:.1f}% since the level was armed "
                f"(limit {limits.max_volume_drop_pct}%)"
            )

    return RiskVerdict(ok=not reasons, reasons=tuple(reasons))
