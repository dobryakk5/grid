"""Пин-лист: что стало монетой, а что честно отвергнуто."""
from app.core.config import settings
from app.intel.watchlist import parse

EMBER = "5dvXTZ5qwgafnHtwu3Ls3QrWx1U4LQsFeCuJgkk4QEC6"
SOLANA = 1399811149


def test_a_chain_may_be_named_by_slug_or_by_number():
    keys, rejected = parse(f"solana:{EMBER}, {settings.rh_chain_id}:0x{'ab' * 20}")
    assert keys == [(SOLANA, EMBER), (settings.rh_chain_id, "0x" + "ab" * 20)]
    assert rejected == []


def test_entries_may_be_separated_by_commas_or_newlines():
    keys, rejected = parse(f"\n  solana:{EMBER}\n  solana:{EMBER[:-1]}A\n")
    assert len(keys) == 2 and rejected == []


def test_an_address_from_the_wrong_family_is_refused_not_collected():
    # Solana-минт, записанный с EVM-сетью, -- это опечатка, а не монета: молча
    # взяв её, мы бы неделю показывали пустую строку вместо ошибки.
    keys, rejected = parse(f"1:{EMBER}")
    assert keys == [] and rejected == [f"1:{EMBER}"]


def test_a_chain_nothing_can_collect_is_refused():
    keys, rejected = parse("77777:0x" + "cd" * 20)
    assert keys == [] and rejected == ["77777:0x" + "cd" * 20]


def test_an_entry_without_a_chain_is_refused():
    keys, rejected = parse(EMBER)
    assert keys == [] and rejected == [EMBER]


def test_the_same_coin_twice_is_one_coin():
    keys, _rejected = parse(f"solana:{EMBER},solana:{EMBER}")
    assert keys == [(SOLANA, EMBER)]


def test_good_entries_survive_a_bad_neighbour():
    # Одна опечатка не должна отменять остальной список -- и не должна пройти
    # незамеченной.
    keys, rejected = parse(f"solana:{EMBER},мусор,solana:{EMBER[:-1]}B")
    assert len(keys) == 2 and rejected == ["мусор"]


def test_an_empty_setting_is_a_working_state():
    assert parse("") == ([], [])
    assert parse("   ") == ([], [])
