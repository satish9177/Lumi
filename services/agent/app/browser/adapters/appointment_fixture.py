"""Reviewed adapter for the deterministic appointment fixture.

Locator policy, and why it is a policy rather than a style preference:

* Controls are found by **role and accessible name** (`Confirm booking`) or by
  **label** (`Booking reference`). Those are the things a site cannot change
  without changing what a human sees, so a locator that breaks is a signal, not
  noise.
* Values an approval is bound to are read from **stable semantic attributes**
  (`data-testid`, plus `data-iso`, `data-amount`, `data-currency`). Re-deriving a
  price by parsing rendered prose is how a currency symbol becomes a wrong
  number.
* Locators are **re-resolved after every navigation**, and the facts are
  re-read from freshly resolved locators immediately before the irreversible
  click. Nothing is carried across a page transition -- not an element handle,
  and not a value observed on an earlier page.
* Playwright's actionability checks are used as a *precondition*, never as
  authority. "This button is visible and enabled" is a statement about a web
  page. Authority to press it lives in Lumi's action ledger and nowhere else.

Page text is data throughout. The adapter reads specific fields from specific
elements; it never treats prose on the page as an instruction, and there is no
code path by which page content can change what operation runs, what is
approved, or what is submitted.
"""

import re
import uuid
from urllib.parse import quote
from datetime import datetime
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, ConfigDict, Field

from app.browser.protocol import LookupStatus, OperationStatus
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationContext,
    OperationResult,
    Reconciliation,
    RetryPolicy,
)
from app.domain.booking import (
    BookingProposal,
    ObservedSlot,
    changed_facts,
    describe_changes,
)

SITE_NAME = "appointment_fixture"

#: A booking id on the confirmation page. Used to prove the postcondition, and
#: to wait for the *confirmation*, not merely for the click to return.
_BOOKING_ID = re.compile(r"^BK-\d{4}$")
_CONFIRMATION_URL = re.compile(r"/bookings/BK-\d{4}$")


# ---- typed schemas ----------------------------------------------------------


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specialty: str = Field(default="", max_length=120)
    day: str = Field(default="", max_length=40)


class SlotSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str
    doctor: str
    specialty: str
    time: datetime
    price: int
    currency: str


class SearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    slots: list[SlotSummary]


class SlotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=64)


class SlotOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    available: bool
    slot: SlotSummary | None = None
    confirm_button_visible: bool = False
    confirm_button_enabled: bool = False


class PrepareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=64)
    reference: str = Field(min_length=1, max_length=80)


class PrepareOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    ready: bool
    slot: SlotSummary | None = None
    reference_field_filled: bool = False
    confirm_button_enabled: bool = False


class CommitInput(BaseModel):
    """The approved proposal, plus the reference Lumi derived from the action.

    The worker is handed values, not authority. It re-observes the page and
    refuses if these no longer match; it has no way to amend them, because the
    only thing it can do with a mismatch is report one.
    """

    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=80)
    proposal: BookingProposal


class CommitOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    booking_id: str | None = None
    reference: str
    submitted: bool
    postcondition_verified: bool = False
    changed_facts: list[dict[str, Any]] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None


class LookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=80)


class LookupOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    result: LookupStatus
    booking_count: int = 0
    booking_id: str | None = None
    booking: dict[str, Any] | None = None


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    specialty: str = Field(default="", max_length=60)
    doctor: str = Field(default="", max_length=60)


