"""Request and response models for Milestone 10 S1: M10 file roots and approved documents.

Read the responses as the complete list of what may leave the runtime about a file: an opaque id, a
display name, a *root-relative* name, a format, a size and times. **No absolute path, no volume serial or
file index, no canonical root path and no `local_path`** appear in any response model below; the one
request that carries an absolute path (`RegisterRootBody.path` / `AddDroppedFileBody.path`) comes only from
Electron main (a native dialog, or main's own dropped-file store), never from the renderer or a model.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.authenticated import Recipient
from app.domain.documents import (
    MAX_FINDINGS,
    MAX_OBJECTIVE_CHARS,
    MAX_PURPOSE_CHARS,
    MAX_SUMMARY_CHARS,
)
from app.files.broker import ListedFile
from app.repositories.documents import DocumentAnswerRecord, DocumentDisclosureRecord
from app.domain.documents import LocalComparison
from app.services.documents import (
    DisclosureCardView,
    DocumentTaskView,
    ProviderContext,
    RootListing,
    RootView,
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- requests -------------------------------------------------------------------------------------


class RegisterRootBody(_Body):
    """From main only: the folder a native dialog returned, and the permissions the person ticked."""

    path: str = Field(min_length=3, max_length=1024)
    label: str = Field(min_length=1, max_length=64)
    can_read: bool
    can_create: bool
    can_modify: bool = False


class RevokeRootBody(_Body):
    expected_revision: int | None = Field(default=None, ge=1)


class CreateDocumentTaskBody(_Body):
    objective: str = Field(default="", max_length=MAX_OBJECTIVE_CHARS)


class AddRootFileBody(_Body):
    root_id: uuid.UUID
    #: Root-relative. Absolute, UNC, device, `..`, ADS and reserved names are refused by the service.
    relative_path: str = Field(min_length=1, max_length=512)


class AddDroppedFileBody(_Body):
    """From main only, resolved from its own dropped-file store after revalidation."""

    path: str = Field(min_length=3, max_length=1024)
    display_name: str = Field(min_length=1, max_length=255)


class ExtractBody(_Body):
    file_id: uuid.UUID


class CompareBody(_Body):
    first_document_id: uuid.UUID
    second_document_id: uuid.UUID


class CreateDisclosureBody(_Body):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=2)
    recipient: Recipient
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=MAX_PURPOSE_CHARS)


class DocumentGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class DocumentRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Literal["user_declined", "user_stopped"] = "user_declined"


class RecordComparisonBody(_Body):
    disclosure_id: uuid.UUID
    result: dict[str, Any] | None = None
    failure: Literal["model_unavailable", "invalid_output"] | None = None


# ---- responses ------------------------------------------------------------------------------------


class FileRootResponse(BaseModel):
    root_id: uuid.UUID
    label: str
    can_read: bool
    can_create: bool
    can_modify: bool
    revision: int
    created_at: datetime

    @classmethod
    def from_view(cls, view: RootView) -> "FileRootResponse":
        return cls(
            root_id=view.root_id,
            label=view.label,
            can_read=view.can_read,
            can_create=view.can_create,
            can_modify=view.can_modify,
            revision=view.revision,
            created_at=view.created_at,
        )


class FileRootListResponse(BaseModel):
    roots: list[FileRootResponse]


class ListedFileResponse(BaseModel):
    relative_path: str
    name: str
    size_bytes: int
    modified_at: datetime
    format: Literal["pdf", "docx", "txt"]

    @classmethod
    def from_listed(cls, item: ListedFile) -> "ListedFileResponse":
        from datetime import UTC

        return cls(
            relative_path=item.relative_path,
            name=item.name,
            size_bytes=item.size,
            modified_at=datetime.fromtimestamp(item.mtime_ns / 1_000_000_000, tz=UTC),
            format=item.format,
        )


class RootListingResponse(BaseModel):
    root_id: uuid.UUID
    files: list[ListedFileResponse]
    truncated: bool

    @classmethod
    def from_listing(cls, listing: RootListing) -> "RootListingResponse":
        return cls(
            root_id=listing.root_id,
            files=[ListedFileResponse.from_listed(item) for item in listing.files],
            truncated=listing.truncated,
        )


class FileResponse(BaseModel):
    file_id: uuid.UUID
    source: Literal["ROOT_FILE", "DROPPED_FILE"]
    display_name: str
    relative_path: str | None
    root_id: uuid.UUID | None
    format: Literal["pdf", "docx", "txt"]
    size_bytes: int
    added_at: datetime


class DocumentResponse(BaseModel):
    document_id: uuid.UUID
    file_id: uuid.UUID
    format: Literal["pdf", "docx", "txt"]
    page_count: int | None
    text_chars: int
    truncated: bool
    flags: dict[str, int]
    #: Private text for the trusted view only (bounded). Never sent to a provider from here.
    preview: str | None
    expires_at: datetime
    purged: bool


class CardDocumentResponse(BaseModel):
    doc_ref: Literal["d1", "d2"]
    document_id: uuid.UUID
    label: str
    #: EXACTLY what the provider would receive for this document (redacted, bounded).
    excerpt: str | None


class DocumentCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    recipient: Recipient
    model: str
    purpose: str
    documents: list[CardDocumentResponse]
    max_excerpt_bytes: int
    text_bytes: int | None
    redaction_count: int | None
    truncated: bool | None
    redaction_policy: str

    @classmethod
    def from_view(cls, card: DisclosureCardView) -> "DocumentCardResponse":
        return cls(
            grant_id=card.grant_id,
            grant_revision=card.grant_revision,
            grant_status=card.grant_status,
            expires_at=card.expires_at,
            recipient=card.recipient,
            model=card.model,
            purpose=card.purpose,
            documents=[
                CardDocumentResponse(doc_ref=item.doc_ref, document_id=item.document_id, label=item.label, excerpt=item.excerpt)
                for item in card.documents
            ],
            max_excerpt_bytes=card.max_excerpt_bytes,
            text_bytes=card.text_bytes,
            redaction_count=card.redaction_count,
            truncated=card.truncated,
            redaction_policy=card.redaction_policy,
        )


class DocumentDisclosureResponse(BaseModel):
    disclosure_id: uuid.UUID
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"]
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    text_bytes: int
    redaction_count: int
    truncated: bool

    @classmethod
    def from_record(cls, record: DocumentDisclosureRecord) -> "DocumentDisclosureResponse":
        return cls(
            disclosure_id=record.id,
            status=record.status,
            error_code=record.error_code,
            started_at=record.started_at,
            finished_at=record.finished_at,
            text_bytes=record.text_bytes,
            redaction_count=record.redaction_count,
            truncated=record.truncated,
        )


class EvidenceResponse(BaseModel):
    doc_ref: Literal["d1", "d2"]
    quote: str


class FindingResponse(BaseModel):
    kind: Literal["match", "gap", "difference"]
    text: str
    evidence: list[EvidenceResponse]


class ComparisonAnswerResponse(BaseModel):
    kind: Literal["comparison", "cannot_compare"]
    summary: str | None = Field(max_length=MAX_SUMMARY_CHARS)
    reason: str | None
    findings: list[FindingResponse] = Field(max_length=MAX_FINDINGS)
    recipient: Recipient
    model: str
    created_at: datetime

    @classmethod
    def from_record(cls, record: DocumentAnswerRecord) -> "ComparisonAnswerResponse":
        return cls(
            kind=record.kind,
            summary=record.summary,
            reason=record.reason,
            findings=[FindingResponse.model_validate(item) for item in record.findings],
            recipient=record.recipient,
            model=record.model,
            created_at=record.created_at,
        )


class DocumentTaskResponse(BaseModel):
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: Literal["local", "awaiting_approval", "approved", "expired", "comparing", "compared", "failed", "outcome_unknown"]
    files: list[FileResponse]
    documents: list[DocumentResponse]
    card: DocumentCardResponse | None
    disclosure: DocumentDisclosureResponse | None
    answer: ComparisonAnswerResponse | None

    @classmethod
    def from_view(cls, view: DocumentTaskView) -> "DocumentTaskResponse":
        return cls(
            task_id=view.task_id,
            task_status=view.task_status,
            task_revision=view.task_revision,
            objective=view.objective,
            phase=view.phase,
            files=[
                FileResponse(
                    file_id=item.file_id,
                    source=item.source,
                    display_name=item.display_name,
                    relative_path=item.relative_path,
                    root_id=item.root_id,
                    format=item.format,
                    size_bytes=item.size_bytes,
                    added_at=item.added_at,
                )
                for item in view.files
            ],
            documents=[
                DocumentResponse(
                    document_id=item.document_id,
                    file_id=item.file_id,
                    format=item.format,
                    page_count=item.page_count,
                    text_chars=item.text_chars,
                    truncated=item.truncated,
                    flags=item.flags,
                    preview=item.preview,
                    expires_at=item.expires_at,
                    purged=item.purged,
                )
                for item in view.documents
            ],
            card=None if view.card is None else DocumentCardResponse.from_view(view.card),
            disclosure=None if view.disclosure is None else DocumentDisclosureResponse.from_record(view.disclosure),
            answer=None if view.answer is None else ComparisonAnswerResponse.from_record(view.answer),
        )


class LatestDocumentTaskResponse(BaseModel):
    task: DocumentTaskResponse | None


class DocumentShapeResponse(BaseModel):
    characters: int
    words: int
    lines: int
    headings: list[str]


class LocalComparisonResponse(BaseModel):
    first: DocumentShapeResponse
    second: DocumentShapeResponse
    shared_terms: list[str]
    only_first: list[str]
    only_second: list[str]
    overlap: float

    @classmethod
    def from_comparison(cls, comparison: LocalComparison) -> "LocalComparisonResponse":
        def shape(value: Any) -> DocumentShapeResponse:
            return DocumentShapeResponse(
                characters=value.characters, words=value.words, lines=value.lines, headings=list(value.headings)
            )

        return cls(
            first=shape(comparison.first),
            second=shape(comparison.second),
            shared_terms=list(comparison.shared_terms),
            only_first=list(comparison.only_first),
            only_second=list(comparison.only_second),
            overlap=comparison.overlap,
        )


class ProjectedDocumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_ref: Literal["d1", "d2"]
    excerpt: str
    truncated: bool


class DocumentProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    classification: Literal["document_private"]
    trust: Literal["untrusted_environment"]
    purpose: str
    documents: list[ProjectedDocumentResponse]


class DocumentProviderContextResponse(BaseModel):
    """Released once, after the claim committed: the purpose and the redacted excerpts. No names, no paths."""

    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    purpose: str
    recipient: Recipient
    model: str
    projection: DocumentProjectionResponse

    @classmethod
    def from_context(cls, context: ProviderContext) -> "DocumentProviderContextResponse":
        return cls(
            disclosure_id=context.disclosure_id,
            task_id=context.task_id,
            purpose=context.purpose,
            recipient=context.recipient,
            model=context.model,
            projection=DocumentProjectionResponse.model_validate(context.projection),
        )
