import json
import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.action_status import ActionStatus, ApprovalStatus, AttemptOutcome, RiskTier
from app.domain.booking_criteria import BookingCriteria
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.browser_profile import BrowserProfile, ProfileStatus
from app.domain.login_takeover import LoginAttempt, LoginAttemptStatus
from app.domain.digest import canonical_json
from app.domain.page_observation import (
    DisclosureSpec,
    EvidenceQuote,
    PageAnswer,
    PageLink,
    TextBlock,
)
from app.domain.research import (
    GrantStatus,
    ObservedLink,
    # Same name, different model: a research block has its own limits, and the
    # Milestone 7a block type must not silently validate one.
)
from app.domain.research import TextBlock as ResearchTextBlock
from app.domain.research import (
    ResearchAnswer,
    ResearchBudgets,
    ResearchDisclosure,
    ResearchEvidence,
    ResearchOperation,
    ResearchScope,
    SearchResult,
)
from app.domain.task_status import TaskStatus
from app.repositories.actions import ApprovalRecord, AttemptRecord
from app.repositories.observations import ObservationRecord
from app.repositories.research import AnswerRecord as ResearchAnswerRecord
from app.repositories.research import GrantRecord
from app.repositories.research import ObservationRecord as ResearchObservationRecord
from app.repositories.research import SessionRecord
from app.repositories.tasks import TaskEventRecord, TaskRecord
from app.services.actions import ActionView
from app.services.booking_preparation import SLOT_ID_PATTERN
from app.services.research_tasks import ResearchView as ResearchViewModel
from app.services.research_tasks import StepResult as StepResultModel

MAX_REQUEST_BYTES = 64_000


class TaskRequestPayload(BaseModel):
    """Structured task request. `type` is required; other JSON fields are kept verbatim."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.]*$")
    text: str | None = Field(default=None, max_length=4_000)

    @field_validator("type")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        # Milestone 9 S2: a desktop read is created only by its own route, after a local observation.
        # Milestone 10 final audit (Pass A, F3): nor any M9/M10 controller-owned task type.
        if value in (
            "desktop_read", "desktop_action", "desktop_action_planning", "desktop_vision",
            "document_task", "file_transfer_task", "project_run_task",
        ):
            raise ValueError("this task type is created only by its own route")
        return value


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
    #: The highest event sequence written for this task, so a client replaying
    #: `/events` knows when it has caught up.
    last_event_sequence: int
    request: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, task: TaskRecord) -> "TaskResponse":
        return cls(
            id=task.id,
            status=task.status,
            revision=task.revision,
            last_event_sequence=task.last_event_sequence,
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
    #: Exactly one of these is set: the exact approval the user granted for
    #: this proposal, or the single-use step authorization a research grant
    #: derived. Never both, never neither (a database CHECK).
    approval_id: uuid.UUID | None
    step_authorization_id: uuid.UUID | None
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
            step_authorization_id=attempt.step_authorization_id,
            runtime_generation=attempt.runtime_generation,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            outcome=attempt.outcome,
            result=attempt.result,
            error_code=attempt.error_code,
        )


#: Card-only strings a desktop proposal keeps for the trusted approval card. Most are another program's
#: text (a window title, a control name); `value` (S4) is the person's own trusted text to be written,
#: which is exactly as sensitive and is never returned by a generic read either. The generic action
#: reads do not return any of them.
_DESKTOP_CARD_STRINGS = frozenset(
    {
        "window_title", "control_name", "application_label",
        "container_name", "option_name", "value",
    }
)


def _public_proposal(tool_name: str, proposal: dict[str, Any]) -> dict[str, Any]:
    if tool_name.startswith("DESKTOP_"):
        return {key: value for key, value in proposal.items() if key not in _DESKTOP_CARD_STRINGS}
    return proposal


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
            proposal=_public_proposal(action.tool_name, action.proposal),
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


class BrowserDispatchResponse(BaseModel):
    """One piece of browser work recorded for an action.

    `effect` and `submitted` are the fields worth reading: together they answer
    "was a browser sent to change the world for this action, and did it press
    the button", which is the question a duplicate-execution bug would fail.
    """

    id: uuid.UUID
    attempt_id: uuid.UUID | None
    worker_generation: uuid.UUID
    operation: str
    site: str
    effect: BrowserEffect
    status: DispatchStatus
    submitted: bool
    observation_id: uuid.UUID | None
    error_code: str | None
    duration_ms: int | None
    started_at: datetime
    finished_at: datetime | None


class BrowserDispatchListResponse(BaseModel):
    action_id: uuid.UUID
    dispatches: list[BrowserDispatchResponse]


class PrepareBookingBody(BaseModel):
    """Only which slot to observe. Every booked value is read by the worker."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(pattern=SLOT_ID_PATTERN)


