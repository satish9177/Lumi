import uuid

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
