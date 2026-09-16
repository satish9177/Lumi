"""Task-level booking changes a conversation can ask for (Milestone 5).

Refining constraints and cancelling are durable task changes. They run under
the same task lock as the ledger, never touch a booking that may already exist
at the site, and invalidate a prepared booking the new constraints exclude so
its approval can never be spent.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest

from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.booking import BookingProposal
from app.domain.booking_criteria import (
    BookingCriteria,
    InvalidBookingCriteriaError,
    revised_request,
)
from app.domain.errors import (
    ApprovalNotUsableError,
    BookingCriteriaMismatchError,
    InvalidActionTransitionError,
    StaleTaskRevisionError,
    TaskAlreadyBookedError,
    TaskHasUnresolvedActionError,
    TaskKindMismatchError,
)
from app.domain.task_status import TaskStatus
from app.services.actions import ActionService, ActionView
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BookingTaskService
from app.services.browser_execution import BrowserWorkerConfig
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.browser_harness import Process, SiteControl, WorkerProcess, browser_worker, fixture_site

EVENING_UNDER_1000 = {
    "type": "appointment_booking",
    "text": "Find me a dermatologist Saturday evening under 1000",
    "source": "voice",
    "voice_turn_id": "item_test01",
    "specialty": "Dermatology",
    "day": "Saturday",
    "earliest_time": "17:00",
    "latest_time": "21:00",
    "max_price": 1000,
    "max_price_currency": "INR",
}
SLOT_A = {"site": "appointment_fixture", "slot_id": "slot-a-1830", "doctor": "Dr A",
          "time": "2026-09-19T18:30:00+05:30", "price": 800, "currency": "INR"}
SLOT_B = {"site": "appointment_fixture", "slot_id": "slot-b-1915", "doctor": "Dr B",
          "time": "2026-09-19T19:15:00+05:30", "price": 950, "currency": "INR"}


def _criteria(**changes: Any) -> BookingCriteria:
    base = BookingCriteria.from_request(EVENING_UNDER_1000).model_dump()
    return BookingCriteria.model_validate({**base, **changes})


@pytest.fixture
def booking_tasks(action_service: ActionService) -> BookingTaskService:
    return BookingTaskService(action_service)


async def _prepared(
    action_service: ActionService, task_id: uuid.UUID, proposal: dict[str, Any] = SLOT_A
) -> ActionView:
    view = await action_service.propose_exclusive_action(
        task_id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=proposal
    )
    return await action_service.request_approval(view.action.id, expected_revision=view.action.revision)


# ---- domain ------------------------------------------------------------------


def test_criteria_round_trip_through_the_request() -> None:
    criteria = BookingCriteria.from_request(EVENING_UNDER_1000)
    assert criteria.request_fields() == {
        key: EVENING_UNDER_1000[key]
        for key in ("specialty", "day", "earliest_time", "latest_time", "max_price", "max_price_currency")
    }
    revised = revised_request(EVENING_UNDER_1000, criteria.model_copy(update={"max_price": 800}))
    assert revised["max_price"] == 800
    assert revised["text"] == EVENING_UNDER_1000["text"]
    assert revised["voice_turn_id"] == "item_test01"
    cleared = revised_request(EVENING_UNDER_1000, BookingCriteria(specialty="Dermatology"))
    assert "earliest_time" not in cleared and "max_price" not in cleared and "day" not in cleared


def test_milestone_4_criteria_stay_lenient_and_new_bounds_fail_closed() -> None:
    lenient = BookingCriteria.from_request({"type": "appointment_booking", "specialty": "<b>", "day": "Someday"})
    assert (lenient.specialty, lenient.day) == ("", "")
    for broken in (
        {"earliest_time": "6pm"},
        {"latest_time": "24:00"},
        {"max_price": -1, "max_price_currency": "INR"},
        {"max_price": 900},
        {"max_price_currency": "INR"},
        {"max_price": "900", "max_price_currency": "INR"},
        {"earliest_time": "20:00", "latest_time": "18:00"},
    ):
        with pytest.raises(InvalidBookingCriteriaError):
            BookingCriteria.from_request({"type": "appointment_booking", **broken})


@pytest.mark.parametrize(
    ("time", "price", "currency", "admitted"),
    [
        ("2026-09-19T18:30:00+05:30", 800, "INR", True),
        ("2026-09-19T17:00:00+05:30", 1000, "INR", True),
        ("2026-09-19T21:00:00+05:30", 1000, "INR", True),
        ("2026-09-19T16:59:00+05:30", 800, "INR", False),
        ("2026-09-19T21:01:00+05:30", 800, "INR", False),
        ("2026-09-19T18:30:00+05:30", 1001, "INR", False),
        ("2026-09-19T18:30:00+05:30", 10, "USD", False),
        # The clinic's own offset decides the wall clock: 13:00Z is 18:30 IST.
        ("2026-09-19T13:00:00+00:00", 800, "INR", False),
    ],
)
def test_admits_applies_the_window_in_the_sites_offset(time: str, price: int, currency: str, admitted: bool) -> None:
    assert _criteria().admits(time=datetime.fromisoformat(time), price=price, currency=currency) is admitted


def test_a_prepared_booking_on_another_day_is_not_admitted() -> None:
    proposal = BookingProposal.model_validate({**SLOT_A, "time": "2026-09-20T18:30:00+05:30"})
    assert not _criteria().admits_proposal(proposal)
    assert _criteria().admits_proposal(BookingProposal.model_validate(SLOT_A))


# ---- revision ----------------------------------------------------------------


async def test_revising_to_the_same_criteria_writes_nothing(
    booking_tasks: BookingTaskService, task_service: TaskService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    same, invalidated = await booking_tasks.revise_criteria(task.id, expected_revision=1, criteria=_criteria())
    assert invalidated == [] and same.revision == 1
    assert len(await task_service.list_events(task.id)) == 1


async def test_a_refinement_that_excludes_the_prepared_booking_rejects_it_atomically(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_B)
    approved = await action_service.approve_action(prepared.action.id, expected_revision=prepared.action.revision)
    current = await task_service.get_task(task.id)

    revised, invalidated = await booking_tasks.revise_criteria(
        task.id, expected_revision=current.revision, criteria=_criteria(max_price=900)
    )
    assert invalidated == [approved.action.id]
    assert revised.request["max_price"] == 900
    assert revised.status is TaskStatus.READY

    # The approval the user granted for the ₹950 booking is gone for good.
    stale = await action_service.get_action(approved.action.id)
    assert stale.action.status is ActionStatus.REJECTED
    assert stale.approval is None
    with pytest.raises(InvalidActionTransitionError):
        await action_service.start_attempt(approved.action.id)

    events = await task_service.list_events(task.id)
    tail = [(event.event_type, event.payload.get("reason")) for event in events[-2:]]
    assert tail == [("task.criteria_updated", "criteria_changed"), ("action.rejected", "criteria_changed")]
    assert events[-2].payload["invalidated_action_ids"] == [str(approved.action.id)]
    assert events[-2].payload["criteria"]["max_price"] == 900
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


async def test_a_refinement_the_prepared_booking_still_satisfies_keeps_it(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_A)
    current = await task_service.get_task(task.id)
    _, invalidated = await booking_tasks.revise_criteria(
        task.id, expected_revision=current.revision, criteria=_criteria(earliest_time="18:00")
    )
    assert invalidated == []
    kept = await action_service.get_action(prepared.action.id)
    assert kept.action.status is ActionStatus.WAITING_APPROVAL
    assert kept.approval is not None


async def test_a_changed_specialty_always_invalidates(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_A)
    current = await task_service.get_task(task.id)
    _, invalidated = await booking_tasks.revise_criteria(
        task.id, expected_revision=current.revision, criteria=_criteria(specialty="Dentistry")
    )
    assert invalidated == [prepared.action.id]


async def test_a_refinement_needs_the_revision_it_was_derived_from(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    await _prepared(action_service, task.id, SLOT_B)
    with pytest.raises(StaleTaskRevisionError):
        await booking_tasks.revise_criteria(task.id, expected_revision=1, criteria=_criteria(max_price=900))
    assert (await task_service.get_task(task.id)).request["max_price"] == 1000


@pytest.mark.parametrize("outcome", [None, AttemptOutcome.OUTCOME_UNKNOWN])
async def test_a_booking_that_may_exist_blocks_refinement_and_cancel(
    booking_tasks: BookingTaskService,
    task_service: TaskService,
    action_service: ActionService,
    outcome: AttemptOutcome | None,
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_A)
    approved = await action_service.approve_action(prepared.action.id)
    await action_service.start_attempt(approved.action.id)
    if outcome is not None:
        await action_service.finish_attempt(approved.action.id, outcome=outcome)
    current = await task_service.get_task(task.id)

    with pytest.raises(TaskHasUnresolvedActionError):
        await booking_tasks.revise_criteria(
            task.id, expected_revision=current.revision, criteria=_criteria(max_price=900)
        )
    with pytest.raises(TaskHasUnresolvedActionError):
        await booking_tasks.cancel(task.id)
    after = await task_service.get_task(task.id)
    assert after.revision == current.revision
    assert after.status is (TaskStatus.EXECUTING if outcome is None else TaskStatus.OUTCOME_UNKNOWN)


async def test_a_confirmed_booking_blocks_refinement_and_cancel(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_A)
    approved = await action_service.approve_action(prepared.action.id)
    await action_service.start_attempt(approved.action.id)
    await action_service.finish_attempt(approved.action.id, outcome=AttemptOutcome.SUCCEEDED)
    current = await task_service.get_task(task.id)
    with pytest.raises(TaskAlreadyBookedError):
        await booking_tasks.revise_criteria(
            task.id, expected_revision=current.revision, criteria=_criteria(max_price=900)
        )
    with pytest.raises(TaskAlreadyBookedError):
        await booking_tasks.cancel(task.id)


async def test_refinement_and_approval_serialize_on_the_task_lock(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    """Whichever wins, an excluded booking can never start an attempt."""
    for _ in range(5):
        task = await task_service.create_task(EVENING_UNDER_1000)
        prepared = await _prepared(action_service, task.id, SLOT_B)
        current = await task_service.get_task(task.id)

        async def approve() -> object:
            try:
                return await action_service.approve_action(prepared.action.id)
            except (InvalidActionTransitionError, ApprovalNotUsableError) as error:
                return error

        async def refine() -> object:
            return await booking_tasks.revise_criteria(
                task.id, expected_revision=current.revision, criteria=_criteria(max_price=900)
            )

        await asyncio.gather(approve(), refine(), return_exceptions=True)
        final = await action_service.get_action(prepared.action.id)
        refined = (await task_service.get_task(task.id)).request.get("max_price") == 900
        if refined:
            assert final.action.status is ActionStatus.REJECTED
            with pytest.raises(InvalidActionTransitionError):
                await action_service.start_attempt(prepared.action.id)
        assert final.attempts == ()


# ---- cancel --------------------------------------------------------------------


async def test_cancel_rejects_the_open_booking_and_cancels_once(
    booking_tasks: BookingTaskService, task_service: TaskService, action_service: ActionService
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    prepared = await _prepared(action_service, task.id, SLOT_A)
    cancelled, rejected = await booking_tasks.cancel(task.id)
    assert cancelled.status is TaskStatus.CANCELLED
    assert rejected == [prepared.action.id]
    assert (await action_service.get_action(prepared.action.id)).action.status is ActionStatus.REJECTED

    again, none = await booking_tasks.cancel(task.id)
    assert none == [] and again.revision == cancelled.revision
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events][-2:] == ["action.rejected", "task.cancelled"]
    assert events[-1].payload["rejected_action_ids"] == [str(prepared.action.id)]


async def test_booking_task_changes_refuse_other_task_kinds(
    booking_tasks: BookingTaskService, task_service: TaskService
) -> None:
    task = await task_service.create_task({"type": "note"})
    with pytest.raises(TaskKindMismatchError):
        await booking_tasks.cancel(task.id)
    with pytest.raises(TaskKindMismatchError):
        await booking_tasks.revise_criteria(task.id, expected_revision=1, criteria=_criteria())


# ---- API -------------------------------------------------------------------------


async def test_criteria_and_cancel_routes(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": EVENING_UNDER_1000})).json()
    criteria = {**_criteria(max_price=800).model_dump(mode="json")}
    response = await client.post(
        f"/tasks/{task['id']}/booking/criteria", json={"expected_revision": 1, "criteria": criteria}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invalidated_action_ids"] == []
    assert body["task"]["request"]["max_price"] == 800
    assert body["task"]["revision"] == 2

    stale = await client.post(
        f"/tasks/{task['id']}/booking/criteria", json={"expected_revision": 1, "criteria": criteria}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_revision"

    cancelled = await client.post(f"/tasks/{task['id']}/booking/cancel", json={"expected_revision": 2})
    assert cancelled.status_code == 200
    assert cancelled.json()["task"]["status"] == "CANCELLED"
    closed = await client.post(
        f"/tasks/{task['id']}/booking/criteria", json={"expected_revision": 3, "criteria": criteria}
    )
    assert closed.json()["error"]["code"] == "task_not_accepting_actions"


@pytest.mark.parametrize(
    "criteria",
    [
        {"specialty": "Dermatology", "max_price": 800},
        {"specialty": "Dermatology", "earliest_time": "6 pm"},
        {"specialty": "Dermatology", "url": "http://example.test"},
        {"specialty": "<script>"},
        {"day": "Caturday"},
    ],
)
async def test_criteria_route_refuses_unsafe_constraints(client: httpx.AsyncClient, criteria: dict[str, Any]) -> None:
    task = (await client.post("/tasks", json={"request": EVENING_UNDER_1000})).json()
    response = await client.post(
        f"/tasks/{task['id']}/booking/criteria", json={"expected_revision": 1, "criteria": criteria}
    )
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalid_request", "message": "Request validation failed."}}


async def test_a_booking_task_cannot_be_created_with_unreadable_bounds(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/tasks", json={"request": {**EVENING_UNDER_1000, "max_price_currency": "rupees"}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# ---- with a real browser worker ------------------------------------------------


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Process]:
    with fixture_site(tmp_path_factory.mktemp("voice") / "site.log") as process:
        yield process


@pytest.fixture(scope="module")
def worker(site: Process, tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkerProcess]:
    with browser_worker(tmp_path_factory.mktemp("voice") / "worker.log", site_origin=site.base_url) as process:
        yield process


@pytest.fixture
def control(site: Process) -> SiteControl:
    site_control = SiteControl(site.base_url)
    site_control.reset()
    return site_control


@pytest.fixture
def preparation(
    task_service: TaskService,
    action_service: ActionService,
    runtime_generation: RuntimeGeneration,
    worker: WorkerProcess,
) -> BookingPreparationService:
    return BookingPreparationService(
        tasks=task_service,
        actions=action_service,
        runtime_generation=runtime_generation.id,
        worker=BrowserWorkerConfig(base_url=worker.base_url, token=worker.token, timeout_seconds=60.0),
    )


@pytest.mark.browser
async def test_search_applies_and_records_the_constraints(
    preparation: BookingPreparationService,
    booking_tasks: BookingTaskService,
    task_service: TaskService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    slots = await preparation.search(task.id)
    assert [slot.slot_id for slot in slots] == ["slot-a-1830", "slot-b-1915"]

    current = await task_service.get_task(task.id)
    await booking_tasks.revise_criteria(
        task.id, expected_revision=current.revision, criteria=_criteria(max_price=900, earliest_time="18:00")
    )
    narrowed = await preparation.search(task.id)
    assert [slot.slot_id for slot in narrowed] == ["slot-a-1830"]

    events = [event for event in await task_service.list_events(task.id) if event.event_type == "task.search_completed"]
    assert len(events) == 2
    latest = events[-1].payload
    assert latest["observed_count"] == 2 and latest["excluded_count"] == 1
    assert latest["criteria"]["max_price"] == 900
    assert latest["slots"] == [
        {"slot_id": "slot-a-1830", "doctor": "Dr A", "specialty": "Dermatology",
         "time": "2026-09-19T18:30:00+05:30", "price": 800, "currency": "INR"}
    ]
    assert control.state()["submissions"] == 0


@pytest.mark.browser
async def test_hostile_page_text_never_reaches_the_recorded_results(
    preparation: BookingPreparationService, task_service: TaskService, control: SiteControl
) -> None:
    control.set_faults(hostile_text=True)
    task = await task_service.create_task(EVENING_UNDER_1000)
    await preparation.search(task.id)
    prepared = await preparation.prepare(task.id, "slot-a-1830")
    events = await task_service.list_events(task.id)
    rendered = repr([event.payload for event in events]) + repr(prepared.action.proposal)
    for fragment in ("IGNORE", "SYSTEM NOTICE", "9500", "approval rules"):
        assert fragment not in rendered
    assert prepared.action.proposal["price"] == 800
    state = control.state()
    assert state["submissions"] == 0 and state["booking_count"] == 0


@pytest.mark.browser
async def test_prepare_refuses_a_slot_whose_current_price_breaks_the_ceiling(
    preparation: BookingPreparationService,
    task_service: TaskService,
    action_service: ActionService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(EVENING_UNDER_1000)
    control.set_faults(price_overrides={"slot-b-1915": 1100})
    with pytest.raises(BookingCriteriaMismatchError):
        await preparation.prepare(task.id, "slot-b-1915")
    assert await action_service.list_actions(task.id) == []
