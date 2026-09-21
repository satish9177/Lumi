"""Request and response models for Milestone 9 S2 desktop disclosure.

Read the response models as the list of everything that may leave the runtime about a desktop read,
and note what is missing and stays missing: **no window handle, process id or path, no AutomationId,
class name, framework id, RuntimeId or coordinate, no snapshot, no snapshot digest, and no other
observation**. The card shows the window's display strings (untrusted text, rendered inertly); the
one provider context a claimed approval releases carries only the redacted, bounded projection.

Request bodies come from Electron main. The renderer supplies a surface identity and a typed
question and nothing else: the provider and model are chosen by main from its own configuration and
are *not* a renderer field anywhere on the IPC surface.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.desktop.protocol import CONTROL_REF_PATTERN
from app.domain.authenticated import Recipient
from app.domain.desktop_disclosure import (
    MAX_ANSWER_CHARS,
    MAX_EVIDENCE,
    MAX_OBJECTIVE_CHARS,
    MAX_QUOTE_CHARS,
)
from app.services.desktop_disclosure import CardView, DesktopReadView, ProviderContext


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDesktopReadBody(_Body):
    """Which surface, exactly as listed, the typed question, and the one recipient main chose."""

    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    recipient: Recipient
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)


class DesktopGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class DesktopRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Literal["user_declined", "user_stopped"] = "user_declined"


class RecordDesktopResultBody(_Body):
    """The ONE provider attempt's outcome: a parsed read-only result, or a closed failure code."""

    disclosure_id: uuid.UUID
    result: dict[str, Any] | None = None
    failure: Literal["model_unavailable", "invalid_output"] | None = None


class DesktopCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    recipient: Recipient
    model: str
    observed_at: datetime
    application_label: str
    window_title: str
    max_nodes: int
    max_text_bytes: int
    redaction_policy: str
    observation_available: bool
    node_count: int | None
    text_bytes: int | None
    redaction_count: int | None
    truncated: bool | None
    truncation: list[str]

    @classmethod
    def from_view(cls, card: CardView) -> "DesktopCardResponse":
        return cls(
            grant_id=card.grant_id,
            grant_revision=card.grant_revision,
            grant_status=card.grant_status,
            expires_at=card.expires_at,
            recipient=card.recipient,
            model=card.model,
            observed_at=card.observed_at,
            application_label=card.application_label,
            window_title=card.window_title,
            max_nodes=card.max_nodes,
            max_text_bytes=card.max_text_bytes,
            redaction_policy=card.redaction_policy,
            observation_available=card.observation_available,
            node_count=card.node_count,
            text_bytes=card.text_bytes,
            redaction_count=card.redaction_count,
            truncated=card.truncated,
            truncation=list(card.truncation),
        )


class DesktopDisclosureResponse(BaseModel):
    disclosure_id: uuid.UUID
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool


class DesktopEvidenceResponse(BaseModel):
    control_ref: str = Field(pattern=CONTROL_REF_PATTERN.pattern)
    quote: str = Field(max_length=MAX_QUOTE_CHARS)


class DesktopAnswerResponse(BaseModel):
    """The private, grounded result. Desktop text: it goes to the trusted view and nowhere else."""

    kind: Literal["answer", "cannot_answer"]
    answer: str | None = Field(max_length=MAX_ANSWER_CHARS)
    reason: str | None
    evidence: list[DesktopEvidenceResponse] = Field(max_length=MAX_EVIDENCE)
    recipient: Recipient
    model: str
    observed_at: datetime | None
    created_at: datetime


class DesktopReadResponse(BaseModel):
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: Literal[
        "awaiting_approval", "approved", "reasoning", "answered", "failed", "outcome_unknown", "declined", "expired"
    ]
    card: DesktopCardResponse | None
    disclosure: DesktopDisclosureResponse | None
    answer: DesktopAnswerResponse | None

    @classmethod
    def from_view(cls, view: DesktopReadView) -> "DesktopReadResponse":
        answer = view.answer
        return cls(
            task_id=view.task_id,
            task_status=view.task_status,
            task_revision=view.task_revision,
            objective=view.objective,
            phase=view.phase,
            card=None if view.card is None else DesktopCardResponse.from_view(view.card),
            disclosure=None
            if view.disclosure is None
            else DesktopDisclosureResponse(
                disclosure_id=view.disclosure.disclosure_id,
                status=view.disclosure.status,
                error_code=view.disclosure.error_code,
                started_at=view.disclosure.started_at,
                finished_at=view.disclosure.finished_at,
                node_count=view.disclosure.node_count,
                text_bytes=view.disclosure.text_bytes,
                redaction_count=view.disclosure.redaction_count,
                truncated=view.disclosure.truncated,
            ),
            answer=None
            if answer is None
            else DesktopAnswerResponse(
                kind=answer.kind,
                answer=answer.answer,
                reason=answer.reason,
                evidence=[DesktopEvidenceResponse(**item) for item in answer.evidence],
                recipient=answer.recipient,
                model=answer.model,
                observed_at=view.card.observed_at if view.card is not None else None,
                created_at=answer.created_at,
            ),
        )


class LatestDesktopReadResponse(BaseModel):
    read: DesktopReadResponse | None


class ProjectedNodeResponse(BaseModel):
    """One projected node: exactly the fixed, reviewed field set. Nothing else can be added."""

    model_config = ConfigDict(extra="forbid")

    control_ref: str = Field(pattern=CONTROL_REF_PATTERN.pattern)
    parent_ref: str | None = Field(default=None, pattern=CONTROL_REF_PATTERN.pattern)
    role: str
    name: str | None = None
    text: str | None = None
    enabled: bool
    visible: bool
    focused: bool
    selected: bool | None = None
    checked: Literal["on", "off", "mixed"] | None = None
    expanded: bool | None = None


class ProviderProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    classification: Literal["desktop_private"]
    trust: Literal["untrusted_environment"]
    observed_at: str
    truncated: bool
    truncation: list[str]
    node_count: int
    nodes: list[ProjectedNodeResponse]


class ProviderContextResponse(BaseModel):
    """Released once, after the claim committed. The approved question plus the redacted projection."""

    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    objective: str
    recipient: Recipient
    model: str
    projection: ProviderProjectionResponse

    @classmethod
    def from_context(cls, context: ProviderContext) -> "ProviderContextResponse":
        return cls(
            disclosure_id=context.disclosure_id,
            task_id=context.task_id,
            objective=context.objective,
            recipient=context.recipient,
            model=context.model,
            projection=ProviderProjectionResponse.model_validate(context.projection),
        )
