import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import task_events, tasks
from app.domain.task_status import TaskEventType, TaskStatus


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: uuid.UUID
    status: TaskStatus
    revision: int
    last_event_sequence: int
    request: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskEventRecord:
    id: int
    task_id: uuid.UUID
    sequence: int
    task_revision: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


def _task(row: Row[Any]) -> TaskRecord:
    return TaskRecord(
        id=row.id,
        status=TaskStatus(row.status),
        revision=row.revision,
        last_event_sequence=row.last_event_sequence,
        request=row.request,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _event(row: Row[Any]) -> TaskEventRecord:
    return TaskEventRecord(
        id=row.id,
        task_id=row.task_id,
        sequence=row.sequence,
        task_revision=row.task_revision,
        event_type=row.event_type,
        payload=row.payload,
        created_at=row.created_at,
    )


class TaskRepository:
    """SQL for tasks and their events. Callers own the transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_task(
        self, *, task_id: uuid.UUID, status: TaskStatus, request: dict[str, Any]
    ) -> TaskRecord:
        result = await self._connection.execute(
            insert(tasks)
            .values(id=task_id, status=status.value, revision=1, last_event_sequence=1, request=request)
            .returning(*tasks.c)
        )
        return _task(result.one())

    async def get_task(self, task_id: uuid.UUID) -> TaskRecord | None:
        result = await self._connection.execute(select(tasks).where(tasks.c.id == task_id))
        row = result.one_or_none()
        return _task(row) if row is not None else None

    async def lock_task(self, task_id: uuid.UUID) -> TaskRecord | None:
        """Read the task and hold its row lock for the rest of the transaction.

        Action work takes this lock first, so everything that touches one task
        serializes in a single order: no deadlocks, and event sequence
        allocation stays ordered without relying on retries.
        """
        result = await self._connection.execute(
            select(tasks).where(tasks.c.id == task_id).with_for_update()
        )
        row = result.one_or_none()
        return _task(row) if row is not None else None

    async def advance_task(
        self, *, task_id: uuid.UUID, expected_revision: int, status: TaskStatus | None = None
    ) -> TaskRecord | None:
        """Compare-and-swap one revision forward, optionally changing the status.

        `status=None` keeps the current status and only allocates the next event
        sequence, for changes that belong to the task's timeline without moving
        the task itself. Returns None when the task is missing or its revision
        moved.
        """
        changes: dict[str, Any] = {
            "revision": tasks.c.revision + 1,
            "last_event_sequence": tasks.c.last_event_sequence + 1,
            "updated_at": func.now(),
        }
        if status is not None:
            changes["status"] = status.value
        result = await self._connection.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.revision == expected_revision)
            .values(**changes)
            .returning(*tasks.c)
        )
        row = result.one_or_none()
        return _task(row) if row is not None else None

    async def revise_request(
        self, *, task_id: uuid.UUID, expected_revision: int, request: dict[str, Any]
    ) -> TaskRecord | None:
        """Compare-and-swap the task request one revision forward.

        The caller holds the task lock and appends the event that explains the
        change in the same transaction.
        """
        result = await self._connection.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.revision == expected_revision)
            .values(
                request=request,
                revision=tasks.c.revision + 1,
                last_event_sequence=tasks.c.last_event_sequence + 1,
                updated_at=func.now(),
            )
            .returning(*tasks.c)
        )
        row = result.one_or_none()
        return _task(row) if row is not None else None

    async def update_status(
        self, *, task_id: uuid.UUID, expected_revision: int, status: TaskStatus
    ) -> TaskRecord | None:
        return await self.advance_task(
            task_id=task_id, expected_revision=expected_revision, status=status
        )

    async def append_event(
        self,
        *,
        task: TaskRecord,
        event_type: TaskEventType,
        payload: dict[str, Any],
    ) -> TaskEventRecord:
        """Record the event for the task state just written in this transaction."""
        result = await self._connection.execute(
            insert(task_events)
            .values(
                task_id=task.id,
                sequence=task.last_event_sequence,
                task_revision=task.revision,
                event_type=event_type.value,
                payload=payload,
            )
            .returning(*task_events.c)
        )
        return _event(result.one())

    async def list_events(
        self, task_id: uuid.UUID, *, after_sequence: int, limit: int
    ) -> list[TaskEventRecord]:
        result = await self._connection.execute(
            select(task_events)
            .where(task_events.c.task_id == task_id, task_events.c.sequence > after_sequence)
            .order_by(task_events.c.sequence)
            .limit(limit)
        )
        return [_event(row) for row in result]
