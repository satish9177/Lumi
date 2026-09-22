"""Milestone 9 S3/S4: the closed set of desktop effects, their proposals, and how their failures are
classified.

Model proposes; the controller validates and authorizes; the worker executes ONE bounded effect. This
module is the controller's vocabulary: six operations, each a strict pydantic model that the runtime
builds itself from live facts and re-parses from the persisted action before executing, so the values that
run are the values that were approved.

What a proposal never contains: a window handle, process id or path, an executable path, an argument, a
coordinate, a selector, a key, a risk classification or a provider. S3's three proposals are built by the
runtime itself from the trusted panel's opaque choices; a model never sees them. S4's three mutation
proposals (`set_control_value`, `select_control`, `invoke_control`) are built by the runtime from a
validated, closed action a model proposed (`app.domain.desktop_planning`) -- the model still never
supplies a raw value, a risk tier or an approval; it names an opaque control (and, for `set_control_value`,
an opaque local value ref the runtime alone resolves to trusted text). The value that ends up in a
`SetValueProposal` below is the resolved trusted text, exactly as `window_title` and `control_name`
already carry untrusted-but-necessary display text for S3's cards: it is shown on the exact trusted
approval card, never logged, and reaches the worker only inside one authenticated RPC.
"""

import uuid
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.desktop.errors import DesktopReason
from app.desktop.protocol import (
    APP_ID_PATTERN,
    CONTROL_REF_PATTERN,
    MAX_APPLICATION_LABEL,
    MAX_SET_VALUE_LENGTH,
    MAX_TEXT_PER_NODE,
    MAX_TITLE,
    SURFACE_REF_PATTERN,
    InvokeEffect,
    ScrollStep,
)
from app.domain.action_status import AttemptOutcome, RiskTier

DESKTOP_ACTION_TASK_TYPE: Final = "desktop_action"
TOOL_PREFIX: Final = "DESKTOP_"

#: A desktop action must be approved within this window; the ledger's approval expiry is the authority.
#: The observation a scroll or an S4 mutation is proposed against must also be this fresh when the
#: approval is claimed.
ACTION_OBSERVATION_MAX_AGE_SECONDS: Final = 60


class DesktopOperation(StrEnum):
    FOCUS = "focus_surface"
    SCROLL = "scroll_control"
    LAUNCH = "launch_app"
    SET_VALUE = "set_control_value"
    SELECT = "select_control"
    INVOKE = "invoke_control"


#: The three operations only a validated model proposal (never the trusted panel directly) may open.
MODEL_PROPOSED_OPERATIONS: Final = frozenset(
    {DesktopOperation.SET_VALUE, DesktopOperation.SELECT, DesktopOperation.INVOKE}
)

TOOL_FOR: Final[dict[DesktopOperation, str]] = {
    DesktopOperation.FOCUS: "DESKTOP_FOCUS",
    DesktopOperation.SCROLL: "DESKTOP_SCROLL",
    DesktopOperation.LAUNCH: "DESKTOP_LAUNCH",
    DesktopOperation.SET_VALUE: "DESKTOP_SET_VALUE",
    DesktopOperation.SELECT: "DESKTOP_SELECT",
    DesktopOperation.INVOKE: "DESKTOP_INVOKE",
}
OPERATION_FOR_TOOL: Final[dict[str, DesktopOperation]] = {tool: op for op, tool in TOOL_FOR.items()}
#: Focus and scroll change what is in front of the user or what part of a list they see; launching an
#: application starts a process; the S4 mutations change application state directly. None of them is
#: read-only, so none is R0. The S4 mutations are R2, like launch: a typed value, a selection or an
#: invoked control can have consequences a focus change or a scroll cannot.
RISK_FOR: Final[dict[DesktopOperation, RiskTier]] = {
    DesktopOperation.FOCUS: RiskTier.R1,
    DesktopOperation.SCROLL: RiskTier.R1,
    DesktopOperation.LAUNCH: RiskTier.R2,
    DesktopOperation.SET_VALUE: RiskTier.R2,
    DesktopOperation.SELECT: RiskTier.R2,
    DesktopOperation.INVOKE: RiskTier.R2,
}


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_generation: uuid.UUID


class FocusProposal(_Proposal):
    operation: Literal[DesktopOperation.FOCUS] = DesktopOperation.FOCUS
    surface_ref: Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
    surface_epoch: int = Field(ge=1)
    #: Card text only: untrusted application strings, shown inert.
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)


