"""SQL for extracted documents, document-disclosure grants, disclosures and answers (Milestone 10 S1).

The single-use property of a disclosure is a database fact, as for M9 S2: `claim_grant` is one
compare-and-swap (`ACTIVE` -> `COMPLETED`, guarded by revision, status and expiry, and filtered by
`kind = 'document_disclose'` so no other grant kind can be spent here), and `document_disclosures` is
UNIQUE on both the grant and the task. Callers own the transaction.

`text_for` is the only method that returns extracted text. It exists for the local comparison, the
trusted view and the projection builder; nothing it returns is written to a task event or a log.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import document_answers, document_disclosures, documents, file_refs, task_grants
from app.domain.documents import DOCUMENT_DISCLOSE_KIND, DOCUMENT_PRIVATE, DocumentDiscloseScope
from app.domain.research import GrantStatus

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """A document's metadata. **No text**: use `DocumentRepository.text_for`."""

    id: uuid.UUID
    task_id: uuid.UUID
    file_ref_id: uuid.UUID
    format: str
    page_count: int | None
    text_sha256: str
    text_chars: int
    truncated: bool
    flags: dict[str, int]
    created_at: datetime
    expires_at: datetime
    purged: bool


@dataclass(frozen=True, slots=True)
class DocumentGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    scope: DocumentDiscloseScope
    scope_digest: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class DocumentDisclosureRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    recipient: str
    model: str
    projection_digest: str
    document_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DocumentAnswerRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    disclosure_id: uuid.UUID
    recipient: str
    model: str
    kind: str
    summary: str | None
    reason: str | None
    findings: list[dict[str, Any]]
    created_at: datetime


_METADATA = (
    documents.c.id,
    documents.c.task_id,
    documents.c.file_ref_id,
    documents.c.format,
    documents.c.page_count,
    documents.c.text_sha256,
    documents.c.text_chars,
    documents.c.truncated,
    documents.c.flags,
    documents.c.created_at,
    documents.c.expires_at,
    documents.c.purged_at,
)


def _document(row: Row[Any]) -> DocumentRecord:
    return DocumentRecord(
        id=row.id,
        task_id=row.task_id,
        file_ref_id=row.file_ref_id,
        format=row.format,
        page_count=row.page_count,
        text_sha256=row.text_sha256,
        text_chars=row.text_chars,
        truncated=row.truncated,
        flags={str(key): int(value) for key, value in dict(row.flags).items()},
        created_at=row.created_at,
        expires_at=row.expires_at,
        purged=row.purged_at is not None,
    )


def _grant(row: Row[Any]) -> DocumentGrantRecord:
    return DocumentGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=int(row.revision),
        scope=DocumentDiscloseScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


def _disclosure(row: Row[Any]) -> DocumentDisclosureRecord:
    return DocumentDisclosureRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        recipient=row.recipient,
        model=row.model,
        projection_digest=row.projection_digest,
        document_count=row.document_count,
        text_bytes=row.text_bytes,
        redaction_count=row.redaction_count,
        truncated=row.truncated,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )


def _answer(row: Row[Any]) -> DocumentAnswerRecord:
    return DocumentAnswerRecord(
        id=row.id,
        task_id=row.task_id,
        disclosure_id=row.disclosure_id,
        recipient=row.recipient,
        model=row.model,
        kind=row.kind,
        summary=row.summary,
        reason=row.reason,
        findings=list(row.findings),
        created_at=row.created_at,
    )


class DocumentRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- documents -----------------------------------------------------------------------------

    async def insert_document(
        self,
        *,
        document_id: uuid.UUID,
        task_id: uuid.UUID,
        file_ref_id: uuid.UUID,
        format: str,
        page_count: int | None,
        text_value: str,
        text_sha256: str,
        truncated: bool,
        flags: dict[str, int],
        retention: timedelta,
    ) -> DocumentRecord:
        row = (
            await self._connection.execute(
                insert(documents)
                .values(
                    id=document_id,
                    task_id=task_id,
                    file_ref_id=file_ref_id,
                    format=format,
                    page_count=page_count,
                    text=text_value,
                    text_sha256=text_sha256,
                    text_chars=len(text_value),
                    truncated=truncated,
                    flags=flags,
                    classification=DOCUMENT_PRIVATE,
                    expires_at=func.now() + retention,
                )
                .returning(*_METADATA)
            )
        ).one()
        return _document(row)

    async def get_document(self, document_id: uuid.UUID) -> DocumentRecord | None:
        row = (await self._connection.execute(select(*_METADATA).where(documents.c.id == document_id))).one_or_none()
        return _document(row) if row is not None else None

    async def document_for_ref(self, file_ref_id: uuid.UUID) -> DocumentRecord | None:
        row = (
            await self._connection.execute(select(*_METADATA).where(documents.c.file_ref_id == file_ref_id))
        ).one_or_none()
        return _document(row) if row is not None else None

    async def documents_for_task(self, task_id: uuid.UUID) -> list[DocumentRecord]:
        rows = await self._connection.execute(
            select(*_METADATA).where(documents.c.task_id == task_id).order_by(documents.c.created_at, documents.c.id)
        )
        return [_document(row) for row in rows]

    async def text_for(self, document_id: uuid.UUID, *, task_id: uuid.UUID) -> str | None:
        """The extracted text of ONE document of ONE task, if it has not expired. Never logged."""
        return (
            await self._connection.execute(
                select(documents.c.text).where(
                    documents.c.id == document_id,
                    documents.c.task_id == task_id,
                    documents.c.purged_at.is_(None),
                    documents.c.expires_at > func.now(),
                )
            )
        ).scalar_one_or_none()

    async def purge_expired(self, *, retention: timedelta) -> int:
        """Retention for everything S1 keeps that came from a document or a dropped file's location.

        Extracted text past `expires_at`; a comparison's quoted evidence and summary once the documents it
        quoted are that old; and a dropped file's absolute path (`file_refs.local_path`), which is only
        needed to re-verify the file while its text is still usable.
        """
        result = await self._connection.execute(
            update(documents)
            .where(documents.c.purged_at.is_(None), documents.c.expires_at <= func.now())
            .values(text=None, purged_at=func.now())
        )
        await self._connection.execute(
            update(document_answers)
            .where(document_answers.c.created_at <= func.now() - retention, document_answers.c.findings != text("'[]'::jsonb"))
            .values(findings=[], summary=text("CASE WHEN kind = 'comparison' THEN 'Expired.' ELSE NULL END"))
        )
        await self._connection.execute(
            update(file_refs)
            .where(
                file_refs.c.source == "DROPPED_FILE",
                file_refs.c.created_at <= func.now() - retention,
                file_refs.c.local_path != "",
            )
            .values(local_path="")
        )
        return int(result.rowcount or 0)

    # ---- the disclosure grant ------------------------------------------------------------------

    async def insert_grant(self, *, grant_id: uuid.UUID, scope: DocumentDiscloseScope) -> DocumentGrantRecord:
        row = (
            await self._connection.execute(
                insert(task_grants)
                .values(
                    id=grant_id,
                    task_id=scope.task_id,
                    kind=DOCUMENT_DISCLOSE_KIND,
                    status=GrantStatus.PENDING.value,
                    revision=1,
                    policy_version=scope.policy_version,
                    scope=scope.model_dump(mode="json"),
                    scope_digest=scope.digest,
                )
                .returning(*task_grants.c)
            )
        ).one()
        return _grant(row)

    async def get_grant(self, grant_id: uuid.UUID) -> DocumentGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(task_grants.c.id == grant_id, task_grants.c.kind == DOCUMENT_DISCLOSE_KIND)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> DocumentGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants)
                .where(task_grants.c.task_id == task_id, task_grants.c.kind == DOCUMENT_DISCLOSE_KIND)
                .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
                .limit(1)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> DocumentGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.task_id == task_id,
                    task_grants.c.kind == DOCUMENT_DISCLOSE_KIND,
                    task_grants.c.status.in_(_OPEN),
                )
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> DocumentGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DOCUMENT_DISCLOSE_KIND,
                    task_grants.c.revision == expected_revision,
                    task_grants.c.status == GrantStatus.PENDING.value,
                    task_grants.c.scope_digest == scope_digest,
                )
                .values(
                    status=GrantStatus.ACTIVE.value,
                    revision=task_grants.c.revision + 1,
                    confirmed_at=func.now(),
                    expires_at=func.now() + ttl,
                    updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def revoke_grant(self, *, grant_id: uuid.UUID, expected_revision: int | None) -> DocumentGrantRecord | None:
        conditions = [
            task_grants.c.id == grant_id,
            task_grants.c.kind == DOCUMENT_DISCLOSE_KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(*conditions)
                .values(
                    status=GrantStatus.REVOKED.value,
                    revision=task_grants.c.revision + 1,
                    confirmed_at=func.coalesce(task_grants.c.confirmed_at, func.now()),
                    revoked_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def claim_grant(self, *, grant_id: uuid.UUID, expected_revision: int) -> DocumentGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DOCUMENT_DISCLOSE_KIND,
                    task_grants.c.revision == expected_revision,
                    task_grants.c.status == GrantStatus.ACTIVE.value,
                    and_(task_grants.c.expires_at.is_not(None), task_grants.c.expires_at > func.now()),
                )
                .values(
                    status=GrantStatus.COMPLETED.value,
                    revision=task_grants.c.revision + 1,
                    completed_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def grant_is_expired(self, grant_id: uuid.UUID) -> bool:
        row = (
            await self._connection.execute(
                select(task_grants.c.expires_at <= func.now()).where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DOCUMENT_DISCLOSE_KIND
                )
            )
        ).scalar_one_or_none()
        return bool(row)

    # ---- the disclosure and its answer ---------------------------------------------------------

    async def insert_disclosure(
        self,
        *,
        disclosure_id: uuid.UUID,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        recipient: str,
        model: str,
        projection_digest: str,
        document_count: int,
        text_bytes: int,
        redaction_count: int,
        truncated: bool,
    ) -> DocumentDisclosureRecord:
        row = (
            await self._connection.execute(
                insert(document_disclosures)
                .values(
                    id=disclosure_id,
                    task_id=task_id,
                    grant_id=grant_id,
                    recipient=recipient,
                    model=model,
                    projection_digest=projection_digest,
                    document_count=document_count,
                    text_bytes=text_bytes,
                    redaction_count=redaction_count,
                    truncated=truncated,
                    status="STARTED",
                )
                .returning(*document_disclosures.c)
            )
        ).one()
        return _disclosure(row)

    async def get_disclosure(self, disclosure_id: uuid.UUID) -> DocumentDisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(document_disclosures).where(document_disclosures.c.id == disclosure_id)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def disclosure_for_task(self, task_id: uuid.UUID) -> DocumentDisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(document_disclosures).where(document_disclosures.c.task_id == task_id)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def finish_disclosure(
        self, *, disclosure_id: uuid.UUID, status: str, error_code: str | None
    ) -> DocumentDisclosureRecord | None:
        row = (
            await self._connection.execute(
                update(document_disclosures)
                .where(document_disclosures.c.id == disclosure_id, document_disclosures.c.status == "STARTED")
                .values(status=status, error_code=error_code, finished_at=func.now())
                .returning(*document_disclosures.c)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def list_started(self, *, older_than_seconds: int | None = None) -> list[DocumentDisclosureRecord]:
        statement = select(document_disclosures).where(document_disclosures.c.status == "STARTED")
        if older_than_seconds is not None:
            statement = statement.where(
                document_disclosures.c.started_at
                < func.now() - text(f"interval '{int(older_than_seconds)} seconds'")
            )
        return [_disclosure(row) for row in (await self._connection.execute(statement)).all()]

    async def insert_answer(
        self,
        *,
        answer_id: uuid.UUID,
        task_id: uuid.UUID,
        disclosure_id: uuid.UUID,
        recipient: str,
        model: str,
        kind: str,
        summary: str | None,
        reason: str | None,
        findings: list[dict[str, Any]],
    ) -> DocumentAnswerRecord:
        row = (
            await self._connection.execute(
                insert(document_answers)
                .values(
                    id=answer_id,
                    task_id=task_id,
                    disclosure_id=disclosure_id,
                    classification=DOCUMENT_PRIVATE,
                    recipient=recipient,
                    model=model,
                    kind=kind,
                    summary=summary,
                    reason=reason,
                    findings=findings,
                )
                .returning(*document_answers.c)
            )
        ).one()
        return _answer(row)

    async def answer_for_task(self, task_id: uuid.UUID) -> DocumentAnswerRecord | None:
        row = (
            await self._connection.execute(select(document_answers).where(document_answers.c.task_id == task_id))
        ).one_or_none()
        return _answer(row) if row is not None else None
