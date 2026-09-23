"""Request and response models for Milestone 8b S5 form planning and disclosure approval.

Read the response models as the list of everything that may leave the runtime
about a saved detail, a planning grant or a disclosure approval -- and note what
is missing and stays missing: **no raw saved value, no value digest, no element
identity hash, no manifest digest, no selector, no account fingerprint, no revoke
epoch and no origin URL** (the site's display host is enough for a person and for
the card). A masked preview is still private data: it appears only in the trusted
card and in the one planning context a confirmed grant permits.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.authenticated import Recipient
from app.domain.form_prepare import MAX_FORM_FIELDS, ManifestField
from app.domain.protected_values import PROTECTED_KINDS, ProtectedKind
from app.domain.research import GrantStatus
from app.domain.local_form_draft import DraftStatus
from app.repositories.form_drafts import DraftRecord
from app.repositories.form_prepare import FormPrepareGrantRecord, SavedDetail
from app.services.form_draft import HandoverView
from app.services.form_prepare import DisclosureView, FormPlanView, PlanningContext


class SaveProtectedValueBody(BaseModel):
    """One direct user value for one closed kind. The kind is in the path."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=400)


class SavedDetailResponse(BaseModel):
    """A saved detail as everything but its row may see it. Never the value."""

    data_ref: ProtectedKind
    kind: ProtectedKind
    preview: str
    updated_at: datetime

    @classmethod
    def from_detail(cls, detail: SavedDetail) -> "SavedDetailResponse":
        return cls(
            data_ref=detail.kind,
            kind=detail.kind,
            preview=detail.preview,
            updated_at=detail.updated_at,
        )


class SavedDetailListResponse(BaseModel):
    details: list[SavedDetailResponse]


class PrepareFormScopeBody(BaseModel):
    """The user's selection of saved details a planner may see previews of.

    Everything else in the scope -- the provider, the origin, the account, the
    revoke epoch, the field maximum -- is built by the runtime.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_data_refs: list[ProtectedKind] = Field(min_length=1, max_length=len(PROTECTED_KINDS))


class ConfirmFormGrantBody(BaseModel):
    """The trusted click: this grant, at the revision that was on screen."""

    model_config = ConfigDict(extra="forbid")

    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class RevokeFormGrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    grant_id: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class ProposeFormBody(BaseModel):
    """A planner's `prepare_form` proposal, verbatim, and who produced it.

    The proposal is parsed by a closed model in the runtime; nothing here is
    trusted until it has been. There is no field for a value, an origin, a
    selector or a URL, and the parser refuses one if it is smuggled inside.
    """

    model_config = ConfigDict(extra="forbid")

    proposal: dict[str, Any]
    provider: Recipient


class DisclosureDecisionBody(BaseModel):
    """The trusted click on the manifest card: an expected revision, nothing else."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class FormGrantScopeResponse(BaseModel):
    allowed_data_refs: list[ProtectedKind]
    planning_recipient: Recipient
    failover: Literal["none"]
    max_fields: int
    freeze_required: Literal[True]
    classification: Literal["account_private"]


class FormGrantResponse(BaseModel):
    grant_id: uuid.UUID
    status: GrantStatus
    revision: int
    expires_at: datetime | None
    scope: FormGrantScopeResponse

    @classmethod
    def from_record(cls, record: FormPrepareGrantRecord) -> "FormGrantResponse":
        scope = record.scope
        return cls(
            grant_id=record.id,
            status=record.status,
            revision=record.revision,
            expires_at=record.expires_at,
            scope=FormGrantScopeResponse(
                allowed_data_refs=list(scope.allowed_data_refs),
                planning_recipient=scope.planning_recipient,
                failover=scope.failover,
                max_fields=scope.max_fields,
                freeze_required=scope.freeze_required,
                classification=scope.classification,
            ),
        )


class DisclosureFieldResponse(BaseModel):
    """One line of the card: which saved detail, option or state goes in which field."""

    field_label: str
    control_type: str
    kind: Literal["saved_detail", "option", "checkbox"]
    data_ref: ProtectedKind | None = None
    preview: str | None = None
    option_label: str | None = None
    checked: bool | None = None
    #: Milestone 10 S4: where a workflow value came from. Absent for a global saved detail.
    provenance: Literal["document_extracted", "provider_derived"] | None = None

    @classmethod
    def from_field(cls, field: ManifestField) -> "DisclosureFieldResponse":
        if field.data_ref is not None:
            return cls(
                field_label=field.field_label,
                control_type=field.control_type,
                kind="saved_detail",
                data_ref=field.data_ref,
                preview=field.preview,
                provenance=field.provenance,
            )
        if field.option_ref is not None:
            return cls(
                field_label=field.field_label,
                control_type=field.control_type,
                kind="option",
                option_label=field.option_label,
            )
        return cls(
            field_label=field.field_label,
            control_type=field.control_type,
            kind="checkbox",
            checked=field.checked,
        )


