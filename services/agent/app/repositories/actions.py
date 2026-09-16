import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, func, insert, null, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import action_attempts, actions, approvals, runtime_generations
from app.domain.action_status import (
    OPEN_APPROVAL_STATUSES,
    ActionStatus,
    ApprovalStatus,
    AttemptOutcome,
    RiskTier,
)


@dataclass(frozen=True, slots=True)
class ActionRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    idempotency_key: str
    tool_name: str
    risk_tier: RiskTier
    proposal: dict[str, Any]
    proposal_digest: str
    status: ActionStatus
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    id: uuid.UUID
    action_id: uuid.UUID
    action_revision: int
    proposal_digest: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    rejected_at: datetime | None
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: uuid.UUID
    action_id: uuid.UUID
    attempt_number: int
    approval_id: uuid.UUID
    runtime_generation: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None
    result: dict[str, Any] | None
    error_code: str | None
    created_at: datetime


def _action(row: Row[Any]) -> ActionRecord:
    return ActionRecord(
        id=row.id,
        task_id=row.task_id,
        idempotency_key=row.idempotency_key,
        tool_name=row.tool_name,
        risk_tier=RiskTier(row.risk_tier),
        proposal=row.proposal,
        proposal_digest=row.proposal_digest,
        status=ActionStatus(row.status),
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _approval(row: Row[Any]) -> ApprovalRecord:
    return ApprovalRecord(
        id=row.id,
        action_id=row.action_id,
        action_revision=row.action_revision,
        proposal_digest=row.proposal_digest,
        status=ApprovalStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        approved_at=row.approved_at,
        rejected_at=row.rejected_at,
        consumed_at=row.consumed_at,
    )


def _attempt(row: Row[Any]) -> AttemptRecord:
    return AttemptRecord(
        id=row.id,
        action_id=row.action_id,
        attempt_number=row.attempt_number,
        approval_id=row.approval_id,
        runtime_generation=row.runtime_generation,
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=AttemptOutcome(row.outcome) if row.outcome is not None else None,
        result=row.result,
        error_code=row.error_code,
        created_at=row.created_at,
    )


_OPEN_APPROVALS = [status.value for status in OPEN_APPROVAL_STATUSES]


class ActionRepository:
    """SQL for the action ledger. Callers own the transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- runtime generations ------------------------------------------------

    async def register_generation(self, generation_id: uuid.UUID) -> datetime:
        result = await self._connection.execute(
            insert(runtime_generations)
            .values(id=generation_id)
            .returning(runtime_generations.c.started_at)
        )
        started_at: datetime = result.scalar_one()
        return started_at

    # ---- actions ------------------------------------------------------------

    async def insert_action_if_absent(
        self,
        *,
        action_id: uuid.UUID,
        task_id: uuid.UUID,
        idempotency_key: str,
        tool_name: str,
        risk_tier: RiskTier,
        proposal: dict[str, Any],
        proposal_digest: str,
        status: ActionStatus,
    ) -> ActionRecord | None:
        """Insert, or return None when (task_id, idempotency_key) already exists.

        Callers hold the task row lock, so a concurrent duplicate has already
        committed by the time this runs and the follow-up read always sees it.
        """
        result = await self._connection.execute(
            pg_insert(actions)
            .values(
                id=action_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                risk_tier=risk_tier.value,
                proposal=proposal,
                proposal_digest=proposal_digest,
                status=status.value,
                revision=1,
            )
            .on_conflict_do_nothing(index_elements=["task_id", "idempotency_key"])
            .returning(*actions.c)
        )
        row = result.one_or_none()
        return _action(row) if row is not None else None

    async def get_action(self, action_id: uuid.UUID) -> ActionRecord | None:
        result = await self._connection.execute(select(actions).where(actions.c.id == action_id))
        row = result.one_or_none()
        return _action(row) if row is not None else None

    async def get_action_by_idempotency_key(
        self, *, task_id: uuid.UUID, idempotency_key: str
    ) -> ActionRecord | None:
        result = await self._connection.execute(
            select(actions).where(
                actions.c.task_id == task_id, actions.c.idempotency_key == idempotency_key
            )
        )
        row = result.one_or_none()
        return _action(row) if row is not None else None

    async def list_actions(self, task_id: uuid.UUID, *, limit: int) -> list[ActionRecord]:
        result = await self._connection.execute(
            select(actions)
            .where(actions.c.task_id == task_id)
            .order_by(actions.c.created_at, actions.c.id)
            .limit(limit)
        )
        return [_action(row) for row in result]

    async def update_action_status(
        self, *, action_id: uuid.UUID, expected_revision: int, status: ActionStatus
    ) -> ActionRecord | None:
        """Compare-and-swap. None when the action is missing or its revision moved."""
        result = await self._connection.execute(
            update(actions)
            .where(actions.c.id == action_id, actions.c.revision == expected_revision)
            .values(status=status.value, revision=actions.c.revision + 1, updated_at=func.now())
            .returning(*actions.c)
        )
        row = result.one_or_none()
        return _action(row) if row is not None else None

    # ---- approvals ----------------------------------------------------------

    async def insert_pending_approval(
        self,
        *,
        approval_id: uuid.UUID,
        action_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
        ttl: timedelta,
    ) -> ApprovalRecord:
        result = await self._connection.execute(
            insert(approvals)
            .values(
                id=approval_id,
                action_id=action_id,
                action_revision=action_revision,
                proposal_digest=proposal_digest,
                status=ApprovalStatus.PENDING.value,
                expires_at=func.now() + ttl,
            )
            .returning(*approvals.c)
        )
        return _approval(result.one())

    async def get_open_approval(self, action_id: uuid.UUID) -> ApprovalRecord | None:
        """The one PENDING or APPROVED approval, if any (a partial unique index)."""
        result = await self._connection.execute(
            select(approvals).where(
                approvals.c.action_id == action_id, approvals.c.status.in_(_OPEN_APPROVALS)
            )
        )
        row = result.one_or_none()
        return _approval(row) if row is not None else None

    async def list_approvals(self, action_id: uuid.UUID) -> list[ApprovalRecord]:
        result = await self._connection.execute(
            select(approvals)
            .where(approvals.c.action_id == action_id)
            .order_by(approvals.c.created_at, approvals.c.id)
        )
        return [_approval(row) for row in result]

    async def grant_approval(
        self, *, approval_id: uuid.UUID, action_revision: int
    ) -> ApprovalRecord | None:
        """PENDING -> APPROVED, re-binding to the action revision it produced."""
        result = await self._connection.execute(
            update(approvals)
            .where(
                approvals.c.id == approval_id, approvals.c.status == ApprovalStatus.PENDING.value
            )
            .values(
                status=ApprovalStatus.APPROVED.value,
                approved_at=func.now(),
                action_revision=action_revision,
            )
            .returning(*approvals.c)
        )
        row = result.one_or_none()
        return _approval(row) if row is not None else None

    async def reject_approval(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        result = await self._connection.execute(
            update(approvals)
            .where(approvals.c.id == approval_id, approvals.c.status.in_(_OPEN_APPROVALS))
            # approved_at is deliberately left in place: a rejection after an
            # approval is part of the audit trail, not an erasure of it.
            .values(status=ApprovalStatus.REJECTED.value, rejected_at=func.now())
            .returning(*approvals.c)
        )
        row = result.one_or_none()
        return _approval(row) if row is not None else None

    async def claim_approval(
        self, *, approval_id: uuid.UUID, action_revision: int, proposal_digest: str
    ) -> ApprovalRecord | None:
        """Consume the approval, or return None if it is not claimable right now.

        Every binding is re-checked in the same statement that claims it, so
        expiry, a superseded action revision and a second claim are all decided
        by the database rather than by a check that ran a moment earlier.
        """
        result = await self._connection.execute(
            update(approvals)
            .where(
                approvals.c.id == approval_id,
                approvals.c.status == ApprovalStatus.APPROVED.value,
                approvals.c.action_revision == action_revision,
                approvals.c.proposal_digest == proposal_digest,
                approvals.c.expires_at > func.now(),
            )
            .values(status=ApprovalStatus.CONSUMED.value, consumed_at=func.now())
            .returning(*approvals.c)
        )
        row = result.one_or_none()
        return _approval(row) if row is not None else None

    async def is_expired(self, approval_id: uuid.UUID) -> bool:
        """Ask the database, not the application clock."""
        return bool(
            await self._connection.scalar(
                select(approvals.c.expires_at <= func.now()).where(approvals.c.id == approval_id)
            )
        )

    # ---- attempts -----------------------------------------------------------

    async def insert_attempt(
        self,
        *,
        attempt_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_number: int,
        approval_id: uuid.UUID,
        runtime_generation: uuid.UUID,
    ) -> AttemptRecord:
        result = await self._connection.execute(
            insert(action_attempts)
            .values(
                id=attempt_id,
                action_id=action_id,
                attempt_number=attempt_number,
                approval_id=approval_id,
                runtime_generation=runtime_generation,
            )
            .returning(*action_attempts.c)
        )
        return _attempt(result.one())

    async def next_attempt_number(self, action_id: uuid.UUID) -> int:
        highest = await self._connection.scalar(
            select(func.max(action_attempts.c.attempt_number)).where(
                action_attempts.c.action_id == action_id
            )
        )
        return int(highest or 0) + 1

    async def get_unfinished_attempt(self, action_id: uuid.UUID) -> AttemptRecord | None:
        result = await self._connection.execute(
            select(action_attempts).where(
                action_attempts.c.action_id == action_id,
                action_attempts.c.finished_at.is_(None),
            )
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def list_attempts(self, action_id: uuid.UUID) -> list[AttemptRecord]:
        result = await self._connection.execute(
            select(action_attempts)
            .where(action_attempts.c.action_id == action_id)
            .order_by(action_attempts.c.attempt_number)
        )
        return [_attempt(row) for row in result]

    async def finish_attempt(
        self,
        *,
        attempt_id: uuid.UUID,
        outcome: AttemptOutcome,
        result: dict[str, Any] | None,
        error_code: str | None,
    ) -> AttemptRecord | None:
        """Record an outcome, only for an attempt that does not have one yet."""
        updated = await self._connection.execute(
            update(action_attempts)
            .where(
                action_attempts.c.id == attempt_id, action_attempts.c.finished_at.is_(None)
            )
            .values(
                finished_at=func.now(),
                outcome=outcome.value,
                # null(), not None: None would store a JSON null, which is not
                # the same as "no result" and fails the object CHECK.
                result=result if result is not None else null(),
                error_code=error_code,
            )
            .returning(*action_attempts.c)
        )
        row = updated.one_or_none()
        return _attempt(row) if row is not None else None

    async def list_attempts_from_other_generations(
        self, current_generation: uuid.UUID
    ) -> list[AttemptRecord]:
        """Unfinished attempts a previous runtime process left behind.

        Attempts carrying the current generation are this process's own live
        work and are never touched.
        """
        result = await self._connection.execute(
            select(action_attempts)
            .where(
                action_attempts.c.finished_at.is_(None),
                action_attempts.c.runtime_generation != current_generation,
            )
            .order_by(action_attempts.c.started_at, action_attempts.c.id)
        )
        return [_attempt(row) for row in result]
