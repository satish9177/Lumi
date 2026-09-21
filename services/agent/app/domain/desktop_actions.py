"""Milestone 9 S3: the closed set of desktop effects, their proposals, and how their failures are classified.

Model proposes; the controller validates and authorizes; the worker executes ONE bounded effect. This
module is the controller's vocabulary: three operations, each a strict pydantic model that the runtime
builds itself from live facts and re-parses from the persisted action before executing, so the values that
run are the values that were approved.

What a proposal never contains: a window handle, process id or path, an executable path, an argument, a
coordinate, a selector, a key, or a typed value. A model does not build these at all in S3; the user picks
a surface, a scrollable control or a registered application in the trusted panel.
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
    MAX_TEXT_PER_NODE,
    MAX_TITLE,
    SURFACE_REF_PATTERN,
    ScrollStep,
)
from app.domain.action_status import AttemptOutcome, RiskTier

DESKTOP_ACTION_TASK_TYPE: Final = "desktop_action"
TOOL_PREFIX: Final = "DESKTOP_"

#: A desktop action must be approved within this window; the ledger's approval expiry is the authority.
#: The observation a scroll is proposed against must also be this fresh when the approval is claimed.
ACTION_OBSERVATION_MAX_AGE_SECONDS: Final = 60


class DesktopOperation(StrEnum):
    FOCUS = "focus_surface"
    SCROLL = "scroll_control"
    LAUNCH = "launch_app"


TOOL_FOR: Final[dict[DesktopOperation, str]] = {
    DesktopOperation.FOCUS: "DESKTOP_FOCUS",
    DesktopOperation.SCROLL: "DESKTOP_SCROLL",
    DesktopOperation.LAUNCH: "DESKTOP_LAUNCH",
}
OPERATION_FOR_TOOL: Final[dict[str, DesktopOperation]] = {tool: op for op, tool in TOOL_FOR.items()}
#: Focus and scroll change what is in front of the user or what part of a list they see; launching an
#: application starts a process. None of them is read-only, so none is R0.
RISK_FOR: Final[dict[DesktopOperation, RiskTier]] = {
    DesktopOperation.FOCUS: RiskTier.R1,
    DesktopOperation.SCROLL: RiskTier.R1,
    DesktopOperation.LAUNCH: RiskTier.R2,
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


DesktopProposal = FocusProposal | ScrollProposal | LaunchProposal
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
