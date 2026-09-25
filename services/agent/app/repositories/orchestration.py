"""SQL for Milestone 11 S2 durable orchestration. Callers own the transaction, exactly like every other
repository in this layer."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import orchestration_steps, orchestrations


@dataclass(frozen=True, slots=True)
class OrchestrationRecord:
    id: uuid.UUID
    objective: str
    status: str
    pause_reason: str | None
    revision: int
    step_count: int
    child_task_count: int
    planner_calls: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    stopped_at: datetime | None
    #: Milestone 12 S2: the one document task this orchestration's document resources refer into. Set once.
    document_task_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class StepRecord:
    id: uuid.UUID
    orchestration_id: uuid.UUID
    sequence: int
    capability_id: str
    status: str
    child_task_id: uuid.UUID | None
    result_handle: str | None
    result_summary: str | None
    created_at: datetime
    updated_at: datetime
    #: Milestone 12 S3: a controller-authored note while still PENDING/AWAITING_APPROVAL (never a result).
    pending_note: str | None = None


def _orchestration(row: Row[Any]) -> OrchestrationRecord:
    return OrchestrationRecord(
        id=row.id,
        objective=row.objective,
        status=row.status,
        pause_reason=row.pause_reason,
        revision=row.revision,
        step_count=row.step_count,
        child_task_count=row.child_task_count,
        planner_calls=row.planner_calls,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        stopped_at=row.stopped_at,
        document_task_id=row.document_task_id,
    )


def _step(row: Row[Any]) -> StepRecord:
    return StepRecord(
        id=row.id,
        orchestration_id=row.orchestration_id,
        sequence=row.sequence,
        capability_id=row.capability_id,
        status=row.status,
        child_task_id=row.child_task_id,
        result_handle=row.result_handle,
        result_summary=row.result_summary,
        created_at=row.created_at,
        updated_at=row.updated_at,
        pending_note=row.pending_note,
    )


def orchestration_is_live() -> Any:
    """SQL: unexpired and not terminal. The database's clock decides, exactly like `workflow_is_live`."""
    return and_(
        orchestrations.c.status.in_(("RUNNING", "PAUSED")),
        orchestrations.c.expires_at > func.now(),
    )


class OrchestrationRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- orchestrations ---------------------------------------------------------------------------

    async def insert(self, *, orchestration_id: uuid.UUID, objective: str, ttl: timedelta) -> OrchestrationRecord:
        result = await self._connection.execute(
            insert(orchestrations)
            .values(id=orchestration_id, objective=objective, status="RUNNING", revision=1, expires_at=func.now() + ttl)
            .returning(*orchestrations.c)
        )
        return _orchestration(result.one())

    async def get(self, orchestration_id: uuid.UUID, *, lock: bool = False) -> OrchestrationRecord | None:
        statement = select(orchestrations).where(orchestrations.c.id == orchestration_id)
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _orchestration(row) if row is not None else None

    async def is_live(self, orchestration_id: uuid.UUID) -> bool:
        found = await self._connection.scalar(
            select(orchestrations.c.id).where(orchestrations.c.id == orchestration_id, orchestration_is_live())
        )
        return found is not None

    async def latest(self) -> OrchestrationRecord | None:
        row = (
            await self._connection.execute(
                select(orchestrations).order_by(orchestrations.c.created_at.desc(), orchestrations.c.id).limit(1)
            )
        ).one_or_none()
        return _orchestration(row) if row is not None else None

    async def _transition(self, orchestration_id: uuid.UUID, *, expected_statuses: tuple[str, ...], **values: Any) -> OrchestrationRecord | None:
        result = await self._connection.execute(
            update(orchestrations)
            .where(orchestrations.c.id == orchestration_id, orchestrations.c.status.in_(expected_statuses))
            .values(revision=orchestrations.c.revision + 1, updated_at=func.now(), **values)
            .returning(*orchestrations.c)
        )
        row = result.one_or_none()
        return _orchestration(row) if row is not None else None

    async def record_planner_call(self, orchestration_id: uuid.UUID) -> OrchestrationRecord | None:
        """Counted at the point a planner call is about to be made, whatever it returns."""
        result = await self._connection.execute(
            update(orchestrations)
            .where(orchestrations.c.id == orchestration_id)
            .values(planner_calls=orchestrations.c.planner_calls + 1, revision=orchestrations.c.revision + 1, updated_at=func.now())
            .returning(*orchestrations.c)
        )
        row = result.one_or_none()
        return _orchestration(row) if row is not None else None

    async def record_step_added(self, orchestration_id: uuid.UUID, *, new_child_task: bool) -> None:
        values: dict[str, Any] = {"step_count": orchestrations.c.step_count + 1, "revision": orchestrations.c.revision + 1, "updated_at": func.now()}
        if new_child_task:
            values["child_task_count"] = orchestrations.c.child_task_count + 1
        await self._connection.execute(update(orchestrations).where(orchestrations.c.id == orchestration_id).values(**values))

    async def set_document_task_id(self, orchestration_id: uuid.UUID, *, document_task_id: uuid.UUID) -> OrchestrationRecord | None:
        """Set-once: only while `document_task_id` is still NULL. `None` means it was already set (to this
        task or a different one) -- the caller re-reads the current value rather than assuming success."""
        result = await self._connection.execute(
            update(orchestrations)
            .where(orchestrations.c.id == orchestration_id, orchestrations.c.document_task_id.is_(None))
            .values(document_task_id=document_task_id, revision=orchestrations.c.revision + 1, updated_at=func.now())
            .returning(*orchestrations.c)
        )
        row = result.one_or_none()
        return _orchestration(row) if row is not None else None

    async def pause(self, orchestration_id: uuid.UUID, *, reason: str) -> OrchestrationRecord | None:
        return await self._transition(orchestration_id, expected_statuses=("RUNNING",), status="PAUSED", pause_reason=reason)

    async def relabel_pause(self, orchestration_id: uuid.UUID, *, reason: str) -> OrchestrationRecord | None:
        """Update an already-paused orchestration's reason without resuming it -- a re-check found the
        step still unresolved, but for a more (or less) specific reason than before."""
        return await self._transition(orchestration_id, expected_statuses=("PAUSED",), status="PAUSED", pause_reason=reason)

    async def resume_running(self, orchestration_id: uuid.UUID) -> OrchestrationRecord | None:
        return await self._transition(orchestration_id, expected_statuses=("PAUSED",), status="RUNNING", pause_reason=None)

    async def succeed(self, orchestration_id: uuid.UUID) -> OrchestrationRecord | None:
        return await self._transition(orchestration_id, expected_statuses=("RUNNING",), status="SUCCEEDED")

    async def fail(self, orchestration_id: uuid.UUID) -> OrchestrationRecord | None:
        return await self._transition(orchestration_id, expected_statuses=("RUNNING",), status="FAILED")

    async def stop(self, orchestration_id: uuid.UUID) -> OrchestrationRecord | None:
        return await self._transition(
            orchestration_id,
            expected_statuses=("RUNNING", "PAUSED"),
            status="STOPPED",
            pause_reason=None,
            stopped_at=func.now()
        )

    # ---- steps --------------------------------------------------------------------------------------

    async def insert_step(
        self,
        *,
        orchestration_id: uuid.UUID,
        sequence: int,
        capability_id: str,
        status: str,
        child_task_id: uuid.UUID | None,
        result_handle: str | None = None,
        result_summary: str | None = None,
        pending_note: str | None = None,
    ) -> StepRecord:
        result = await self._connection.execute(
            insert(orchestration_steps)
            .values(
                id=uuid.uuid4(),
                orchestration_id=orchestration_id,
                sequence=sequence,
                capability_id=capability_id,
                status=status,
                child_task_id=child_task_id,
                result_handle=result_handle,
                result_summary=result_summary,
                pending_note=pending_note,
            )
            .returning(*orchestration_steps.c)
        )
        return _step(result.one())

    async def steps(self, orchestration_id: uuid.UUID) -> list[StepRecord]:
        result = await self._connection.execute(
            select(orchestration_steps)
            .where(orchestration_steps.c.orchestration_id == orchestration_id)
            .order_by(orchestration_steps.c.sequence)
        )
        return [_step(row) for row in result]

    async def last_step(self, orchestration_id: uuid.UUID, *, lock: bool = False) -> StepRecord | None:
        statement = (
            select(orchestration_steps)
            .where(orchestration_steps.c.orchestration_id == orchestration_id)
            .order_by(orchestration_steps.c.sequence.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _step(row) if row is not None else None

    async def step_for_task(self, task_id: uuid.UUID) -> StepRecord | None:
        row = (
            await self._connection.execute(select(orchestration_steps).where(orchestration_steps.c.child_task_id == task_id))
        ).one_or_none()
        return _step(row) if row is not None else None

    async def update_pending_note(
        self, step_id: uuid.UUID, *, status: str, pending_note: str | None
    ) -> StepRecord | None:
        """Milestone 12 S3: refresh an UNRESOLVED step's own controller-authored note (e.g.
        `manual_handoff_required`'s safe instruction) without settling it -- `status` must be the step's
        current one (`PENDING` or `AWAITING_APPROVAL`), so this can never itself resolve a step; only
        `resolve_step` does that, and it clears this column when it does."""
        assert status in ("PENDING", "AWAITING_APPROVAL")
        result = await self._connection.execute(
            update(orchestration_steps)
            .where(orchestration_steps.c.id == step_id, orchestration_steps.c.status == status)
            .values(pending_note=pending_note, updated_at=func.now())
            .returning(*orchestration_steps.c)
        )
        row = result.one_or_none()
        return _step(row) if row is not None else None

    async def resolve_step(
        self, step_id: uuid.UUID, *, status: str, result_handle: str | None, result_summary: str | None
    ) -> StepRecord | None:
        """Move a step to its final status. `expected` fences a double-resolution: only an unresolved step
        (`PENDING`/`AWAITING_APPROVAL`) can be settled, never overwritten once terminal."""
        result = await self._connection.execute(
            update(orchestration_steps)
            .where(
                orchestration_steps.c.id == step_id,
                orchestration_steps.c.status.in_(("PENDING", "AWAITING_APPROVAL")),
            )
            .values(
                status=status, result_handle=result_handle, result_summary=result_summary,
                pending_note=None, updated_at=func.now(),
            )
            .returning(*orchestration_steps.c)
        )
        row = result.one_or_none()
        return _step(row) if row is not None else None

    async def update_step_status(self, step_id: uuid.UUID, *, status: str) -> StepRecord | None:
        """A still-unresolved step's status changes without settling it (e.g. `PENDING` -> `AWAITING_APPROVAL`)."""
        result = await self._connection.execute(
            update(orchestration_steps)
            .where(
                orchestration_steps.c.id == step_id,
                orchestration_steps.c.status.in_(("PENDING", "AWAITING_APPROVAL")),
            )
            .values(status=status, updated_at=func.now())
            .returning(*orchestration_steps.c)
        )
        row = result.one_or_none()
        return _step(row) if row is not None else None


__all__ = ["OrchestrationRecord", "OrchestrationRepository", "StepRecord", "orchestration_is_live"]
