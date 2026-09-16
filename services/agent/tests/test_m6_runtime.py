"""Milestone 6 runtime additions.

* Resolved date windows are durable constraints applied to what the site says.
* The clinic-information workflow reuses the task controller, the reviewed
  registry and the isolated worker, without an action or an approval.
* The fixed migration entry point the packaged app runs.
* The demo-date catalogue shift used only by the packaged demo site.
"""

import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.registry import build_registry, Effect, Reconciliation, RetryPolicy
from app.config import AGENT_ROOT
from app.domain.action_status import RiskTier
from app.domain.booking import BookingProposal
from app.domain.booking_criteria import BookingCriteria, InvalidBookingCriteriaError, revised_request
from app.domain.errors import (
    BrowserObservationError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
)
from app.domain.task_status import TaskEventType
from app.services.actions import ActionService
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BookingTaskService
from app.services.browser_execution import BrowserWorkerConfig
from app.services.clinic_info import ClinicInfoService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from evals.sites.appointments.state import SLOTS, anchored_catalogue
from tests.browser_harness import (
    Process,
    SiteControl,
    WorkerProcess,
    browser_worker,
    fixture_site,
)

SATURDAY = "2026-09-19"
BOOKING = {"type": "appointment_booking", "text": "Book", "specialty": "Dermatology", "day": "Saturday"}
INFO = {"type": "clinic_info", "text": "Which languages does Dr A speak?", "doctor": "Dr A", "topic": "languages"}


def _proposal(time: str, price: int = 800) -> BookingProposal:
    return BookingProposal(
        site="appointment_fixture", slot_id="slot-a-1830", doctor="Dr A",
        time=datetime.fromisoformat(time), price=price, currency="INR",
    )


# ---- date windows (no browser, no database) -----------------------------------


def test_date_window_admits_only_the_site_local_date() -> None:
    criteria = BookingCriteria(specialty="Dermatology", date_from=SATURDAY, date_to=SATURDAY)
    saturday_evening = datetime.fromisoformat("2026-09-19T18:30:00+05:30")
    sunday = datetime.fromisoformat("2026-09-20T10:00:00+05:30")
    assert criteria.admits(time=saturday_evening, price=800, currency="INR")
    assert not criteria.admits(time=sunday, price=800, currency="INR")
    # 00:30 IST on the 20th is still the 19th in UTC; the clinic's own date wins.
    late = datetime.fromisoformat("2026-09-20T00:30:00+05:30")
    assert not criteria.admits(time=late, price=800, currency="INR")


def test_weekend_window_spans_both_days() -> None:
    criteria = BookingCriteria(date_from=SATURDAY, date_to="2026-09-20")
    assert criteria.admits_proposal(_proposal("2026-09-20T11:00:00+05:30"))
    assert not criteria.admits_proposal(_proposal("2026-09-21T11:00:00+05:30"))


