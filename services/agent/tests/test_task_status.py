import pytest

from app.domain.action_status import (
    ActionStatus,
    AttemptOutcome,
    action_status_for,
    can_transition,
    is_action_terminal,
    requires_approval,
    task_status_for,
)
from app.domain.action_status import RiskTier
from app.domain.task_status import (
    TERMINAL_STATUSES,
    TaskStatus,
    accepts_actions,
    can_cancel,
    is_terminal,
)


def test_task_states_are_exactly_defined() -> None:
    # Pinned so a new member cannot be added without also updating the CHECK
    # constraint in a migration, which test_schema.py cross-checks.
    assert [status.value for status in TaskStatus] == [
        "CREATED",
        "PLANNING",
        "READY",
        "WAITING_APPROVAL",
        "EXECUTING",
        "VERIFYING",
        "OUTCOME_UNKNOWN",
        "RECONCILING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "PAUSED",
    ]


def test_action_states_are_exactly_defined() -> None:
    assert [status.value for status in ActionStatus] == [
        "PROPOSED",
        "WAITING_APPROVAL",
        "APPROVED",
        # Milestone 7b: authorised by a confirmed scope, not by an exact
        # approval of this step. Deliberately a different state.
        "AUTHORIZED",
        "REJECTED",
        "EXECUTING",
        "SUCCEEDED",
        "FAILED",
        "OUTCOME_UNKNOWN",
        "RECONCILING",
    ]


def test_terminal_states() -> None:
    assert TERMINAL_STATUSES == {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


def test_an_outcome_unknown_task_is_not_terminal() -> None:
    # It is unresolved, not finished: reconciliation still has work to do.
    assert not is_terminal(TaskStatus.OUTCOME_UNKNOWN)
    assert not is_terminal(TaskStatus.RECONCILING)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_only_non_terminal_tasks_can_be_cancelled(status: TaskStatus) -> None:
    assert can_cancel(status) is not is_terminal(status)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_only_non_terminal_tasks_accept_actions(status: TaskStatus) -> None:
    assert accepts_actions(status) is not is_terminal(status)


# ---- the action state machine -----------------------------------------------


def test_outcome_unknown_can_only_be_left_through_reconciliation() -> None:
    allowed = [
        target
        for target in ActionStatus
        if can_transition(ActionStatus.OUTCOME_UNKNOWN, target)
    ]
    assert allowed == [ActionStatus.RECONCILING]


@pytest.mark.parametrize(
    "forbidden",
    [ActionStatus.APPROVED, ActionStatus.EXECUTING, ActionStatus.FAILED, ActionStatus.SUCCEEDED],
)
def test_an_unknown_outcome_is_never_silently_resolved(forbidden: ActionStatus) -> None:
    """Above all: never back to EXECUTING (a blind retry) or FAILED (a lie)."""
    assert not can_transition(ActionStatus.OUTCOME_UNKNOWN, forbidden)


def test_execution_cannot_be_un_started() -> None:
    assert not can_transition(ActionStatus.EXECUTING, ActionStatus.APPROVED)
    assert not can_transition(ActionStatus.EXECUTING, ActionStatus.EXECUTING)


def test_reconciliation_may_conclude_that_it_still_does_not_know() -> None:
    assert can_transition(ActionStatus.RECONCILING, ActionStatus.OUTCOME_UNKNOWN)
    assert can_transition(ActionStatus.RECONCILING, ActionStatus.SUCCEEDED)
    assert can_transition(ActionStatus.RECONCILING, ActionStatus.FAILED)


@pytest.mark.parametrize(
    "status", [ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.REJECTED]
)
def test_terminal_actions_have_no_outgoing_transitions(status: ActionStatus) -> None:
    assert is_action_terminal(status)
    assert not any(can_transition(status, target) for target in ActionStatus)


def test_a_rejected_action_can_never_execute() -> None:
    assert not can_transition(ActionStatus.REJECTED, ActionStatus.EXECUTING)
    assert not can_transition(ActionStatus.REJECTED, ActionStatus.APPROVED)


@pytest.mark.parametrize("outcome", list(AttemptOutcome))
def test_every_outcome_maps_onto_an_action_status(outcome: AttemptOutcome) -> None:
    assert can_transition(ActionStatus.EXECUTING, action_status_for(outcome))


def test_a_resolved_action_returns_the_task_to_ready() -> None:
    # An action succeeding does not make the whole task SUCCEEDED; it makes the
    # task ready for a planner to decide what happens next.
    assert task_status_for(ActionStatus.SUCCEEDED) is TaskStatus.READY
    assert task_status_for(ActionStatus.FAILED) is TaskStatus.READY
    assert task_status_for(ActionStatus.REJECTED) is TaskStatus.READY


def test_an_unresolved_action_is_mirrored_onto_its_task() -> None:
    assert task_status_for(ActionStatus.OUTCOME_UNKNOWN) is TaskStatus.OUTCOME_UNKNOWN
    assert task_status_for(ActionStatus.RECONCILING) is TaskStatus.RECONCILING
    assert task_status_for(ActionStatus.WAITING_APPROVAL) is TaskStatus.WAITING_APPROVAL
    assert task_status_for(ActionStatus.EXECUTING) is TaskStatus.EXECUTING


def test_proposing_alone_does_not_move_the_task() -> None:
    assert task_status_for(ActionStatus.PROPOSED) is None


@pytest.mark.parametrize("tier", list(RiskTier))
def test_every_risk_tier_still_requires_explicit_approval(tier: RiskTier) -> None:
    # The policy engine that may auto-approve R0/R1 is a later milestone; until
    # it exists the fail-safe answer is the only correct one.
    assert requires_approval(tier) is True
