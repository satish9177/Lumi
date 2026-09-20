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
    #: The task's booking constraints were revised (voice or UI refinement).
    TASK_CRITERIA_UPDATED = "task.criteria_updated"
    #: A read-only search finished; the payload holds the typed, filtered slots
    #: the user may now choose from.
    TASK_SEARCH_COMPLETED = "task.search_completed"
    #: A read-only clinic-information lookup finished; the payload holds the
    #: typed public profile facts that were read.
    TASK_INFO_LOOKUP_COMPLETED = "task.info_lookup_completed"
    #: A grounded answer was recorded for an inspected page. The payload holds
    #: ids, the content hash and the status -- never the page text or answer.
    TASK_PAGE_ANSWER_RECORDED = "task.page_answer_recorded"
    #: Milestone 7b public research. The trusted scope card was built, the user
    #: confirmed it, it was withdrawn, or a grounded answer was recorded.
    TASK_RESEARCH_SCOPE_REQUESTED = "task.research_scope_requested"
    TASK_RESEARCH_SCOPE_GRANTED = "task.research_scope_granted"
    TASK_RESEARCH_SCOPE_REVOKED = "task.research_scope_revoked"
    TASK_RESEARCH_ANSWER_RECORDED = "task.research_answer_recorded"
    #: Milestone 8a S3 authenticated account reading. Ids, reasons and counts
    #: only -- never page text, an answer, an address or an identity.
    TASK_AUTHENTICATED_SCOPE_REQUESTED = "task.authenticated_scope_requested"
    TASK_AUTHENTICATED_SCOPE_GRANTED = "task.authenticated_scope_granted"
    TASK_AUTHENTICATED_SCOPE_REVOKED = "task.authenticated_scope_revoked"
    TASK_AUTHENTICATED_ANSWER_RECORDED = "task.authenticated_answer_recorded"
    TASK_AUTHENTICATED_PAUSED = "task.authenticated_paused"
    TASK_AUTHENTICATED_RESUMED = "task.authenticated_resumed"
    #: Milestone 8b S5 form planning. Ids, digests and counts only -- never a
    #: label, a preview, a saved value, an origin or a manifest.
    TASK_FORM_PREPARE_SCOPE_REQUESTED = "task.form_prepare_scope_requested"
    TASK_FORM_PREPARE_SCOPE_GRANTED = "task.form_prepare_scope_granted"
    TASK_FORM_PREPARE_SCOPE_REVOKED = "task.form_prepare_scope_revoked"
    TASK_FORM_PLANNING_CONTEXT_BUILT = "task.form_planning_context_built"
    ACTION_PROPOSED = "action.proposed"
    ACTION_APPROVAL_REQUESTED = "action.approval_requested"
    ACTION_APPROVED = "action.approved"
    #: Authorised by a scoped task grant, not by an exact approval of this step.
    ACTION_AUTHORIZED = "action.authorized"
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
