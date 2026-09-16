from enum import StrEnum


class TaskStatus(StrEnum):
    """Persisted task states.

    Stored as text guarded by a CHECK constraint rather than a native PostgreSQL
    enum, so a later migration can add states (WAITING_APPROVAL, OUTCOME_UNKNOWN,
    RECONCILING, ...) by replacing the constraint inside an ordinary transaction.
    Adding a member here therefore always requires a migration.
    """

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class TaskEventType(StrEnum):
    TASK_CREATED = "task.created"
    TASK_CANCELLED = "task.cancelled"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_cancel(status: TaskStatus) -> bool:
    """Any non-terminal task may be cancelled; terminal states never change."""
    return not is_terminal(status)
