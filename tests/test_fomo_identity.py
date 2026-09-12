from app.fomo.identity import candidate_claims, exclusive_addresses, wallets_per_entry

ALICE = "0x" + "a1" * 20
BOB = "0x" + "b0" * 20
ROUTER = "0x" + "cc" * 20
CUSTODIAL = "0x" + "de" * 20


def test_an_address_is_recorded_against_every_identity_that_mentions_it():
    claims = candidate_claims([[ALICE, ROUTER], [BOB, ROUTER]])

    assert claims[ALICE] == frozenset({0})
    assert claims[ROUTER] == frozenset({0, 1})


def test_addresses_are_matched_however_they_were_cased():
    claims = candidate_claims([[ALICE.upper()], [" " + ALICE + " "]])
    assert claims[ALICE] == frozenset({0, 1})


def test_a_router_every_trader_touches_is_dropped_before_the_lookup():
    # The router is in both traders' JSON, so it names neither -- and it must
    # not even be queried, or it would match and name whoever came first.
    claims = candidate_claims([[ALICE, ROUTER], [BOB, ROUTER]])

    assert exclusive_addresses(claims) == sorted([ALICE, BOB])


def test_the_wallet_that_trades_is_the_one_that_names_the_identity():
    claims = candidate_claims([[CUSTODIAL, ALICE, ROUTER], [BOB, ROUTER]])

    found = wallets_per_entry(claims, {ALICE, BOB}, 2)

    # The custodial address FOMO publishes is simply never in chain_swaps,
    # so it falls away without needing to be recognised as custodial.
    assert found == [(ALICE,), (BOB,)]


def test_an_identity_with_nothing_on_chain_stays_unnamed():
    claims = candidate_claims([[CUSTODIAL]])
    assert wallets_per_entry(claims, {ALICE}, 1) == [()]


def test_two_trading_wallets_under_one_name_are_reported_not_guessed():
    claims = candidate_claims([[ALICE, BOB]])

    assert wallets_per_entry(claims, {ALICE, BOB}, 1) == [tuple(sorted([ALICE, BOB]))]


def test_a_malformed_address_is_ignored_rather_than_stored():
    claims = candidate_claims([["not-an-address", "0x123", ALICE]])
    assert list(claims) == [ALICE]


def test_every_entry_gets_a_slot_even_when_nothing_matched():
    claims = candidate_claims([[ALICE], [], [ROUTER]])
    assert len(wallets_per_entry(claims, set(), 3)) == 3
