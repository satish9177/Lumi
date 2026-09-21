"""Milestone 8b S5: form-planning scope, the `prepare_form` proposal and the manifest.

**S5 is approval-only.** Nothing in this module can express a change to a page:
there is no operation, no value to type and no way to reach a browser. It exists
so Lumi can say, exactly and reviewably, *which saved detail would go into which
field of which observed form, for which origin* -- and so a user can approve that
statement and nothing more.

Three separate pieces of authority, kept apart on purpose:

```text
form_prepare grant   permission for ONE planner to see a bounded form structure and
                     masked previews of chosen saved details (a task grant, S3-style)
prepare_form         a planner *proposal*: field -> saved detail / option / checkbox
                     state. Controller-validated. Not a worker operation.
manifest approval    the exact, single-use approval of one disclosure manifest
                     (the existing action + approval machinery)
```

The manifest never contains a raw protected value: it carries `dataRef`,
`valueDigest` and the masked `preview`. It binds task, profile, account, revoke
epoch, origin, observation, both epochs, every element's semantic identity, every
saved value's digest and every chosen option or checkbox state, so changing any of
them changes `manifest_digest` and an approval of the old digest authorises
nothing.
"""

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domain.authenticated import ACCOUNT_PRIVATE, Recipient
from app.domain.authenticated_forms import (
    ELEMENT_REF,
    FORM_REF,
    OPTION_REF,
    ElementProjection,
)
from app.domain.digest import canonical_json
from app.domain.protected_values import PROTECTED_KINDS, ProtectedKind
from app.domain.research import MAX_HOST_CHARS, OBSERVATION_REF

FORM_PREPARE_KIND: Final = "form_prepare"
FORM_PREPARE_POLICY_VERSION: Final = "form-prepare-v1"
#: What a manifest built now carries. S6 changed what approving one *does*, so it is
#: a new policy version; the grant scope keeps `form-prepare-v1` (it authorises a
#: planner to see a structure, which S6 did not change).
MANIFEST_POLICY_VERSION: Final = "form-prepare-v2"
#: The persisted tool identity of the exact disclosure approval. It is **not** a
#: worker operation: the browser worker has no operation of this name, and the
#: runtime never dispatches an action carrying it.
FORM_PREPARE_TOOL: Final = "prepare_form"
FORM_DISCLOSURE_KIND: Final = "form_disclosure_manifest"
MAX_FORM_FIELDS: Final = 12
#: What a terminal S5 approval records. Nothing was prepared in the browser.
PREPARED_NOTHING: Final = "prepared_nothing"

_DIGEST = r"^[0-9a-f]{64}$"
_CONTROL_TEXT: Final = frozenset({"text", "email", "tel", "number", "textarea"})
_CONTROL_CHOICE: Final = frozenset({"select_single", "radiogroup"})
_CONTROL_CHECK: Final = frozenset({"checkbox"})
SUPPORTED_CONTROLS: Final = _CONTROL_TEXT | _CONTROL_CHOICE | _CONTROL_CHECK


#: Refusals that mean "the facts moved since this was proposed", not "this input is
#: malformed". A fresh observation or a fresh manifest is the only way forward.
FORM_PREPARE_STATE_CODES: Final = frozenset(
    {
        "protected_value_changed",
        "account_changed",
        "stale_observation",
        "stale_document_epoch",
        "stale_form_epoch",
        "origin_changed",
        "grant_not_usable",
        # Milestone 8b S6: the browser, the draft or the window is not in the state the
        # request needs. Never malformed input: the same request is fine in another state.
        "form_is_dirty",
        "preparation_mode_required",
        "preparation_destination_missing",
        "preparation_navigation_failed",
        "legacy_manifest_not_executable",
        "draft_changed",
        "draft_not_live",
        "draft_not_found",
        "freeze_owned",
        "worker_busy",
        "step_in_flight",
        "left_site_scope",
        "login_required",
        "no_form_observed",
    }
)


