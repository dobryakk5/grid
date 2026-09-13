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
    # CASHCAT has no address baked in, so the pair must not resolve to one.
    with pytest.raises(DexConfigError) as exc:
        resolve_pair("CASHCATUSDG")
    assert "CASHCAT" in str(exc.value)
    assert "DEX_TOKENS" in str(exc.value)


def test_override_supplies_address_and_decimals(monkeypatch):
    monkeypatch.setattr(
        settings,
        "dex_tokens",
        '{"CASHCAT": {"address": "0x' + "ab" * 20 + '", "decimals": 8}}',
    )
    pair = resolve_pair("CASHCATUSDG")
    assert pair.base.address == "0x" + "ab" * 20
    assert pair.base.decimals == 8
    assert pair.base.unit == Decimal("0.00000001")


def test_override_rejects_a_non_address(monkeypatch):
    monkeypatch.setattr(settings, "dex_tokens", '{"CASHCAT": {"address": "0xnope"}}')
    with pytest.raises(DexConfigError):
        resolve_token("CASHCAT")


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


def test_the_robinhood_chain_stable_pair_resolves_without_any_override():
    pair = resolve_pair("PONSUSDG")

    assert pair.quote.address == "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
    assert pair.quote.decimals == 6
    assert pair.base.address == "0x39dbed3a2bd333467115de45665cc57f813c4571"


def test_usdc_is_not_a_pair_on_this_chain():
    # USDC is how capital bridges in; inside Robinhood Chain it is held as USDG.
    with pytest.raises(DexConfigError):
        resolve_pair("PONSUSDC")


def test_eth_quoted_pools_match_by_address_now_that_weth_is_known():
    from app.dex.tokens import native_alias_addresses

    assert "0x0bd7d308f8e1639fab988df18a8011f41eacad73" in native_alias_addresses()


def test_plain_symbol_drops_only_the_address_fragment():
    from app.dex.tokens import plain_symbol

    assert plain_symbol("ICOIN-5D6EF090") == "ICOIN"
    assert plain_symbol("PONS") == "PONS"
    # A dash is legal in a ticker; only an 8-hex tail is the registry's own
    # disambiguator, and only that may be stripped.
    assert plain_symbol("AI-AGENT") == "AI-AGENT"
    assert plain_symbol("") == ""
