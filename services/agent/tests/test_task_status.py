import pytest

from app.domain.task_status import TERMINAL_STATUSES, TaskStatus, can_cancel, is_terminal


def test_milestone_one_states_are_exactly_defined() -> None:
    assert [status.value for status in TaskStatus] == [
        "CREATED",
        "PLANNING",
        "READY",
        "EXECUTING",
        "VERIFYING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "PAUSED",
    ]


def test_terminal_states() -> None:
    assert TERMINAL_STATUSES == {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}


@pytest.mark.parametrize("status", list(TaskStatus))
def test_only_non_terminal_tasks_can_be_cancelled(status: TaskStatus) -> None:
    assert can_cancel(status) is not is_terminal(status)