class FormPrepareRefusal(ValueError):
    """A refused form-preparation input. `code` is stable and safe to show."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The form preparation was refused ({code}).")
        self.code = code


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest_of(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def account_binding(account_fingerprint: str) -> str:
    """A one-way binding to the account, so the fingerprint itself is never stored twice."""
    return hashlib.sha256(f"form-prepare-account-v1:{account_fingerprint}".encode()).hexdigest()


# ---- the grant scope -----------------------------------------------------------


class FormPrepareScope(_Frozen):
    """Exactly what one form-planning grant authorises. Immutable once confirmed.

    It permits one thing: letting *one* named provider see the bounded form
    structure of the user's own authenticated observation, plus masked previews of
    the listed saved details. It permits no browser action of any kind.
    """

    schema_version: Literal[1] = 1
    kind: Literal["form_prepare"] = FORM_PREPARE_KIND
    policy_version: Literal["form-prepare-v1"] = FORM_PREPARE_POLICY_VERSION
    profile_id: uuid.UUID
    site: str = Field(min_length=3, max_length=MAX_HOST_CHARS)
    account_fingerprint: str = Field(pattern=_DIGEST)
    profile_revoke_epoch: int = Field(ge=0)
    #: The account-reading grant this planning grew out of. Same task, same profile.
    source_authenticated_grant_id: uuid.UUID
    #: Exactly one planner, the one already trusted with this account's text.
    planning_recipient: Recipient
    #: If it is unavailable Lumi stops. There is no second provider.
    failover: Literal["none"] = "none"
    #: The one origin a future local draft may target, derived by the controller
    #: from the profile and the observed page -- never from a model or a page.
    recipient_origin: str = Field(min_length=9, max_length=MAX_HOST_CHARS + 10)
    #: A promise about the *future* action. It enables nothing in S5.
    freeze_required: Literal[True] = True
    allowed_data_refs: list[ProtectedKind] = Field(min_length=1, max_length=len(PROTECTED_KINDS))
    max_fields: int = Field(default=MAX_FORM_FIELDS, ge=1, le=MAX_FORM_FIELDS)
    classification: Literal["account_private"] = ACCOUNT_PRIVATE

    @field_validator("allowed_data_refs")
    @classmethod
    def _unique_refs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("allowed data refs must be unique")
        return sorted(value, key=PROTECTED_KINDS.index)

    @field_validator("recipient_origin")
    @classmethod
    def _origin_is_bare(cls, value: str) -> str:
        if not re.fullmatch(r"https?://[a-z0-9.-]+(:[0-9]{1,5})?", value):
            raise ValueError("a recipient origin is a scheme and a host, nothing else")
        return value

    @property
    def digest(self) -> str:
        return _digest_of(self.model_dump(mode="json"))


# ---- the proposal --------------------------------------------------------------


class TextEntry(_Frozen):
    """A text-like field and the saved detail that would go into it."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    data_ref: ProtectedKind


class ChoiceEntry(_Frozen):
    """A select or radio group and the option that would be chosen."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    option_ref: str = Field(pattern=OPTION_REF)


class CheckEntry(_Frozen):
    """A checkbox and the state it would be given. There is no free text."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    checked: StrictBool


ProposalEntry = TextEntry | ChoiceEntry | CheckEntry


class PrepareFormProposal(_Frozen):
    """What a planner may say. Closed: no value, origin, selector, URL or provider."""

    operation: Literal["prepare_form"] = "prepare_form"
    observation: str = Field(pattern=OBSERVATION_REF)
    form_ref: str = Field(pattern=FORM_REF)
    entries: list[ProposalEntry]


def parse_prepare_form(payload: Any) -> PrepareFormProposal:
    """Strictly parse a `prepare_form` proposal, with a stable refusal code.

    The entry count is checked before anything else so `13` and `0` have their own
    codes. Any key outside the closed shapes -- a value, an origin, a selector, a
    provider -- is `unsupported_proposal`.
    """
    if not isinstance(payload, dict):
        raise FormPrepareRefusal("unsupported_proposal")
    entries = payload.get("entries")
    if isinstance(entries, list):
        if not entries:
            raise FormPrepareRefusal("no_entries")
        if len(entries) > MAX_FORM_FIELDS:
            raise FormPrepareRefusal("too_many_entries")
    try:
        return PrepareFormProposal.model_validate(payload)
    except ValidationError:
        raise FormPrepareRefusal("unsupported_proposal") from None


