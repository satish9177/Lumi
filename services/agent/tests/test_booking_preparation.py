"""Desktop-facing booking preparation, execution binding and managed workers.

The proposal a user approves is built from what the isolated worker observed,
never from caller-supplied values. Execution and reconciliation are bound to
the action revision the user reviewed, and a task cannot grow a second booking
while one is unresolved.
"""

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.managed import ManagedBrowserWorker, _ready_port, worker_environment
from app.browser.protocol import WORKER_TOKEN_HEADER
from app.config import Settings
from app.domain.action_status import ActionStatus, RiskTier
from app.domain.errors import (
    ActionAlreadyOpenError,
    BookingSlotUnavailableError,
    InvalidActionTransitionError,
    StaleActionRevisionError,
    TaskKindMismatchError,
)
from app.services.actions import ActionService
from app.services.booking_preparation import BookingPreparationService
from app.services.browser_execution import BrowserExecutionService, BrowserWorkerConfig
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.browser_harness import (
    Process,
    SiteControl,
    WorkerProcess,
    browser_worker,
    fixture_site,
)

BOOKING_REQUEST = {
    "type": "appointment_booking",
    "text": "Book an appointment",
    "specialty": "Dermatology",
    "day": "Saturday",
}


# ---- unit: no browser ---------------------------------------------------------


def test_worker_environment_is_built_from_an_allowlist() -> None:
    token = SecretStr("w" * 43)
    environment = worker_environment(
        token=token,
        site_origin="http://127.0.0.1:8801",
        headless=True,
        parent_pid=4242,
        source={
            "SystemRoot": r"C:\Windows",
            "DATABASE_URL": "postgresql+asyncpg://secret",
            "LUMI_RUNTIME_TOKEN": "runtime-secret",
            "OPENAI_API_KEY": "sk-secret",
            "BROWSER_WORKER_TOKEN": "other",
        },
    )
    assert environment["SystemRoot"] == r"C:\Windows"
    assert environment["LUMI_BROWSER_TOKEN"] == token.get_secret_value()
    assert environment["LUMI_BROWSER_ALLOWED_ORIGINS"] == "appointment_fixture=http://127.0.0.1:8801"
    assert environment["LUMI_BROWSER_PARENT_PID"] == "4242"
    for leaked in ("DATABASE_URL", "LUMI_RUNTIME_TOKEN", "OPENAI_API_KEY", "BROWSER_WORKER_TOKEN"):
        assert leaked not in environment
    assert "secret" not in "".join(environment.values())


@pytest.mark.parametrize(
    "line",
    [
        b'{"event": "lumi-worker-ready", "port": 0}\n',
        b'{"event": "lumi-worker-ready", "port": 70000}\n',
        b'{"event": "lumi-worker-ready", "port": true}\n',
        b'{"event": "lumi-worker-ready", "port": 8000, "token": "x"}\n',
        b'{"event": "other", "port": 8000}\n',
        b'{"event": "lumi-worker-ready", "port": 8000}',
        b"not json\n",
        b"\xff\n",
        b"",
    ],
)
def test_malformed_worker_readiness_is_refused(line: bytes) -> None:
    with pytest.raises(ValueError):
        _ready_port(line)


def test_worker_readiness_record_yields_the_port() -> None:
    assert _ready_port(b'{"event": "lumi-worker-ready", "port": 8123}\n') == 8123


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:8801", "https://127.0.0.1:8801", "http://127.0.0.1", "http://127.0.0.1:0",
     "http://127.0.0.1:8801/path", "http://10.0.0.1:8801", "http://127.0.0.1:99999"],
)
def test_managed_site_origin_must_be_numeric_loopback(origin: str, migrated_database_url: str) -> None:
    with pytest.raises(ValueError):
        Settings(
            database_url=SecretStr(migrated_database_url),
            runtime_token=SecretStr("t" * 40),
            browser_site_origin=origin,
        )