class DoctorProfileSummary(BaseModel):
    """Typed public facts about one doctor. Nothing else leaves the page."""

    model_config = ConfigDict(extra="forbid")

    doctor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    doctor: str = Field(min_length=1, max_length=120)
    specialty: str = Field(max_length=120)
    clinic: str = Field(max_length=120)
    address: str = Field(max_length=200)
    hours: str = Field(max_length=80)
    consultation_fee: int = Field(ge=0, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    languages: list[str] = Field(max_length=12)
    walk_ins: bool


class ProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    origin: str
    page: str
    profiles: list[DoctorProfileSummary]


# ---- page reading -----------------------------------------------------------


async def _text(locator: Locator) -> str:
    return (await locator.inner_text()).strip()


async def _required_attribute(locator: Locator, name: str) -> str:
    value = await locator.get_attribute(name)
    if value is None:
        raise PlaywrightError(f"expected attribute {name!r} was missing from the page")
    return value


async def _read_slot(page: Page, *, from_card: Locator | None = None) -> SlotSummary:
    """Read one slot's consequential facts from freshly resolved locators."""
    scope: Page | Locator = from_card if from_card is not None else page
    price = scope.get_by_test_id("slot-price")
    return SlotSummary(
        slot_id=(
            await _required_attribute(from_card, "data-slot-id")
            if from_card is not None
            else await _text(page.get_by_test_id("slot-id"))
        ),
        doctor=await _text(scope.get_by_test_id("slot-doctor")),
        specialty=(
            await _text(scope.get_by_test_id("slot-specialty"))
            if await scope.get_by_test_id("slot-specialty").count()
            else ""
        ),
        time=datetime.fromisoformat(
            await _required_attribute(scope.get_by_test_id("slot-time"), "data-iso")
        ),
        price=int(await _required_attribute(price, "data-amount")),
        currency=await _required_attribute(price, "data-currency"),
    )


async def _slot_is_unavailable(page: Page) -> bool:
    return await page.get_by_test_id("slot-unavailable").count() > 0


def _observed(slot: SlotSummary) -> ObservedSlot:
    return ObservedSlot(
        slot_id=slot.slot_id,
        doctor=slot.doctor,
        time=slot.time,
        price=slot.price,
        currency=slot.currency,
    )


# ---- operations -------------------------------------------------------------


async def search_appointments(context: OperationContext, payload: SearchInput) -> OperationResult:
    page = context.page
    await page.goto(f"{context.origin}/", wait_until="domcontentloaded")
    await page.get_by_label("Specialty").fill(payload.specialty)
    if payload.day:
        await page.get_by_label("Day").select_option(payload.day)
    await page.get_by_role("button", name="Search appointments").click()
    # Re-resolve after the navigation. Nothing from the form page survives it.
    await page.get_by_role("heading", name="Available appointments").wait_for()

    cards = page.get_by_test_id("slot-card")
    slots = [await _read_slot(page, from_card=cards.nth(index)) for index in range(await cards.count())]
    output = SearchOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="search_results",
        slots=slots,
    )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


async def read_available_slots(context: OperationContext, payload: SlotInput) -> OperationResult:
    page = context.page
    await page.goto(f"{context.origin}/slots/{payload.slot_id}", wait_until="domcontentloaded")
    if await _slot_is_unavailable(page):
        output = SlotOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="slot_unavailable",
            available=False,
        )
        return OperationResult(
            OperationStatus.RESOURCE_UNAVAILABLE, output.model_dump(mode="json"), "slot_unavailable"
        )

    confirm = page.get_by_role("button", name="Confirm booking")
    output = SlotOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="slot_detail",
        available=True,
        slot=await _read_slot(page),
        confirm_button_visible=await confirm.is_visible(),
        confirm_button_enabled=await confirm.is_enabled(),
    )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


async def prepare_booking(context: OperationContext, payload: PrepareInput) -> OperationResult:
    """Fill the form and report readiness. Sends nothing the site acts on."""
    page = context.page
    await page.goto(f"{context.origin}/slots/{payload.slot_id}", wait_until="domcontentloaded")
    if await _slot_is_unavailable(page):
        output = PrepareOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="slot_unavailable",
            ready=False,
        )
        return OperationResult(
            OperationStatus.RESOURCE_UNAVAILABLE, output.model_dump(mode="json"), "slot_unavailable"
        )

    reference_field = page.get_by_label("Booking reference")
    await reference_field.fill(payload.reference)
    confirm = page.get_by_role("button", name="Confirm booking")
    output = PrepareOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="slot_detail",
        ready=True,
        slot=await _read_slot(page),
        reference_field_filled=(await reference_field.input_value()) == payload.reference,
        confirm_button_enabled=await confirm.is_enabled(),
    )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