class ScrollProposal(_Proposal):
    operation: Literal[DesktopOperation.SCROLL] = DesktopOperation.SCROLL
    surface_ref: Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    control_ref: Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
    step: ScrollStep
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)
    control_role: str = Field(max_length=32)
    control_name: str = Field(max_length=MAX_TEXT_PER_NODE)


class LaunchProposal(_Proposal):
    operation: Literal[DesktopOperation.LAUNCH] = DesktopOperation.LAUNCH
    app_id: Annotated[str, Field(pattern=APP_ID_PATTERN.pattern)]
    #: From the trusted registry, never from the caller.
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)


class SetValueProposal(_Proposal):
    """S4. The runtime built this from a validated model proposal plus the plan's own trusted local
    value (never from the model, and never a password/OTP/read-only/sensitive-surface target --
    `DesktopPlanningService` and the worker both refuse those independently)."""

    operation: Literal[DesktopOperation.SET_VALUE] = DesktopOperation.SET_VALUE
    #: Which `desktop_action_plans` row this was built from. An opaque id only, used solely so a plan
    #: can never fund a second execution card (`DesktopActionRepository.action_for_plan`).
    plan_id: uuid.UUID
    surface_ref: Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    control_ref: Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
    #: The opaque local value ref this was resolved from (`v1`..`v10`). Safe to keep in the durable
    #: dispatch row as an identifier; the actual text below never is.
    value_ref: Annotated[str, Field(pattern=r"^v([1-9]|10)$")]
    #: The exact text that will be written. Shown on the trusted card verbatim; never logged.
    value: str = Field(max_length=MAX_SET_VALUE_LENGTH)
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)
    control_role: str = Field(max_length=32)
    control_name: str = Field(max_length=MAX_TEXT_PER_NODE)


class SelectProposal(_Proposal):
    operation: Literal[DesktopOperation.SELECT] = DesktopOperation.SELECT
    plan_id: uuid.UUID
    surface_ref: Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    container_ref: Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
    option_ref: Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)
    container_role: str = Field(max_length=32)
    container_name: str = Field(max_length=MAX_TEXT_PER_NODE)
    option_role: str = Field(max_length=32)
    option_name: str = Field(max_length=MAX_TEXT_PER_NODE)


class InvokeProposal(_Proposal):
    operation: Literal[DesktopOperation.INVOKE] = DesktopOperation.INVOKE
    plan_id: uuid.UUID
    surface_ref: Annotated[str, Field(pattern=SURFACE_REF_PATTERN.pattern)]
    surface_epoch: int = Field(ge=1)
    observation_id: uuid.UUID
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    control_ref: Annotated[str, Field(pattern=CONTROL_REF_PATTERN.pattern)]
    #: Always `NAME_TOGGLE` in this release: the one closed, reviewed, deterministically-verifiable
    #: effect. Derived by the controller, never supplied by the model.
    effect: InvokeEffect
    application_label: str = Field(max_length=MAX_APPLICATION_LABEL)
    window_title: str = Field(max_length=MAX_TITLE)
    control_role: str = Field(max_length=32)
    control_name: str = Field(max_length=MAX_TEXT_PER_NODE)


DesktopProposal = (
    FocusProposal | ScrollProposal | LaunchProposal | SetValueProposal | SelectProposal | InvokeProposal
)
_ADAPTER: Final[TypeAdapter[DesktopProposal]] = TypeAdapter(
    Annotated[DesktopProposal, Field(discriminator="operation")]
)


def parse_proposal(document: dict[str, object]) -> DesktopProposal:
    """Strict re-parse of a persisted proposal. Anything unexpected is an error, never a guess."""
    return _ADAPTER.validate_python(document)


def dump_proposal(proposal: DesktopProposal) -> dict[str, object]:
    return proposal.model_dump(mode="json")


# ---- classification ---------------------------------------------------------------------------------------

#: A refusal carrying one of these says nothing about whether the effect happened: the worker died or went
#: quiet, or it told us the effect may have begun. Everything else was raised BEFORE any effect could begin
#: (an identity, trust or takeover check), so it is a known failure.
UNKNOWN_CODES: Final = frozenset(
    {
        DesktopReason.WORKER_UNAVAILABLE,
        DesktopReason.OBSERVATION_TIMEOUT,
        DesktopReason.EFFECT_UNCERTAIN,
        # The worker turns any unexpected exception into this, and so does the client for a body it cannot parse.
        # From an EFFECT call that cannot be told apart from a failure after the effect, so it is never "known".
        DesktopReason.BACKEND_FAILED,
    }
)


def outcome_for_refusal(code: DesktopReason) -> AttemptOutcome:
    return AttemptOutcome.OUTCOME_UNKNOWN if code in UNKNOWN_CODES else AttemptOutcome.FAILED
