from enum import StrEnum


class TaskStatus(StrEnum):
    """Persisted task states.

    Stored as text guarded by a CHECK constraint rather than a native PostgreSQL
    enum, so a later migration can add states (WAITING_CONTEXT, WAITING_INPUT,
    WAITING_USER_AUTH, RECOVERING, ...) by replacing the constraint inside an
    ordinary transaction. Adding a member here therefore always requires a
    migration, and a test fails if the two drift apart.
    """

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    # Lumi does not know whether the action's side effect reached the outside
    # world. Never equivalent to FAILED and never automatically retried.
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class TaskEventType(StrEnum):
    TASK_CREATED = "task.created"
    TASK_CANCELLED = "task.cancelled"
    ACTION_PROPOSED = "action.proposed"
    ACTION_APPROVAL_REQUESTED = "action.approval_requested"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_EXECUTION_STARTED = "action.execution_started"
    ACTION_SUCCEEDED = "action.succeeded"
    ACTION_FAILED = "action.failed"
    ACTION_OUTCOME_UNKNOWN = "action.outcome_unknown"
    ACTION_RECONCILIATION_STARTED = "action.reconciliation_started"
    ACTION_RECONCILED = "action.reconciled"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def can_cancel(status: TaskStatus) -> bool:
    """Any non-terminal task may be cancelled; terminal states never change."""
    return not is_terminal(status)


def accepts_actions(status: TaskStatus) -> bool:
    """A terminal task must never gain, approve or execute an action."""
    return not is_terminal(status)
