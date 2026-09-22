"""Milestone 9 S5: the scoped desktop visual fallback.

Two separate authorities, kept apart on purpose, the way S2/S4 already keep disclosure and execution
apart:

```text
a deterministic, LOCAL classification of one already-persisted S1 observation
   -> "UIA cannot answer this" (uia_empty / uia_missing_required_semantics / uia_truncated_without_target)
   -> trusted CAPTURE card: one surface, one task, short lifetime -- no provider named, none contacted
   -> trusted click: CaptureGrantScope grant (task_grants, kind=desktop_vision_capture)
   -> claim: ONE fresh worker capture, for LOCAL use only (on-device display, local OCR)
   -> if that still is not enough, a SEPARATE trusted DISCLOSURE card: application/window, one image,
      one provider, one model, one purpose
   -> trusted click: DiscloseGrantScope grant (task_grants, kind=desktop_vision_disclose)
   -> claim: a SECOND, FRESH worker capture (never the first capture's own bytes) goes to Electron main
      for exactly ONE provider attempt; no failover, no retry, no second image
   -> the provider returns evidence/candidates only -- never an action, a click or a coordinate
```

The model never chooses to fall back to pixels: `classify_fallback_eligibility` is pure, deterministic
code over an already-persisted observation, and nothing calls the capture or disclosure card into
existence without one of its three closed reasons.

A vision result is `VisionResult`: a bounded list of `VisionCandidate` evidence, each a label, a
confidence and a region normalised to the captured crop. `extra="forbid"` at every level means there is
nowhere in this shape to put a click, a coordinate, an action, an approval or a provider -- a visual
candidate can never become an executable target by itself. Turning a candidate back into something
that can run is not implemented anywhere in this codebase; if a person needs to act on what a screen
shows, semantic UIA has to find that control on its own (S1-S4), or the task reports
`manual_handoff_required`.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.desktop.protocol import DesktopObservation, DesktopRole
from app.domain.authenticated import Recipient
from app.domain.desktop_disclosure import DesktopDisclosureRefusal, DisplayTarget, validate_objective
from app.domain.digest import canonical_json


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

DESKTOP_VISION_TASK_TYPE: Final = "desktop_vision"
DESKTOP_VISION_CAPTURE_KIND: Final = "desktop_vision_capture"
DESKTOP_VISION_DISCLOSE_KIND: Final = "desktop_vision_disclose"
CAPTURE_POLICY_VERSION: Final = "desktop-vision-capture-v1"
DISCLOSE_POLICY_VERSION: Final = "desktop-vision-disclose-v1"
DESKTOP_PRIVATE: Final = "desktop_private"

#: A capture/disclosure approval is short-lived, and single-use regardless of expiry.
DEFAULT_GRANT_TTL_SECONDS: Final = 5 * 60
#: A STARTED capture/disclosure whose runtime never heard back is not left claiming to be in flight.
STALE_CLAIM_SECONDS: Final = 5 * 60
#: The S1 observation a fallback reason is classified from must itself be fresh: the brief's own
#: eligibility test is about the semantic tree right now, not a stale read from minutes ago.
MAX_CLASSIFYING_OBSERVATION_AGE_SECONDS: Final = 2 * 60

MAX_PURPOSE_CHARS: Final = 400
MAX_CANDIDATES: Final = 8
MAX_LABEL_CHARS: Final = 120
MAX_OBSERVED_TEXT_CHARS: Final = 200
MAX_MODEL_NAME: Final = 64

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class DesktopVisionRefusal(ValueError):
    """A refused desktop-vision input or state. `code` is stable and text-free."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The desktop visual fallback was refused ({code}).")
        self.code = code


#: Refusals that are *state* (the world moved on since the card), not a malformed request.
STATE_CODES: Final = frozenset(
    {
        "grant_not_found",
        "grant_not_pending",
        "grant_not_active",
        "grant_expired",
        "grant_changed",
        "capture_not_found",
        "capture_not_succeeded",
        "capture_stale",
        "disclosure_not_started",
        "disclosure_already_recorded",
        "observation_stale",
        "observation_unavailable",
    }
)


# ---- the deterministic fallback trigger -----------------------------------------------------


class FallbackReason(StrEnum):
    """The closed, deterministic reasons visual fallback may even be offered. A model never picks one:
    this is pure code over an already-persisted S1 observation, checked before any capture card exists."""

    UIA_EMPTY = "uia_empty"
    UIA_MISSING_REQUIRED_SEMANTICS = "uia_missing_required_semantics"
    UIA_TRUNCATED_WITHOUT_TARGET = "uia_truncated_without_target"


#: A tree this shallow, almost entirely roles UIA could not classify, is not a usable semantic surface
#: even though it is not literally empty (a custom-drawn canvas app is the common real case).
_MIN_RECOGNISABLE_NODES: Final = 4
_UNKNOWN_FRACTION_THRESHOLD: Final = 0.8


