import uuid

from app.domain.action_status import ActionStatus
from app.domain.task_status import TaskStatus


class TaskNotFoundError(Exception):
    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"Task {task_id} was not found.")
        self.task_id = task_id


class TaskNotCancellableError(Exception):
    def __init__(self, task_id: uuid.UUID, status: TaskStatus) -> None:
        super().__init__(f"Task {task_id} is {status} and can no longer be cancelled.")
        self.task_id = task_id
        self.status = status


class StaleTaskRevisionError(Exception):
    def __init__(self, task_id: uuid.UUID, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"Task {task_id} is at revision {current_revision}, not {expected_revision}."
        )
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class TaskConcurrencyError(Exception):
    """The task kept changing underneath an unconditioned update."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"Task {task_id} changed concurrently. Retry the request.")
        self.task_id = task_id


class TaskNotAcceptingActionsError(Exception):
    def __init__(self, task_id: uuid.UUID, status: TaskStatus) -> None:
        super().__init__(f"Task {task_id} is {status} and accepts no further action work.")
        self.task_id = task_id
        self.status = status


class ActionNotFoundError(Exception):
    def __init__(self, action_id: uuid.UUID) -> None:
        super().__init__(f"Action {action_id} was not found.")
        self.action_id = action_id


class ActionProposalConflictError(Exception):
    """The idempotency key is already bound to a different proposal.

    Returning the stored action would silently answer a question the caller did
    not ask; replacing the proposal would invalidate an approval the user may
    already have granted. Both are refused.
    """

    def __init__(self, task_id: uuid.UUID, idempotency_key: str, action_id: uuid.UUID) -> None:
        super().__init__(
            f"Idempotency key {idempotency_key!r} on task {task_id} already identifies action "
            f"{action_id}, which holds a different proposal."
        )
        self.task_id = task_id
        self.idempotency_key = idempotency_key
        self.action_id = action_id


class StaleActionRevisionError(Exception):
    def __init__(self, action_id: uuid.UUID, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"Action {action_id} is at revision {current_revision}, not {expected_revision}."
        )
        self.action_id = action_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class InvalidActionTransitionError(Exception):
    def __init__(self, action_id: uuid.UUID, current: ActionStatus, target: ActionStatus) -> None:
        super().__init__(f"Action {action_id} cannot move from {current} to {target}.")
        self.action_id = action_id
        self.current = current
        self.target = target


class ApprovalNotUsableError(Exception):
    """The action has no approval that may be claimed for execution."""

    def __init__(self, action_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"Action {action_id} cannot execute: {reason}.")
        self.action_id = action_id
        self.reason = reason


class AttemptNotFoundError(Exception):
    def __init__(self, action_id: uuid.UUID) -> None:
        super().__init__(f"Action {action_id} has no unfinished execution attempt.")
        self.action_id = action_id


class ActionConcurrencyError(Exception):
    def __init__(self, action_id: uuid.UUID) -> None:
        super().__init__(f"Action {action_id} changed concurrently. Retry the request.")
        self.action_id = action_id


class ActionAlreadyOpenError(Exception):
    """The task already has a booking that is unresolved or already succeeded.

    Preparing a second booking while one is waiting, executing or of unknown
    outcome would be a way around "never retry an unknown outcome", so it is
    refused under the task row lock.
    """

    def __init__(self, task_id: uuid.UUID, action_id: uuid.UUID, status: ActionStatus) -> None:
        super().__init__(
            f"Task {task_id} already has action {action_id} in status {status}; "
            "resolve it before preparing another."
        )
        self.task_id = task_id
        self.action_id = action_id
        self.status = status


class TaskKindMismatchError(Exception):
    def __init__(self, task_id: uuid.UUID, expected: str) -> None:
        super().__init__(f"Task {task_id} is not a {expected} task.")
        self.task_id = task_id
        self.expected = expected


class BookingSlotUnavailableError(Exception):
    """The site says the requested slot is not offered. Nothing was proposed."""

    def __init__(self, slot_id: str) -> None:
        super().__init__("That appointment slot is no longer offered.")
        self.slot_id = slot_id


class BrowserObservationError(Exception):
    """A read-only browser observation did not produce usable facts."""

    def __init__(self, code: str) -> None:
        super().__init__("The appointment site could not be read.")
        self.code = code


class TaskHasUnresolvedActionError(Exception):
    """A booking of this task may already have reached the outside world.

    Revising or cancelling the task now would let the timeline say something
    Lumi does not know. Resolve the action (it finishes, or reconciliation
    establishes what happened) first.
    """

    def __init__(self, task_id: uuid.UUID, action_id: uuid.UUID, status: ActionStatus) -> None:
        super().__init__(
            f"Task {task_id} has booking {action_id} in {status}; resolve it before changing the task."
        )
        self.task_id = task_id
        self.action_id = action_id
        self.status = status


class TaskAlreadyBookedError(Exception):
    """The task's booking already succeeded; its constraints are history now."""

    def __init__(self, task_id: uuid.UUID, action_id: uuid.UUID) -> None:
        super().__init__(f"Task {task_id} already has a confirmed booking ({action_id}).")
        self.task_id = task_id
        self.action_id = action_id


class BookingCriteriaError(Exception):
    """Booking constraints that cannot be applied, or a slot they exclude."""

    def __init__(self, task_id: uuid.UUID, reason: str) -> None:
        super().__init__(f"Task {task_id}: {reason}")
        self.task_id = task_id
        self.reason = reason


class BookingCriteriaMismatchError(Exception):
    """The slot as observed now is outside the task's constraints."""

    def __init__(self, task_id: uuid.UUID, slot_id: str) -> None:
        super().__init__(f"Slot {slot_id} no longer matches the constraints of task {task_id}.")
        self.task_id = task_id
        self.slot_id = slot_id
