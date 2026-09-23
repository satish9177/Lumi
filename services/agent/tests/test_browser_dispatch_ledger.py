"""PostgreSQL invariants for browser dispatches. No browser, no worker process.

The database is where "exactly once" is actually enforced. The application can
be careful, but careful is not a guarantee; these tests check that the wrong
thing cannot be written down even by code that tries.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import AttemptOutcome, RiskTier
from app.domain.effects import EffectLockedError
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.repositories.actions import ActionRepository
from app.repositories.browser import BrowserRepository
from app.services.actions import ActionService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService

PROPOSAL = {
    "site": "appointment_fixture",
    "slot_id": "slot-a-1830",
    "doctor": "Dr A",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
    "currency": "INR",
}


async def _executing_attempt(
    action_service: ActionService, task_service: TaskService, key: str = "booking-001"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Drive an action to EXECUTING. Returns (action_id, attempt_id)."""
    task = await task_service.create_task({"type": "appointment_booking"})
    view, _ = await action_service.propose_action(
        task.id,
        idempotency_key=key,
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=PROPOSAL,
    )
    view = await action_service.request_approval(view.action.id)
    view = await action_service.approve_action(view.action.id)
    view = await action_service.start_attempt(view.action.id)
    attempt = next(a for a in view.attempts if a.finished_at is None)
    return view.action.id, attempt.id


async def _worker_generation(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> uuid.UUID:
    worker_generation = uuid.uuid4()
    async with engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=worker_generation,
            runtime_generation=runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
    return worker_generation


async def _dispatch(
    engine: AsyncEngine,
    *,
    action_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    worker_generation: uuid.UUID,
    effect: BrowserEffect = BrowserEffect.CONSEQUENTIAL,
    operation: str = "commit_booking",
) -> uuid.UUID:
    dispatch_id = uuid.uuid4()
    async with engine.begin() as connection:
        await BrowserRepository(connection).insert_dispatch(
            dispatch_id=dispatch_id,
            action_id=action_id,
            attempt_id=attempt_id,
            worker_generation=worker_generation,
            operation=operation,
            site="appointment_fixture",
            effect=effect,
        )
    return dispatch_id


# ---- one dispatch per attempt -----------------------------------------------


async def test_one_execution_attempt_dispatches_browser_work_exactly_once(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """The load-bearing constraint. The database, not the code, refuses this."""
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)

    await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )
    with pytest.raises(IntegrityError):
        await _dispatch(
            engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
        )

    async with engine.connect() as connection:
        assert await BrowserRepository(connection).count_consequential_dispatches(action_id) == 1


async def test_consequential_browser_work_requires_an_execution_attempt(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """No attempt means no approval was claimed, so nothing may be consequential."""
    action_id, _ = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)

    with pytest.raises(IntegrityError):
        await _dispatch(
            engine, action_id=action_id, attempt_id=None, worker_generation=worker_generation
        )


async def test_a_read_only_lookup_needs_no_attempt(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """Reconciliation is not execution, and must not be counted as one."""
    action_id, _ = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)

    await _dispatch(
        engine,
        action_id=action_id,
        attempt_id=None,
        worker_generation=worker_generation,
        effect=BrowserEffect.READ_ONLY,
        operation="lookup_booking",
    )
    await _dispatch(
        engine,
        action_id=action_id,
        attempt_id=None,
        worker_generation=worker_generation,
        effect=BrowserEffect.READ_ONLY,
        operation="lookup_booking",
    )

    async with engine.connect() as connection:
        repository = BrowserRepository(connection)
        assert await repository.count_consequential_dispatches(action_id) == 0
        assert len(await repository.list_dispatches(action_id)) == 2


async def test_a_dispatch_must_name_a_registered_worker_generation(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    """Work cannot be attributed to a worker the runtime never handshook with."""
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    with pytest.raises(IntegrityError):
        await _dispatch(
            engine,
            action_id=action_id,
            attempt_id=attempt_id,
            worker_generation=uuid.uuid4(),
        )


# ---- closing a dispatch -----------------------------------------------------


async def test_a_dispatch_is_open_until_it_is_closed(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)
    dispatch_id = await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )

    async with engine.connect() as connection:
        open_row = await BrowserRepository(connection).get_dispatch(dispatch_id)
    assert open_row is not None
    assert open_row.status is DispatchStatus.DISPATCHED
    assert open_row.finished_at is None

    async with engine.begin() as connection:
        closed = await BrowserRepository(connection).finish_dispatch(
            dispatch_id=dispatch_id,
            status=DispatchStatus.OK,
            submitted=True,
            observation_id=uuid.uuid4(),
            error_code=None,
            duration_ms=412,
            result={"booking_id": "BK-0001"},
        )
    assert closed is not None
    assert closed.status is DispatchStatus.OK
    assert closed.finished_at is not None
    assert closed.submitted is True