class DisclosureCardResponse(BaseModel):
    """The controller-authored manifest card. Ids, labels, masked previews only."""

    action_id: uuid.UUID
    revision: int
    action_status: str
    approval_status: str | None
    approval_expires_at: datetime | None
    site: str
    form_label: str | None
    fields: list[DisclosureFieldResponse]
    #: True when a `country` value is shown as-is (a country cannot be masked).
    reveals_country: bool
    #: What the spent approval ended in: `local_draft_prepared`, `local_draft_partial`,
    #: `local_draft_not_written`, or the historical S5 `prepared_nothing`.
    result_code: str | None
    #: True for a `form-prepare-v2` manifest, whose approval fills the form locally with the
    #: network frozen. False for a historical S5 manifest, which is never executable.
    executable: bool
    #: Milestone 10 S4: `workflow` when the values placed are a workflow's adopted values.
    value_source: Literal["saved_details", "workflow"] = "saved_details"

    @classmethod
    def from_view(cls, view: DisclosureView) -> "DisclosureCardResponse":
        manifest = view.manifest
        return cls(
            action_id=view.action.id,
            revision=view.action.revision,
            action_status=view.action.status.value,
            approval_status=view.approval_status,
            approval_expires_at=view.approval_expires_at,
            site=manifest.site_display,
            form_label=manifest.form_label,
            fields=[DisclosureFieldResponse.from_field(field) for field in manifest.fields],
            reveals_country="country" in manifest.data_refs,
            result_code=view.result_code,
            executable=view.executable,
            value_source="saved_details" if manifest.workflow_id is None else "workflow",
        )


class DraftCardResponse(BaseModel):
    """The local draft, as the trusted card shows it. Ids, a status and counts only --
    never a value, a field label, a hash or an origin."""

    draft_id: uuid.UUID
    revision: int
    status: DraftStatus
    field_count: int
    #: Some approved fields were not verified: the form needs manual review.
    partial: bool
    site: str

    @classmethod
    def from_record(cls, record: DraftRecord, *, site: str, approved_fields: int | None = None) -> "DraftCardResponse":
        return cls(
            draft_id=record.id,
            revision=record.revision,
            status=record.status,
            field_count=len(record.fields),
            partial=record.status is DraftStatus.STALE,
            site=site,
        )


class HandoverCardResponse(BaseModel):
    """The second exact approval: lifting the freeze while the page is dirty."""

    action_id: uuid.UUID
    revision: int
    action_status: str
    approval_status: str | None
    approval_expires_at: datetime | None
    draft_id: uuid.UUID
    field_count: int
    partial: bool
    site: str
    #: `handed_over`, `handover_refused` or `handover_unknown` once the approval was spent.
    result_code: str | None

    @classmethod
    def from_view(cls, view: HandoverView) -> "HandoverCardResponse":
        return cls(
            action_id=view.action.id,
            revision=view.action.revision,
            action_status=view.action.status.value,
            approval_status=view.approval_status,
            approval_expires_at=view.approval_expires_at,
            draft_id=view.proposal.draft_id,
            field_count=view.proposal.field_count,
            partial=view.proposal.partial,
            site=view.proposal.site_display,
            result_code=view.result_code,
        )


class DraftDecisionBody(BaseModel):
    """The trusted click on a draft card: the revision that was on screen, nothing else."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class FormPlanResponse(BaseModel):
    task_id: uuid.UUID
    task_status: str
    objective: str
    site: str | None
    saved_details: list[SavedDetailResponse]
    grant: FormGrantResponse | None
    disclosure: DisclosureCardResponse | None
    form_count: int
    candidate_element_count: int
    max_fields: int = MAX_FORM_FIELDS
    #: The profile is open in a headed preparation window, nothing written yet.
    preparing: bool = False
    draft: DraftCardResponse | None = None
    handover: HandoverCardResponse | None = None
    #: Milestone 10 S4: `workflow` for a workflow's `form` step; `saved_details` are then its adopted values.
    value_source: Literal["saved_details", "workflow"] = "saved_details"

    @classmethod
    def from_view(cls, view: FormPlanView, handover: HandoverView | None = None) -> "FormPlanResponse":
        return cls(
            value_source="workflow" if view.value_source == "workflow" else "saved_details",
            preparing=view.preparing,
            draft=DraftCardResponse.from_record(view.draft, site=view.site or "") if view.draft else None,
            handover=HandoverCardResponse.from_view(handover) if handover else None,
            task_id=view.task.id,
            task_status=view.task.status.value,
            objective=view.objective,
            site=view.site,
            saved_details=[SavedDetailResponse.from_detail(item) for item in view.saved_details],
            grant=FormGrantResponse.from_record(view.grant) if view.grant else None,
            disclosure=DisclosureCardResponse.from_view(view.disclosure) if view.disclosure else None,
            form_count=view.form_count,
            candidate_element_count=view.candidate_element_count,
        )


class PlanningContextResponse(BaseModel):
    """What the ONE named provider may see. Structure and masked previews only."""

    grant_id: uuid.UUID
    recipient: Recipient
    objective: str
    site_display: str
    observation: str
    forms: list[dict[str, Any]]
    saved_data: list[dict[str, str]]

    @classmethod
    def from_context(cls, context: PlanningContext) -> "PlanningContextResponse":
        return cls(
            grant_id=context.grant_id,
            recipient=context.recipient,
            objective=context.objective,
            site_display=context.site_display,
            observation=context.observation_ref,
            forms=list(context.forms),
            saved_data=list(context.saved_data),
        )
