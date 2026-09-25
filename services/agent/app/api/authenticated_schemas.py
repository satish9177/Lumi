"""Request and response models for Milestone 8a S3 authenticated account reading.

Read the response models as the list of everything that may leave the runtime
about an authenticated task, and note what is missing and stays missing: **no
URL of any kind, no cookie, no header, no profile directory, no raw account
identity, and not even the account fingerprint hash or the profile's revoke
epoch** -- the renderer and main have no use for either, so neither is sent.
Every piece of text in an observation is the redacted projection the provider
received.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import ActionResponse, TaskResponse
from app.domain.action_status import AttemptOutcome
from app.domain.authenticated import (
    AuthenticatedBudgets,
    AuthenticatedDisclosure,
    AuthenticatedReadScope,
    AuthOperation,
    Recipient,
)
from app.domain.browser_profile import ProfileStatus
from app.domain.research import GrantStatus, ObservedLink, ResearchAnswer, ResearchEvidence
from app.domain.research import TextBlock as ResearchTextBlock
from app.repositories.authenticated import (
    AuthenticatedAnswerRecord,
    AuthenticatedGrantRecord,
    AuthenticatedObservationRecord,
)
from app.services.authenticated_read import AuthenticatedView, ProfileSummary, StepResult


class PrepareAuthenticatedBody(BaseModel):
    """The one provider that may receive account text, chosen from a list main
    built from its own configuration. Everything authority-bearing in the scope
    -- the site, operations, methods, budgets, account fingerprint and revoke
    epoch -- is built by the runtime from the profile row."""

    model_config = ConfigDict(extra="forbid")

    recipient: Recipient


class ConfirmAuthenticatedGrantBody(BaseModel):
    """The trusted click: this grant, at the revision that was on screen."""

    model_config = ConfigDict(extra="forbid")

    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class RevokeAuthenticatedGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    #: Milestone 12 S3: `grant_unusable` is Electron main's own orchestration Continue-time re-observe path
    #: (`OrchestrationCoordinator`/`AgentTaskController.continueAccountRead`), used only when a fresh,
    #: forced observation could not proceed under the existing grant (a different account now signed in, or
    #: the grant expired) -- never chosen by a model, and never a new authority: `revoke()` itself is
    #: unchanged, this only adds one more honest label for why it was called.
    reason: Literal["user_stopped", "user_declined", "task_cancelled", "grant_unusable"] = "user_stopped"


class ExecuteAuthenticatedStepBody(BaseModel):
    """One step, as the trusted planner submits it.

    `step` is deliberately an opaque object here and is parsed by the closed
    `AuthenticatedStep` union, so an operation outside the vocabulary -- or any
    key outside it, such as `url`, `selector`, `provider` or `key` -- is refused
    with a stable code rather than as a generic schema error.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    step: dict[str, Any]
    planner_calls: int = Field(default=0, ge=0, le=1_000)


class RecordAuthenticatedAnswerBody(BaseModel):
    """The one grounded answer, and which provider wrote it. The runtime
    refuses any provider that is not the grant's recipient."""

    model_config = ConfigDict(extra="forbid")

    answer: ResearchAnswer
    provider: Recipient
    model: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    planner_calls: int = Field(default=0, ge=0, le=1_000)


class AuthenticatedScopeResponse(BaseModel):
    """The scope as the card shows it. No fingerprint, no epoch."""

    model_config = ConfigDict(extra="forbid")

    policy_version: str
    site: str
    allowed_origins: list[str]
    allowed_operations: list[AuthOperation]
    methods: list[str]
    classification: Literal["account_private"]
    allowed: list[str]
    forbidden: list[str]
    website_side_effects_possible: bool
    disclosure: AuthenticatedDisclosure
    budgets: AuthenticatedBudgets

    @classmethod
    def from_scope(cls, scope: AuthenticatedReadScope) -> "AuthenticatedScopeResponse":
        return cls(
            policy_version=scope.policy_version,
            site=scope.site,
            allowed_origins=scope.allowed_origins,
            allowed_operations=scope.allowed_operations,
            methods=list(scope.methods),
            classification=scope.classification,
            allowed=scope.allowed,
            forbidden=scope.forbidden,
            website_side_effects_possible=scope.website_side_effects_possible,
            disclosure=scope.disclosure,
            budgets=scope.budgets,
        )


class AuthenticatedGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope_digest: str
    scope: AuthenticatedScopeResponse
    created_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None

    @classmethod
    def from_record(cls, record: AuthenticatedGrantRecord) -> "AuthenticatedGrantResponse":
        return cls(
            id=record.id,
            task_id=record.task_id,
            status=record.status,
            revision=record.revision,
            policy_version=record.policy_version,
            scope_digest=record.scope_digest,
            scope=AuthenticatedScopeResponse.from_scope(record.scope),
            created_at=record.created_at,
            confirmed_at=record.confirmed_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            completed_at=record.completed_at,
        )


