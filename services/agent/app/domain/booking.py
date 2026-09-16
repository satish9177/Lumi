"""The first real consequential tool: a typed booking proposal.

The action ledger stores a proposal as opaque JSON on purpose -- it must outlive
any one tool's schema. This module is where that JSON becomes a checked type. An
action whose proposal does not parse here never reaches a browser, and the
values that parse here are the only values an approval can ever authorise.

The other half of the job is `changed_facts`. Approval is granted against facts
observed at one moment; a website is free to change them afterwards. Re-reading
the page immediately before the irreversible click, and comparing it against the
*persisted* proposal rather than against anything the page or the worker has
said since, is what stops an approval for one booking from paying for another.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Reference Lumi writes into the site's own booking-reference field. Derived
#: from the action id, so it is stable across attempts and across restarts: the
#: same logical operation always carries the same reference, which is what makes
#: an authoritative lookup after a lost response possible at all. It is
#: deliberately *not* derived from the attempt id -- a second attempt must be
#: recognisable as the same operation, not look like a new one.
REFERENCE_PREFIX = "lumi-"


def booking_reference(action_id: uuid.UUID) -> str:
    return f"{REFERENCE_PREFIX}{action_id}"


class BookingProposal(BaseModel):
    """The exact booking an approval is bound to.

    `extra="forbid"`: an unrecognised field in a stored proposal means the
    proposal was written by something this code does not understand, and
    executing the part it does understand would be a guess.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Which reviewed site this action targets. Not a free-form URL: the worker
    #: resolves it against its own allowlist, so a proposal can never send the
    #: browser somewhere nobody reviewed.
    site: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    slot_id: str = Field(min_length=1, max_length=64)
    doctor: str = Field(min_length=1, max_length=120)
    time: datetime
    price: int = Field(ge=0, le=10_000_000)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @field_validator("time")
    @classmethod
    def _require_offset(cls, value: datetime) -> datetime:
        # A naive timestamp cannot be compared with what a page shows without
        # inventing a timezone, and inventing one is how an appointment moves.
        if value.tzinfo is None:
            raise ValueError("time must carry a UTC offset")
        return value


class ObservedSlot(BaseModel):
    """What the page said, this time, about the slot being booked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str
    doctor: str
    time: datetime
    price: int
    currency: str


@dataclass(frozen=True, slots=True)
class ChangedFact:
    field: str
    approved: str
    observed: str


def changed_facts(proposal: BookingProposal, observed: ObservedSlot) -> tuple[ChangedFact, ...]:
    """Consequential values that no longer match what was approved.

    Times are compared as instants, so an equivalent time written in a different
    offset is not a change. Everything else is compared exactly: a doctor whose
    name is spelled differently is a different booking until a human says it is
    not.
    """
    differences: list[ChangedFact] = []
    if proposal.slot_id != observed.slot_id:
        differences.append(ChangedFact("slot_id", proposal.slot_id, observed.slot_id))
    if proposal.doctor != observed.doctor:
        differences.append(ChangedFact("doctor", proposal.doctor, observed.doctor))
    if proposal.time != observed.time:
        differences.append(
            ChangedFact("time", proposal.time.isoformat(), observed.time.isoformat())
        )
    if proposal.price != observed.price:
        differences.append(ChangedFact("price", str(proposal.price), str(observed.price)))
    if proposal.currency != observed.currency:
        differences.append(ChangedFact("currency", proposal.currency, observed.currency))
    return tuple(differences)


def describe_changes(changes: tuple[ChangedFact, ...]) -> list[dict[str, Any]]:
    """A bounded, structured description for the timeline. No page text."""
    return [
        {"field": change.field, "approved": change.approved, "observed": change.observed}
        for change in changes
    ]


class BookingProposalError(ValueError):
    """A persisted proposal that this tool cannot safely execute."""

    def __init__(self, action_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"Action {action_id} does not hold a valid booking proposal: {reason}")
        self.action_id = action_id
        self.reason = reason


def parse_booking_proposal(action_id: uuid.UUID, proposal: dict[str, Any]) -> BookingProposal:
    try:
        return BookingProposal.model_validate(proposal)
    except ValueError as error:
        raise BookingProposalError(action_id, str(error)) from None
