import json
import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.task_status import TaskStatus
from app.repositories.tasks import TaskEventRecord, TaskRecord

MAX_REQUEST_BYTES = 64_000


class TaskRequestPayload(BaseModel):
    """Structured task request. `type` is required; other JSON fields are kept verbatim."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.]*$")
    text: str | None = Field(default=None, max_length=4_000)


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: TaskRequestPayload

    @model_validator(mode="after")
    def _bounded_size(self) -> Self:
        encoded = json.dumps(self.request.model_dump(mode="json", exclude_none=True))
        if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError(f"request must be at most {MAX_REQUEST_BYTES} bytes of JSON")
        return self


class CancelTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


class TaskResponse(BaseModel):
    id: uuid.UUID
    status: TaskStatus
    revision: int
    request: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, task: TaskRecord) -> "TaskResponse":
        return cls(
            id=task.id,
            status=task.status,
            revision=task.revision,
            request=task.request,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


class TaskEventResponse(BaseModel):
    id: int
    task_id: uuid.UUID
    sequence: int
    task_revision: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_record(cls, event: TaskEventRecord) -> "TaskEventResponse":
        return cls(
            id=event.id,
            task_id=event.task_id,
            sequence=event.sequence,
            task_revision=event.task_revision,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )


class TaskEventListResponse(BaseModel):
    task_id: uuid.UUID
    events: list[TaskEventResponse]


class HealthResponse(BaseModel):
    status: str
    database: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    current_revision: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
