"""Milestone 8b S6: the network-frozen local form draft, as data.

**S6 fills an authenticated form in Lumi's own browser with the network frozen,
verifies the values are in the fields, and hands the browser over. It never
submits.** This module is the vocabulary that promise is built from; nothing here
touches a browser.

```text
FillFormInput     what the trusted runtime tells the worker to write (contains the
                  one raw value that ever leaves the database, in memory, on loopback)
FillResult        what the worker may say back: refs, counts, hashes, a stable code
DraftFieldRecord  what is *stored* about a written field: hashes, never a value
draft_digest      the identity a handover approval binds
HandoverProposal  the exact approval for lifting the freeze while the page is dirty
```

The raw value appears in exactly one type, `FillFieldInput.text_value`, and that
type is never persisted, logged, echoed, returned or shown in a repr. Every
other type here is safe to store and to put in a diagnostic.
"""

import hashlib
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.domain.authenticated import AUTH_TAB_REF
from app.domain.authenticated_forms import ELEMENT_REF, FORM_REF, OPTION_REF
from app.domain.digest import canonical_json
from app.domain.form_prepare import MAX_FORM_FIELDS
from app.domain.protected_values import ProtectedKind

#: The worker operations. Neither is a planner operation and neither is reachable
#: from a model: the runtime builds every input from persisted, approved state.
FILL_OPERATION: Final = "authenticated_prepare_form"
HANDOVER_OPERATION: Final = "authenticated_form_handover"
#: The persisted tool identity of the second exact approval.
HANDOVER_TOOL: Final = "handover_form"
HANDOVER_KIND: Final = "form_draft_handover"
DRAFT_POLICY_VERSION: Final = "local-form-draft-v1"
#: The manifest policy of an S6-era disclosure. A `form-prepare-v1` manifest is a
#: historical S5 approval and is never executable.
EXECUTABLE_MANIFEST_POLICY: Final = "form-prepare-v2"

_DIGEST = r"^[0-9a-f]{64}$"
_CONTROL_TYPES = Literal[
    "text", "email", "tel", "number", "textarea", "select_single", "radiogroup", "checkbox"
]
_TEXT_LIKE: Final = frozenset({"text", "email", "tel", "number", "textarea"})
_CHOICE_LIKE: Final = frozenset({"select_single", "radiogroup"})

#: The user-facing product promise, in one place so a test can hold the UI to it.
PROMISE: Final = (
    "Lumi fills the form in its own browser with the network frozen, verifies the values are "
    "in the fields, and hands you the browser. Nothing was sent while Lumi was filling. If the "
    "form needs the network to accept a value, Lumi stops and tells you."
)


class DraftStatus(StrEnum):
    """A row in `form_drafts`. A closed set: there is no other state."""

    #: Every approved field was written and verified; the browser is still frozen.
    PREPARED = "PREPARED"
    #: Some fields were written and preparation did not complete (or the session
    #: is no longer known to be intact). The browser is still frozen and dirty.
    STALE = "STALE"
    #: The dirty page was destroyed while frozen. Nothing remains.
    DISCARDED = "DISCARDED"
    #: The user took the browser over; the network was restored on approval.
    HANDED_OVER = "HANDED_OVER"


LIVE_DRAFT_STATUSES: Final = frozenset({DraftStatus.PREPARED, DraftStatus.STALE})