async def test_a_closed_dispatch_is_never_reopened_or_rewritten(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """A late answer cannot overwrite an outcome that was already recorded."""
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)
    dispatch_id = await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )

    async with engine.begin() as connection:
        await BrowserRepository(connection).finish_dispatch(
            dispatch_id=dispatch_id,
            status=DispatchStatus.OUTCOME_UNKNOWN,
            submitted=True,
            observation_id=None,
            error_code="timeout_after_submission",
            duration_ms=30_000,
            result=None,
        )
    async with engine.begin() as connection:
        second = await BrowserRepository(connection).finish_dispatch(
            dispatch_id=dispatch_id,
            status=DispatchStatus.OK,
            submitted=True,
            observation_id=None,
            error_code=None,
            duration_ms=1,
            result=None,
        )
    assert second is None

    async with engine.connect() as connection:
        current = await BrowserRepository(connection).get_dispatch(dispatch_id)
    assert current is not None
    assert current.status is DispatchStatus.OUTCOME_UNKNOWN
    assert current.error_code == "timeout_after_submission"


async def test_closing_an_orphan_claims_nothing_about_the_submission(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)
    dispatch_id = await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )

    async with engine.begin() as connection:
        orphan = await BrowserRepository(connection).close_orphaned_dispatch(attempt_id)
    assert orphan is not None
    assert orphan.id == dispatch_id
    assert orphan.status is DispatchStatus.OUTCOME_UNKNOWN
    assert orphan.error_code == "runtime_restart"
    # Not a claim that nothing was submitted -- just nothing observed one.
    assert orphan.submitted is False

    # Running recovery twice does not rewrite an already-closed dispatch.
    async with engine.begin() as connection:
        assert await BrowserRepository(connection).close_orphaned_dispatch(attempt_id) is None


async def test_a_dispatch_cannot_be_both_finished_and_in_flight(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """`finished_at` and `DISPATCHED` are mutually exclusive, in the database."""
    from sqlalchemy import text

    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)
    dispatch_id = await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )

    with pytest.raises(DBAPIError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE browser_dispatches SET finished_at = now() WHERE id = :id"),
                {"id": dispatch_id},
            )


async def test_an_attempt_and_its_dispatch_stay_linked(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    action_id, attempt_id = await _executing_attempt(action_service, task_service)
    worker_generation = await _worker_generation(engine, runtime_generation)
    dispatch_id = await _dispatch(
        engine, action_id=action_id, attempt_id=attempt_id, worker_generation=worker_generation
    )

    async with engine.connect() as connection:
        found = await BrowserRepository(connection).get_dispatch_for_attempt(attempt_id)
        attempts = await ActionRepository(connection).list_attempts(action_id)
    assert found is not None
    assert found.id == dispatch_id
    assert [attempt.id for attempt in attempts] == [attempt_id]


async def test_two_actions_dispatch_independently(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    runtime_generation: RuntimeGeneration,
) -> None:
    """The one-per-attempt rule is per attempt. (Since M10 S5 two bookings are never IN FLIGHT at once --
    the shared effect lock refuses the second claim -- so the second runs after the first is settled.)"""
    worker_generation = await _worker_generation(engine, runtime_generation)
    first_action, first_attempt = await _executing_attempt(action_service, task_service, "a")

    await _dispatch(
        engine,
        action_id=first_action,
        attempt_id=first_attempt,
        worker_generation=worker_generation,
    )
    with pytest.raises(EffectLockedError):
        await _executing_attempt(action_service, task_service, "b")
    await action_service.finish_attempt(first_action, outcome=AttemptOutcome.SUCCEEDED)
    second_action, second_attempt = await _executing_attempt(action_service, task_service, "b")
    await _dispatch(
        engine,
        action_id=second_action,
        attempt_id=second_attempt,
        worker_generation=worker_generation,
    )

    async with engine.connect() as connection:
        repository = BrowserRepository(connection)
        assert await repository.count_consequential_dispatches(first_action) == 1
        assert await repository.count_consequential_dispatches(second_action) == 1