async def commit_booking(context: OperationContext, payload: CommitInput) -> OperationResult:
    """The one consequential operation. Everything before the click is refusal logic.

    Order matters and is not negotiable:

    1. Navigate fresh. Never book from a page left over from preparation.
    2. Refuse if the slot is gone.
    3. Read the facts, compare against the *approved* proposal, refuse on any
       difference. The approval authorises those values, not this slot.
    4. Fill the reference Lumi derived from the action.
    5. Re-read the facts from newly resolved locators and compare again, because
       the page may have changed between step 3 and here.
    6. Only now set `submitted` and click.
    7. Prove the booking exists by reading a receipt id off the confirmation
       page. A click that returned is not a booking.
    """
    page = context.page
    proposal = payload.proposal
    await page.goto(f"{context.origin}/slots/{proposal.slot_id}", wait_until="domcontentloaded")

    if await _slot_is_unavailable(page):
        output = CommitOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="slot_unavailable",
            reference=payload.reference,
            submitted=False,
        )
        return OperationResult(
            OperationStatus.RESOURCE_UNAVAILABLE, output.model_dump(mode="json"), "slot_unavailable"
        )

    observed = await _read_slot(page)
    changes = changed_facts(proposal, _observed(observed))
    if changes:
        return _changed_resource(context, payload, changes)

    await page.get_by_label("Booking reference").fill(payload.reference)

    # Re-observation immediately before the point of no return.
    re_observed = await _read_slot(page)
    changes = changed_facts(proposal, _observed(re_observed))
    if changes:
        return _changed_resource(context, payload, changes)

    confirm = page.get_by_role("button", name="Confirm booking")
    # An actionability check, and nothing more than that. It says the page is
    # ready to receive a click; the approval already said Lumi may make one.
    if not await confirm.is_enabled():
        output = CommitOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="slot_detail",
            reference=payload.reference,
            submitted=False,
        )
        return OperationResult(
            OperationStatus.FAILED_BEFORE_EFFECT,
            output.model_dump(mode="json"),
            "confirm_button_not_actionable",
        )

    context.submitted = True
    # The flag is set first, and the navigation is awaited as part of the click,
    # so there is no window in which a submission has gone out while this
    # dispatch still believes it has done nothing.
    async with page.expect_navigation(wait_until="domcontentloaded"):
        await confirm.click()

    # Re-resolve on whatever page the submission landed on. Nothing from the
    # form page is reused here.
    rejection = page.get_by_test_id("booking-rejected")
    if await rejection.count():
        # The site itself said no, after the button was pressed. That is a
        # definitive negative acknowledgement, not a lost response: the page
        # exists precisely to say that nothing was booked. `submitted` stays
        # true so the ledger records that a button really was pressed.
        output = CommitOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="booking_rejected",
            reference=payload.reference,
            submitted=True,
        )
        return OperationResult(
            OperationStatus.RESOURCE_UNAVAILABLE,
            output.model_dump(mode="json"),
            "rejected_by_site",
        )

    if not _CONFIRMATION_URL.search(page.url):
        # Neither a confirmation nor a refusal. Lumi cannot say what happened.
        output = CommitOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="unrecognised",
            reference=payload.reference,
            submitted=True,
        )
        return OperationResult(
            OperationStatus.OUTCOME_UNKNOWN,
            output.model_dump(mode="json"),
            "unrecognised_page_after_submission",
        )

    # Postcondition: a receipt identifier, on a confirmation page, carrying the
    # reference we submitted. Any one of those alone could be a coincidence.
    booking_id = await _text(page.get_by_test_id("receipt-booking-id"))
    receipt_reference = await _text(page.get_by_test_id("receipt-reference"))
    receipt_price = int(
        await _required_attribute(page.get_by_test_id("receipt-price"), "data-amount")
    )
    receipt_currency = await _required_attribute(
        page.get_by_test_id("receipt-price"), "data-currency"
    )
    receipt_doctor = await _text(page.get_by_test_id("receipt-doctor"))
    verified = bool(_BOOKING_ID.match(booking_id)) and receipt_reference == payload.reference

    output = CommitOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="confirmation",
        booking_id=booking_id if verified else None,
        reference=payload.reference,
        submitted=True,
        postcondition_verified=verified,
        receipt={
            "booking_id": booking_id,
            "reference": receipt_reference,
            "doctor": receipt_doctor,
            "price": receipt_price,
            "currency": receipt_currency,
        },
    )
    if not verified:
        # The submission went out and the page did not prove a booking. Whether
        # one exists is exactly the question reconciliation answers.
        return OperationResult(
            OperationStatus.OUTCOME_UNKNOWN,
            output.model_dump(mode="json"),
            "postcondition_not_verified",
        )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


