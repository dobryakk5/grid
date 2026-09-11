import pytest

from app.fomo.seed import SeedError, parse_seed

ADDRESS_A = "0x" + "aa" * 20
ADDRESS_B = "0x" + "bb" * 20


def test_full_row_is_parsed():
    wallets = parse_seed({"wallets": [
        {"address": ADDRESS_A, "fomo_user_id": "u1", "handle": "alice", "display_name": "Alice"},
    ]})

    assert len(wallets) == 1
    assert wallets[0].fomo_user_id == "u1"
    assert wallets[0].evm_address == ADDRESS_A
    assert wallets[0].user_handle == "alice"
    assert wallets[0].display_name == "Alice"


def test_wallet_without_a_fomo_id_gets_a_stable_synthetic_one():
    first = parse_seed({"wallets": [{"address": ADDRESS_A}]})
    again = parse_seed({"wallets": [{"address": ADDRESS_A.upper().replace("0X", "0x")}]})

    assert first[0].fomo_user_id == f"manual:{ADDRESS_A.lower()}"
    # Same wallet in different casing must not become a second registry row.
    assert again[0].fomo_user_id == first[0].fomo_user_id


def test_a_bare_address_string_is_accepted():
    wallets = parse_seed({"wallets": [ADDRESS_A]})
    assert wallets[0].evm_address == ADDRESS_A
    assert wallets[0].user_handle is None


def test_a_bare_top_level_array_is_accepted():
    wallets = parse_seed([{"address": ADDRESS_A}, {"address": ADDRESS_B}])
    assert [w.evm_address for w in wallets] == [ADDRESS_A, ADDRESS_B]


def test_unknown_keys_such_as_note_are_ignored():
    wallets = parse_seed({"wallets": [{"address": ADDRESS_A, "note": "found via bench"}]})
    assert wallets[0].evm_address == ADDRESS_A


def test_blank_handle_becomes_none_rather_than_an_empty_string():
    wallets = parse_seed({"wallets": [{"address": ADDRESS_A, "handle": "   "}]})
    assert wallets[0].user_handle is None


def test_a_malformed_address_is_rejected_before_it_reaches_the_database():
    with pytest.raises(SeedError) as exc:
        parse_seed({"wallets": [{"address": "0xnothex"}]})
    assert "not a 0x-prefixed" in str(exc.value)


def test_a_missing_address_is_rejected():
    with pytest.raises(SeedError):
        parse_seed({"wallets": [{"handle": "alice"}]})


def test_a_repeated_address_is_rejected():
    with pytest.raises(SeedError) as exc:
        parse_seed({"wallets": [{"address": ADDRESS_A}, {"address": ADDRESS_A}]})
    assert "repeats address" in str(exc.value)


def test_a_wrong_shaped_document_is_rejected():
    with pytest.raises(SeedError):
        parse_seed("just a string")
    with pytest.raises(SeedError):
        parse_seed({"wallets": "not an array"})


def test_the_checked_in_seed_file_is_valid():
    # The shipped config must always parse -- a typo here would only surface
    # at deploy time otherwise.
    from pathlib import Path

    from app.fomo.seed import load_seed_file

    path = Path(__file__).resolve().parents[1] / "config" / "fomo_wallets.json"
    wallets = load_seed_file(path)
    assert wallets, "config/fomo_wallets.json should carry at least one wallet"
    assert all(w.evm_address.startswith("0x") for w in wallets)