# ---- saved-value snapshots -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProtectedSnapshot:
    """What the planning path may know about a saved value. Never the value."""

    kind: str
    value_digest: str
    preview: str
    length: int


# ---- the manifest --------------------------------------------------------------


class ManifestField(_Frozen):
    """One approved field: exactly one of a saved detail, an option or a checkbox state."""

    element_ref: str = Field(pattern=ELEMENT_REF)
    element_identity_hash: str = Field(pattern=_DIGEST)
    field_label: str = Field(max_length=120)
    control_type: Literal[
        "text", "email", "tel", "number", "textarea", "select_single", "radiogroup", "checkbox"
    ]
    data_ref: ProtectedKind | None = None
    value_digest: str | None = Field(default=None, pattern=_DIGEST)
    preview: str | None = Field(default=None, max_length=120)
    option_ref: str | None = Field(default=None, pattern=OPTION_REF)
    option_label: str | None = Field(default=None, max_length=120)
    option_identity_hash: str | None = Field(default=None, pattern=_DIGEST)
    checked: bool | None = None

    @model_validator(mode="after")
    def _one_variant(self) -> Self:
        text = (self.data_ref, self.value_digest, self.preview)
        choice = (self.option_ref, self.option_label, self.option_identity_hash)
        if self.control_type in _CONTROL_TEXT:
            valid = all(item is not None for item in text) and all(item is None for item in choice)
            valid = valid and self.checked is None
        elif self.control_type in _CONTROL_CHOICE:
            valid = all(item is not None for item in choice) and all(item is None for item in text)
            valid = valid and self.checked is None
        else:
            valid = self.checked is not None and all(item is None for item in (*text, *choice))
        if not valid:
            raise ValueError("a manifest field carries exactly the variant its control needs")
        return self

    @property
    def sort_key(self) -> int:
        return int(self.element_ref[1:])


class DisclosureManifest(_Frozen):
    """The exact, reviewable statement a user approves. Free of raw saved values.

    `policy_version` is `form-prepare-v2` for every manifest built since Milestone 8b
    S6, whose approval funds one network-frozen local draft. A `form-prepare-v1`
    manifest is a historical S5 approval (it could only ever end in
    `prepared_nothing`): it still parses, and its digest still verifies, so history
    stays readable -- and it is **never executable**, whatever its state.
    """

    schema_version: Literal[1] = 1
    kind: Literal["form_disclosure_manifest"] = FORM_DISCLOSURE_KIND
    policy_version: Literal["form-prepare-v1", "form-prepare-v2"] = MANIFEST_POLICY_VERSION
    classification: Literal["account_private"] = ACCOUNT_PRIVATE
    task_id: uuid.UUID
    profile_id: uuid.UUID
    form_prepare_grant_id: uuid.UUID
    planning_recipient: Recipient
    site_display: str = Field(min_length=3, max_length=MAX_HOST_CHARS)
    recipient_origin: str = Field(min_length=9, max_length=MAX_HOST_CHARS + 10)
    account_binding: str = Field(pattern=_DIGEST)
    profile_revoke_epoch: int = Field(ge=0)
    observation_id: uuid.UUID
    observation_ref: str = Field(pattern=OBSERVATION_REF)
    tab: str = Field(pattern=r"^t[1-3]$")
    document_epoch: int = Field(ge=1)
    form_epoch: int = Field(ge=1)
    form_ref: str = Field(pattern=FORM_REF)
    form_label: str | None = Field(default=None, max_length=120)
    fields: list[ManifestField] = Field(min_length=1, max_length=MAX_FORM_FIELDS)
    manifest_digest: str = Field(pattern=_DIGEST)

    @model_validator(mode="after")
    def _canonical_and_bound(self) -> Self:
        keys = [field.sort_key for field in self.fields]
        if keys != sorted(keys) or len(set(keys)) != len(keys):
            raise ValueError("manifest fields are unique and in canonical order")
        if self.manifest_digest != self.compute_digest():
            raise ValueError("manifest_digest does not match the manifest")
        return self

    def _body(self) -> dict[str, Any]:
        body = self.model_dump(mode="json")
        body.pop("manifest_digest")
        return body

    def compute_digest(self) -> str:
        return _digest_of(self._body())

    @classmethod
    def build(cls, **facts: Any) -> Self:
        """Order the fields canonically, then bind them with the digest."""
        fields = sorted(facts.pop("fields"), key=lambda item: item.sort_key)
        unsigned = cls.model_construct(**facts, fields=fields, manifest_digest="0" * 64)
        body = unsigned.model_dump(mode="json")
        body.pop("manifest_digest")
        return cls(**facts, fields=fields, manifest_digest=_digest_of(body))

    def proposal(self) -> dict[str, Any]:
        """The persisted action proposal: the manifest itself, and nothing else."""
        return self.model_dump(mode="json")

    @property
    def data_refs(self) -> list[str]:
        return [field.data_ref for field in self.fields if field.data_ref is not None]


