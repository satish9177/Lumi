from enum import StrEnum

from app.domain.task_status import TaskEventType, TaskStatus


class RiskTier(StrEnum):
    """How consequential an action is. The policy engine is a later milestone.

    R0 local read; R1 scoped read or preparation; R2 disclosure or an otherwise
    consequential effect; R3 financial, destructive or security-sensitive.
    """

    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"


class ActionStatus(StrEnum):
    """Persisted action states, text plus a CHECK constraint like TaskStatus."""

    PROPOSED = "PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    # Milestone 7b: authorised by a reusable, scoped task grant rather than by
    # an exact per-action approval. Deliberately a distinct state from
    # APPROVED: the user confirmed a bounded scope once, not this exact step,
    # and the timeline must not claim otherwise.
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # Distinct from FAILED on purpose: FAILED means Lumi knows the operation did
    # not happen, OUTCOME_UNKNOWN means Lumi does not know whether it did.
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    RECONCILING = "RECONCILING"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    # Claimed by an execution attempt. Single-use: never returns to APPROVED.
    CONSUMED = "CONSUMED"


class AttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


#: Outcomes where Lumi knows what happened to the side effect.
KNOWN_OUTCOMES: frozenset[AttemptOutcome] = frozenset(
    {AttemptOutcome.SUCCEEDED, AttemptOutcome.FAILED}
)

TERMINAL_ACTION_STATUSES: frozenset[ActionStatus] = frozenset(
    {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.REJECTED}
)

OPEN_APPROVAL_STATUSES: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.PENDING, ApprovalStatus.APPROVED}
)

_ALLOWED_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset(
        {ActionStatus.WAITING_APPROVAL, ActionStatus.AUTHORIZED, ActionStatus.REJECTED}
    ),
    ActionStatus.WAITING_APPROVAL: frozenset({ActionStatus.APPROVED, ActionStatus.REJECTED}),
    ActionStatus.APPROVED: frozenset({ActionStatus.EXECUTING, ActionStatus.REJECTED}),
    # No edge from AUTHORIZED to APPROVED, or back from EXECUTING: a scoped
    # authorization never becomes an exact approval, and a started attempt is
    # never un-started.
    ActionStatus.AUTHORIZED: frozenset({ActionStatus.EXECUTING, ActionStatus.REJECTED}),
    # No edge back to APPROVED: a started attempt is never un-started.
    ActionStatus.EXECUTING: frozenset(
        {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.OUTCOME_UNKNOWN}
    ),
    # The only edge out of OUTCOME_UNKNOWN is authoritative reconciliation.
    ActionStatus.OUTCOME_UNKNOWN: frozenset({ActionStatus.RECONCILING}),
    ActionStatus.RECONCILING: frozenset(
        {ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.OUTCOME_UNKNOWN}
    ),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.REJECTED: frozenset(),
}

#: Task state an action status implies. None means "leave the task status alone".
#: A resolved action returns the task to READY so a future planner can continue;
#: an action succeeding never makes the whole task SUCCEEDED.
_TASK_STATUS_FOR_ACTION: dict[ActionStatus, TaskStatus | None] = {
    ActionStatus.PROPOSED: None,
    ActionStatus.WAITING_APPROVAL: TaskStatus.WAITING_APPROVAL,
    ActionStatus.APPROVED: TaskStatus.READY,
    ActionStatus.AUTHORIZED: TaskStatus.READY,
    ActionStatus.REJECTED: TaskStatus.READY,
    ActionStatus.EXECUTING: TaskStatus.EXECUTING,
    ActionStatus.SUCCEEDED: TaskStatus.READY,
    ActionStatus.FAILED: TaskStatus.READY,
    ActionStatus.OUTCOME_UNKNOWN: TaskStatus.OUTCOME_UNKNOWN,
    ActionStatus.RECONCILING: TaskStatus.RECONCILING,
}


def can_transition(current: ActionStatus, target: ActionStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[current]


def is_action_terminal(status: ActionStatus) -> bool:
    return status in TERMINAL_ACTION_STATUSES


def task_status_for(status: ActionStatus) -> TaskStatus | None:
    return _TASK_STATUS_FOR_ACTION[status]


def action_status_for(outcome: AttemptOutcome) -> ActionStatus:
    return ActionStatus(outcome.value)


def requires_approval(risk_tier: RiskTier) -> bool:
    """Every action needs a durable approval in this milestone.

    The tier is persisted so the later policy engine can auto-approve R0/R1
    local reads. Until that engine exists the fail-safe answer is the only
    correct one: nothing executes without an explicit, durable approval.
    """
    _ = risk_tier
    return True


def event_type_for_outcome(outcome: AttemptOutcome) -> TaskEventType:
    return {
        AttemptOutcome.SUCCEEDED: TaskEventType.ACTION_SUCCEEDED,
        AttemptOutcome.FAILED: TaskEventType.ACTION_FAILED,
        AttemptOutcome.OUTCOME_UNKNOWN: TaskEventType.ACTION_OUTCOME_UNKNOWN,
    }[outcome]
