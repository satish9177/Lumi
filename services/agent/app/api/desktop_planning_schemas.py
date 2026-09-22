"""Request and response models for Milestone 9 S4 desktop action planning.

Mirrors `app.api.desktop_disclosure_schemas` closely. What is missing and stays missing: no window
handle, process id or path, no coordinate, no snapshot, no snapshot digest, no raw candidate value (the
card shows the person's own values back to them; the provider is shown only `ValueDescriptorResponse`).

Request bodies come from Electron main. The renderer supplies a surface identity, a typed objective and
the person's own candidate values, and nothing else: the provider and model are chosen by main from its
own configuration, exactly as in S2.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.desktop.protocol import CONTROL_REF_PATTERN
from app.domain.authenticated import Recipient
from app.domain.desktop_planning import (
    MAX_OBJECTIVE_CHARS,
    MAX_VALUE_CHARS,
    MAX_VALUE_CLASSIFICATION_CHARS,
    MAX_VALUES,
    VALUE_REF_PATTERN,
)
from app.services.desktop_planning import DesktopPlanView, PlanCardView, PlanProviderContext


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanValueBody(_Body):
    """One candidate value the person typed directly in the trusted panel."""

    classification: str = Field(min_length=1, max_length=MAX_VALUE_CLASSIFICATION_CHARS)
    value: str = Field(max_length=MAX_VALUE_CHARS)


class CreateDesktopPlanBody(_Body):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    recipient: Recipient
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
    values: list[PlanValueBody] = Field(default_factory=list, max_length=MAX_VALUES)


class DesktopPlanGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class DesktopPlanRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Literal["user_declined", "user_stopped"] = "user_declined"


class RecordDesktopPlanResultBody(_Body):
    """The ONE provider attempt's outcome: a parsed action proposal, or a closed failure code."""

    plan_id: uuid.UUID
    result: dict[str, Any] | None = None
    failure: Literal["model_unavailable", "invalid_output"] | None = None


class ValueDescriptorResponse(BaseModel):
    value_ref: str = Field(pattern=VALUE_REF_PATTERN.pattern)
    classification: str
    length: int


class PlanValueResponse(ValueDescriptorResponse):
    """Card-only: the person's own value, shown back to them. Never sent to a provider raw."""

    value: str


class DesktopPlanCardResponse(BaseModel):
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
    values: list[PlanValueResponse]

    @classmethod
    def from_view(cls, card: PlanCardView) -> "DesktopPlanCardResponse":
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
            values=[
                PlanValueResponse(value_ref=item.value_ref, classification=item.classification, length=item.length, value=item.raw)
                for item in card.values
            ],
        )


class ProposedActionResponse(BaseModel):
    """The ONE closed action a provider proposed. Opaque refs only -- never a raw value."""

    action: Literal["invoke", "set_value", "select"]
    control_ref: str | None = Field(default=None, pattern=CONTROL_REF_PATTERN.pattern)
    value_ref: str | None = Field(default=None, pattern=VALUE_REF_PATTERN.pattern)
    container_ref: str | None = Field(default=None, pattern=CONTROL_REF_PATTERN.pattern)
    option_ref: str | None = Field(default=None, pattern=CONTROL_REF_PATTERN.pattern)


class DesktopPlanStateResponse(BaseModel):
    plan_id: uuid.UUID
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool
    proposed_action: ProposedActionResponse | None


class DesktopPlanResponse(BaseModel):
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: Literal[
        "awaiting_approval", "approved", "reasoning", "proposed", "failed", "outcome_unknown", "declined", "expired"
    ]
    card: DesktopPlanCardResponse | None
    plan: DesktopPlanStateResponse | None
    action_id: uuid.UUID | None

    @classmethod
    def from_view(cls, view: DesktopPlanView) -> "DesktopPlanResponse":
        plan = view.plan
        return cls(
            task_id=view.task_id,
            task_status=view.task_status,
            task_revision=view.task_revision,
            objective=view.objective,
            phase=view.phase,
            card=None if view.card is None else DesktopPlanCardResponse.from_view(view.card),
            plan=None
            if plan is None
            else DesktopPlanStateResponse(
                plan_id=plan.plan_id,
                status=plan.status,
                error_code=plan.error_code,
                started_at=plan.started_at,
                finished_at=plan.finished_at,
                node_count=plan.node_count,
                text_bytes=plan.text_bytes,
                redaction_count=plan.redaction_count,
                truncated=plan.truncated,
                proposed_action=ProposedActionResponse.model_validate(plan.proposed_action)
                if plan.proposed_action is not None
                else None,
            ),
            action_id=view.action_id,
        )


class LatestDesktopPlanResponse(BaseModel):
    plan: DesktopPlanResponse | None


class ProjectedPlanNodeResponse(BaseModel):
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


class PlanProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    classification: Literal["desktop_private"]
    trust: Literal["untrusted_environment"]
    observed_at: str
    truncated: bool
    truncation: list[str]
    node_count: int
    nodes: list[ProjectedPlanNodeResponse]


class PlanProviderContextResponse(BaseModel):
    """Released once, after the claim committed. The approved objective, the redacted projection and
    the candidate value DESCRIPTORS only (never the raw text)."""

    plan_id: uuid.UUID
    task_id: uuid.UUID
    objective: str
    recipient: Recipient
    model: str
    projection: PlanProjectionResponse
    values: list[ValueDescriptorResponse]

    @classmethod
    def from_context(cls, context: PlanProviderContext) -> "PlanProviderContextResponse":
        return cls(
            plan_id=context.plan_id,
            task_id=context.task_id,
            objective=context.objective,
            recipient=context.recipient,
            model=context.model,
            projection=PlanProjectionResponse.model_validate(context.projection),
            values=[
                ValueDescriptorResponse(value_ref=item.value_ref, classification=item.classification, length=item.length)
                for item in context.value_descriptors
            ],
        )