def parse_manifest(proposal: Mapping[str, Any]) -> DisclosureManifest:
    try:
        return DisclosureManifest.model_validate(dict(proposal))
    except ValidationError:
        raise FormPrepareRefusal("manifest_invalid") from None


# ---- element identity ----------------------------------------------------------


def element_identity_hash(
    *, observation_id: uuid.UUID, tab: str | None, document_epoch: int, form_epoch: int,
    element: ElementProjection,
) -> str:
    """A stable hash of everything the reviewed inventory says about one element.

    Built from the projected semantic identity (form, frame, role, control type,
    accessible name, the four state flags, submit-likeness and length limit) and
    the two epochs. It never includes a selector, id, name, class, value,
    coordinate or locator description -- none of those exist in the projection.
    """
    return _digest_of(
        {
            "observation_id": str(observation_id),
            "tab": tab,
            "document_epoch": document_epoch,
            "form_epoch": form_epoch,
            "form_ref": element.form_ref,
            "element_ref": element.element_ref,
            "frame_ref": element.frame_ref,
            "role": element.role,
            "control_type": element.control_type,
            "accessible_name": element.accessible_name,
            "required": element.required,
            "read_only": element.read_only,
            "enabled": element.enabled,
            "visible": element.visible,
            "submit_like": element.submit_like,
            "max_length": element.max_length,
        }
    )


def option_identity_hash(*, element_hash: str, option_ref: str, option_label: str) -> str:
    return _digest_of(
        {"element_identity_hash": element_hash, "option_ref": option_ref, "option_label": option_label}
    )


# ---- validating a proposal against an observation ------------------------------


@dataclass(frozen=True, slots=True)
class ObservedForm:
    """The observation facts validation needs, without the observation's text."""

    observation_id: uuid.UUID
    observation_ref: str
    tab: str
    document_epoch: int
    form_epoch: int
    form_ref: str
    form_label: str | None
    elements: tuple[ElementProjection, ...]