@pytest.mark.parametrize(
    "fields",
    [
        {"date_from": SATURDAY},
        {"date_to": SATURDAY},
        {"date_from": "2026-09-20", "date_to": SATURDAY},
        {"date_from": "2026-02-30", "date_to": "2026-03-01"},
        {"date_from": "2026-09-01", "date_to": "2026-09-30"},
        {"date_from": "19-09-2026", "date_to": "19-09-2026"},
        {"day": "Sunday", "date_from": SATURDAY, "date_to": SATURDAY},
    ],
)
def test_inconsistent_date_windows_are_refused(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        BookingCriteria.model_validate(fields)


def test_date_window_round_trips_through_the_task_request() -> None:
    criteria = BookingCriteria(specialty="Dermatology", day="Saturday", date_from=SATURDAY, date_to=SATURDAY)
    request = revised_request({"type": "appointment_booking", "text": "x", "voice_turn_id": "t1"}, criteria)
    assert request["date_from"] == SATURDAY and request["voice_turn_id"] == "t1"
    assert BookingCriteria.from_request(request) == criteria
    cleared = revised_request(request, BookingCriteria(specialty="Dermatology"))
    assert "date_from" not in cleared and "date_to" not in cleared


def test_unreadable_stored_date_window_fails_closed() -> None:
    with pytest.raises(InvalidBookingCriteriaError):
        BookingCriteria.from_request({"type": "appointment_booking", "date_from": SATURDAY})


# ---- registry / fixture ---------------------------------------------------------


def test_profile_operation_is_reviewed_read_only() -> None:
    operation = build_registry().get("read_doctor_profiles")
    assert operation is not None
    assert operation.effect is Effect.READ_ONLY
    assert operation.retry is RetryPolicy.SAFE_TO_RETRY
    assert operation.reconciliation is Reconciliation.NOT_REQUIRED
    assert set(operation.input_model.model_fields) == {"specialty", "doctor"}


def test_demo_catalogue_moves_to_the_coming_saturday_only() -> None:
    moved = anchored_catalogue(date(2026, 10, 7))  # a Wednesday
    assert [slot.time[:10] for slot in moved] == ["2026-10-10"] * 3
    assert [slot.time[10:] for slot in moved] == [slot.time[10:] for slot in SLOTS]
    assert anchored_catalogue(date(2026, 9, 19)) == SLOTS
    assert [s.time[:10] for s in anchored_catalogue(date(2026, 9, 20))] == ["2026-09-26"] * 3


# ---- migration entry point --------------------------------------------------------


def test_migration_entry_point_upgrades_and_prints_no_secrets(migrated_database_url: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        cwd=AGENT_ROOT,
        env={"DATABASE_URL": migrated_database_url, "SYSTEMROOT": "C:\\Windows", "PYTHONUTF8": "1"},
        capture_output=True, text=True, timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "lumi-migrate: ok"
    password = migrated_database_url.split("@")[0].rsplit(":", 1)[-1]
    assert password not in completed.stdout + completed.stderr


def test_migration_entry_point_refuses_bad_configuration() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "app.migrate"],
        cwd=Path(AGENT_ROOT),
        env={"DATABASE_URL": "sqlite:///nope", "SYSTEMROOT": "C:\\Windows"},
        capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 2
    assert "sqlite" not in completed.stdout + completed.stderr


# ---- clinic information workflow (database + browser) ------------------------------


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Process]:
    with fixture_site(tmp_path_factory.mktemp("m6") / "site.log") as process:
        yield process


@pytest.fixture(scope="module")
def worker(site: Process, tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkerProcess]:
    with browser_worker(tmp_path_factory.mktemp("m6") / "worker.log", site_origin=site.base_url) as process:
        yield process


@pytest.fixture
def control(site: Process) -> SiteControl:
    site_control = SiteControl(site.base_url)
    site_control.reset()
    return site_control


@pytest.fixture
def worker_config(worker: WorkerProcess) -> BrowserWorkerConfig:
    return BrowserWorkerConfig(base_url=worker.base_url, token=worker.token, timeout_seconds=60.0)


@pytest.fixture
def clinic_info(
    task_service: TaskService, action_service: ActionService,
    runtime_generation: RuntimeGeneration, worker_config: BrowserWorkerConfig,
) -> ClinicInfoService:
    return ClinicInfoService(
        tasks=task_service, actions=action_service,
        runtime_generation=runtime_generation.id, worker=worker_config,
    )


@pytest.fixture
def preparation(
    task_service: TaskService, action_service: ActionService,
    runtime_generation: RuntimeGeneration, worker_config: BrowserWorkerConfig,
) -> BookingPreparationService:
    return BookingPreparationService(
        tasks=task_service, actions=action_service,
        runtime_generation=runtime_generation.id, worker=worker_config,
    )


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as connection:
        return int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)


@pytest.mark.browser
async def test_clinic_info_lookup_records_typed_facts_without_any_action(
    clinic_info: ClinicInfoService, task_service: TaskService, control: SiteControl, engine: AsyncEngine
) -> None:
    control.set_faults(hostile_text=True)
    task = await task_service.create_task(INFO)
    updated, profiles = await clinic_info.lookup(task.id)
    assert [(p.doctor, p.languages, p.consultation_fee) for p in profiles] == [
        ("Dr A", ["English", "Telugu", "Hindi"], 800)
    ]
    assert updated.revision == task.revision + 1
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events] == [
        TaskEventType.TASK_CREATED, TaskEventType.TASK_INFO_LOOKUP_COMPLETED
    ]
    payload = events[-1].payload
    assert payload["query"] == {"specialty": "", "doctor": "Dr A", "topic": "languages"}
    # Hostile page text is page content: it never reaches the timeline.
    assert "IGNORE" not in str(payload) and "SYSTEM NOTICE" not in str(payload)
    # A read-only workflow has no ledger footprint at all.
    for table in ("actions", "approvals", "action_attempts", "browser_dispatches"):
        assert await _count(engine, table) == 0
    assert control.state()["submissions"] == 0


