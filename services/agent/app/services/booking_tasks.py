"""Task-level booking changes: revised constraints, recorded searches, safe cancel.

These are the durable half of a conversation about a booking. A user refining
"only after 6 PM" or saying "cancel this" changes the *task*, and a task change
can make a prepared-but-unexecuted booking wrong. Each method therefore runs as
one transaction under the task row lock -- the same lock every ledger operation
takes -- so a refinement and an approval can never interleave:

* a booking that may already have reached the site (`EXECUTING`,
  `OUTCOME_UNKNOWN`, `RECONCILING`) blocks the change outright;
* a confirmed booking (`SUCCEEDED`) blocks it too: its constraints are history;
* a not-yet-executed booking the new constraints no longer admit is rejected in
  the same transaction, so its approval can never be spent afterwards.

Nothing here talks to the browser.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.action_status import ActionStatus
from app.domain.booking import BookingProposalError, parse_booking_proposal
from app.domain.booking_criteria import (
    BookingCriteria,
    InvalidBookingCriteriaError,
    revised_request,
)
from app.domain.errors import (
    BookingCriteriaError,
    StaleTaskRevisionError,
    TaskAlreadyBookedError,
    TaskConcurrencyError,
    TaskHasUnresolvedActionError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotCancellableError,
    TaskNotFoundError,
)
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions, can_cancel
from app.repositories.actions import ActionRecord, ActionRepository
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.actions import ActionService
from app.services.browser_execution import COMMIT_BOOKING

BOOKING_TASK_TYPE = "appointment_booking"

#: The booking may already exist at the clinic; Lumi does not know.
UNRESOLVED_BOOKING_STATUSES = frozenset(
    {ActionStatus.EXECUTING, ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING}
)
#: Prepared, possibly approved, never executed. Safe to reject.
OPEN_BOOKING_STATUSES = frozenset(
    {ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL, ActionStatus.APPROVED}
)
_ACTION_SCAN_LIMIT = 500


class BookingTaskService:
    def __init__(self, actions: ActionService) -> None:
        self._actions = actions
        self._engine = actions.engine

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    async def _lock_booking_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != BOOKING_TASK_TYPE:
            raise TaskKindMismatchError(task_id, BOOKING_TASK_TYPE)
        return task

    @staticmethod
    async def _bookings(connection: AsyncConnection, task_id: uuid.UUID) -> list[ActionRecord]:
        actions = await ActionRepository(connection).list_actions(task_id, limit=_ACTION_SCAN_LIMIT)
        return [action for action in actions if action.tool_name == COMMIT_BOOKING]

    @staticmethod
    def _refuse_if_settled_or_unresolved(task_id: uuid.UUID, bookings: Sequence[ActionRecord]) -> None:
        for action in bookings:
            if action.status in UNRESOLVED_BOOKING_STATUSES:
                raise TaskHasUnresolvedActionError(task_id, action.id, action.status)
        for action in bookings:
            if action.status is ActionStatus.SUCCEEDED:
                raise TaskAlreadyBookedError(task_id, action.id)

    @staticmethod
    def _still_admitted(action: ActionRecord, previous: BookingCriteria | None, criteria: BookingCriteria) -> bool:
        if previous is None or not previous.same_specialty(criteria):
            return False
        try:
            proposal = parse_booking_proposal(action.id, action.proposal)
        except BookingProposalError:
            return False
        return criteria.admits_proposal(proposal)

    # ---- use cases ----------------------------------------------------------

    async def revise_criteria(
        self, task_id: uuid.UUID, *, expected_revision: int, criteria: BookingCriteria
    ) -> tuple[TaskRecord, list[uuid.UUID]]:
        """Replace the task's constraints; invalidate bookings they now exclude.

        Revising to the constraints already stored is a no-op: nothing is
        written and the current task is returned, so a replayed refinement
        cannot churn the timeline.
        """
        async with self._engine.begin() as connection:
            repository = TaskRepository(connection)
            task = await self._lock_booking_task(connection, task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            if task.revision != expected_revision:
                raise StaleTaskRevisionError(task_id, expected_revision, task.revision)
            try:
                previous: BookingCriteria | None = BookingCriteria.from_request(task.request)
            except InvalidBookingCriteriaError:
                previous = None
            bookings = await self._bookings(connection, task_id)
            self._refuse_if_settled_or_unresolved(task_id, bookings)
            if previous == criteria:
                return task, []

            invalidated = [
                action
                for action in bookings
                if action.status in OPEN_BOOKING_STATUSES
                and not self._still_admitted(action, previous, criteria)
            ]
            revised = await repository.revise_request(
                task_id=task_id,
                expected_revision=task.revision,
                request=revised_request(task.request, criteria),
            )
            if revised is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await repository.append_event(
                task=revised,
                event_type=TaskEventType.TASK_CRITERIA_UPDATED,
                payload={
                    "criteria": criteria.model_dump(mode="json"),
                    "invalidated_action_ids": [str(action.id) for action in invalidated],
                    "reason": "criteria_changed",
                },
            )
            current = revised
            for action in invalidated:
                current = await self._actions.reject_in_transaction(
                    connection, task=current, action=action, reason="criteria_changed"
                )
            return current, [action.id for action in invalidated]

    async def record_search(
        self,
        task_id: uuid.UUID,
        *,
        criteria: BookingCriteria,
        slots: Sequence[dict[str, Any]],
        observed_count: int,
    ) -> TaskRecord:
        """Append the filtered search result to the timeline.

        The constraints are re-read under the lock: if they changed while the
        worker was reading the site, these results answer a question the user
        no longer asks and are not recorded.
        """
        async with self._engine.begin() as connection:
            repository = TaskRepository(connection)
            task = await self._lock_booking_task(connection, task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            try:
                current = BookingCriteria.from_request(task.request)
            except InvalidBookingCriteriaError:
                raise BookingCriteriaError(task_id, "stored criteria are unreadable") from None
            if current != criteria:
                raise BookingCriteriaError(task_id, "criteria changed during the search")
            advanced = await repository.advance_task(task_id=task_id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await repository.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_SEARCH_COMPLETED,
                payload={
                    "criteria": criteria.model_dump(mode="json"),
                    "slots": list(slots),
                    "observed_count": observed_count,
                    "excluded_count": observed_count - len(slots),
                },
            )
            return advanced

    async def cancel(
        self, task_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> tuple[TaskRecord, list[uuid.UUID]]:
        """Cancel a booking task without misreporting a booking that may exist.

        Refused while a booking is unresolved or confirmed. Otherwise every open
        booking is rejected and the task is cancelled, in one transaction.
        Cancelling an already cancelled task is an idempotent no-op.
        """
        async with self._engine.begin() as connection:
            repository = TaskRepository(connection)
            task = await self._lock_booking_task(connection, task_id)
            if task.status is TaskStatus.CANCELLED:
                return task, []
            if not can_cancel(task.status):
                raise TaskNotCancellableError(task_id, task.status)
            if expected_revision is not None and task.revision != expected_revision:
                raise StaleTaskRevisionError(task_id, expected_revision, task.revision)
            bookings = await self._bookings(connection, task_id)
            self._refuse_if_settled_or_unresolved(task_id, bookings)

            current = task
            rejected: list[uuid.UUID] = []
            for action in bookings:
                if action.status in OPEN_BOOKING_STATUSES:
                    current = await self._actions.reject_in_transaction(
                        connection, task=current, action=action, reason="task_cancelled"
                    )
                    rejected.append(action.id)
            cancelled = await repository.update_status(
                task_id=task_id, expected_revision=current.revision, status=TaskStatus.CANCELLED
            )
            if cancelled is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await repository.append_event(
                task=cancelled,
                event_type=TaskEventType.TASK_CANCELLED,
                payload={
                    "from_status": current.status.value,
                    "to_status": cancelled.status.value,
                    "rejected_action_ids": [str(action_id) for action_id in rejected],
                    "reason": "user_cancelled",
                },
            )
            return cancelled, rejected
