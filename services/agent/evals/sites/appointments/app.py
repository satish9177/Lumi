"""The deterministic appointment-booking fixture website.

Evaluation infrastructure, not Lumi application code. It exists so browser
execution can be measured against a site whose state is completely known: a
fixed catalogue, an authoritative booking store, submission counters, and fault
injection that can make a response disappear *after* the booking was created.

The normal execution path is ordinary HTTP and ordinary HTML. The browser worker
drives it through Playwright exactly as it would drive a real site; it never
calls this backend directly. The only shortcut anywhere is `/__eval__/*`, which
is the test control plane: it configures faults and reads the authoritative
counters, and it is never used to perform or to observe a booking on behalf of
the worker.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from evals.sites.appointments import pages
from evals.sites.appointments.state import (
    PROFILES,
    AppointmentStore,
    Faults,
    Slot,
)

#: How long a dropped response holds the connection open. Long enough that no
#: test ever races it, short enough that a stray process still exits.
HANG_SECONDS = 3_600.0

MAX_REFERENCE_LENGTH = 80

router = APIRouter()


def _store(request: Request) -> AppointmentStore:
    store: AppointmentStore = request.app.state.store
    return store


def _html(markup: str, status_code: int = status.HTTP_200_OK) -> HTMLResponse:
    return HTMLResponse(content=markup, status_code=status_code)


# ---- the site a browser actually uses ---------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _html(pages.search_form())


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    specialty: str = Query(default="", max_length=120),
    day: str = Query(default="", max_length=40),
) -> HTMLResponse:
    store = _store(request)
    matches: list[tuple[Slot, int]] = [
        (slot, store.price_of(slot))
        for slot in store.catalogue
        if store.slot_is_available(slot.slot_id)
        and (not specialty or slot.specialty.casefold() == specialty.strip().casefold())
        and (not day or slot.day.casefold() == day.strip().casefold())
    ]
    return _html(pages.search_results(specialty, day, matches))


@router.get("/slots/{slot_id}", response_class=HTMLResponse)
async def slot_detail(request: Request, slot_id: str) -> HTMLResponse:
    store = _store(request)
    if not store.slot_is_available(slot_id):
        return _html(pages.slot_unavailable(slot_id), status.HTTP_404_NOT_FOUND)
    slot = store.catalogue_by_id[slot_id]
    views = store.record_slot_page_view(slot_id)
    price = store.price_of(slot)
    # A page that changes underneath the worker: after the configured number of
    # views the quoted price moves. Re-observation before the click is what has
    # to catch this, not a value cached from an earlier page load.
    reloads = store.faults.stale_page_reloads
    if reloads and views > reloads:
        price += 150
    return _html(pages.booking_form(slot, price, hostile_text=store.faults.hostile_text))


@router.get("/doctors", response_class=HTMLResponse)
async def doctors(
    request: Request,
    specialty: str = Query(default="", max_length=120),
    doctor: str = Query(default="", max_length=120),
) -> HTMLResponse:
    """Public doctor profiles. Read-only; the clinic-information workflow reads this."""
    store = _store(request)
    store.record_profile_view()
    wanted_doctor = doctor.strip().casefold()
    matches = [
        (profile, store.fee_of(profile))
        for profile in PROFILES
        if (not specialty or profile.specialty.casefold() == specialty.strip().casefold())
        and (not wanted_doctor or profile.doctor.casefold() == wanted_doctor)
    ]
    return _html(pages.doctor_profiles(matches, hostile_text=store.faults.hostile_text))


async def _form_values(request: Request) -> dict[str, str]:
    """Parse an urlencoded form body without pulling in python-multipart."""
    raw = (await request.body()).decode("utf-8", errors="replace")
    return {key: values[0] for key, values in parse_qs(raw, keep_blank_values=True).items()}


async def _never_responds() -> AsyncIterator[bytes]:
    """Headers go out, the body never does, the connection is torn down."""
    if False:  # pragma: no cover - makes this an async generator.
        yield b""
    raise ConnectionResetError("the fixture dropped this response on purpose")


@router.post("/bookings")
async def create_booking(request: Request) -> Response:
    """Submit a booking.

    Every call here is a real submission and is counted as one, whatever happens
    next. In particular a *duplicate* reference creates a *second* booking: this
    fixture must never quietly launder a double submission into a single side
    effect, or "exactly one booking exists" would stop proving anything about
    Lumi.
    """
    store = _store(request)
    form = await _form_values(request)
    slot_id = form.get("slot_id", "")
    reference = form.get("reference", "").strip()

    store.record_submission(reference)

    if not reference or len(reference) > MAX_REFERENCE_LENGTH:
        store.record_rejected_submission()
        return _html(pages.submission_rejected("missing_reference"), status.HTTP_400_BAD_REQUEST)
    if not store.slot_is_available(slot_id):
        store.record_rejected_submission()
        return _html(pages.submission_rejected("slot_unavailable"), status.HTTP_409_CONFLICT)

    faults = store.faults
    # A response lost *before* anything was created. From the browser this looks
    # identical to losing one after -- which is the whole problem.
    if faults.drop_response_before_commit_for_reference == reference:
        if faults.drop_response_mode == "hang":
            await asyncio.sleep(HANG_SECONDS)
        return StreamingResponse(_never_responds(), media_type="text/html")
    if faults.reject_submissions:
        store.record_rejected_submission()
        return _html(pages.submission_rejected("clinic_declined"), status.HTTP_409_CONFLICT)

    booking = store.create_booking(store.catalogue_by_id[slot_id], reference)

    # The booking now exists. Everything below only decides whether the client
    # gets to find out, which is the whole point of the lost-response scenario.
    if faults.drop_response_for_reference == reference:
        if faults.drop_response_mode == "hang":
            await asyncio.sleep(HANG_SECONDS)
        return StreamingResponse(_never_responds(), media_type="text/html")

    return RedirectResponse(
        url=f"/bookings/{booking.booking_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/bookings/lookup", response_class=HTMLResponse)
async def lookup(
    request: Request, reference: str | None = Query(default=None, max_length=MAX_REFERENCE_LENGTH)
) -> HTMLResponse:
    """Read-only. This is the page reconciliation reads, and it books nothing.

    "No booking found" is authoritative *for this fixture*: the store is the
    single writer, a created booking is visible to the very next lookup, and
    bookings are never expired or hidden. A real clinic site would offer no such
    guarantee, which is why the adapter, not this page, decides whether absence
    may be reported as a known failure.
    """
    store = _store(request)
    if reference is None:
        return _html(pages.lookup_form())
    if store.faults.lookup_unavailable:
        return _html(
            pages.lookup_unavailable(reference), status.HTTP_503_SERVICE_UNAVAILABLE
        )
    found = store.find_by_reference(reference)
    if not found:
        return _html(pages.lookup_not_found(reference), status.HTTP_404_NOT_FOUND)
    return _html(pages.lookup_found(reference, found))


@router.get("/bookings/{booking_id}", response_class=HTMLResponse)
async def booking_detail(request: Request, booking_id: str) -> HTMLResponse:
    store = _store(request)
    booking = store.get(booking_id)
    if booking is None:
        return _html(pages.submission_rejected("unknown_booking"), status.HTTP_404_NOT_FOUND)
    return _html(pages.confirmation(booking, hostile_text=store.faults.hostile_text))


# ---- test control plane -----------------------------------------------------
#
# Used by tests to configure faults and to read authoritative state. Never used
# by the browser worker, and never a substitute for driving the site.

eval_router = APIRouter(prefix="/__eval__")


@eval_router.get("/state")
async def eval_state(request: Request) -> dict[str, Any]:
    return _store(request).snapshot()


@eval_router.post("/reset")
async def eval_reset(request: Request) -> dict[str, str]:
    _store(request).reset()
    return {"status": "reset"}


@eval_router.post("/faults")
async def eval_faults(request: Request) -> dict[str, Any]:
    body: dict[str, Any] = await request.json()
    removed: Sequence[str] = body.get("removed_slots", ())
    overrides: dict[str, int] = body.get("price_overrides", {})
    _store(request).set_faults(
        Faults(
            drop_response_for_reference=body.get("drop_response_for_reference"),
            drop_response_mode=body.get("drop_response_mode", "hang"),
            drop_response_before_commit_for_reference=body.get(
                "drop_response_before_commit_for_reference"
            ),
            reject_submissions=bool(body.get("reject_submissions", False)),
            removed_slots=set(removed),
            price_overrides={str(k): int(v) for k, v in overrides.items()},
            hostile_text=bool(body.get("hostile_text", False)),
            lookup_unavailable=bool(body.get("lookup_unavailable", False)),
            stale_page_reloads=int(body.get("stale_page_reloads", 0)),
            fee_overrides={
                str(k): int(v) for k, v in dict(body.get("fee_overrides", {})).items()
            },
        )
    )
    return {"status": "configured"}


def create_site(store: AppointmentStore | None = None) -> FastAPI:
    app = FastAPI(title="Clinic Appointment Fixture", version="1.0.0")
    app.state.store = store if store is not None else AppointmentStore()
    app.include_router(router)
    app.include_router(eval_router)
    return app