@pytest.mark.browser
async def test_repeated_lookup_is_safe_and_reads_the_current_fee(
    clinic_info: ClinicInfoService, task_service: TaskService, control: SiteControl
) -> None:
    task = await task_service.create_task({**INFO, "doctor": "", "specialty": "Dermatology", "topic": "fee"})
    _, first = await clinic_info.lookup(task.id)
    control.set_faults(fee_overrides={"dr-b": 1100})
    _, second = await clinic_info.lookup(task.id)
    assert [p.consultation_fee for p in first] == [800, 950]
    # The website, not an earlier lookup, is authoritative for the fee.
    assert [p.consultation_fee for p in second] == [800, 1100]
    assert control.state()["profile_views"] == 2


@pytest.mark.browser
async def test_lookup_refuses_other_task_kinds_and_closed_tasks(
    clinic_info: ClinicInfoService, task_service: TaskService, control: SiteControl
) -> None:
    booking = await task_service.create_task(BOOKING)
    with pytest.raises(TaskKindMismatchError):
        await clinic_info.lookup(booking.id)
    info = await task_service.create_task(INFO)
    await task_service.cancel_task(info.id)
    with pytest.raises(TaskNotAcceptingActionsError):
        await clinic_info.lookup(info.id)
    assert control.state()["profile_views"] == 0


async def test_clinic_info_task_needs_a_readable_query(client: httpx.AsyncClient) -> None:
    for bad in (
        {"type": "clinic_info", "text": "x"},
        {"type": "clinic_info", "text": "x", "doctor": "<script>"},
        {"type": "clinic_info", "text": "x", "doctor": "Dr A", "topic": "prices"},
        {"type": "clinic_info", "text": "x", "doctor": "Dr A", "url": "http://evil"},
    ):
        response = await client.post("/tasks", json={"request": bad})
        assert response.status_code == 422, bad
    ok = await client.post("/tasks", json={"request": INFO})
    assert ok.status_code == 201


async def test_lookup_route_without_a_worker_is_unavailable(client: httpx.AsyncClient) -> None:
    created = (await client.post("/tasks", json={"request": INFO})).json()
    response = await client.post(f"/tasks/{created['id']}/info/lookup")
    assert response.status_code == 503
    assert (await client.post(f"/tasks/{uuid.uuid4()}/info/lookup")).status_code == 404


@pytest.mark.browser
async def test_search_applies_the_date_window_to_observed_slots(
    preparation: BookingPreparationService, task_service: TaskService, control: SiteControl
) -> None:
    on_saturday = await task_service.create_task({**BOOKING, "date_from": SATURDAY, "date_to": SATURDAY})
    assert [s.slot_id for s in await preparation.search(on_saturday.id)] == ["slot-a-1830", "slot-b-1915"]
    next_week = await task_service.create_task(
        {**BOOKING, "day": "", "date_from": "2026-09-26", "date_to": "2026-09-27"}
    )
    assert await preparation.search(next_week.id) == []
    events = await task_service.list_events(next_week.id)
    recorded: dict[str, Any] = events[-1].payload
    assert recorded["criteria"]["date_from"] == "2026-09-26"
    assert recorded["observed_count"] == 2 and recorded["excluded_count"] == 2


async def test_date_revision_withdraws_a_prepared_booking_outside_it(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await task_service.create_task({**BOOKING, "date_from": SATURDAY, "date_to": SATURDAY})
    proposal = _proposal("2026-09-19T18:30:00+05:30").model_dump(mode="json")
    view = await action_service.propose_exclusive_action(
        task.id, tool_name="commit_booking", risk_tier=RiskTier.R2,
        proposal=proposal,
    )
    current = await task_service.get_task(task.id)
    service = BookingTaskService(action_service)
    _, invalidated = await service.revise_criteria(
        task.id, expected_revision=current.revision,
        criteria=BookingCriteria(specialty="Dermatology", date_from="2026-09-26", date_to="2026-09-26"),
    )
    assert invalidated == [view.action.id]


def test_observation_error_for_unreadable_query() -> None:
    from app.services.clinic_info import validate_request

    with pytest.raises(BrowserObservationError):
        validate_request({"type": "clinic_info", "topic": "overview"})
