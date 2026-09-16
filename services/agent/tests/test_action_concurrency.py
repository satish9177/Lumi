"""Races on one action. Every one of these must produce a single side effect."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import action_attempts, approvals
from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.errors import (
    ActionProposalConflictError,
    ApprovalNotUsableError,
    InvalidActionTransitionError,
    StaleActionRevisionError,
)
from app.repositories.tasks import TaskRecord
from app.services.actions import ActionService, ActionView
from app.services.tasks import TaskService

PROPOSAL = {"appointment_id": "slot-123", "doctor": "Dr Example", "price": 800}
CONCURRENCY = 8


async def _task(task_service: TaskService) -> TaskRecord:
    return await task_service.create_task({"type": "appointment_booking"})


async def _propose(
    service: ActionService, task_id: Any, *, proposal: dict[str, Any] | None = None
) -> tuple[ActionView, bool]:
    return await service.propose_action(
        task_id,
        idempotency_key="booking-001",
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=proposal if proposal is not None else PROPOSAL,
    )


async def _approved(service: ActionService, task_id: Any) -> ActionView:
    view, _ = await _propose(service, task_id)
    view = await service.request_approval(view.action.id)
    return await service.approve_action(view.action.id)


def _errors(results: Sequence[Any]) -> list[BaseException]:
    return [result for result in results if isinstance(result, BaseException)]


def _successes(results: Sequence[Any]) -> list[Any]:
    return [result for result in results if not isinstance(result, BaseException)]


async def _count(engine: AsyncEngine, table: Any, action_id: Any) -> int:
    async with engine.connect() as connection:
        total = await connection.scalar(
            select(func.count()).select_from(table).where(table.c.action_id == action_id)
        )
    return int(total or 0)


async def test_concurrent_duplicate_proposals_create_exactly_one_action(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await _task(task_service)

    results = await asyncio.gather(*(_propose(action_service, task.id) for _ in range(CONCURRENCY)))

    assert len({view.action.id for view, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events].count("action.proposed") == 1


async def test_concurrent_proposals_with_different_bodies_leave_the_first_intact(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await _task(task_service)
    bodies = [{**PROPOSAL, "price": price} for price in range(CONCURRENCY)]

    results = await asyncio.gather(
        *(_propose(action_service, task.id, proposal=body) for body in bodies),
        return_exceptions=True,
    )

    # Exactly one body wins; every other caller is told there is a conflict
    # rather than silently receiving an action for someone else's proposal.
    assert len(_successes(results)) == 1
    assert all(isinstance(error, ActionProposalConflictError) for error in _errors(results))
    actions = await action_service.list_actions(task.id)
    assert len(actions) == 1


async def test_concurrent_approvals_produce_one_transition_and_one_approval(
    action_service: ActionService, task_service: TaskService, engine: AsyncEngine
) -> None:
    task = await _task(task_service)
    view, _ = await _propose(action_service, task.id)
    view = await action_service.request_approval(view.action.id)

    results = await asyncio.gather(
        *(action_service.approve_action(view.action.id) for _ in range(CONCURRENCY)),
        return_exceptions=True,
    )

    assert len(_successes(results)) == 1
    assert all(isinstance(error, InvalidActionTransitionError) for error in _errors(results))
    async with engine.connect() as connection:
        granted = await connection.scalar(
            select(func.count())
            .select_from(approvals)
            .where(
                approvals.c.action_id == view.action.id,
                approvals.c.status == ApprovalStatus.APPROVED.value,
            )
        )
    assert granted == 1
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events].count("action.approved") == 1


async def test_concurrent_execution_starts_create_exactly_one_attempt(
    action_service: ActionService, task_service: TaskService, engine: AsyncEngine
) -> None:
    task = await _task(task_service)
    view = await _approved(action_service, task.id)

    results = await asyncio.gather(
        *(action_service.start_attempt(view.action.id) for _ in range(CONCURRENCY)),
        return_exceptions=True,
    )

    assert len(_successes(results)) == 1
    assert await _count(engine, action_attempts, view.action.id) == 1
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events].count("action.execution_started") == 1


async def test_a_consumed_approval_cannot_execute_twice(
    action_service: ActionService, task_service: TaskService, engine: AsyncEngine
) -> None:
    task = await _task(task_service)
    view = await _approved(action_service, task.id)
    started = await action_service.start_attempt(view.action.id)
    # Take the action back to a state where a replayed start would be tried.
    await action_service.finish_attempt(
        started.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN
    )

    for _ in range(3):
        try:
            await action_service.start_attempt(started.action.id)
        except (InvalidActionTransitionError, ApprovalNotUsableError):
            pass
        else:  # pragma: no cover - a second attempt would be the bug under test.
            raise AssertionError("a consumed approval funded a second attempt")

    assert await _count(engine, action_attempts, view.action.id) == 1


async def test_a_stale_action_revision_cannot_mutate_the_action(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await _task(task_service)
    view, _ = await _propose(action_service, task.id)
    stale = view.action.revision
    view = await action_service.request_approval(view.action.id)

    mutations: tuple[Callable[[], Awaitable[ActionView]], ...] = (
        lambda: action_service.approve_action(view.action.id, expected_revision=stale),
        lambda: action_service.reject_action(view.action.id, expected_revision=stale),
        lambda: action_service.start_attempt(view.action.id, expected_revision=stale),
    )
    for mutate in mutations:
        try:
            await mutate()
        except StaleActionRevisionError as error:
            assert error.current_revision == view.action.revision
        else:  # pragma: no cover
            raise AssertionError("a stale revision mutated the action")

    current = await action_service.get_action(view.action.id)
    assert current.action.status is ActionStatus.WAITING_APPROVAL
    assert current.action.revision == view.action.revision


async def test_concurrent_reconciliation_starts_produce_one_transition(
    action_service: ActionService, task_service: TaskService
) -> None:
    task = await _task(task_service)
    view = await _approved(action_service, task.id)
    view = await action_service.start_attempt(view.action.id)
    view = await action_service.finish_attempt(
        view.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN
    )

    results = await asyncio.gather(
        *(action_service.begin_reconciliation(view.action.id) for _ in range(CONCURRENCY)),
        return_exceptions=True,
    )

    assert len(_successes(results)) == 1
    events = await task_service.list_events(task.id)
    assert [e.event_type for e in events].count("action.reconciliation_started") == 1


async def test_concurrent_reject_and_execute_never_both_win(
    action_service: ActionService, task_service: TaskService, engine: AsyncEngine
) -> None:
    task = await _task(task_service)
    view = await _approved(action_service, task.id)

    results = await asyncio.gather(
        action_service.reject_action(view.action.id),
        action_service.start_attempt(view.action.id),
        return_exceptions=True,
    )

    assert len(_successes(results)) == 1
    final = await action_service.get_action(view.action.id)
    attempts = await _count(engine, action_attempts, view.action.id)
    # A rejected action has no attempt; an executing one was never rejected.
    assert (final.action.status is ActionStatus.REJECTED) == (attempts == 0)