class BookingSlotResponse(BaseModel):
    slot_id: str
    doctor: str
    specialty: str
    time: datetime
    price: int
    currency: str


class BookingSearchResponse(BaseModel):
    task_id: uuid.UUID
    slots: list[BookingSlotResponse]


class DoctorProfileResponse(BaseModel):
    doctor_id: str
    doctor: str
    specialty: str
    clinic: str
    address: str
    hours: str
    consultation_fee: int
    currency: str
    languages: list[str]
    walk_ins: bool


class ClinicInfoResponse(BaseModel):
    task: TaskResponse
    profiles: list[DoctorProfileResponse]


class ReviseBookingCriteriaBody(BaseModel):
    """The complete new constraints and the task revision they were derived from.

    The whole criteria set is sent, not a patch: the caller read the current
    constraints at `expected_revision` and says exactly what they become.
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    criteria: BookingCriteria


class ReviseBookingCriteriaResponse(BaseModel):
    task: TaskResponse
    #: Prepared bookings rejected because the new constraints exclude them.
    invalidated_action_ids: list[uuid.UUID]


class CancelBookingTaskResponse(BaseModel):
    task: TaskResponse
    #: Prepared, never-executed bookings rejected by the cancellation.
    rejected_action_ids: list[uuid.UUID]


# ---- Milestone 7a: public page inspection -----------------------------------------


class PrepareInspectionBody(BaseModel):
    """Only who may receive page text. The URL and question come from the task."""

    model_config = ConfigDict(extra="forbid")

    disclosure: DisclosureSpec


class PageObservationResponse(BaseModel):
    """The stored observation. Page text here is untrusted environment data."""

    id: uuid.UUID
    task_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    dispatch_id: uuid.UUID
    worker_generation: uuid.UUID
    schema_version: int
    provenance: Literal["untrusted_environment"]
    requested_url: str
    final_url: str
    redirects: list[str]
    title: str
    document_epoch: int
    settled: bool
    truncated: bool
    observed_at: datetime
    content_hash: str
    blocks: list[TextBlock]
    links: list[PageLink]
    total_text_chars: int
    total_link_count: int

    @classmethod
    def from_record(cls, record: ObservationRecord) -> "PageObservationResponse":
        observation = record.observation
        return cls(
            id=observation.observation_id,
            task_id=record.task_id,
            action_id=record.action_id,
            attempt_id=record.attempt_id,
            dispatch_id=record.dispatch_id,
            worker_generation=record.worker_generation,
            schema_version=observation.schema_version,
            provenance=observation.provenance,
            requested_url=observation.requested_url,
            final_url=observation.final_url,
            redirects=observation.redirects,
            title=observation.title,
            document_epoch=observation.document_epoch,
            settled=observation.settled,
            truncated=observation.truncated,
            observed_at=observation.observed_at,
            content_hash=observation.content_hash,
            blocks=observation.blocks,
            links=observation.links,
            total_text_chars=observation.total_text_chars,
            total_link_count=observation.total_link_count,
        )


class PageAnswerResponse(BaseModel):
    observation_id: uuid.UUID
    content_hash: str
    status: str
    answer: str
    evidence: list[EvidenceQuote]
    provider: str
    model: str
    answered_at: datetime


class InspectionResponse(BaseModel):
    action: ActionResponse
    observation: PageObservationResponse | None
    answer: PageAnswerResponse | None

    @classmethod
    def build(cls, action: ActionView, record: ObservationRecord | None) -> "InspectionResponse":
        answer = None
        if record is not None and record.answer is not None:
            answer = PageAnswerResponse(
                observation_id=record.id,
                content_hash=record.observation.content_hash,
                status=record.answer.answer.status,
                answer=record.answer.answer.answer,
                evidence=record.answer.answer.evidence,
                provider=record.answer.provider,
                model=record.answer.model,
                answered_at=record.answer.answered_at,
            )
        return cls(
            action=ActionResponse.from_view(action),
            observation=PageObservationResponse.from_record(record) if record is not None else None,
            answer=answer,
        )


class RecordPageAnswerBody(BaseModel):
    """A grounded answer, bound to the exact observation it was built from."""

    model_config = ConfigDict(extra="forbid")

    observation_id: uuid.UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer: PageAnswer
    provider: Literal["openai", "gemini", "deepseek", "scripted"]
    model: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# ---- Milestone 7b: public web research -------------------------------------
#
# Note what crosses this boundary and what does not. The response carries
# semantic refs, bounded labels, hosts, and the addresses of pages Lumi
# actually visited (its cited sources). It does **not** carry the addresses
# observed links or search results point at: those live in the runtime's
# `targets` column and in the worker's ref tables, so a planner is never handed
# an address it could paste back as a destination.


class PrepareResearchBody(BaseModel):
    """Who may receive observed page text, and the budgets to enforce.

    The objective comes from the persisted task. Everything authority-bearing
    in the scope -- the operation list, the network rules, the policy version,
    the seed addresses -- is built by the runtime from its own configuration.
    """

    model_config = ConfigDict(extra="forbid")

    disclosure: ResearchDisclosure
    budgets: ResearchBudgets = Field(default_factory=ResearchBudgets)


class ConfirmResearchGrantBody(BaseModel):
    """The trusted click: this grant, at the revision that was on screen."""

    model_config = ConfigDict(extra="forbid")

    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class RevokeResearchGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Literal["user_stopped", "user_declined", "task_cancelled"] = "user_stopped"


class ResearchGrantResponse(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope_digest: str
    scope: ResearchScope
    created_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_record(cls, record: GrantRecord) -> "ResearchGrantResponse":
        return cls(
            id=record.id,
            task_id=record.task_id,
            status=record.status,
            revision=record.revision,
            policy_version=record.policy_version,
            scope_digest=record.scope_digest,
            scope=record.scope,
            created_at=record.created_at,
            confirmed_at=record.confirmed_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            completed_at=record.completed_at,
        )


class ResearchSessionResponse(BaseModel):
    id: uuid.UUID
    status: str
    worker_generation: uuid.UUID
    created_at: datetime
    closed_at: datetime | None

    @classmethod
    def from_record(cls, record: SessionRecord) -> "ResearchSessionResponse":
        return cls(
            id=record.id,
            status=record.status,
            worker_generation=record.worker_generation,
            created_at=record.created_at,
            closed_at=record.closed_at,
        )


class ResearchObservationResponse(BaseModel):
    """One bounded observation. Everything in it is untrusted page data."""

    id: uuid.UUID
    task_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    session_id: uuid.UUID | None
    worker_generation: uuid.UUID | None
    sequence: int
    #: The model-facing ref for this observation: `o<sequence>`.
    ref: str
    schema_version: int
    provenance: Literal["untrusted_environment"]
    kind: str
    operation: ResearchOperation
    tab: str | None
    document_epoch: int
    query: str | None
    requested_url: str | None
    final_url: str | None
    final_host: str | None
    redirects: list[str]
    title: str
    settled: bool
    truncated: bool
    observed_at: datetime
    content_hash: str
    blocks: list[ResearchTextBlock]
    links: list[ObservedLink]
    results: list[SearchResult]
    open_tabs: list[str]
    total_text_chars: int
    total_link_count: int

    @classmethod
    def from_record(cls, record: ResearchObservationRecord) -> "ResearchObservationResponse":
        observation = record.observation
        return cls(
            id=observation.observation_id,
            task_id=record.task_id,
            action_id=record.action_id,
            attempt_id=record.attempt_id,
            session_id=record.session_id,
            worker_generation=record.worker_generation,
            sequence=observation.sequence,
            ref=observation.ref,
            schema_version=observation.schema_version,
            provenance=observation.provenance,
            kind=observation.kind,
            operation=observation.operation,
            tab=observation.tab,
            document_epoch=observation.document_epoch,
            query=observation.query,
            requested_url=observation.requested_url,
            final_url=observation.final_url,
            final_host=observation.final_host,
            redirects=observation.redirects,
            title=observation.title,
            settled=observation.settled,
            truncated=observation.truncated,
            observed_at=observation.observed_at,
            content_hash=observation.content_hash,
            blocks=observation.blocks,
            links=observation.links,
            results=observation.results,
            open_tabs=observation.open_tabs,
            total_text_chars=observation.total_text_chars,
            total_link_count=observation.total_link_count,
        )


class ResearchBudgetUsageResponse(BaseModel):
    steps: int
    observations: int
    planner_calls: int
    active_seconds: float
    tabs: int


class ResearchAnswerResponse(BaseModel):
    status: str
    stop_reason: str
    answer: str
    evidence: list[ResearchEvidence]
    provider: str
    model: str
    steps_used: int
    observations_used: int
    planner_calls: int
    created_at: datetime

    @classmethod
    def from_record(cls, record: ResearchAnswerRecord) -> "ResearchAnswerResponse":
        return cls(
            status=record.answer.status,
            stop_reason=record.answer.stop_reason,
            answer=record.answer.answer,
            evidence=record.answer.evidence,
            provider=record.provider,
            model=record.model,
            steps_used=record.steps_used,
            observations_used=record.observations_used,
            planner_calls=record.planner_calls,
            created_at=record.created_at,
        )


class ResearchResponse(BaseModel):
    task: TaskResponse
    objective: str
    grant: ResearchGrantResponse | None
    session: ResearchSessionResponse | None
    observations: list[ResearchObservationResponse]
    answer: ResearchAnswerResponse | None
    usage: ResearchBudgetUsageResponse
    #: Whether a public search provider is configured at all. Without one the
    #: task can still navigate to an address the user typed and follow links.
    search_configured: bool
    #: A step of this task has no outcome Lumi can stand behind. Further steps
    #: are refused while this is true, and nothing is repeated on its behalf.
    unresolved_step: bool

    @classmethod
    def from_view(cls, view: ResearchViewModel) -> "ResearchResponse":
        return cls(
            task=TaskResponse.from_record(view.task),
            objective=view.objective,
            grant=ResearchGrantResponse.from_record(view.grant) if view.grant else None,
            session=ResearchSessionResponse.from_record(view.session) if view.session else None,
            observations=[
                ResearchObservationResponse.from_record(record) for record in view.observations
            ],
            answer=ResearchAnswerResponse.from_record(view.answer) if view.answer else None,
            usage=ResearchBudgetUsageResponse(
                steps=view.usage.steps,
                observations=view.usage.observations,
                planner_calls=view.usage.planner_calls,
                active_seconds=round(view.usage.active_seconds, 3),
                tabs=view.usage.tabs,
            ),
            search_configured=view.search_configured,
            unresolved_step=view.unresolved_step,
        )


class ResearchStepResponse(BaseModel):
    research: ResearchResponse
    action: ActionResponse
    observation: ResearchObservationResponse | None
    outcome: AttemptOutcome
    error_code: str | None
    replayed: bool

    @classmethod
    def from_result(cls, result: StepResultModel) -> "ResearchStepResponse":
        return cls(
            research=ResearchResponse.from_view(result.view),
            action=ActionResponse.from_view(result.action),
            observation=(
                ResearchObservationResponse.from_record(result.observation)
                if result.observation
                else None
            ),
            outcome=result.outcome,
            error_code=result.error_code,
            replayed=result.replayed,
        )


class ExecuteResearchStepBody(BaseModel):
    """One step, as the trusted planner submits it.

    `step` is deliberately an opaque object here and is parsed by the closed
    `ResearchStep` union in `app.domain.research`, so an operation outside the
    vocabulary is refused with a stable code rather than as a generic schema
    error. `request_id` makes the submission idempotent: replaying it returns
    the stored result of that step and executes nothing.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    step: dict[str, Any]
    planner_calls: int = Field(default=0, ge=0, le=1_000)


