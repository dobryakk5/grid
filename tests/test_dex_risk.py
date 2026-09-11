from decimal import Decimal

from app.dex.dexscreener import MarketSnapshot
from app.dex.risk import RiskLimits, evaluate


LIMITS = RiskLimits(
    min_liquidity_usd=Decimal("5000000"),
    min_volume_h24_usd=Decimal("1000000"),
    max_liquidity_drop_pct=Decimal("40"),
    max_volume_drop_pct=Decimal("60"),
)


def snapshot(*, liquidity="7500000", volume="8000000", price="0.55") -> MarketSnapshot:
    return MarketSnapshot(
        symbol="PONSUSDG",
        observed_at_ms=0,
        price_quote=Decimal(price),
        price_usd=Decimal(price),
        pair_address="0xpair",
        pair_liquidity_usd=Decimal(liquidity),
        token_liquidity_usd=Decimal(liquidity),
        token_volume_h24=Decimal(volume),
        pools_considered=2,
    )


def test_healthy_market_passes():
    assert evaluate(snapshot(), LIMITS).ok


def test_thin_liquidity_is_blocked_with_a_reason():
    verdict = evaluate(snapshot(liquidity="2100000"), LIMITS)
    assert not verdict.ok
    assert any("liquidity" in reason for reason in verdict.reasons)


def test_dead_volume_is_blocked():
    verdict = evaluate(snapshot(volume="400000"), LIMITS)
    assert not verdict.ok
    assert any("volume24h" in reason for reason in verdict.reasons)


def test_price_reaching_the_level_on_a_collapsing_pool_is_blocked():
    baseline = snapshot(liquidity="7500000", volume="8000000", price="0.56")
    current = snapshot(liquidity="6000000", volume="2500000", price="0.55")
    # Both absolute floors still pass; only the collapse guard catches this.
    assert evaluate(current, LIMITS).ok
    verdict = evaluate(current, LIMITS, baseline=baseline)
    assert not verdict.ok
    assert any("fell" in reason for reason in verdict.reasons)


def test_a_mild_drift_does_not_block():
    baseline = snapshot(liquidity="7500000", volume="8000000")
    current = snapshot(liquidity="7100000", volume="6800000")
    assert evaluate(current, LIMITS, baseline=baseline).ok


def test_growing_liquidity_is_never_a_reason_to_block():
    baseline = snapshot(liquidity="5100000", volume="1100000")
    current = snapshot(liquidity="9000000", volume="9000000")
    assert evaluate(current, LIMITS, baseline=baseline).ok
