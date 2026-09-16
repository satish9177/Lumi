"""Acceptance: a runtime that dies mid-execution never guesses what happened.

The scenario these tests protect is the whole point of Milestone 2. An approved
consequential action has been dispatched; the process dies before any result
comes back. The side effect may or may not have reached the outside world, and
nothing in the database can say which. The only honest answer is
OUTCOME_UNKNOWN, and the only way out of it is authoritative reconciliation --
never a blind retry.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.task_status import TaskStatus
from app.services.actions import ActionService
from app.services.recovery import RecoveryService
from app.services.runtime import register_runtime_generation
from app.services.tasks import TaskService
from tests.conftest import truncate_all

REQUEST = {"type": "appointment_booking", "text": "Book Saturday evening"}
PROPOSAL = {
    "appointment_id": "slot-123",
    "doctor": "Dr Example",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
}
PROPOSE = {
    "idempotency_key": "booking-001",
    "tool_name": "commit_booking",
    "risk_tier": "R2",
    "proposal": PROPOSAL,
}


# ---- the subprocess runtime -------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _start_runtime(database_url: str, port: int, log: IO[bytes]) -> subprocess.Popen[bytes]:
    from app.config import AGENT_ROOT

    environment = {**os.environ, "DATABASE_URL": database_url}
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "--factory", "app.main:create_app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=AGENT_ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _wait_healthy(process: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"runtime exited early:\n{log_path.read_text(errors='replace')}")
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    pytest.fail(f"runtime never became healthy:\n{log_path.read_text(errors='replace')}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=30)


def _json(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def _drive_to_executing(base_url: str) -> dict[str, Any]:
    """Task -> proposal -> approval request -> approval -> execution attempt."""
    task = _json(httpx.post(f"{base_url}/tasks", json={"request": REQUEST}))
    action = _json(httpx.post(f"{base_url}/tasks/{task['id']}/actions", json=PROPOSE))
    action = _json(
        httpx.post(
            f"{base_url}/actions/{action['id']}/approval-request",
            json={"expected_revision": action["revision"]},
        )
    )
    action = _json(
        httpx.post(
            f"{base_url}/actions/{action['id']}/approve",
            json={"expected_revision": action["revision"]},
        )
    )
    assert action["status"] == "APPROVED"
    return _json(
        httpx.post(
            f"{base_url}/actions/{action['id']}/attempts",
            json={"expected_revision": action["revision"]},
        )
    )


# ---- the acceptance test ----------------------------------------------------


@pytest.mark.parametrize(
    ("reconciled_as", "expected_action_status", "expected_task_status"),
    [
        ("SUCCEEDED", "SUCCEEDED", "READY"),
        ("FAILED", "FAILED", "READY"),
        ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
    ],
)
def test_a_hard_killed_execution_becomes_outcome_unknown_and_is_reconciled(
    migrated_database_url: str,
    tmp_path: Path,
    reconciled_as: str,
    expected_action_status: str,
    expected_task_status: str,
) -> None:
    truncate_all(migrated_database_url)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    # 1-6. Runtime A: approve a consequential action and persist the intent to
    # execute it, then confirm the ledger really is mid-flight.
    first_log = tmp_path / "first.log"
    with first_log.open("wb") as log:
        first = _start_runtime(migrated_database_url, port, log)
        try:
            _wait_healthy(first, base_url, first_log)
            executing = _drive_to_executing(base_url)
            action_id = executing["id"]
            task_id = executing["task_id"]

            assert executing["status"] == "EXECUTING"
            assert len(executing["attempts"]) == 1
            original_attempt = executing["attempts"][0]
            assert original_attempt["finished_at"] is None
            assert original_attempt["outcome"] is None
            assert executing["approval"] is None  # Claimed by this attempt.
            assert _json(httpx.get(f"{base_url}/tasks/{task_id}"))["status"] == "EXECUTING"
        finally:
            # 7. Hard kill. No graceful shutdown, no cleanup hook, nothing
            # in-process gets a chance to record what happened.
            _stop(first)
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{base_url}/health", timeout=1)

    # 8-10. Runtime B: startup recovery must refuse to guess.
    second_log = tmp_path / "second.log"
    with second_log.open("wb") as log:
        second = _start_runtime(migrated_database_url, port, log)
        try:
            _wait_healthy(second, base_url, second_log)
            assert second.pid != first.pid
            recovered = _json(httpx.get(f"{base_url}/actions/{action_id}"))

            assert recovered["status"] == "OUTCOME_UNKNOWN"
            assert _json(httpx.get(f"{base_url}/tasks/{task_id}"))["status"] == "OUTCOME_UNKNOWN"
            # The original attempt is still there, now carrying an unknown
            # outcome. It was not rewritten into a failure.
            assert len(recovered["attempts"]) == 1
            attempt = recovered["attempts"][0]
            assert attempt["id"] == original_attempt["id"]
            assert attempt["attempt_number"] == 1
            assert attempt["outcome"] == "OUTCOME_UNKNOWN"
            assert attempt["finished_at"] is not None
            assert attempt["error_code"] == "runtime_restart"
            assert attempt["runtime_generation"] == original_attempt["runtime_generation"]
            # And crucially: nothing was retried on the strength of a guess.
            assert recovered["approval"] is None

            # 11-13. Authoritative reconciliation, which looks rather than acts.
            reconciling = _json(
                httpx.post(
                    f"{base_url}/actions/{action_id}/reconciliation",
                    json={"expected_revision": recovered["revision"]},
                )
            )
            assert reconciling["status"] == "RECONCILING"
            final = _json(
                httpx.post(
                    f"{base_url}/actions/{action_id}/reconciliation/finish",
                    json={
                        "result": reconciled_as,
                        "evidence": {"source": "test_fixture"},
                        "expected_revision": reconciling["revision"],
                    },
                )
            )
            events = _json(httpx.get(f"{base_url}/tasks/{task_id}/events"))["events"]
        finally:
            _stop(second)

    assert final["status"] == expected_action_status
    # Still exactly one execution attempt, start to finish.
    assert len(final["attempts"]) == 1
    assert final["attempts"][0]["id"] == original_attempt["id"]
    assert final["attempts"][0]["attempt_number"] == 1

    assert [event["event_type"] for event in events] == [
        "task.created",
        "action.proposed",
        "action.approval_requested",
        "action.approved",
        "action.execution_started",
        "action.outcome_unknown",
        "action.reconciliation_started",
        "action.reconciled",
    ]
    unknown = next(e for e in events if e["event_type"] == "action.outcome_unknown")
    assert unknown["payload"]["reason"] == "runtime_restart"
    assert unknown["payload"]["attempt_id"] == original_attempt["id"]
    reconciled = events[-1]
    assert reconciled["payload"]["result"] == reconciled_as
    assert reconciled["payload"]["evidence"] == {"source": "test_fixture"}


def test_a_recovered_action_is_still_refused_a_second_attempt(
    migrated_database_url: str, tmp_path: Path
) -> None:
    """The recovered action must not be executable again, in any state."""
    truncate_all(migrated_database_url)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    first_log = tmp_path / "first.log"
    with first_log.open("wb") as log:
        first = _start_runtime(migrated_database_url, port, log)
        try:
            _wait_healthy(first, base_url, first_log)
            executing = _drive_to_executing(base_url)
        finally:
            _stop(first)

    second_log = tmp_path / "second.log"
    with second_log.open("wb") as log:
        second = _start_runtime(migrated_database_url, port, log)
        try:
            _wait_healthy(second, base_url, second_log)
            action_id = executing["id"]

            retry = httpx.post(f"{base_url}/actions/{action_id}/attempts")
            reapprove = httpx.post(f"{base_url}/actions/{action_id}/approval-request")
            finish = httpx.post(
                f"{base_url}/actions/{action_id}/attempts/finish",
                json={"outcome": "SUCCEEDED"},
            )
            final = _json(httpx.get(f"{base_url}/actions/{action_id}"))
        finally:
            _stop(second)

    assert retry.status_code == 409
    assert reapprove.status_code == 409
    assert finish.status_code == 409
    assert final["status"] == "OUTCOME_UNKNOWN"
    assert len(final["attempts"]) == 1


def test_restarting_twice_does_not_re_recover_or_duplicate_events(
    migrated_database_url: str, tmp_path: Path
) -> None:
    truncate_all(migrated_database_url)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    with (tmp_path / "first.log").open("wb") as log:
        first = _start_runtime(migrated_database_url, port, log)
        try:
            _wait_healthy(first, base_url, tmp_path / "first.log")
            executing = _drive_to_executing(base_url)
        finally:
            _stop(first)

    timelines = []
    for name in ("second", "third"):
        log_path = tmp_path / f"{name}.log"
        with log_path.open("wb") as log:
            runtime = _start_runtime(migrated_database_url, port, log)
            try:
                _wait_healthy(runtime, base_url, log_path)
                events = _json(
                    httpx.get(f"{base_url}/tasks/{executing['task_id']}/events")
                )["events"]
                timelines.append([event["event_type"] for event in events])
            finally:
                _stop(runtime)

    # The second recovery pass finds nothing left to recover.
    assert timelines[0] == timelines[1]
    assert timelines[0].count("action.outcome_unknown") == 1


# ---- in-process recovery behaviour ------------------------------------------


async def test_recovery_leaves_the_current_generations_own_work_alone(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    """Healthy in-flight work is not stale work; ownership decides, not a timeout."""
    task = await task_service.create_task({"type": "appointment_booking"})
    view, _ = await action_service.propose_action(
        task.id,
        idempotency_key="booking-001",
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=PROPOSAL,
    )
    view = await action_service.request_approval(view.action.id)
    view = await action_service.approve_action(view.action.id)
    view = await action_service.start_attempt(view.action.id)

    recovered = await RecoveryService(engine).recover_unfinished_attempts(runtime_generation.id)

    assert recovered == []
    current = await action_service.get_action(view.action.id)
    assert current.action.status is ActionStatus.EXECUTING
    assert current.attempts[0].finished_at is None


async def test_recovery_only_touches_unfinished_attempts(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    task = await task_service.create_task({"type": "appointment_booking"})
    view, _ = await action_service.propose_action(
        task.id,
        idempotency_key="booking-001",
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=PROPOSAL,
    )
    view = await action_service.request_approval(view.action.id)
    view = await action_service.approve_action(view.action.id)
    view = await action_service.start_attempt(view.action.id)
    await action_service.finish_attempt(view.action.id, outcome=AttemptOutcome.SUCCEEDED)

    next_generation = await register_runtime_generation(engine)
    recovered = await RecoveryService(engine).recover_unfinished_attempts(next_generation.id)

    assert recovered == []
    current = await action_service.get_action(view.action.id)
    assert current.action.status is ActionStatus.SUCCEEDED
    assert (await task_service.get_task(task.id)).status is TaskStatus.READY
