from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.api.routes import ProfilePayload


def payload(**overrides):
    values = {
        "name": "BTC grid",
        "symbol": "BTCUSDT",
        "lower_price": Decimal("62000"),
        "upper_price": Decimal("67000"),
        "step_price": Decimal("1000"),
        "quote_per_level": Decimal("25"),
    }
    values.update(overrides)
    return ProfilePayload(**values)


def test_existing_payload_defaults_to_accumulation_arithmetic():
    profile = payload()
    assert profile.strategy == "accumulation"
    assert profile.grid_mode == "arithmetic"


def test_geometric_profile_and_budget_validation():
    profile = payload(
        strategy="classic",
        grid_mode="geometric",
        step_percent=Decimal("2"),
        max_investment=Decimal("125"),
    )
    assert profile.strategy == "classic"

    with pytest.raises(ValidationError, match="max_investment"):
        payload(max_investment=Decimal("100"))


def test_price_guards_must_be_outside_grid():
    with pytest.raises(ValidationError, match="stop_loss"):
        payload(stop_loss=Decimal("63000"))
    with pytest.raises(ValidationError, match="take_profit"):
        payload(take_profit=Decimal("66000"))
