"""Milestone 10 S5: regressions for the S5 adversarial review (see docs/reviews/milestone-10-s5.md, section 6)."""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.effects import EffectLockedError, ReconciliationLimitedError, placement_key
from app.domain.errors import InvalidActionTransitionError
from app.repositories.browser import BrowserRepository
from app.services.actions import ActionService, ActionView
from app.services.recovery import RecoveryService
from app.services.tasks import TaskService
from tests.test_effect_registry import (
    BOOKING,
    ROOT,
    _answer,
    _lookup_service,
    approved_booking,
    key_rows,
    unknown_booking,
)

UNREVIEWED = {**BOOKING, "site": "eventually_consistent_clinic"}


async def _worker(engine: AsyncEngine, runtime: Any) -> uuid.UUID:
    worker = uuid.uuid4()
    async with engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=worker, runtime_generation=runtime.id, worker_started_at=datetime.now(UTC)
        )
    return worker


async def _commit_dispatch(engine: AsyncEngine, action: ActionView, worker: uuid.UUID, error_code: str) -> None:
    async with engine.begin() as connection:
        repository = BrowserRepository(connection)
        dispatch_id = uuid.uuid4()
        await repository.insert_dispatch(
            dispatch_id=dispatch_id, action_id=action.action.id, attempt_id=action.attempts[0].id,
            worker_generation=worker, operation="commit_booking", site="appointment_fixture",
            effect=BrowserEffect.CONSEQUENTIAL,
        )
        await repository.finish_dispatch(
            dispatch_id=dispatch_id, status=DispatchStatus.OUTCOME_UNKNOWN, submitted=False, observation_id=None,
            error_code=error_code, duration_ms=None, result={},
        )


async def test_review_1_absence_is_not_authoritative_while_the_same_worker_may_still_be_committing(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    worker = await _worker(engine, runtime_generation)
    # The runtime lost the commit's answer, and the worker that received it is the one answering the lookup.
    await _commit_dispatch(engine, first, worker, "browser_worker_lost_response")
    same = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    same.fixed_worker = worker
    done = await same.reconcile_booking(first.action.id)
    assert done.action.status is ActionStatus.OUTCOME_UNKNOWN
    # A different worker generation: the one holding the commit is gone, so the fixture's absence is a fact.
    fresh = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    assert (await fresh.reconcile_booking(first.action.id)).action.status is ActionStatus.FAILED


async def test_review_1_a_commit_the_worker_itself_answered_is_settled_even_on_the_same_worker(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    worker = await _worker(engine, runtime_generation)
    await _commit_dispatch(engine, first, worker, "timeout_after_submission")
    same = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    same.fixed_worker = worker
    assert (await same.reconcile_booking(first.action.id)).action.status is ActionStatus.FAILED


async def test_review_2_a_booking_approval_refused_by_the_lock_is_withdrawn(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    second = await approved_booking(action_service, task_service)
    service = _lookup_service(engine, action_service, runtime_generation, _answer("FOUND"))
    with pytest.raises(EffectLockedError):
        await service.execute_booking(second.action.id)
    assert service.operations == []
    assert (await action_service.get_action(second.action.id)).action.status is ActionStatus.REJECTED
    reconciling = await action_service.begin_reconciliation(first.action.id)
    await action_service.finish_reconciliation(
        first.action.id, result=AttemptOutcome.SUCCEEDED, expected_revision=reconciling.action.revision
    )
    # After A is known, B's old approval cannot book: a fresh card and approval are required.
    with pytest.raises(InvalidActionTransitionError):
        await action_service.start_attempt(second.action.id)


async def test_review_3_a_lookup_in_flight_pauses_more_lookups_and_startup_closes_an_orphan(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service, UNREVIEWED)
    worker = await _worker(engine, runtime_generation)
    async with engine.begin() as connection:
        await BrowserRepository(connection).insert_dispatch(
            dispatch_id=uuid.uuid4(), action_id=first.action.id, attempt_id=None, worker_generation=worker,
            operation="lookup_booking", site="eventually_consistent_clinic", effect=BrowserEffect.READ_ONLY,
        )
    service = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    with pytest.raises(ReconciliationLimitedError) as limited:
        await service.reconcile_booking(first.action.id)
    assert limited.value.reason == "lookup_in_flight" and service.operations == []
    await RecoveryService(engine).recover_interrupted_reconciliations()  # startup: nothing can be in flight yet
    await service.reconcile_booking(first.action.id)
    assert service.operations == ["lookup_booking"]


async def test_review_3_concurrent_reconciliations_never_exceed_the_bound(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service, UNREVIEWED)
    service = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    await service.reconcile_booking(first.action.id)
    await service.reconcile_booking(first.action.id)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE browser_dispatches SET started_at = now() - interval '1 hour' WHERE action_id = :a"),
            {"a": first.action.id},
        )
    await asyncio.gather(*(service.reconcile_booking(first.action.id) for _ in range(5)), return_exceptions=True)
    # Two earlier looks, then at most ONE more: the others were refused under the task row lock.
    assert len(service.operations) <= 3
    lookups = await _count(engine, first.action.id)
    assert lookups == len(service.operations)


async def _count(engine: AsyncEngine, action_id: uuid.UUID) -> int:
    async with engine.connect() as connection:
        value = await connection.scalar(
            text("SELECT count(*) FROM browser_dispatches WHERE action_id = :a AND operation = 'lookup_booking'"),
            {"a": action_id},
        )
    return int(value or 0)


async def test_review_4_the_generic_route_adds_nothing_to_a_controller_task(
    client: httpx.AsyncClient, task_service: TaskService
) -> None:
    for kind in ("file_transfer_task", "project_run_task", "desktop_action", "document_task"):
        task = await task_service.create_task({"type": kind})
        for key in ("transfer-download", "transfer-place", "project-start"):
            response = await client.post(
                f"/tasks/{task.id}/actions",
                json={"idempotency_key": key, "tool_name": "record_note", "risk_tier": "R0", "proposal": {}},
            )
            assert response.status_code == 422, (kind, key)
        assert (await client.get(f"/tasks/{task.id}/actions")).json()["actions"] == []


def test_review_6_names_ntfs_treats_as_equal_share_one_key() -> None:
    assert placement_key(root_id=ROOT, file_name="fıle.pdf") == placement_key(root_id=ROOT, file_name="FILE.pdf")
    assert placement_key(root_id=ROOT, file_name="Resume.pdf") == placement_key(root_id=ROOT, file_name="rESUME.PDF")


async def test_review_7_an_underivable_unresolved_action_is_keyed_conservatively(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    task = await task_service.create_task({"type": "file_transfer_task"})
    view = await action_service.propose_exclusive_action(
        task.id, tool_name="transfer_place", risk_tier=RiskTier.R2, proposal={"broken": True}
    )
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE actions SET status = 'OUTCOME_UNKNOWN', revision = revision + 1 WHERE id = :a"),
            {"a": view.action.id},
        )
    assert await RecoveryService(engine).backfill_effect_keys() == [view.action.id]
    assert await key_rows(engine, view.action.id) == [("unparsed:file_create:transfer_place", "file_create")]


async def test_final_audit_a3_generic_task_creation_cannot_mint_a_controller_task(client: httpx.AsyncClient) -> None:
    for kind in ("document_task", "file_transfer_task", "project_run_task", "desktop_action_planning", "desktop_vision"):
        response = await client.post("/tasks", json={"request": {"type": kind, "text": "x"}})
        assert response.status_code == 422, kind
