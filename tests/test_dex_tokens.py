from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex.tokens import (
    NATIVE_ADDRESS,
    DexConfigError,
    list_pairs,
    resolve_pair,
    resolve_token,
)


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


def test_native_eth_pair_needs_no_extra_configuration():
    pair = resolve_pair("ponseth")
    assert pair.symbol == "PONSETH"
    assert pair.base.symbol == "PONS"
    assert pair.quote.address == NATIVE_ADDRESS
    assert pair.quote.native is True
    assert pair.chain == "robinhood"


def test_unconfigured_token_names_the_env_override():
    with pytest.raises(DexConfigError) as exc:
        resolve_pair("PONSUSDG")
    assert "USDG" in str(exc.value)
    assert "DEX_TOKENS" in str(exc.value)


def test_override_supplies_address_and_decimals(monkeypatch):
    monkeypatch.setattr(
        settings,
        "dex_tokens",
        '{"USDG": {"address": "0x' + "ab" * 20 + '", "decimals": 6}}',
    )
    pair = resolve_pair("PONSUSDG")
    assert pair.quote.address == "0x" + "ab" * 20
    assert pair.quote.decimals == 6
    assert pair.quote.unit == Decimal("0.000001")


def test_override_rejects_a_non_address(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", '{"USDG": {"address": "0xnope"}}')
    with pytest.raises(DexConfigError):
        resolve_token("USDG")


def test_override_rejects_broken_json(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", "{not json")
    with pytest.raises(DexConfigError):
        resolve_token("PONS")


def test_unknown_pair_lists_the_known_ones():
    with pytest.raises(DexConfigError) as exc:
        resolve_pair("DOGEUSDT")
    assert "PONSETH" in str(exc.value)
    assert "PONSETH" in list_pairs()


def test_wei_conversion_roundtrips_at_full_precision():
    token = resolve_token("PONS")
    assert token.to_wei(Decimal("1.5")) == 1_500_000_000_000_000_000
    assert token.from_wei(1_500_000_000_000_000_000) == Decimal("1.5")