#: Stable codes. Worker and runtime both use these and nothing else.
FILL_ERROR_CODES: Final = frozenset(
    {
        "not_frozen",
        "element_changed",
        "unsupported_under_freeze",
        "page_never_settles",
        "form_is_dirty",
        "draft_changed",
        "protected_value_changed",
        "account_changed",
        "account_identity_unknown",
        "login_required",
        "left_site_scope",
        "freeze_owned",
        "worker_busy",
        "preparation_mode_required",
        "stale_observation",
        "stale_document_epoch",
        "unsupported_control",
        "invalid_input",
        "freeze_failed",
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- hashes a verified field may be summarised by ------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_verified_hash(actual_value: str) -> str:
    """SHA-256(UTF-8(value read back from the field)). Equals the approved digest
    exactly when the field holds exactly the approved value."""
    return _sha(actual_value)


def choice_verified_hash(option_identity_hash: str) -> str:
    return _sha(f"selected:{option_identity_hash}")


def check_verified_hash(element_identity_hash: str, checked: bool) -> str:
    return _sha(f"checked:{element_identity_hash}:{'true' if checked else 'false'}")


# ---- what the worker is told to write ---------------------------------------------------


class FillFieldInput(_Frozen):
    """One approved field, as the worker writes it. Exactly one variant per control."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    element_identity_hash: str = Field(pattern=_DIGEST)
    control_type: _CONTROL_TYPES
    #: **The one raw protected value in this module.** In memory only: never
    #: persisted, logged, echoed or returned. `repr=False` keeps it out of any
    #: accidental `repr`/traceback of the model.
    text_value: str | None = Field(default=None, min_length=1, max_length=300, repr=False)
    #: The digest the user approved. The worker re-derives it from `text_value`
    #: and refuses a mismatch, so a runtime bug cannot write an unapproved value.
    value_digest: str | None = Field(default=None, pattern=_DIGEST)
    option_ref: str | None = Field(default=None, pattern=OPTION_REF)
    option_identity_hash: str | None = Field(default=None, pattern=_DIGEST)
    checked: StrictBool | None = None

    @model_validator(mode="after")
    def _one_variant(self) -> Self:
        text = (self.text_value, self.value_digest)
        choice = (self.option_ref, self.option_identity_hash)
        if self.control_type in _TEXT_LIKE:
            valid = all(item is not None for item in text) and all(item is None for item in choice)
            valid = valid and self.checked is None
        elif self.control_type in _CHOICE_LIKE:
            valid = all(item is not None for item in choice) and all(item is None for item in text)
            valid = valid and self.checked is None
        else:
            valid = self.checked is not None and all(item is None for item in (*text, *choice))
        if not valid:
            raise ValueError("a fill field carries exactly the variant its control needs")
        if self.text_value is not None and self.value_digest is not None:
            if _sha(self.text_value) != self.value_digest:
                raise ValueError("the value is not the one that was approved")
        return self


class FillFormInput(_Frozen):
    """The strict execution input of the one LOCAL_DRAFT operation.

    Built by the runtime from the persisted, approved manifest and the protected
    values, never from the planner proposal. There is no selector, XPath, DOM id,
    name, class, URL, origin, script, button or submit target in it, and no field
    in which to put one.
    """

    site: str = Field(min_length=1, max_length=253, pattern=r"^[a-z0-9.:-]+$")
    expected_account_fingerprint: str = Field(pattern=_DIGEST)
    observation_id: uuid.UUID
    tab: str = Field(pattern=AUTH_TAB_REF)
    document_epoch: int = Field(ge=1)
    form_epoch: int = Field(ge=1)
    form_ref: str = Field(pattern=FORM_REF)
    manifest_digest: str = Field(pattern=_DIGEST)
    fields: list[FillFieldInput] = Field(min_length=1, max_length=MAX_FORM_FIELDS)

    @model_validator(mode="after")
    def _unique_elements(self) -> Self:
        refs = [field.element_ref for field in self.fields]
        if len(set(refs)) != len(refs):
            raise ValueError("a form field is written at most once")
        return self


# ---- what the worker may say back ---------------------------------------------------------


class VerifiedField(_Frozen):
    element_ref: str = Field(pattern=ELEMENT_REF)
    verified_local_value_hash: str = Field(pattern=_DIGEST)


class FillResult(_Frozen):
    """Structured local verification metadata. **Never a value, a selector, HTML,
    an option value, a URL or a cookie**, and never page text: after the first
    protected value is written the page may reflect it anywhere."""

    draft_complete: bool
    fields_attempted: int = Field(ge=0, le=MAX_FORM_FIELDS)
    fields_verified: int = Field(ge=0, le=MAX_FORM_FIELDS)
    verified_fields: list[VerifiedField] = Field(default_factory=list, max_length=MAX_FORM_FIELDS)
    first_failed_element_ref: str | None = Field(default=None, pattern=ELEMENT_REF)
    error_code: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    #: True once any mutating primitive was invoked. The page is then dirty and the
    #: freeze stays on until a discard or an approved handover.
    dirty: bool = False
    #: Refused requests while frozen (a page's own autosave, beacon, redirect...).
    blocked_request_count: int = Field(default=0, ge=0)
    #: Broker resolutions and dials during the writes. Must both be zero.
    resolution_delta: int = Field(default=0, ge=0)
    dial_delta: int = Field(default=0, ge=0)
    guard_in_flight: int = Field(default=0, ge=0)
    broker_active_connections: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.fields_verified != len(self.verified_fields):
            raise ValueError("fields_verified counts verified_fields")
        if self.fields_verified > self.fields_attempted:
            raise ValueError("cannot verify more fields than were attempted")
        if self.draft_complete and (self.error_code is not None or not self.dirty):
            raise ValueError("a complete draft has no error and is dirty")
        if self.draft_complete and (
            self.fields_verified != self.fields_attempted or self.fields_verified < 1
        ):
            raise ValueError("a complete draft verified every field it attempted, and at least one")
        if not self.draft_complete and self.error_code is None:
            raise ValueError("an incomplete draft names why")
        return self


# ---- what is stored about a written field ---------------------------------------------------


class DraftFieldRecord(_Frozen):
    """Safe audit material for one written field: hashes and refs, never a value,
    selector or locator description."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    element_identity_hash: str = Field(pattern=_DIGEST)
    control_type: _CONTROL_TYPES
    data_ref: ProtectedKind | None = None
    #: The approved hash (from the manifest). Not a value.
    value_digest: str | None = Field(default=None, pattern=_DIGEST)
    option_ref: str | None = Field(default=None, pattern=OPTION_REF)
    option_identity_hash: str | None = Field(default=None, pattern=_DIGEST)
    checked: bool | None = None
    verified_local_value_hash: str = Field(pattern=_DIGEST)
    written_at: datetime


def draft_digest(
    *,
    task_id: uuid.UUID,
    profile_id: uuid.UUID,
    action_id: uuid.UUID,
    manifest_digest: str,
    dispatch_id: uuid.UUID,
    observation_id: uuid.UUID,
    tab: str,
    document_epoch: int,
    form_epoch: int,
    form_ref: str,
    status: DraftStatus,
    fields: list[DraftFieldRecord],
) -> str:
    """The identity a handover approval binds. No raw value is an input.

    Fields are ordered by element number and `written_at` is left out: a draft is
    the same draft whatever the clock said, and what a person approves is *which
    values are in which fields*, not when they were written.
    """
    ordered = sorted(fields, key=lambda item: int(item.element_ref[1:]))
    payload: dict[str, Any] = {
        "policy_version": DRAFT_POLICY_VERSION,
        "task_id": str(task_id),
        "profile_id": str(profile_id),
        "action_id": str(action_id),
        "manifest_digest": manifest_digest,
        "dispatch_id": str(dispatch_id),
        "observation_id": str(observation_id),
        "tab": tab,
        "document_epoch": document_epoch,
        "form_epoch": form_epoch,
        "form_ref": form_ref,
        "status": status.value,
        "fields": [item.model_dump(mode="json", exclude={"written_at"}) for item in ordered],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---- the second exact approval ---------------------------------------------------------------


class HandoverProposal(_Frozen):
    """The persisted proposal of a handover approval: safe references only.

    No raw value, no field label, no origin and no manifest. It names *which*
    draft, by the digest that pins exactly what is in its fields.
    """

    schema_version: Literal[1] = 1
    kind: Literal["form_draft_handover"] = HANDOVER_KIND
    policy_version: Literal["local-form-draft-v1"] = DRAFT_POLICY_VERSION
    classification: Literal["account_private"] = "account_private"
    task_id: uuid.UUID
    profile_id: uuid.UUID
    draft_id: uuid.UUID
    draft_digest: str = Field(pattern=_DIGEST)
    site_display: str = Field(min_length=3, max_length=253)
    field_count: int = Field(ge=1, le=MAX_FORM_FIELDS)
    partial: bool


class LocalDraftRefusal(ValueError):
    """A refused draft operation. `code` is stable and safe to show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The form draft was refused ({code}).")
        self.code = code


__all__ = [
    "DRAFT_POLICY_VERSION",
    "EXECUTABLE_MANIFEST_POLICY",
    "FILL_ERROR_CODES",
    "FILL_OPERATION",
    "HANDOVER_KIND",
    "HANDOVER_OPERATION",
    "HANDOVER_TOOL",
    "LIVE_DRAFT_STATUSES",
    "PROMISE",
    "DraftFieldRecord",
    "DraftStatus",
    "FillFieldInput",
    "FillFormInput",
    "FillResult",
    "HandoverProposal",
    "LocalDraftRefusal",
    "VerifiedField",
    "check_verified_hash",
    "choice_verified_hash",
    "draft_digest",
    "text_verified_hash",
]
