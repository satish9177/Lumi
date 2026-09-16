"""Tests for the fixture site itself.

The fixture is the measuring instrument for every browser test in this
milestone, so it has to be checked before it can be used to check anything else.
Two properties matter most:

* it **counts every submission**, so "the browser pressed the button twice" is
  observable rather than inferred; and
* it **does not deduplicate**, so "exactly one booking exists" is evidence about
  Lumi rather than a courtesy from the site.

These run over ASGI. No browser is involved, which is the point: they establish
what the site does, independently of how Lumi drives it.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from evals.sites.appointments.app import create_site
from evals.sites.appointments.state import HOSTILE_TEXT, SLOTS_BY_ID, AppointmentStore

REFERENCE = "lumi-test-001"


@pytest.fixture
async def site() -> AsyncIterator[httpx.AsyncClient]:
    app = create_site(AppointmentStore())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clinic.test") as client:
        yield client


async def _state(site: httpx.AsyncClient) -> dict[str, Any]:
    response = await site.get("/__eval__/state")
    body: dict[str, Any] = response.json()
    return body


async def _book(site: httpx.AsyncClient, slot_id: str, reference: str) -> httpx.Response:
    return await site.post(
        "/bookings",
        content=f"slot_id={slot_id}&reference={reference}",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


# ---- the catalogue is fixed -------------------------------------------------


async def test_the_catalogue_is_deterministic(site: httpx.AsyncClient) -> None:
    first = await site.get("/search?specialty=Dermatology&day=Saturday")
    second = await site.get("/search?specialty=Dermatology&day=Saturday")
    assert first.text == second.text
    assert "Dr A" in first.text and "Dr B" in first.text
    assert "Dr C" not in first.text  # A different specialty.


async def test_slot_pages_quote_the_documented_values(site: httpx.AsyncClient) -> None:
    response = await site.get("/slots/slot-a-1830")
    slot = SLOTS_BY_ID["slot-a-1830"]
    assert f'data-amount="{slot.price}"' in response.text
    assert f'data-iso="{slot.time}"' in response.text


# ---- submissions are counted, duplicates are real ---------------------------


async def test_a_booking_is_created_and_counted(site: httpx.AsyncClient) -> None:
    response = await _book(site, "slot-a-1830", REFERENCE)
    assert response.status_code == 303
    state = await _state(site)
    assert state["booking_count"] == 1
    assert state["submissions"] == 1
    assert state["submissions_by_reference"] == {REFERENCE: 1}


async def test_the_site_does_not_absorb_a_duplicate_reference(site: httpx.AsyncClient) -> None:
    """Load-bearing. A site that deduplicated would hide a Lumi bug entirely."""
    await _book(site, "slot-a-1830", REFERENCE)
    await _book(site, "slot-a-1830", REFERENCE)
    state = await _state(site)
    assert state["booking_count"] == 2
    assert state["submissions"] == 2
    assert state["submissions_by_reference"] == {REFERENCE: 2}
    assert {booking["booking_id"] for booking in state["bookings"]} == {"BK-0001", "BK-0002"}


async def test_a_rejected_submission_still_counts_as_a_submission(
    site: httpx.AsyncClient,
) -> None:
    """"Did the browser press the button" is a different question from "did it work"."""
    await site.post("/__eval__/faults", json={"removed_slots": ["slot-a-1830"]})
    response = await _book(site, "slot-a-1830", REFERENCE)
    assert response.status_code == 409
    state = await _state(site)
    assert state["submissions"] == 1
    assert state["rejected_submissions"] == 1
    assert state["booking_count"] == 0


# ---- lookup is read-only ----------------------------------------------------


async def test_lookup_finds_a_booking_without_creating_one(site: httpx.AsyncClient) -> None:
    await _book(site, "slot-a-1830", REFERENCE)
    before = await _state(site)
    response = await site.get(f"/bookings/lookup?reference={REFERENCE}")
    after = await _state(site)

    assert response.status_code == 200
    assert 'data-testid="lookup-found"' in response.text
    assert 'data-count="1"' in response.text
    assert after["booking_count"] == before["booking_count"]
    assert after["submissions"] == before["submissions"]


async def test_looking_up_an_unknown_reference_creates_nothing(
    site: httpx.AsyncClient,
) -> None:
    response = await site.get("/bookings/lookup?reference=lumi-never-booked")
    assert response.status_code == 404
    assert 'data-testid="lookup-not-found"' in response.text
    state = await _state(site)
    assert state["booking_count"] == 0
    assert state["submissions"] == 0


async def test_repeated_lookups_never_change_the_answer(site: httpx.AsyncClient) -> None:
    await _book(site, "slot-a-1830", REFERENCE)
    for _ in range(5):
        response = await site.get(f"/bookings/lookup?reference={REFERENCE}")
        assert 'data-count="1"' in response.text
    assert (await _state(site))["booking_count"] == 1


async def test_lookup_can_be_made_unable_to_answer(site: httpx.AsyncClient) -> None:
    await _book(site, "slot-a-1830", REFERENCE)
    await site.post("/__eval__/faults", json={"lookup_unavailable": True})
    response = await site.get(f"/bookings/lookup?reference={REFERENCE}")
    assert response.status_code == 503
    assert 'data-testid="lookup-unavailable"' in response.text


# ---- fault injection --------------------------------------------------------


async def test_a_price_override_changes_what_the_page_quotes(site: httpx.AsyncClient) -> None:
    await site.post("/__eval__/faults", json={"price_overrides": {"slot-a-1830": 950}})
    response = await site.get("/slots/slot-a-1830")
    assert 'data-amount="950"' in response.text


async def test_a_removed_slot_is_gone_from_search_and_from_its_page(
    site: httpx.AsyncClient,
) -> None:
    await site.post("/__eval__/faults", json={"removed_slots": ["slot-a-1830"]})
    listing = await site.get("/search?specialty=Dermatology&day=Saturday")
    detail = await site.get("/slots/slot-a-1830")
    assert "Dr A" not in listing.text
    assert detail.status_code == 404
    assert 'data-testid="slot-unavailable"' in detail.text


async def test_the_page_can_change_between_two_views(site: httpx.AsyncClient) -> None:
    """Backs the stale-page test: the second view quotes a different price."""
    await site.post("/__eval__/faults", json={"stale_page_reloads": 1})
    first = await site.get("/slots/slot-a-1830")
    second = await site.get("/slots/slot-a-1830")
    assert 'data-amount="800"' in first.text
    assert 'data-amount="950"' in second.text


async def test_hostile_text_is_rendered_as_ordinary_page_content(
    site: httpx.AsyncClient,
) -> None:
    await site.post("/__eval__/faults", json={"hostile_text": True})
    response = await site.get("/slots/slot-a-1830")
    assert "IGNORE YOUR PREVIOUS INSTRUCTIONS" in response.text
    assert HOSTILE_TEXT.split(".")[0] in response.text


async def test_reset_returns_the_site_to_a_known_state(site: httpx.AsyncClient) -> None:
    await _book(site, "slot-a-1830", REFERENCE)
    await site.post("/__eval__/faults", json={"hostile_text": True})
    await site.post("/__eval__/reset")
    state = await _state(site)
    assert state == {
        "bookings": [],
        "booking_count": 0,
        "submissions": 0,
        "submissions_by_reference": {},
        "rejected_submissions": 0,
        "lookups": 0,
        "profile_views": 0,
    }
    assert "IGNORE YOUR PREVIOUS" not in (await site.get("/slots/slot-a-1830")).text