class AuthenticatedProfileResponse(BaseModel):
    """Trusted, controller-authored profile facts. The label is the user's own."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    label: str
    site: str
    status: ProfileStatus

    @classmethod
    def from_summary(cls, summary: ProfileSummary) -> "AuthenticatedProfileResponse":
        return cls(id=summary.id, label=summary.label, site=summary.site, status=summary.status)


class AuthenticatedObservationResponse(BaseModel):
    """One bounded, redacted observation. Everything in it is untrusted data."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    task_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_generation: uuid.UUID | None
    sequence: int
    ref: str
    #: The version of *this response shape* -- text and links, exactly as in S3.
    #: A stored observation may be schema version 2 (S4 adds a local form
    #: inventory), but the inventory is deliberately not part of this response:
    #: nothing outside the runtime's own database and worker receives it in S4.
    schema_version: Literal[1]
    provenance: Literal["untrusted_environment"]
    classification: Literal["account_private"]
    kind: str
    operation: AuthOperation
    tab: str | None
    document_epoch: int
    host: str | None
    title: str
    settled: bool
    truncated: bool
    observed_at: datetime
    content_hash: str
    blocks: list[ResearchTextBlock]
    links: list[ObservedLink]
    open_tabs: list[str]
    total_text_chars: int
    total_link_count: int
    redactions: dict[str, int]

    @classmethod
    def from_record(
        cls, record: AuthenticatedObservationRecord
    ) -> "AuthenticatedObservationResponse":
        observation = record.observation
        return cls(
            id=observation.observation_id,
            task_id=record.task_id,
            action_id=record.action_id,
            attempt_id=record.attempt_id,
            worker_generation=record.worker_generation,
            sequence=observation.sequence,
            ref=observation.ref,
            schema_version=1,
            provenance=observation.provenance,
            classification=observation.classification,
            kind=observation.kind,
            operation=observation.operation,
            tab=observation.tab,
            document_epoch=observation.document_epoch,
            host=observation.host,
            title=observation.title,
            settled=observation.settled,
            truncated=observation.truncated,
            observed_at=observation.observed_at,
            content_hash=observation.content_hash,
            blocks=observation.blocks,
            links=observation.links,
            open_tabs=observation.open_tabs,
            total_text_chars=observation.total_text_chars,
            total_link_count=observation.total_link_count,
            redactions=observation.redactions,
        )


class AuthenticatedUsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: int
    observations: int
    planner_calls: int
    active_seconds: float
    tabs: int


class AuthenticatedAnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Literal["account_private"]
    profile_id: uuid.UUID
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
    def from_record(cls, record: AuthenticatedAnswerRecord) -> "AuthenticatedAnswerResponse":
        return cls(
            classification="account_private",
            profile_id=record.profile_id,
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


class AuthenticatedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: TaskResponse
    objective: str
    classification: Literal["account_private"] = "account_private"
    profile: AuthenticatedProfileResponse | None
    grant: AuthenticatedGrantResponse | None
    observations: list[AuthenticatedObservationResponse]
    answer: AuthenticatedAnswerResponse | None
    usage: AuthenticatedUsageResponse
    #: `login_required`, `account_changed`, `account_identity_unknown` or
    #: `left_site_scope` while the task is paused; otherwise null. Set by
    #: deterministic code, never by a model.
    pause_reason: str | None
    unresolved_step: bool

    @classmethod
    def from_view(cls, view: AuthenticatedView) -> "AuthenticatedResponse":
        return cls(
            task=TaskResponse.from_record(view.task),
            objective=view.objective,
            profile=AuthenticatedProfileResponse.from_summary(view.profile) if view.profile else None,
            grant=AuthenticatedGrantResponse.from_record(view.grant) if view.grant else None,
            observations=[
                AuthenticatedObservationResponse.from_record(record) for record in view.observations
            ],
            answer=AuthenticatedAnswerResponse.from_record(view.answer) if view.answer else None,
            usage=AuthenticatedUsageResponse(
                steps=view.usage.steps,
                observations=view.usage.observations,
                planner_calls=view.usage.planner_calls,
                active_seconds=round(view.usage.active_seconds, 3),
                tabs=view.usage.tabs,
            ),
            pause_reason=view.pause_reason.value if view.pause_reason else None,
            unresolved_step=view.unresolved_step,
        )


class AuthenticatedStepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authenticated: AuthenticatedResponse
    action: ActionResponse
    observation: AuthenticatedObservationResponse | None
    outcome: AttemptOutcome
    error_code: str | None
    pause_reason: str | None
    replayed: bool

    @classmethod
    def from_result(cls, result: StepResult) -> "AuthenticatedStepResponse":
        return cls(
            authenticated=AuthenticatedResponse.from_view(result.view),
            action=ActionResponse.from_view(result.action),
            observation=(
                AuthenticatedObservationResponse.from_record(result.observation)
                if result.observation
                else None
            ),
            outcome=result.outcome,
            error_code=result.error_code,
            pause_reason=result.pause_reason.value if result.pause_reason else None,
            replayed=result.replayed,
        )
