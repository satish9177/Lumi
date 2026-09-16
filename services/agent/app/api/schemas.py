import json
import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.digest import canonical_json
from app.domain.task_status import TaskStatus
from app.repositories.actions import ApprovalRecord, AttemptRecord
from app.repositories.tasks import TaskEventRecord, TaskRecord
from app.services.actions import ActionView

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


MAX_PROPOSAL_BYTES = 64_000
MAX_RESULT_BYTES = 16_000


class ProposeActionBody(BaseModel):
    """A proposed action.

    There is deliberately no digest field: the server computes the digest from
    the proposal it received, so an approval can only ever be bound to bytes the
    server actually stored.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.]*$")
    risk_tier: RiskTier
    proposal: dict[str, Any]

    @model_validator(mode="after")
    def _bounded_proposal(self) -> Self:
        if len(canonical_json(self.proposal).encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError(f"proposal must be at most {MAX_PROPOSAL_BYTES} bytes of JSON")
        return self


class ExpectedRevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


class RejectActionBody(ExpectedRevisionBody):
    reason: str | None = Field(default=None, max_length=500)


class FinishAttemptBody(ExpectedRevisionBody):
    """What the executor observed.

    `OUTCOME_UNKNOWN` is accepted and is not a failure: it is how an executor
    reports that it lost the response and cannot say whether the side effect
    happened.
    """

    outcome: AttemptOutcome
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _bounded_result(self) -> Self:
        if self.result is not None:
            if len(canonical_json(self.result).encode("utf-8")) > MAX_RESULT_BYTES:
                raise ValueError(f"result must be at most {MAX_RESULT_BYTES} bytes of JSON")
        return self


class FinishReconciliationBody(ExpectedRevisionBody):
    """The authoritative answer, or an honest admission that there isn't one."""

    result: AttemptOutcome
    evidence: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _bounded_evidence(self) -> Self:
        if self.evidence is not None:
            if len(canonical_json(self.evidence).encode("utf-8")) > MAX_RESULT_BYTES:
                raise ValueError(f"evidence must be at most {MAX_RESULT_BYTES} bytes of JSON")
        return self


class ApprovalResponse(BaseModel):
    id: uuid.UUID
    action_id: uuid.UUID
    action_revision: int
    proposal_digest: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    rejected_at: datetime | None
    consumed_at: datetime | None

    @classmethod
    def from_record(cls, approval: ApprovalRecord) -> "ApprovalResponse":
        return cls(
            id=approval.id,
            action_id=approval.action_id,
            action_revision=approval.action_revision,
            proposal_digest=approval.proposal_digest,
            status=approval.status,
            created_at=approval.created_at,
            expires_at=approval.expires_at,
            approved_at=approval.approved_at,
            rejected_at=approval.rejected_at,
            consumed_at=approval.consumed_at,
        )


class AttemptResponse(BaseModel):
    id: uuid.UUID
    action_id: uuid.UUID
    attempt_number: int
    approval_id: uuid.UUID
    runtime_generation: uuid.UUID
    started_at: datetime
    finished_at: datetime | None
    outcome: AttemptOutcome | None
    result: dict[str, Any] | None
    error_code: str | None

    @classmethod
    def from_record(cls, attempt: AttemptRecord) -> "AttemptResponse":
        return cls(
            id=attempt.id,
            action_id=attempt.action_id,
            attempt_number=attempt.attempt_number,
            approval_id=attempt.approval_id,
            runtime_generation=attempt.runtime_generation,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            outcome=attempt.outcome,
            result=attempt.result,
            error_code=attempt.error_code,
        )


class ActionResponse(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    idempotency_key: str
    tool_name: str
    risk_tier: RiskTier
    proposal: dict[str, Any]
    proposal_digest: str
    status: ActionStatus
    revision: int
    created_at: datetime
    updated_at: datetime
    #: The one live (pending or granted) approval, if there is one.
    approval: ApprovalResponse | None
    attempts: list[AttemptResponse]

    @classmethod
    def from_view(cls, view: ActionView) -> "ActionResponse":
        action = view.action
        return cls(
            id=action.id,
            task_id=action.task_id,
            idempotency_key=action.idempotency_key,
            tool_name=action.tool_name,
            risk_tier=action.risk_tier,
            proposal=action.proposal,
            proposal_digest=action.proposal_digest,
            status=action.status,
            revision=action.revision,
            created_at=action.created_at,
            updated_at=action.updated_at,
            approval=(
                ApprovalResponse.from_record(view.approval) if view.approval is not None else None
            ),
            attempts=[AttemptResponse.from_record(attempt) for attempt in view.attempts],
        )


class ActionListResponse(BaseModel):
    task_id: uuid.UUID
    actions: list[ActionResponse]


class HealthResponse(BaseModel):
    status: str
    database: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    current_revision: int | None = None
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