class RecordResearchAnswerBody(BaseModel):
    """The one grounded answer, with the planner budget it was reached under."""

    model_config = ConfigDict(extra="forbid")

    answer: ResearchAnswer
    provider: Literal["openai", "gemini", "deepseek", "scripted"]
    model: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    planner_calls: int = Field(default=0, ge=0, le=1_000)


# ---- Milestone 8a S1: persistent browser profiles ---------------------------


class ActiveTakeoverResponse(BaseModel):
    """The open takeover on a profile, if there is one (Milestone 8a S2).

    Four fields, and deliberately no fifth. This exists so the desktop can
    rediscover -- from durable runtime state alone, after an Electron-main
    restart -- that a headed, human-driven browser window may still be on
    screen, and keep screen capture refused until it is gone. Answering that
    question needs an id, a status and an expiry; it needs no URL, no page
    text, no title, no profile directory, no credential signal and no account
    identity, so none of those are here and adding one would be a boundary
    change, not a convenience.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    attempt_id: uuid.UUID
    status: LoginAttemptStatus
    expires_at: datetime

    @classmethod
    def from_attempt(cls, attempt: LoginAttempt) -> "ActiveTakeoverResponse":
        return cls(
            profile_id=attempt.profile_id,
            attempt_id=attempt.id,
            status=attempt.status,
            expires_at=attempt.expires_at,
        )


class BrowserProfileResponse(BaseModel):
    """One profile, as the trusted controller describes it.

    This model is the boundary. Read it as the list of everything that may
    leave the runtime about a profile, and note what is missing and will stay
    missing: **no directory path, no `userDataDir`, no cookie file, no
    `storageState`, no browser executable path, no token, and no raw account
    identity.** The account fields are hashes or `null`; `null` means "not
    observed" and is never treated as a sign that anybody is signed in.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    label: str
    site: str
    allowed_origins: list[str]
    status: ProfileStatus
    revision: int
    #: Which browser last opened it. Support and version checks, nothing more.
    chromium_build: str | None
    playwright_version: str | None
    app_version: str | None
    #: Whether *some* runtime generation currently holds the lease. The
    #: generation id itself is internal.
    leased: bool
    lease_expires_at: datetime | None
    revoke_epoch: int
    account_fingerprint: str | None
    account_label_hash: str | None
    last_login_completed_at: datetime | None
    last_observed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    #: Milestone 8a S2: the open takeover on this profile, if one exists.
    #: `None` means "no attempt of this profile is in an open status", which
    #: is a durable fact, not an absence of information.
    active_takeover: ActiveTakeoverResponse | None = None

    @classmethod
    def from_profile(
        cls, profile: BrowserProfile, *, active_takeover: LoginAttempt | None = None
    ) -> "BrowserProfileResponse":
        return cls(
            id=profile.id,
            label=profile.label,
            site=profile.site,
            allowed_origins=list(profile.allowed_origins),
            status=profile.status,
            revision=profile.revision,
            chromium_build=profile.chromium_build,
            playwright_version=profile.playwright_version,
            app_version=profile.app_version,
            leased=profile.lease_runtime_generation is not None,
            lease_expires_at=profile.lease_expires_at,
            revoke_epoch=profile.revoke_epoch,
            account_fingerprint=profile.account_fingerprint,
            account_label_hash=profile.account_label_hash,
            last_login_completed_at=profile.last_login_completed_at,
            last_observed_at=profile.last_observed_at,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
            deleted_at=profile.deleted_at,
            active_takeover=(
                ActiveTakeoverResponse.from_attempt(active_takeover)
                if active_takeover is not None
                else None
            ),
        )


class BrowserProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[BrowserProfileResponse]


class CreateBrowserProfileBody(BaseModel):
    """Create one profile for one site.

    `site` is a host name or a URL the *trusted* caller supplied; it is
    canonicalised to a registrable domain through the pinned Public Suffix List
    before anything is written. There is no `profilePath`, no `userDataDir`, no
    `browserExecutablePath` and no `storageState` field here, and adding one
    would be a boundary change, not a convenience.
    """

    model_config = ConfigDict(extra="forbid")

    site: str = Field(min_length=1, max_length=253)
    label: str = Field(min_length=1, max_length=60)


class DeleteBrowserProfileBody(BaseModel):
    """Remove the local profile directory. Never a website logout."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(default=None, ge=1)


# ---- Milestone 8a S2: manual login and human takeover -----------------------


class LoginAttemptResponse(BaseModel):
    """One takeover, as the trusted controller describes it.

    Note what is absent: no page text, no title, no URL, no credential
    signal, no raw account identity. This is a bounded interval and how it
    ended, nothing else.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    profile_id: uuid.UUID
    status: LoginAttemptStatus
    started_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None

    @classmethod
    def from_attempt(cls, attempt: LoginAttempt) -> "LoginAttemptResponse":
        return cls(
            id=attempt.id,
            profile_id=attempt.profile_id,
            status=attempt.status,
            started_at=attempt.started_at,
            expires_at=attempt.expires_at,
            completed_at=attempt.completed_at,
            cancelled_at=attempt.cancelled_at,
        )


