"""Request and response models for Milestone 9 S5 scoped desktop visual fallback.

Read the response models as the list of everything that may leave the runtime about a capture or a
vision disclosure, and note what is missing and stays missing: **no window handle, process id or
path, no raw pixel, no coordinate, no click, no action**. The capture card shows the window's display
strings (untrusted text, rendered inertly) and the deterministic reason UIA was insufficient; the
disclosure card additionally names the provider, model and the person's own typed purpose. What a
claimed capture or disclosure releases to Electron main (`RawCaptureResponse`) carries the image
exactly once, for exactly the one local or provider use it was approved for.

Request bodies come from Electron main. The renderer supplies a surface identity, an objective/purpose
and (for a disclosure) the provider/model main chose from its own configuration -- never a renderer
field anywhere on the IPC surface.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.authenticated import Recipient
from app.domain.desktop_vision import MAX_PURPOSE_CHARS
from app.services.desktop_vision import (
    DesktopVisionView,
    DisclosureProviderContext,
    RawCapture,
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


MAX_OBJECTIVE_CHARS = 500


class CreateDesktopCaptureBody(_Body):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)
    worker_generation: uuid.UUID
    surface_ref: str = Field(pattern=r"^s(?:[1-9]|1[0-6])$")
    surface_epoch: int = Field(ge=1)
    #: Optional: what the caller was looking for. Used only by the deterministic
    #: `uia_truncated_without_target` check; never shown to any provider.
    target_hint: str | None = Field(default=None, max_length=200)


class DesktopVisionGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class DesktopVisionRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)


class CreateDesktopDisclosureBody(_Body):
    recipient: Recipient
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=MAX_PURPOSE_CHARS)


class RecordDesktopCandidatesBody(_Body):
    """The ONE provider attempt's outcome: a closed candidate list, or a closed failure code."""

    disclosure_id: uuid.UUID
    result: dict[str, Any] | None = None
    failure: Literal["model_unavailable", "invalid_output"] | None = None


# ---- responses --------------------------------------------------------------------------------


class CaptureCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    application_label: str
    window_title: str
    fallback_reason: str


class CaptureStateResponse(BaseModel):
    capture_id: uuid.UUID
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    width: int | None
    height: int | None
    dpi: int | None


class DisclosureCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    application_label: str
    window_title: str
    provider: Recipient
    model: str
    purpose: str


class VisionCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["candidate"]
    label: str
    region: dict[str, float]
    confidence: float
    observed_text: str | None = None


class DisclosureStateResponse(BaseModel):
    disclosure_id: uuid.UUID
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    candidate_count: int | None


class DesktopVisionResponse(BaseModel):
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: str
    capture_card: CaptureCardResponse | None
    capture: CaptureStateResponse | None
    disclosure_card: DisclosureCardResponse | None
    disclosure: DisclosureStateResponse | None
    candidates: list[VisionCandidateResponse] | None

    @classmethod
    def from_view(cls, view: DesktopVisionView) -> "DesktopVisionResponse":
        return cls(
            task_id=view.task_id,
            task_status=view.task_status,
            task_revision=view.task_revision,
            objective=view.objective,
            phase=view.phase,
            capture_card=None
            if view.capture_card is None
            else CaptureCardResponse(
                grant_id=view.capture_card.grant_id,
                grant_revision=view.capture_card.grant_revision,
                grant_status=view.capture_card.grant_status,
                expires_at=view.capture_card.expires_at,
                application_label=view.capture_card.application_label,
                window_title=view.capture_card.window_title,
                fallback_reason=view.capture_card.fallback_reason,
            ),
            capture=None
            if view.capture is None
            else CaptureStateResponse(
                capture_id=view.capture.capture_id,
                status=view.capture.status,
                error_code=view.capture.error_code,
                started_at=view.capture.started_at,
                finished_at=view.capture.finished_at,
                width=view.capture.width,
                height=view.capture.height,
                dpi=view.capture.dpi,
            ),
            disclosure_card=None
            if view.disclosure_card is None
            else DisclosureCardResponse(
                grant_id=view.disclosure_card.grant_id,
                grant_revision=view.disclosure_card.grant_revision,
                grant_status=view.disclosure_card.grant_status,
                expires_at=view.disclosure_card.expires_at,
                application_label=view.disclosure_card.application_label,
                window_title=view.disclosure_card.window_title,
                provider=view.disclosure_card.provider,
                model=view.disclosure_card.model,
                purpose=view.disclosure_card.purpose,
            ),
            disclosure=None
            if view.disclosure is None
            else DisclosureStateResponse(
                disclosure_id=view.disclosure.disclosure_id,
                status=view.disclosure.status,
                error_code=view.disclosure.error_code,
                started_at=view.disclosure.started_at,
                finished_at=view.disclosure.finished_at,
                candidate_count=view.disclosure.candidate_count,
            ),
            candidates=None
            if view.candidates is None
            else [VisionCandidateResponse.model_validate(item) for item in view.candidates],
        )


class RawCaptureResponse(BaseModel):
    """Released exactly once, after a claim committed: the image itself, for the one local or
    provider use it was approved for. Never persisted anywhere as a whole."""

    capture_id: uuid.UUID
    image_base64: str
    width: int
    height: int
    dpi: int

    @classmethod
    def from_capture(cls, capture: RawCapture) -> "RawCaptureResponse":
        return cls(
            capture_id=capture.capture_id, image_base64=capture.image_base64,
            width=capture.width, height=capture.height, dpi=capture.dpi,
        )


class DisclosureProviderContextResponse(BaseModel):
    """Released once, after a disclosure claim committed: the purpose, recipient, model and the ONE
    freshly-captured image for exactly one provider attempt."""

    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    purpose: str
    recipient: Recipient
    model: str
    capture: RawCaptureResponse

    @classmethod
    def from_context(cls, context: DisclosureProviderContext) -> "DisclosureProviderContextResponse":
        return cls(
            disclosure_id=context.disclosure_id,
            task_id=context.task_id,
            purpose=context.purpose,
            recipient=context.recipient,
            model=context.model,
            capture=RawCaptureResponse.from_capture(context.capture),
        )
