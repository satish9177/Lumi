"""Deterministic identifier redaction (Milestone 8a S3).

The redactor is a reduction in exposure, never anonymisation. These tests pin
what it hides, what it deliberately leaves alone (an answer that cannot cite a
figure is not an answer), and the two properties everything else leans on:
placeholders are stable within one observation, and redacted text is a fixed
point -- running the redactor again changes nothing, which is what lets the
runtime refuse any worker output that still contains an identifier.
"""

import pytest

from app.browser.operations.authenticated import project_text
from app.domain.redaction import Redactor, is_redacted, luhn_valid

PLANTED = {
    "satish@example.test": "⟦email:1⟧",
    "+91 9876543210": "⟦phone:1⟧",
    "123456789012345": "⟦digits:2345⟧",
    "4242424242424242": "⟦card:4242⟧",
}


@pytest.mark.parametrize(("raw", "expected"), list(PLANTED.items()))
def test_each_planted_identifier_is_replaced(raw: str, expected: str) -> None:
    assert Redactor().redact(f"value {raw} end") == f"value {expected} end"


def test_the_spec_sample_survives_with_its_ordinary_number() -> None:
    text = "satish@example.test\n+91 9876543210\n123456789012345\n4242424242424242\nThere are 17 private repositories."
    redacted = Redactor().redact(text)
    for raw in PLANTED:
        assert raw not in redacted
    assert "There are 17 private repositories." in redacted


@pytest.mark.parametrize(
    "ordinary",
    [
        "There are 17 private repositories.",
        "Updated 2026-09-20 10:30",
        "Total 1,234,567 stars",
        "v1.2.3 build 20260920",
        "5 seats, 12 members, 3 owners",
        "Invoice total 4,999.00 USD",
    ],
)
def test_ordinary_quantities_and_dates_are_left_alone(ordinary: str) -> None:
    assert Redactor().redact(ordinary) == ordinary


def test_only_luhn_valid_card_shaped_numbers_are_cards() -> None:
    assert luhn_valid("4242424242424242")
    assert not luhn_valid("123456789012345")
    assert Redactor().redact("4242 4242 4242 4242") == "⟦card:4242⟧"
    assert Redactor().redact("4242-4242-4242-4242") == "⟦card:4242⟧"
    # A long identifier that is *not* a card is still a long identifier.
    assert Redactor().redact("1234567890123456") == "⟦digits:3456⟧"


def test_nine_digits_is_the_threshold_for_a_bare_number() -> None:
    assert Redactor().redact("12345678") == "12345678"
    assert Redactor().redact("123456789") == "⟦digits:6789⟧"


@pytest.mark.parametrize("phone", ["(415) 555-2671", "555-123-4567", "+44 20 7946 0958", "+1 415 555 2671"])
def test_phone_shaped_identifiers_are_replaced(phone: str) -> None:
    assert "⟦phone:" in Redactor().redact(f"call {phone} now")


def test_placeholders_are_stable_within_one_observation() -> None:
    redactor = Redactor()
    first = redactor.redact("a@example.test wrote to b@example.test and a@example.test")
    assert first == "⟦email:1⟧ wrote to ⟦email:2⟧ and ⟦email:1⟧"
    assert redactor.redact("again a@example.test") == "again ⟦email:1⟧"
    assert Redactor().redact("b@example.test") == "⟦email:1⟧"  # A new observation starts again.
    assert redactor.counts["email"] == 4


@pytest.mark.parametrize("text", list(PLANTED) + ["a@example.test and 4242 4242 4242 4242 and (415) 555-2671"])
def test_redacted_text_is_a_fixed_point(text: str) -> None:
    once = Redactor().redact(text)
    assert is_redacted(once)
    assert Redactor().redact(once) == once
    assert not is_redacted(text)


def test_case_and_dots_in_an_address_do_not_defeat_it() -> None:
    assert Redactor().redact("Contact First.Last+tag@Example.Test today") == "Contact ⟦email:1⟧ today"


def test_an_identifier_is_not_cut_in_half_by_a_block_boundary() -> None:
    """Whole lines are redacted *before* they are split into blocks, so a long
    number that straddles a boundary never survives as two short runs."""
    line = ("word " * 98) + "12345678901234 tail"
    blocks, _truncated, _total = project_text(line, Redactor(), max_chars=4_000, max_blocks=60)
    assert "12345678901234" not in "\n".join(text for text, _ in blocks)
    joined = " ".join(text for text, _ in blocks)
    assert "⟦digits:1234⟧" in joined
    assert all(len(text) <= 500 for text, _ in blocks)


def test_the_text_and_block_budgets_bind_after_redaction() -> None:
    raw = "\n".join(f"line {index} with data" for index in range(200))
    blocks, truncated, total = project_text(raw, Redactor(), max_chars=300, max_blocks=60)
    assert sum(len(text) for text, _ in blocks) <= 300 and truncated is True
    assert total >= 300
    blocks, truncated, _ = project_text(raw, Redactor(), max_chars=4_000, max_blocks=5)
    assert len(blocks) == 5 and truncated is True


def test_redaction_is_a_reduction_not_anonymisation() -> None:
    """Names, usernames and short account numbers are *not* touched, and this
    test exists so nobody 'fixes' the docs into claiming otherwise."""
    survivors = "Satish Kumar, user satish9177, order 1234, account ab-12"
    assert Redactor().redact(survivors) == survivors
    import app.domain.redaction as module

    doc = module.__doc__ or ""
    assert "not anonymisation" in doc
    assert "de-identified" in doc  # named only to say it must never be claimed
