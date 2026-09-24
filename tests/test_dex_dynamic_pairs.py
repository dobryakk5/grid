from decimal import Decimal

import pytest

from app.core.config import settings
from app.dex import tokens as tokens_module
from app.dex.tokens import (
    DexConfigError,
    dynamic_key,
    dynamic_token_by_address,
    dynamic_tokens,
    register_dynamic_token,
    resolve_pair,
    resolve_pair_at,
)

MEME = "0x" + "55" * 20
PONS_REAL = "0x39dbed3a2bd333467115de45665cc57f813c4571"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    # The dynamic registry is module state; a test must not leak into the next.
    monkeypatch.setattr(tokens_module, "_DYNAMIC_TOKENS", {})
    monkeypatch.setattr(settings, "dex_tokens", "")
    monkeypatch.setattr(settings, "dex_chain_slug", "robinhood")
    monkeypatch.setattr(settings, "dex_min_order_quote", Decimal("10"))


def test_a_chain_verified_token_becomes_tradable_against_usdg():
    key = register_dynamic_token("MEME", MEME, 9)

    pair = resolve_pair(f"{key}USDG")

    assert pair.base_coin == key
    assert pair.base.address == MEME
    assert pair.base.decimals == 9
    assert pair.quote_coin == "USDG"
    assert pair.min_order_quote == Decimal("10")


def test_the_same_token_resolves_against_eth_too():
    key = register_dynamic_token("MEME", MEME, 18)
    assert resolve_pair(f"{key}ETH").quote_coin == "ETH"


def test_two_tokens_with_the_same_symbol_stay_distinct():
    # This chain really does carry four DOGGO contracts and three calling
    # themselves USDG. Keyed by symbol alone, the second would silently
    # replace the first and an order would buy the wrong one.
    first = register_dynamic_token("DOGGO", "0x" + "11" * 20, 18)
    second = register_dynamic_token("DOGGO", "0x" + "22" * 20, 6)

    assert first != second
    assert resolve_pair(f"{first}USDG").base.address == "0x" + "11" * 20
    assert resolve_pair(f"{second}USDG").base.address == "0x" + "22" * 20
    assert resolve_pair(f"{second}USDG").base.decimals == 6


def test_a_token_is_found_by_address_not_by_name():
    register_dynamic_token("DOGGO", "0x" + "11" * 20, 18)
    register_dynamic_token("DOGGO", "0x" + "22" * 20, 6)

    found = dynamic_token_by_address("0x" + "22" * 20)

    assert found is not None and found.decimals == 6


def test_the_key_keeps_the_symbol_readable_and_fits_the_column():
    key = dynamic_key("HoodpumpStrategy", "0x" + "ab" * 20)
    assert key.startswith("HOODPUMPSTRA")
    # dex_intents.symbol is String(32) and the quote suffix is appended later.
    assert len(key) + len("USDG") <= 32


def test_a_symbol_of_only_punctuation_still_gets_a_usable_key():
    key = dynamic_key("🚀", "0x" + "cd" * 20)
    assert key.startswith("TOKEN-")


def test_decimals_come_from_the_chain_not_a_default():
    key = register_dynamic_token("SIXDEC", MEME, 6)
    # 1.5 units of a 6-decimals token is 1_500_000 base units; getting this
    # wrong is exactly the mistake the hand-pinned registry existed to avoid.
    assert resolve_pair(f"{key}USDG").base.to_wei(Decimal("1.5")) == 1_500_000


def test_a_builtin_pair_still_wins_over_a_dynamic_one():
    pair = resolve_pair("PONSUSDG")
    assert pair.base.address == PONS_REAL
    assert pair.tick_size == Decimal("0.0001")  # hand-chosen, not the generic one


def test_a_token_cannot_shadow_a_builtin_symbol_at_another_address():
    # Anyone can deploy a contract that calls itself PONS. Resolving that name
    # to their address is how a limit order buys the wrong thing.
    with pytest.raises(DexConfigError) as exc:
        register_dynamic_token("PONS", "0x" + "99" * 20, 18)
    assert "already pins" in str(exc.value)


def test_registering_the_same_builtin_at_its_real_address_is_fine():
    key = register_dynamic_token("PONS", PONS_REAL, 18)
    assert key in dynamic_tokens()


def test_an_unknown_token_still_fails_with_the_known_pair_list():
    with pytest.raises(DexConfigError) as exc:
        resolve_pair("NOSUCHUSDG")
    assert "known pairs" in str(exc.value)


def test_a_malformed_address_is_ignored_rather_than_registered():
    assert register_dynamic_token("JUNK", "not-an-address", 18) is None
    assert not dynamic_tokens()


def test_a_symbol_that_is_only_a_quote_name_is_not_a_pair():
    # "USDG" alone must not split into base "" + quote "USDG".
    with pytest.raises(DexConfigError):
        resolve_pair("USDG")


def test_a_pair_resolves_however_the_symbol_is_cased():
    # resolve_pair() upper-cases what it is handed, so the key has to survive
    # that -- a lower-case hex suffix silently stopped matching itself.
    key = register_dynamic_token("MEME", MEME, 18)

    assert resolve_pair(f"{key}USDG").base.address == MEME
    assert resolve_pair(f"{key}USDG".lower()).base.address == MEME
    assert resolve_pair(f"{key}USDG".upper()).base.address == MEME


def test_a_contract_ground_to_match_a_key_cannot_take_it_over():
    # Same ticker, same first eight hex characters, different contract.
    twin = MEME[:10] + "66" * 16
    key = register_dynamic_token("MEME", MEME, 18)
    with pytest.raises(DexConfigError):
        register_dynamic_token("MEME", twin, 18)
    assert resolve_pair(f"{key}USDG").base.address == MEME


def test_a_pair_is_refused_when_its_key_names_another_contract():
    key = register_dynamic_token("MEME", MEME, 18)
    assert resolve_pair_at(f"{key}USDG", MEME.upper().replace("0X", "0x")).base.address == MEME
    with pytest.raises(DexConfigError):
        resolve_pair_at(f"{key}USDG", "0x" + "77" * 20)
