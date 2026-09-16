"""Invariants the database enforces, and approval expiry.

Application logic is not the only thing standing between Lumi and an impossible
state, so these go around the service layer and write SQL directly.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import action_attempts, actions, approvals
from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.errors import ApprovalNotUsableError
from app.domain.task_status import TaskStatus
from app.repositories.actions import ActionRepository
from app.services.actions import ActionService, ActionView
from app.services.tasks import TaskService

PROPOSAL = {"appointment_id": "slot-123", "doctor": "Dr Example", "price": 800}


async def _proposed(service: ActionService, task_service: TaskService) -> ActionView:
    task = await task_service.create_task({"type": "appointment_booking"})
    view, _ = await service.propose_action(
        task.id,
        idempotency_key="booking-001",
        tool_name="commit_booking",
        risk_tier=RiskTier.R2,
        proposal=PROPOSAL,
    )
    return view


async def _approved(service: ActionService, task_service: TaskService) -> ActionView:
    view = await _proposed(service, task_service)
    view = await service.request_approval(view.action.id)
    return await service.approve_action(view.action.id)


async def _expire(engine: AsyncEngine, approval_id: uuid.UUID) -> None:
    """Age an approval past its TTL without waiting for wall-clock time."""
    async with engine.begin() as connection:
        await connection.execute(
            update(approvals)
            .where(approvals.c.id == approval_id)
            .values(
                created_at=func.now() - text("interval '2 hours'"),
                expires_at=func.now() - text("interval '1 hour'"),
            )
        )


# ---- proposal immutability --------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"proposal": {"appointment_id": "slot-999", "doctor": "Dr Example", "price": 800}},
        {"proposal_digest": "f" * 64},
        {"tool_name": "cancel_booking"},
        {"risk_tier": RiskTier.R0.value},
        {"idempotency_key": "booking-002"},
    ],
)
async def test_the_database_refuses_to_change_a_stored_proposal(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    change: dict[str, Any],
) -> None:
    view = await _proposed(action_service, task_service)

    with pytest.raises(IntegrityError, match="immutable"):
        async with engine.begin() as connection:
            await connection.execute(
                update(actions)
                .where(actions.c.id == view.action.id)
                .values(revision=actions.c.revision + 1, **change)
            )

    stored = await action_service.get_action(view.action.id)
    assert stored.action.proposal == PROPOSAL
    assert stored.action.proposal_digest == view.action.proposal_digest


async def test_the_database_refuses_a_revision_that_does_not_move_forward(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _proposed(action_service, task_service)

    with pytest.raises(IntegrityError, match="revision must increase"):
        async with engine.begin() as connection:
            await connection.execute(
                update(actions)
                .where(actions.c.id == view.action.id)
                .values(status=ActionStatus.APPROVED.value)
            )


# ---- approval binding -------------------------------------------------------


async def test_an_action_can_have_only_one_live_approval(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await action_service.request_approval(
        (await _proposed(action_service, task_service)).action.id
    )

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(approvals).values(
                    id=uuid.uuid4(),
                    action_id=view.action.id,
                    action_revision=view.action.revision,
                    proposal_digest=view.action.proposal_digest,
                    status=ApprovalStatus.PENDING.value,
                    expires_at=func.now() + text("interval '5 minutes'"),
                )
            )


async def test_one_approval_can_never_fund_two_attempts(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    started = await action_service.start_attempt(view.action.id)
    approval_id = started.attempts[0].approval_id

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                insert(action_attempts).values(
                    id=uuid.uuid4(),
                    action_id=view.action.id,
                    attempt_number=2,
                    approval_id=approval_id,
                    runtime_generation=started.attempts[0].runtime_generation,
                )
            )


async def test_an_action_can_have_only_one_unfinished_attempt(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    started = await action_service.start_attempt(view.action.id)

    # A second approval would make this legal at the application layer; the
    # partial unique index is the backstop that still refuses it.
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            approval_id = uuid.uuid4()
            await connection.execute(
                insert(approvals).values(
                    id=approval_id,
                    action_id=view.action.id,
                    action_revision=view.action.revision,
                    proposal_digest=view.action.proposal_digest,
                    status=ApprovalStatus.CONSUMED.value,
                    approved_at=func.now(),
                    consumed_at=func.now(),
                    expires_at=func.now() + text("interval '5 minutes'"),
                )
            )
            await connection.execute(
                insert(action_attempts).values(
                    id=uuid.uuid4(),
                    action_id=view.action.id,
                    attempt_number=2,
                    approval_id=approval_id,
                    runtime_generation=started.attempts[0].runtime_generation,
                )
            )


async def test_an_attempt_cannot_be_finished_without_an_outcome(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    started = await action_service.start_attempt(view.action.id)

    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                update(action_attempts)
                .where(action_attempts.c.id == started.attempts[0].id)
                .values(finished_at=func.now())
            )


# ---- approval expiry --------------------------------------------------------


async def test_the_configured_ttl_is_what_the_approval_records(
    engine: AsyncEngine, task_service: TaskService, runtime_generation: Any
) -> None:
    service = ActionService(engine, runtime_generation=runtime_generation.id, approval_ttl_seconds=60)
    view = await service.request_approval((await _proposed(service, task_service)).action.id)

    assert view.approval is not None
    lifetime = (view.approval.expires_at - view.approval.created_at).total_seconds()
    assert lifetime == pytest.approx(60, abs=1)


async def test_an_expired_request_cannot_be_approved(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await action_service.request_approval(
        (await _proposed(action_service, task_service)).action.id
    )
    assert view.approval is not None
    await _expire(engine, view.approval.id)

    with pytest.raises(ApprovalNotUsableError, match="expired"):
        await action_service.approve_action(view.action.id)


async def test_an_expired_approval_cannot_execute(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    """Expiry is checked when the approval is claimed, not by a cleanup job."""
    view = await _approved(action_service, task_service)
    assert view.approval is not None
    await _expire(engine, view.approval.id)

    with pytest.raises(ApprovalNotUsableError, match="expired"):
        await action_service.start_attempt(view.action.id)

    stored = await action_service.get_action(view.action.id)
    assert stored.action.status is ActionStatus.APPROVED
    assert stored.attempts == ()


async def test_an_expired_approval_is_kept_for_audit(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    assert view.approval is not None
    await _expire(engine, view.approval.id)

    with pytest.raises(ApprovalNotUsableError):
        await action_service.start_attempt(view.action.id)

    async with engine.connect() as connection:
        stored = await ActionRepository(connection).list_approvals(view.action.id)
    assert [approval.id for approval in stored] == [view.approval.id]
    assert stored[0].approved_at is not None


async def test_a_rejected_approval_cannot_execute(
    action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    rejected = await action_service.reject_action(view.action.id)

    with pytest.raises(Exception) as error:
        await action_service.start_attempt(rejected.action.id)
    assert type(error.value).__name__ == "InvalidActionTransitionError"

    stored = await action_service.get_action(view.action.id)
    assert stored.attempts == ()


async def test_a_rejection_keeps_the_approval_record_for_audit(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    assert view.approval is not None

    await action_service.reject_action(view.action.id)

    async with engine.connect() as connection:
        stored = await ActionRepository(connection).list_approvals(view.action.id)
    assert stored[0].status is ApprovalStatus.REJECTED
    assert stored[0].rejected_at is not None
    assert stored[0].approved_at is not None  # The grant that happened is not erased.


# ---- approval / action binding ----------------------------------------------


async def test_an_approval_bound_to_a_superseded_revision_cannot_be_claimed(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    """Any change to the action after approval invalidates that approval."""
    view = await _approved(action_service, task_service)
    assert view.approval is not None
    async with engine.begin() as connection:
        await connection.execute(
            update(approvals)
            .where(approvals.c.id == view.approval.id)
            .values(action_revision=view.approval.action_revision - 1)
        )

    with pytest.raises(ApprovalNotUsableError, match="no longer matches"):
        await action_service.start_attempt(view.action.id)

    assert (await action_service.get_action(view.action.id)).attempts == ()


async def test_an_approval_bound_to_another_digest_cannot_be_claimed(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    assert view.approval is not None
    async with engine.begin() as connection:
        await connection.execute(
            update(approvals)
            .where(approvals.c.id == view.approval.id)
            .values(proposal_digest="a" * 64)
        )

    with pytest.raises(ApprovalNotUsableError, match="no longer matches"):
        await action_service.start_attempt(view.action.id)


async def test_claiming_an_approval_marks_it_consumed_exactly_once(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    assert view.approval is not None
    started = await action_service.start_attempt(view.action.id)

    async with engine.connect() as connection:
        repository = ActionRepository(connection)
        stored = await repository.list_approvals(view.action.id)
        # A fresh claim on the same approval finds nothing to claim.
        reclaimed = await repository.claim_approval(
            approval_id=view.approval.id,
            action_revision=view.approval.action_revision,
            proposal_digest=view.action.proposal_digest,
        )

    assert reclaimed is None
    assert [approval.status for approval in stored] == [ApprovalStatus.CONSUMED]
    assert stored[0].consumed_at is not None
    assert started.attempts[0].approval_id == view.approval.id


async def test_an_outcome_unknown_action_keeps_its_single_attempt(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    view = await _approved(action_service, task_service)
    view = await action_service.start_attempt(view.action.id)
    await action_service.finish_attempt(view.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN)

    async with engine.connect() as connection:
        total = await connection.scalar(
            select(func.count())
            .select_from(action_attempts)
            .where(action_attempts.c.action_id == view.action.id)
        )
    assert total == 1


# ---- action work never revives a terminal task ------------------------------


async def test_cancelling_a_task_mid_execution_does_not_get_undone_by_the_outcome(
    action_service: ActionService, task_service: TaskService
) -> None:
    """The outcome is still recorded, but a CANCELLED task stays cancelled."""
    view = await _approved(action_service, task_service)
    view = await action_service.start_attempt(view.action.id)
    cancelled = await task_service.cancel_task(view.action.task_id)

    resolved = await action_service.finish_attempt(
        view.action.id, outcome=AttemptOutcome.SUCCEEDED
    )

    assert resolved.action.status is ActionStatus.SUCCEEDED
    task = await task_service.get_task(cancelled.id)
    assert task.status is TaskStatus.CANCELLED
    # The timeline still records what the executor observed.
    events = await task_service.list_events(task.id)
    assert [event.event_type for event in events][-1] == "action.succeeded"


async def test_rejecting_an_action_on_a_cancelled_task_does_not_revive_it(
    action_service: ActionService, task_service: TaskService
) -> None:
    view = await _proposed(action_service, task_service)
    await task_service.cancel_task(view.action.task_id)

    rejected = await action_service.reject_action(view.action.id)

    assert rejected.action.status is ActionStatus.REJECTED
    task = await task_service.get_task(view.action.task_id)
    assert task.status is TaskStatus.CANCELLED