def resolve_fields(
    *,
    proposal: PrepareFormProposal,
    observed: ObservedForm,
    scope: FormPrepareScope,
    saved: Mapping[str, ProtectedSnapshot],
) -> list[ManifestField]:
    """Turn a proposal into manifest fields, or refuse it. Nothing is created here.

    Every check is deterministic and runs before an approval, a card or any other
    record exists. The element must belong to *this* form of *this* observation;
    it must be a supported, enabled, writable, visible control; and the entry must
    be the variant that control needs.
    """
    by_ref = {element.element_ref: element for element in observed.elements}
    if len(proposal.entries) > min(scope.max_fields, MAX_FORM_FIELDS):
        raise FormPrepareRefusal("too_many_entries")
    seen: set[str] = set()
    fields: list[ManifestField] = []
    for entry in proposal.entries:
        if entry.element_ref in seen:
            raise FormPrepareRefusal("duplicate_element")
        seen.add(entry.element_ref)
        element = by_ref.get(entry.element_ref)
        if element is None:
            raise FormPrepareRefusal("unknown_element")
        if element.form_ref != proposal.form_ref:
            raise FormPrepareRefusal("wrong_form")
        _check_writable(element)
        identity = element_identity_hash(
            observation_id=observed.observation_id,
            tab=observed.tab,
            document_epoch=observed.document_epoch,
            form_epoch=observed.form_epoch,
            element=element,
        )
        control = element.control_type
        if control in _CONTROL_TEXT:
            if not isinstance(entry, TextEntry):
                raise FormPrepareRefusal("wrong_entry_variant")
            if entry.data_ref not in scope.allowed_data_refs:
                raise FormPrepareRefusal("data_ref_not_allowed")
            snapshot = saved.get(entry.data_ref)
            if snapshot is None:
                raise FormPrepareRefusal("data_ref_unavailable")
            if element.max_length is not None and snapshot.length > element.max_length:
                raise FormPrepareRefusal("value_too_long")
            fields.append(
                ManifestField(
                    element_ref=element.element_ref,
                    element_identity_hash=identity,
                    field_label=element.accessible_name,
                    control_type=control,
                    data_ref=entry.data_ref,
                    value_digest=snapshot.value_digest,
                    preview=snapshot.preview,
                )
            )
        elif control in _CONTROL_CHOICE:
            if not isinstance(entry, ChoiceEntry):
                raise FormPrepareRefusal("wrong_entry_variant")
            option = next((item for item in element.option_refs if item.ref == entry.option_ref), None)
            if option is None:
                raise FormPrepareRefusal("unknown_option")
            fields.append(
                ManifestField(
                    element_ref=element.element_ref,
                    element_identity_hash=identity,
                    field_label=element.accessible_name,
                    control_type=control,
                    option_ref=option.ref,
                    option_label=option.label,
                    option_identity_hash=option_identity_hash(
                        element_hash=identity, option_ref=option.ref, option_label=option.label
                    ),
                )
            )
        else:  # checkbox: `_check_writable` refused every other control
            if not isinstance(entry, CheckEntry):
                raise FormPrepareRefusal("wrong_entry_variant")
            fields.append(
                ManifestField(
                    element_ref=element.element_ref,
                    element_identity_hash=identity,
                    field_label=element.accessible_name,
                    control_type="checkbox",
                    checked=entry.checked,
                )
            )
    return fields


def _check_writable(element: ElementProjection) -> None:
    if element.submit_like or element.control_type == "submit_like":
        raise FormPrepareRefusal("submit_like_control")
    if element.role in ("button", "link", "option"):
        raise FormPrepareRefusal("unsupported_control")
    if element.control_type == "select_multi":
        raise FormPrepareRefusal("unsupported_control")
    if element.control_type not in SUPPORTED_CONTROLS:
        raise FormPrepareRefusal("unsupported_control")
    if not element.enabled:
        raise FormPrepareRefusal("disabled_control")
    if element.read_only:
        raise FormPrepareRefusal("read_only_control")
    if not element.visible:
        raise FormPrepareRefusal("hidden_control")


# ---- freshness -------------------------------------------------------------------


def verify_protected_values_current(
    manifest: DisclosureManifest, current: Mapping[str, str]
) -> None:
    """Refuse a manifest whose saved values are no longer the ones it bound.

    `current` maps a kind to its digest **now**. Written once so S5's approval and
    S6's later pre-write check cannot disagree about what "unchanged" means.
    """
    for field in manifest.fields:
        if field.data_ref is not None and current.get(field.data_ref) != field.value_digest:
            raise FormPrepareRefusal("protected_value_changed")


__all__ = [
    "FORM_DISCLOSURE_KIND",
    "FORM_PREPARE_KIND",
    "FORM_PREPARE_STATE_CODES",
    "FORM_PREPARE_POLICY_VERSION",
    "FORM_PREPARE_TOOL",
    "MANIFEST_POLICY_VERSION",
    "MAX_FORM_FIELDS",
    "PREPARED_NOTHING",
    "CheckEntry",
    "ChoiceEntry",
    "DisclosureManifest",
    "FormPrepareRefusal",
    "FormPrepareScope",
    "ManifestField",
    "ObservedForm",
    "PrepareFormProposal",
    "ProtectedSnapshot",
    "TextEntry",
    "account_binding",
    "element_identity_hash",
    "option_identity_hash",
    "parse_manifest",
    "parse_prepare_form",
    "resolve_fields",
    "verify_protected_values_current",
]