async def test_concurrent_exclusive_proposals_create_one_action(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    proposal = {"site": "appointment_fixture", "slot_id": "slot-a-1830", "doctor": "Dr A",
                "time": "2026-09-19T18:30:00+05:30", "price": 800, "currency": "INR"}

    async def propose() -> object:
        try:
            return await action_service.propose_exclusive_action(
                task.id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=proposal
            )
        except ActionAlreadyOpenError as error:
            return error

    results = await asyncio.gather(*(propose() for _ in range(5)))
    assert sum(not isinstance(result, ActionAlreadyOpenError) for result in results) == 1
    assert len(await action_service.list_actions(task.id)) == 1


async def test_an_unknown_outcome_blocks_preparing_another_booking(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    proposal = {"site": "appointment_fixture", "slot_id": "slot-a-1830", "doctor": "Dr A",
                "time": "2026-09-19T18:30:00+05:30", "price": 800, "currency": "INR"}
    view = await action_service.propose_exclusive_action(
        task.id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=proposal
    )
    view = await action_service.request_approval(view.action.id)
    view = await action_service.approve_action(view.action.id)
    view = await action_service.start_attempt(view.action.id)
    from app.domain.action_status import AttemptOutcome

    await action_service.finish_attempt(view.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN)
    with pytest.raises(ActionAlreadyOpenError):
        await action_service.propose_exclusive_action(
            task.id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=proposal
        )


async def test_prepare_body_accepts_only_a_safe_slot_id(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": BOOKING_REQUEST})).json()
    for body in (
        {"slot_id": "slot-a-1830", "price": 1},
        {"slot_id": "../bookings/lookup?reference=x"},
        {"slot_id": ""},
        {"slot_id": "a" * 65},
        {},
    ):
        response = await client.post(f"/tasks/{task['id']}/booking/prepare", json=body)
        assert response.status_code == 422, body
        assert response.json() == {
            "error": {"code": "invalid_request", "message": "Request validation failed."}
        }


async def test_booking_routes_without_a_worker_answer_503(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": BOOKING_REQUEST})).json()
    response = await client.post(f"/tasks/{task['id']}/booking/search")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "browser_worker_not_configured"


async def test_task_response_reports_its_last_event_sequence(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": BOOKING_REQUEST})).json()
    assert task["last_event_sequence"] == 1


# ---- with a real browser worker ----------------------------------------------


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Process]:
    with fixture_site(tmp_path_factory.mktemp("prep") / "site.log") as process:
        yield process


@pytest.fixture(scope="module")
def worker(site: Process, tmp_path_factory: pytest.TempPathFactory) -> Iterator[WorkerProcess]:
    logs = tmp_path_factory.mktemp("prep")
    with browser_worker(logs / "worker.log", site_origin=site.base_url) as process:
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
def preparation(
    task_service: TaskService,
    action_service: ActionService,
    runtime_generation: RuntimeGeneration,
    worker_config: BrowserWorkerConfig,
) -> BookingPreparationService:
    return BookingPreparationService(
        tasks=task_service,
        actions=action_service,
        runtime_generation=runtime_generation.id,
        worker=worker_config,
    )


@pytest.fixture
def execution(
    engine: AsyncEngine,
    action_service: ActionService,
    runtime_generation: RuntimeGeneration,
    worker_config: BrowserWorkerConfig,
) -> BrowserExecutionService:
    return BrowserExecutionService(
        engine, actions=action_service, runtime_generation=runtime_generation.id, worker=worker_config
    )


@pytest.mark.browser
async def test_search_reads_the_site_with_the_persisted_criteria(
    preparation: BookingPreparationService, task_service: TaskService, control: SiteControl
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    slots = await preparation.search(task.id)
    assert [(slot.slot_id, slot.doctor, slot.price) for slot in slots] == [
        ("slot-a-1830", "Dr A", 800),
        ("slot-b-1915", "Dr B", 950),
    ]
    assert all(slot.specialty == "Dermatology" for slot in slots)
    assert control.state()["submissions"] == 0


@pytest.mark.browser
async def test_prepare_uses_the_price_the_worker_observes_now(
    preparation: BookingPreparationService, task_service: TaskService, control: SiteControl
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    control.set_faults(price_overrides={"slot-a-1830": 1234}, hostile_text=True)
    view = await preparation.prepare(task.id, "slot-a-1830")
    assert view.action.status is ActionStatus.WAITING_APPROVAL
    assert view.action.tool_name == "commit_booking"
    assert view.action.proposal == {
        "site": "appointment_fixture",
        "slot_id": "slot-a-1830",
        "doctor": "Dr A",
        "time": "2026-09-19T18:30:00+05:30",
        "price": 1234,
        "currency": "INR",
    }
    assert view.approval is not None and view.approval.action_revision == view.action.revision
    state = control.state()
    assert state["submissions"] == 0 and state["booking_count"] == 0


@pytest.mark.browser
async def test_a_missing_slot_proposes_nothing(
    preparation: BookingPreparationService,
    task_service: TaskService,
    action_service: ActionService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    control.set_faults(removed_slots=["slot-a-1830"])
    with pytest.raises(BookingSlotUnavailableError):
        await preparation.prepare(task.id, "slot-a-1830")
    assert await action_service.list_actions(task.id) == []


@pytest.mark.browser
async def test_only_booking_tasks_can_prepare(
    preparation: BookingPreparationService, task_service: TaskService
) -> None:
    task = await task_service.create_task({"type": "note"})
    with pytest.raises(TaskKindMismatchError):
        await preparation.prepare(task.id, "slot-a-1830")


@pytest.mark.browser
async def test_a_second_booking_waits_until_the_first_is_rejected(
    preparation: BookingPreparationService,
    task_service: TaskService,
    action_service: ActionService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    first = await preparation.prepare(task.id, "slot-a-1830")
    with pytest.raises(ActionAlreadyOpenError):
        await preparation.prepare(task.id, "slot-b-1915")
    await action_service.reject_action(first.action.id, expected_revision=first.action.revision)
    second = await preparation.prepare(task.id, "slot-b-1915")
    assert second.action.idempotency_key == "commit_booking-2"
    assert control.state()["submissions"] == 0


@pytest.mark.browser
async def test_execution_refuses_a_stale_revision_before_spending_the_approval(
    preparation: BookingPreparationService,
    execution: BrowserExecutionService,
    task_service: TaskService,
    action_service: ActionService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    prepared = await preparation.prepare(task.id, "slot-a-1830")
    approved = await action_service.approve_action(
        prepared.action.id, expected_revision=prepared.action.revision
    )
    with pytest.raises(StaleActionRevisionError):
        await execution.execute_booking(
            approved.action.id, expected_revision=prepared.action.revision
        )
    current = await action_service.get_action(approved.action.id)
    assert current.action.status is ActionStatus.APPROVED
    assert current.attempts == ()
    assert current.approval is not None
    assert control.state()["submissions"] == 0

    done = await execution.execute_booking(
        approved.action.id, expected_revision=approved.action.revision
    )
    assert done.action.status is ActionStatus.SUCCEEDED
    state = control.state()
    assert state["booking_count"] == 1 and state["submissions"] == 1


@pytest.mark.browser
async def test_reconciliation_of_a_non_unknown_action_is_refused_without_a_lookup(
    preparation: BookingPreparationService,
    execution: BrowserExecutionService,
    task_service: TaskService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    prepared = await preparation.prepare(task.id, "slot-a-1830")
    with pytest.raises(InvalidActionTransitionError):
        await execution.reconcile_booking(prepared.action.id)
    assert control.state()["lookups"] == 0


@pytest.mark.browser
async def test_a_changed_price_fails_known_and_the_updated_details_need_a_new_approval(
    preparation: BookingPreparationService,
    execution: BrowserExecutionService,
    task_service: TaskService,
    action_service: ActionService,
    control: SiteControl,
) -> None:
    task = await task_service.create_task(BOOKING_REQUEST)
    prepared = await preparation.prepare(task.id, "slot-a-1830")
    approved = await action_service.approve_action(prepared.action.id)
    control.set_faults(price_overrides={"slot-a-1830": 950})
    failed = await execution.execute_booking(approved.action.id, expected_revision=approved.action.revision)
    assert failed.action.status is ActionStatus.FAILED
    result = failed.attempts[0].result
    assert result is not None
    assert result["changed_facts"] == [{"field": "price", "approved": "800", "observed": "950"}]
    assert control.state()["submissions"] == 0

    updated = await preparation.prepare(task.id, "slot-a-1830")
    assert updated.action.id != failed.action.id
    assert updated.action.proposal["price"] == 950
    assert updated.action.status is ActionStatus.WAITING_APPROVAL


@pytest.mark.browser
def test_worker_refuses_non_read_only_work_without_an_action(worker: WorkerProcess) -> None:
    identity = worker.identity()
    response = httpx.post(
        f"{worker.base_url}/v1/dispatch",
        headers={WORKER_TOKEN_HEADER: worker.token.get_secret_value()},
        json={
            "dispatch_id": str(uuid.uuid4()),
            "runtime_generation": str(uuid.uuid4()),
            "expected_worker_generation": identity["worker_generation"],
            "operation": "prepare_booking",
            "site": "appointment_fixture",
            "input": {"slot_id": "slot-a-1830", "reference": "lumi-x"},
        },
        timeout=30,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "action_required"


@pytest.mark.browser
def test_worker_rejects_a_non_ascii_token_without_crashing(worker: WorkerProcess) -> None:
    response = httpx.get(
        f"{worker.base_url}/health",
        headers={WORKER_TOKEN_HEADER.encode("ascii"): "tökén-with-a-long-enough-body".encode("utf-8")},
        timeout=30,
    )
    assert response.status_code == 401


@pytest.mark.browser
async def test_managed_worker_starts_rotates_and_stops(site: Process, control: SiteControl) -> None:
    managed = ManagedBrowserWorker(
        site_origin=site.base_url, headless=True, timeout_seconds=30.0, maximum_starts=2
    )
    try:
        first = await managed.endpoint()
        first_pid = managed.pid
        assert first.base_url.startswith("http://127.0.0.1:")
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"{first.base_url}/health",
                headers={WORKER_TOKEN_HEADER: first.token.get_secret_value()},
            )
            assert health.status_code == 200
            assert health.json()["sites"] == ["appointment_fixture"]
        assert await managed.endpoint() is first

        # A worker that dies is replaced with a new process and a new credential.
        import subprocess

        process = managed._process
        assert process is not None
        process.kill()
        await asyncio.to_thread(process.wait, 30)
        second = await managed.endpoint()
        assert managed.pid != first_pid
        assert second.token.get_secret_value() != first.token.get_secret_value()
        assert isinstance(process, subprocess.Popen)
    finally:
        await managed.aclose()
    assert managed._process is None
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{second.base_url}/health", timeout=2)


def _managed_worker_processes() -> int:
    """Live processes started with the managed worker's argument vector."""
    import json as _json
    import subprocess

    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like "
        "'*app.browser.main*--ready-stdout*' } | Select-Object -ExpandProperty ProcessId "
        "| ConvertTo-Json"
    )
    output = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    if not output:
        return 0
    parsed = _json.loads(output)
    return len(parsed) if isinstance(parsed, list) else 1


@pytest.mark.browser
@pytest.mark.hardkill
@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows job ownership")
def test_a_managed_runtime_prepares_and_its_worker_dies_with_it(
    migrated_database_url: str, site: Process, control: SiteControl, tmp_path: Path
) -> None:
    import subprocess
    import sys
    import time

    from app.config import AGENT_ROOT
    from tests.browser_harness import (
        RuntimeHttp,
        free_port,
        mint_runtime_token,
        ok,
        runtime_arguments,
        runtime_environment,
    )
    from tests.conftest import truncate_all

    truncate_all(migrated_database_url)
    assert _managed_worker_processes() == 0
    port, token = free_port(), mint_runtime_token()
    base_url = f"http://127.0.0.1:{port}"
    api = RuntimeHttp(base_url, token)
    environment = {
        **runtime_environment(migrated_database_url, token),
        "LUMI_BROWSER_SITE_ORIGIN": site.base_url,
        "LUMI_BROWSER_HEADLESS": "true",
    }
    log_path = tmp_path / "runtime.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, *runtime_arguments(port)],
            cwd=AGENT_ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while True:
                assert process.poll() is None, log_path.read_text(errors="replace")
                try:
                    if api.get(f"{base_url}/health", timeout=2).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, log_path.read_text(errors="replace")
                time.sleep(0.2)

            task = ok(api.post(f"{base_url}/tasks", json={"request": BOOKING_REQUEST}, timeout=30))
            search = ok(api.post(f"{base_url}/tasks/{task['id']}/booking/search", timeout=120))
            assert [slot["slot_id"] for slot in search["slots"]] == ["slot-a-1830", "slot-b-1915"]
            action = ok(api.post(
                f"{base_url}/tasks/{task['id']}/booking/prepare",
                json={"slot_id": "slot-b-1915"}, timeout=120,
            ))
            assert action["status"] == "WAITING_APPROVAL"
            assert action["proposal"]["price"] == 950
            assert _managed_worker_processes() >= 1
        finally:
            process.kill()
            process.wait(timeout=30)

    deadline = time.monotonic() + 20
    while _managed_worker_processes() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert _managed_worker_processes() == 0
    assert control.state()["submissions"] == 0
