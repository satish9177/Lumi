"""Unit tests for the typed booking proposal and changed-value detection.

An approval authorises specific values. These tests fix what counts as "the same
booking" and what counts as a different one, because that judgement is the only
thing standing between a granted approval and a booking nobody agreed to.
"""

import uuid

import pytest

from app.domain.booking import (
    REFERENCE_PREFIX,
    BookingProposal,
    BookingProposalError,
    ObservedSlot,
    booking_reference,
    changed_facts,
    describe_changes,
    parse_booking_proposal,
)

APPROVED = {
    "site": "appointment_fixture",
    "slot_id": "slot-a-1830",
    "doctor": "Dr A",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
    "currency": "INR",
}


def _proposal(**overrides: object) -> BookingProposal:
    return BookingProposal.model_validate({**APPROVED, **overrides})


def _observed(proposal: BookingProposal, **overrides: object) -> ObservedSlot:
    base = {
        "slot_id": proposal.slot_id,
        "doctor": proposal.doctor,
        "time": proposal.time,
        "price": proposal.price,
        "currency": proposal.currency,
    }
    return ObservedSlot.model_validate({**base, **overrides})


# ---- the reference ----------------------------------------------------------


def test_the_reference_is_stable_for_one_action() -> None:
    """Stability is what makes reconciliation after a lost response possible."""
    action_id = uuid.uuid4()
    assert booking_reference(action_id) == booking_reference(action_id)
    assert booking_reference(action_id) != booking_reference(uuid.uuid4())
    assert booking_reference(action_id).startswith(REFERENCE_PREFIX)


def test_the_reference_fits_the_sites_reference_field() -> None:
    from evals.sites.appointments.app import MAX_REFERENCE_LENGTH

    assert len(booking_reference(uuid.uuid4())) <= MAX_REFERENCE_LENGTH


# ---- parsing ----------------------------------------------------------------


def test_a_well_formed_proposal_parses() -> None:
    proposal = parse_booking_proposal(uuid.uuid4(), APPROVED)
    assert proposal.slot_id == "slot-a-1830"
    assert proposal.price == 800


def test_a_proposal_is_frozen_after_parsing() -> None:
    """Nothing downstream can edit the values an approval was bound to."""
    proposal = _proposal()
    with pytest.raises(ValueError):
        proposal.price = 950  # type: ignore[misc]


@pytest.mark.parametrize(
    "broken",
    [
        {**APPROVED, "extra_field": "surprise"},
        {**APPROVED, "price": -1},
        {**APPROVED, "currency": "rupees"},
        {**APPROVED, "time": "2026-09-19T18:30:00"},  # no offset
        {key: value for key, value in APPROVED.items() if key != "price"},
        {},
    ],
)
def test_a_proposal_this_tool_cannot_understand_is_refused(broken: dict[str, object]) -> None:
    """Executing the part that parsed would be executing a guess."""
    with pytest.raises(BookingProposalError):
        parse_booking_proposal(uuid.uuid4(), broken)


def test_a_naive_time_is_refused_rather_than_assigned_a_timezone() -> None:
    with pytest.raises(BookingProposalError, match="offset"):
        parse_booking_proposal(uuid.uuid4(), {**APPROVED, "time": "2026-09-19T18:30:00"})


# ---- change detection -------------------------------------------------------


def test_an_unchanged_page_produces_no_differences() -> None:
    proposal = _proposal()
    assert changed_facts(proposal, _observed(proposal)) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", 950),
        ("doctor", "Dr B"),
        ("slot_id", "slot-b-1915"),
        ("currency", "USD"),
        ("time", "2026-09-19T19:15:00+05:30"),
    ],
)
def test_every_consequential_field_is_checked(field: str, value: object) -> None:
    proposal = _proposal()
    changes = changed_facts(proposal, _observed(proposal, **{field: value}))
    assert [change.field for change in changes] == [field]


def test_the_same_instant_in_another_offset_is_not_a_change() -> None:
    """Otherwise a site that renders in UTC would block every booking."""
    proposal = _proposal()
    assert changed_facts(proposal, _observed(proposal, time="2026-09-19T13:00:00+00:00")) == ()


def test_several_changes_are_all_reported() -> None:
    proposal = _proposal()
    changes = changed_facts(proposal, _observed(proposal, price=950, doctor="Dr B"))
    assert {change.field for change in changes} == {"price", "doctor"}


def test_changes_describe_both_sides_for_the_timeline() -> None:
    proposal = _proposal()
    described = describe_changes(changed_facts(proposal, _observed(proposal, price=950)))
    assert described == [{"field": "price", "approved": "800", "observed": "950"}]