class LoginTakeoverResponse(BaseModel):
    """The result of starting, cancelling or confirming one takeover.

    `refusal_reason` is set only when a *completed* confirmation did not
    result in the profile becoming `AUTHENTICATED` -- e.g. a login surface
    remained, or the browser ended off the profile's own site. It is a
    closed, stable code, never page text.
    """

    model_config = ConfigDict(extra="forbid")

    attempt: LoginAttemptResponse
    profile: BrowserProfileResponse
    refusal_reason: str | None = None


class OpenLoginWindowBody(BaseModel):
    """The trusted "Sign in manually" click. Names the profile revision shown
    on screen; carries nothing else."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class ConfirmSignedInBody(BaseModel):
    """The trusted "I'm signed in" click. This is not, by itself, proof that
    a sign-in succeeded -- it only starts the deterministic post-login check."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class CancelLoginBody(BaseModel):
    """The trusted "Cancel" click. Never a logout."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class HealthResponse(BaseModel):
    status: str
    database: str
    runtime_generation: uuid.UUID


class ErrorDetail(BaseModel):
    code: str
    message: str
    current_revision: int | None = None
    reason: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ObserveDesktopSurfaceBody(BaseModel):
    """Which surface, exactly as listed: the worker generation it was listed under, its ref and its
    epoch. A pair is only unique within one worker generation, and a ref alone could name a different
    window after the inventory moved on, so all three are required."""

    model_config = ConfigDict(extra="forbid")

    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
