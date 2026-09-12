from decimal import Decimal

import pytest

from app.intel.events import classify, extract_numbers


def test_a_project_claim_with_a_figure_is_the_loud_case():
    reading = classify("First buyback executed 3,683,048 DIVI for $15,000 STRCx")
    assert reading["kinds"] == ["BUYBACK"]
    assert reading["importance"] == "HIGH" and reading["verifiable"] is True
    assert reading["numbers"]["usd_max"] == "15000"
    assert reading["stance"] == "POSITIVE"


def test_a_trader_narrating_their_own_position_is_not_a_project_claim():
    reading = classify("Добрал на откате, стоп под локальным минимумом")
    assert reading["verifiable"] is False and reading["importance"] == "LOW"


@pytest.mark.parametrize("text,expected", [
    ("$15,000", "15000"),          # thousands separator
    ("$15,5", "15.5"),             # decimal comma, same feed
    ("15k$", "15000"),
    ("1.2m usd", "1200000.0"),
    ("выкупили на 250 000 USD", "250000"),
])
def test_the_shapes_people_actually_type_are_all_dollars(text, expected):
    assert extract_numbers(text)["usd_max"] == expected


def test_a_token_count_is_not_a_dollar_figure():
    # No currency marker anywhere: 3,683,048 DIVI must not become $3.6m.
    assert "usd" not in extract_numbers("burned 3,683,048 DIVI")


def test_percents_and_tickers_are_kept_apart_from_money():
    numbers = extract_numbers("сожгли 5% supply, беру $DIVI и $STRCX")
    assert numbers["percent"] == ["5"] and numbers["tickers"] == ["DIVI", "STRCX"]
    assert "usd" not in numbers


def test_an_unreadable_note_is_left_for_the_model_rather_than_guessed():
    reading = classify("wen")
    assert reading["kinds"] == [] and reading["confidence"] == Decimal("0")


def test_empty_text_is_not_an_event():
    assert classify("   ")["kinds"] == [] and classify(None)["importance"] == "LOW"


def test_a_buyback_is_not_also_read_as_the_author_buying():
    # "выкупили" contains "купил": plain substring matching turned a project
    # announcement into the author narrating their own entry.
    assert classify("Проект выкупили на $15,000")["kinds"] == ["BUYBACK"]
    assert classify("Купил на просадке")["kinds"] == ["ENTRY"]