def classify_fallback_eligibility(
    observation: DesktopObservation, *, target_hint: str | None = None
) -> FallbackReason | None:
    """`None` when UIA answered well enough that no screenshot is warranted.

    * `uia_empty`: the observation has no nodes at all.
    * `uia_missing_required_semantics`: nodes exist, but almost none of them have a role UIA could
      classify, so the tree carries essentially no usable structure (a custom-drawn control surface).
    * `uia_truncated_without_target`: the observation was cut short (nodes/depth/text/scan) and a
      caller-supplied hint of what it was looking for does not appear, by plain substring match, in any
      projected node's name or text -- so truncation plausibly hid the very thing that was needed.

    This is deliberately narrow and heuristic where it must be (matching S1's own credential-name and
    S2's title-withholding heuristics): a false negative here means "no fallback offered," which is the
    safe direction to be wrong in.
    """
    if observation.node_count == 0:
        return FallbackReason.UIA_EMPTY
    unknown = sum(1 for node in observation.nodes if node.role is DesktopRole.UNKNOWN)
    if observation.node_count < _MIN_RECOGNISABLE_NODES or unknown / observation.node_count >= _UNKNOWN_FRACTION_THRESHOLD:
        return FallbackReason.UIA_MISSING_REQUIRED_SEMANTICS
    if observation.truncated and target_hint:
        hint = target_hint.strip().casefold()
        found = any(
            hint in (node.name or "").casefold() or hint in (node.text or "").casefold()
            for node in observation.nodes
        )
        if not found:
            return FallbackReason.UIA_TRUNCATED_WITHOUT_TARGET
    return None


# ---- the capture grant scope ------------------------------------------------------------------


class CaptureGrantScope(_Frozen):
    """Exactly what one capture grant authorises: ONE scoped screenshot of ONE already-approved
    window, for LOCAL use only. No provider is named because none is ever contacted for this kind."""

    schema_version: Literal[1] = 1
    kind: Literal["desktop_vision_capture"] = DESKTOP_VISION_CAPTURE_KIND
    policy_version: Literal["desktop-vision-capture-v1"] = CAPTURE_POLICY_VERSION
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
    #: The observation whose deterministic classification justified offering this card at all.
    classifying_observation_id: uuid.UUID
    fallback_reason: FallbackReason
    classification: Literal["desktop_private"] = DESKTOP_PRIVATE
    #: Exactly one capture for this approval, ever.
    max_capture_calls: Literal[1] = 1
    display: DisplayTarget

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


# ---- the vision-disclosure grant scope ---------------------------------------------------------


class DiscloseGrantScope(_Frozen):
    """Exactly what one vision-disclosure grant authorises: ONE freshly-recaptured image of the SAME
    window a prior local capture already covered, sent to ONE named provider and model, for ONE stated
    purpose. Text disclosure authority (S2/S4) never reaches this: it is its own grant kind."""

    schema_version: Literal[1] = 1
    kind: Literal["desktop_vision_disclose"] = DESKTOP_VISION_DISCLOSE_KIND
    policy_version: Literal["desktop-vision-disclose-v1"] = DISCLOSE_POLICY_VERSION
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
    #: The prior, SUCCEEDED local capture that established fallback eligibility. Audit link only: this
    #: grant's own claim performs a brand-new capture and never reuses that capture's bytes.
    capture_id: uuid.UUID
    recipient: Recipient
    model: str = Field(pattern=_MODEL.pattern)
    purpose: str = Field(max_length=MAX_PURPOSE_CHARS)
    classification: Literal["desktop_private"] = DESKTOP_PRIVATE
    #: Exactly one provider attempt for this approval, and none after a failed or lost one.
    max_provider_calls: Literal[1] = 1
    failover: Literal["none"] = "none"
    display: DisplayTarget

    @field_validator("purpose")
    @classmethod
    def _plain_purpose(cls, value: str) -> str:
        return validate_objective(value)

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


def validate_purpose(value: object) -> str:
    try:
        return validate_objective(value)
    except DesktopDisclosureRefusal:
        raise DesktopVisionRefusal("purpose_invalid") from None


def validate_provider_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise DesktopVisionRefusal("model_invalid")
    return value


# ---- the closed vision result: evidence only, never authority ----------------------------------


class VisionRegion(_Frozen):
    """A region normalised to the CAPTURED CROP (never the screen, never a window): `0 <= x, y <= 1`
    and the region stays entirely inside the crop. There is no unit here that could be confused with a
    physical pixel or a screen coordinate."""

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _within_crop(self) -> "VisionRegion":
        if self.x + self.w > 1.0 + 1e-6 or self.y + self.h > 1.0 + 1e-6:
            raise ValueError("a region must stay inside the captured crop")
        return self


class VisionCandidate(_Frozen):
    """One piece of visual evidence. Never authority: there is no field here for a click, a key, a
    coordinate, an action or an approval, and `extra='forbid'` refuses a reply that invents one."""

    schema_version: Literal[1] = 1
    kind: Literal["candidate"] = "candidate"
    label: str = Field(min_length=1, max_length=MAX_LABEL_CHARS)
    region: VisionRegion
    confidence: float = Field(ge=0.0, le=1.0)
    observed_text: str | None = Field(default=None, max_length=MAX_OBSERVED_TEXT_CHARS)


class VisionResult(_Frozen):
    schema_version: Literal[1] = 1
    candidates: list[VisionCandidate] = Field(max_length=MAX_CANDIDATES)


def parse_vision_result(payload: object) -> VisionResult:
    """Strictly parse one provider result. Unknown keys refuse the whole result: there is nowhere in
    this shape to put an operation, a click, a coordinate or an approval, so a reply that tries is
    refused, not stripped and not trusted."""
    if not isinstance(payload, dict):
        raise DesktopVisionRefusal("result_malformed")
    try:
        return VisionResult.model_validate(payload)
    except ValidationError:
        raise DesktopVisionRefusal("result_malformed") from None


def observation_age_seconds(observed_at: datetime, *, now: datetime | None = None) -> float:
    reference = now or datetime.now(UTC)
    return (reference - observed_at).total_seconds()
