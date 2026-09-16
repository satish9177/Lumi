import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.errors import (
    StaleTaskRevisionError,
    TaskConcurrencyError,
    TaskNotCancellableError,
    TaskNotFoundError,
)
from app.domain.task_status import TaskEventType, TaskStatus, can_cancel
from app.repositories.tasks import TaskEventRecord, TaskRecord, TaskRepository

# A lost compare-and-swap re-reads the task; a handful of attempts is plenty
# because the next read normally observes the winner's terminal state.
_MAX_CANCEL_ATTEMPTS = 3


class TaskService:
    """Task use cases. Each method is one short transaction with no external I/O inside."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_task(self, request: dict[str, Any]) -> TaskRecord:
        async with self._engine.begin() as connection:
            repository = TaskRepository(connection)
            task = await repository.insert_task(
                task_id=uuid.uuid4(), status=TaskStatus.CREATED, request=request
            )
            await repository.append_event(
                task=task,
                event_type=TaskEventType.TASK_CREATED,
                payload={"status": task.status.value},
            )
        return task

    async def get_task(self, task_id: uuid.UUID) -> TaskRecord:
        async with self._engine.connect() as connection:
            task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    async def list_events(
        self, task_id: uuid.UUID, *, after_sequence: int = 0, limit: int = 100
    ) -> list[TaskEventRecord]:
        async with self._engine.connect() as connection:
            repository = TaskRepository(connection)
            if await repository.get_task(task_id) is None:
                raise TaskNotFoundError(task_id)
            return await repository.list_events(task_id, after_sequence=after_sequence, limit=limit)

    async def cancel_task(
        self, task_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> TaskRecord:
        """Cancel a non-terminal task.

        Rules: a non-terminal task becomes CANCELLED with one new event and
        revision + 1. Cancelling a CANCELLED task is an idempotent no-op (no event,
        same revision). SUCCEEDED and FAILED tasks are rejected. When
        expected_revision is given it must match the current revision.
        """
        for _ in range(_MAX_CANCEL_ATTEMPTS):
            async with self._engine.begin() as connection:
                repository = TaskRepository(connection)
                current = await repository.get_task(task_id)
                if current is None:
                    raise TaskNotFoundError(task_id)
                if expected_revision is not None and current.revision != expected_revision:
                    raise StaleTaskRevisionError(task_id, expected_revision, current.revision)
                if current.status is TaskStatus.CANCELLED:
                    return current
                if not can_cancel(current.status):
                    raise TaskNotCancellableError(task_id, current.status)

                cancelled = await repository.update_status(
                    task_id=task_id,
                    expected_revision=current.revision,
                    status=TaskStatus.CANCELLED,
                )
                if cancelled is None:
                    continue  # Lost a race with another writer; re-read and re-decide.
                await repository.append_event(
                    task=cancelled,
                    event_type=TaskEventType.TASK_CANCELLED,
                    payload={
                        "from_status": current.status.value,
                        "to_status": cancelled.status.value,
                    },
                )
                return cancelled
        raise TaskConcurrencyError(task_id)