def _changed_resource(
    context: OperationContext, payload: CommitInput, changes: tuple[Any, ...]
) -> OperationResult:
    output = CommitOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="slot_detail",
        reference=payload.reference,
        submitted=False,
        changed_facts=describe_changes(changes),
    )
    return OperationResult(
        OperationStatus.CHANGED_RESOURCE, output.model_dump(mode="json"), "approved_values_changed"
    )


async def lookup_booking(context: OperationContext, payload: LookupInput) -> OperationResult:
    """Read-only reconciliation. Navigates, reads, and books nothing.

    There is no form submission here and no button press: the lookup page is a
    GET. That is the property that makes it safe to run against an action whose
    outcome is unknown -- running it cannot create the thing it is looking for.

    `NOT_FOUND` is reported as an authoritative absence. That is only sound
    because of what this fixture guarantees: one writer, a booking visible to
    the very next lookup, and no expiry or hiding of bookings. The caller, not
    this function, decides whether a given site's absence may be trusted.
    """
    page = context.page
    await page.goto(
        f"{context.origin}/bookings/lookup?reference={payload.reference}",
        wait_until="domcontentloaded",
    )

    if await page.get_by_test_id("lookup-unavailable").count():
        output = LookupOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="lookup_unavailable",
            result=LookupStatus.UNKNOWN,
        )
        return OperationResult(
            OperationStatus.OK, output.model_dump(mode="json"), "lookup_unavailable"
        )

    if await page.get_by_test_id("lookup-not-found").count():
        output = LookupOutput(
            observation_id=context.observation_id,
            origin=context.origin,
            page="lookup_not_found",
            result=LookupStatus.NOT_FOUND,
        )
        return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))

    found = page.get_by_test_id("lookup-found")
    count = int(await _required_attribute(found, "data-count"))
    booking_id = await _text(page.get_by_test_id("lookup-booking-id").first)
    output = LookupOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="lookup_found",
        result=LookupStatus.FOUND,
        booking_count=count,
        booking_id=booking_id,
        booking={
            "booking_id": booking_id,
            "reference": await _text(page.get_by_test_id("lookup-reference").first),
            "slot_id": await _text(page.get_by_test_id("lookup-slot-id").first),
            "doctor": await _text(page.get_by_test_id("lookup-doctor").first),
            "time": await _required_attribute(
                page.get_by_test_id("lookup-time").first, "data-iso"
            ),
            "price": int(
                await _required_attribute(page.get_by_test_id("lookup-price").first, "data-amount")
            ),
            "currency": await _required_attribute(
                page.get_by_test_id("lookup-price").first, "data-currency"
            ),
        },
    )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


async def read_doctor_profiles(context: OperationContext, payload: ProfileInput) -> OperationResult:
    """Read public doctor profiles. A GET and a read; nothing is filled or clicked."""
    page = context.page
    await page.goto(
        f"{context.origin}/doctors?specialty={quote(payload.specialty)}&doctor={quote(payload.doctor)}",
        wait_until="domcontentloaded",
    )
    await page.get_by_role("heading", name="Our doctors").wait_for()
    cards = page.get_by_test_id("doctor-profile")
    profiles: list[DoctorProfileSummary] = []
    for index in range(min(await cards.count(), 20)):
        card = cards.nth(index)
        fee = card.get_by_test_id("profile-fee")
        languages = await _required_attribute(card.get_by_test_id("profile-languages"), "data-languages")
        profiles.append(
            DoctorProfileSummary(
                doctor_id=await _required_attribute(card, "data-doctor-id"),
                doctor=await _text(card.get_by_test_id("profile-doctor")),
                specialty=await _text(card.get_by_test_id("profile-specialty")),
                clinic=await _text(card.get_by_test_id("profile-clinic")),
                address=await _text(card.get_by_test_id("profile-address")),
                hours=await _text(card.get_by_test_id("profile-hours")),
                consultation_fee=int(await _required_attribute(fee, "data-amount")),
                currency=await _required_attribute(fee, "data-currency"),
                languages=[item for item in languages.split(",") if item][:12],
                walk_ins=(
                    await _required_attribute(card.get_by_test_id("profile-walk-ins"), "data-walk-ins")
                )
                == "yes",
            )
        )
    output = ProfileOutput(
        observation_id=context.observation_id,
        origin=context.origin,
        page="doctor_profiles",
        profiles=profiles,
    )
    return OperationResult(OperationStatus.OK, output.model_dump(mode="json"))


