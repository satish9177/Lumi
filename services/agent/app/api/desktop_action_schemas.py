"""Request and response models for Milestone 9 S3 desktop actions (focus, scroll, registered launch).

The response models are the list of everything that may leave the runtime about a desktop action. Missing
and staying missing: no window handle, process id or path, no executable path, no argument, no
AutomationId, class name or RuntimeId, no coordinate, no snapshot and no snapshot digest.

Request bodies come from Electron main. The renderer picks an opaque choice (a surface it was listed, a
control of a fresh observation, a registered application id) and a closed scroll step. Nothing else.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.desktop.protocol import APP_ID_PATTERN, CONTROL_REF_PATTERN, SURFACE_REF_PATTERN, ScrollStep
from app.domain.desktop_actions import InvokeProposal, LaunchProposal, ScrollProposal, SelectProposal, SetValueProposal
from app.services.desktop_actions import DesktopActionView


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposeFocusBody(_Body):
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=SURFACE_REF_PATTERN.pattern)
    surface_epoch: int = Field(ge=1)


class ProposeScrollBody(_Body):
    worker_generation: uuid.UUID
    observation_id: uuid.UUID
    control_ref: str = Field(pattern=CONTROL_REF_PATTERN.pattern)
    step: ScrollStep


class ScrollTargetsBody(_Body):
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=SURFACE_REF_PATTERN.pattern)
    surface_epoch: int = Field(ge=1)


class ScrollTargetResponse(BaseModel):
    control_ref: str
    role: str
    #: Untrusted display text.
    name: str


class ScrollTargetsResponse(BaseModel):
    observation_id: uuid.UUID
    targets: list[ScrollTargetResponse]


class ProposeLaunchBody(_Body):
    app_id: str = Field(pattern=APP_ID_PATTERN.pattern)


class DesktopActionDecisionBody(_Body):
    expected_revision: int = Field(ge=1)


class DesktopActionReconcileBody(_Body):
    """S4. The person's own report of what they observed after an unresolved mutation, and nothing
    else: no operation, target, value or provider is namable here."""

    expected_revision: int = Field(ge=1)
    outcome: Literal["succeeded", "failed", "still_unknown"]


class ProposeFromPlanBody(_Body):
    plan_id: uuid.UUID


class DesktopActionResultResponse(BaseModel):
    """The whitelist of result fields the trusted UI may show. Nothing else is ever copied across."""

    outcome: str | None = None
    percent_before: float | None = None
    percent_after: float | None = None
    human_input_during: bool | None = None
    observation_invalidated: bool | None = None
    follow_up_observation_id: str | None = None
    surface_ref: str | None = None
    surface_epoch: int | None = None
    focused: bool | None = None


class DesktopActionResponse(BaseModel):
    action_id: uuid.UUID
    task_id: uuid.UUID
    revision: int
    status: str
    operation: Literal[
        "focus_surface", "scroll_control", "launch_app", "set_control_value", "select_control", "invoke_control"
    ]
    worker_generation: uuid.UUID
    #: Untrusted application strings, shown inert on the trusted card.
    application_label: str
    window_title: str | None
    control_role: str | None
    control_name: str | None
    step: str | None
    app_id: str | None
    #: S4 `set_control_value` only: the exact trusted text that will be written. The person's own
    #: input, shown back to them verbatim; never returned by a generic (non-desktop) action read.
    value: str | None
    container_role: str | None
    container_name: str | None
    option_role: str | None
    option_name: str | None
    effect: str | None
    expires_at: datetime | None
    attempt_outcome: str | None
    error_code: str | None
    result: DesktopActionResultResponse | None

    @classmethod
    def from_view(cls, view: DesktopActionView) -> "DesktopActionResponse":
        proposal = view.proposal
        raw = view.result or {}
        return cls(
            action_id=view.action_id,
            task_id=view.task_id,
            revision=view.revision,
            status=view.status.value,
            operation=view.operation.value,
            worker_generation=proposal.worker_generation,
            application_label=proposal.application_label,
            window_title=None if isinstance(proposal, LaunchProposal) else proposal.window_title,
            control_role=proposal.control_role
            if isinstance(proposal, ScrollProposal | SetValueProposal | InvokeProposal)
            else None,
            control_name=proposal.control_name
            if isinstance(proposal, ScrollProposal | SetValueProposal | InvokeProposal)
            else None,
            step=proposal.step.value if isinstance(proposal, ScrollProposal) else None,
            app_id=proposal.app_id if isinstance(proposal, LaunchProposal) else None,
            value=proposal.value if isinstance(proposal, SetValueProposal) else None,
            container_role=proposal.container_role if isinstance(proposal, SelectProposal) else None,
            container_name=proposal.container_name if isinstance(proposal, SelectProposal) else None,
            option_role=proposal.option_role if isinstance(proposal, SelectProposal) else None,
            option_name=proposal.option_name if isinstance(proposal, SelectProposal) else None,
            effect=proposal.effect.value if isinstance(proposal, InvokeProposal) else None,
            expires_at=view.approval_expires_at,
            attempt_outcome=view.attempt_outcome.value if view.attempt_outcome is not None else None,
            error_code=view.error_code,
            result=DesktopActionResultResponse.model_validate(
                {key: value for key, value in raw.items() if key in DesktopActionResultResponse.model_fields}
            )
            if view.result is not None
            else None,
        )


class LatestDesktopActionResponse(BaseModel):
    action: DesktopActionResponse | None


class RegisteredAppResponse(BaseModel):
    app_id: str
    label: str


class RegisteredAppsResponse(BaseModel):
    apps: list[RegisteredAppResponse]


