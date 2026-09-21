import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.errors import (
    StaleTaskRevisionError,
    TaskConcurrencyError,
    TaskNotCancellableError,
    TaskNotFoundError,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, can_cancel
from app.repositories.authenticated import AuthenticatedRepository
from app.repositories.desktop_disclosure import DesktopDisclosureRepository
from app.repositories.research import ResearchRepository
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

        Cancelling also withdraws any research scope the task holds, in the
        same transaction: a cancelled task must not leave a live grant behind,
        even though a cancelled task already refuses every step.
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
                research = ResearchRepository(connection)
                grant = await research.open_grant_for_task(task_id)
                if grant is not None:
                    closed = await research.close_grant(
                        grant_id=grant.id, status=GrantStatus.REVOKED
                    )
                    session = await research.open_session_for_task(task_id)
                    if session is not None:
                        await research.close_session(session_id=session.id, status="CLOSED")
                    advanced = await repository.advance_task(
                        task_id=task_id, expected_revision=cancelled.revision
                    )
                    if advanced is None:  # pragma: no cover - this writer holds the row.
                        raise TaskConcurrencyError(task_id)
                    await repository.append_event(
                        task=advanced,
                        event_type=TaskEventType.TASK_RESEARCH_SCOPE_REVOKED,
                        payload={
                            "grant_id": str(grant.id),
                            "grant_revision": closed.revision if closed else grant.revision,
                            "grant_status": (closed or grant).status.value,
                            "reason": "task_cancelled",
                        },
                    )
                    return advanced
                # Milestone 8a S3: a cancelled task must not leave an
                # account-reading grant behind either. Its evidence is kept
                # (the user may still read what was found), and the profile's
                # browser is released by the desktop's stop path.
                authenticated = AuthenticatedRepository(connection)
                account_grant = await authenticated.open_grant_for_task(task_id)
                if account_grant is not None:
                    closed_account = await authenticated.close_grant(
                        grant_id=account_grant.id, status=GrantStatus.REVOKED
                    )
                    advanced_account = await repository.advance_task(
                        task_id=task_id, expected_revision=cancelled.revision
                    )
                    if advanced_account is None:  # pragma: no cover - this writer holds the row.
                        raise TaskConcurrencyError(task_id)
                    await repository.append_event(
                        task=advanced_account,
                        event_type=TaskEventType.TASK_AUTHENTICATED_SCOPE_REVOKED,
                        payload={
                            "grant_id": str(account_grant.id),
                            "grant_revision": (
                                closed_account.revision if closed_account else account_grant.revision
                            ),
                            "grant_status": (closed_account or account_grant).status.value,
                            "reason": "task_cancelled",
                        },
                    )
                    return advanced_account
                # Milestone 9 S2: cancelling withdraws a desktop disclosure that was not yet claimed.
                # One that was claimed is already spent (COMPLETED) and is not touched: cancelling cannot
                # un-send a snapshot.
                desktop_grant = await DesktopDisclosureRepository(connection).open_grant_for_task(task_id)
                if desktop_grant is not None:
                    closed_desktop = await DesktopDisclosureRepository(connection).close_grant(
                        grant_id=desktop_grant.id, status=GrantStatus.REVOKED
                    )
                    advanced_desktop = await repository.advance_task(
                        task_id=task_id, expected_revision=cancelled.revision
                    )
                    if advanced_desktop is None:  # pragma: no cover - this writer holds the row.
                        raise TaskConcurrencyError(task_id)
                    await repository.append_event(
                        task=advanced_desktop,
                        event_type=TaskEventType.TASK_DESKTOP_DISCLOSURE_REVOKED,
                        payload={
                            "grant_id": str(desktop_grant.id),
                            "grant_revision": (
                                closed_desktop.revision if closed_desktop else desktop_grant.revision
                            ),
                            "grant_status": (closed_desktop or desktop_grant).status.value,
                            "reason": "task_cancelled",
                        },
                    )
                    return advanced_desktop
                return cancelled
        raise TaskConcurrencyError(task_id)