#: A fixture booking is durably visible to the very next lookup, so "not found"
#: is a fact about the world rather than a fact about our patience. A real
#: clinic site -- eventual consistency, a queue, a booking that only appears
#: once a payment settles -- would not earn this, and the runtime would have to
#: keep such an action in OUTCOME_UNKNOWN.
LOOKUP_ABSENCE_IS_AUTHORITATIVE = True


OPERATIONS: tuple[BrowserOperation, ...] = (
    BrowserOperation(
        name="search_appointments",
        description="Search the clinic's public appointment listing.",
        input_model=SearchInput,
        output_model=SearchOutput,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.SAFE_TO_RETRY,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=30.0,
        timeout_meaning="Nothing was changed. Safe to repeat.",
        preconditions=("the site's search page is reachable",),
        postconditions=("the results heading was rendered before any slot was read",),
        handler=search_appointments,
    ),
    BrowserOperation(
        name="read_available_slots",
        description="Read one slot's current doctor, time and price.",
        input_model=SlotInput,
        output_model=SlotOutput,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.SAFE_TO_RETRY,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=30.0,
        timeout_meaning="Nothing was changed. Safe to repeat.",
        preconditions=("a slot id",),
        postconditions=(
            "either the unavailable marker or a complete set of slot facts was read",
        ),
        handler=read_available_slots,
    ),
    BrowserOperation(
        name="prepare_booking",
        description="Open the booking form and fill the reference. Submits nothing.",
        input_model=PrepareInput,
        output_model=PrepareOutput,
        effect=Effect.PREPARE,
        retry=RetryPolicy.SAFE_TO_RETRY,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=30.0,
        timeout_meaning=(
            "No submission is issued by this operation, so a timeout means no side effect."
        ),
        preconditions=("the slot is still offered",),
        postconditions=("the reference field holds exactly the reference that was passed in",),
        handler=prepare_booking,
    ),
    BrowserOperation(
        name="commit_booking",
        description="Book the approved slot. Irreversible.",
        input_model=CommitInput,
        output_model=CommitOutput,
        effect=Effect.CONSEQUENTIAL,
        retry=RetryPolicy.RECONCILE_BEFORE_RETRY,
        reconciliation=Reconciliation.LOOKUP_BOOKING,
        timeout_seconds=30.0,
        timeout_meaning=(
            "Unknown unless the worker can show the timeout happened before the click. "
            "A timeout after submission must go to lookup_booking, never to FAILED."
        ),
        preconditions=(
            "a durable approval was already claimed by an execution attempt",
            "the slot is still offered",
            "the page's doctor, time, price and currency still match the approved proposal, "
            "re-checked immediately before the click",
        ),
        postconditions=(
            "the browser reached a confirmation page",
            "a receipt booking id matching BK-\\d{4} was read from it",
            "the receipt carries the same reference that was submitted",
        ),
        handler=commit_booking,
    ),
    BrowserOperation(
        name="lookup_booking",
        description="Read-only: does a booking exist for this reference?",
        input_model=LookupInput,
        output_model=LookupOutput,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.SAFE_TO_RETRY,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=30.0,
        timeout_meaning=(
            "The question stays unanswered. The action stays OUTCOME_UNKNOWN; "
            "a timeout here is never evidence of absence."
        ),
        preconditions=("a client reference derived from the persisted action",),
        postconditions=(
            "exactly one of the found, not-found or unavailable markers was present",
        ),
        handler=lookup_booking,
    ),
    BrowserOperation(
        name="read_doctor_profiles",
        description="Read public doctor profiles: clinic, hours, fee, languages, walk-ins.",
        input_model=ProfileInput,
        output_model=ProfileOutput,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.SAFE_TO_RETRY,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=30.0,
        timeout_meaning="Nothing was changed. Safe to repeat.",
        preconditions=("the site's doctor directory is reachable",),
        postconditions=("the directory heading was rendered before any profile was read",),
        handler=read_doctor_profiles,
    ),
)

__all__ = [
    "LOOKUP_ABSENCE_IS_AUTHORITATIVE",
    "OPERATIONS",
    "SITE_NAME",
    "CommitInput",
    "CommitOutput",
    "LookupInput",
    "LookupOutput",
    "PlaywrightError",
    "PlaywrightTimeoutError",
    "PrepareInput",
    "ProfileInput",
    "ProfileOutput",
    "SearchInput",
    "SlotInput",
]
