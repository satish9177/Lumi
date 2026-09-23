"""SQL for Milestone 10 S4 workflows: lineage, candidates and workflow-scoped protected values.

Callers own the transaction. Like `ProtectedValueRepository`, exactly one method here returns a raw adopted
value -- `values_for_execution`, for one approved, frozen local draft. Every other read of a value returns
its kind, preview, digest and length. A candidate's text is returned only to the trusted view (it is the
person's own document text, which the document view already shows).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import workflow_candidates, workflow_steps, workflow_values, workflows
from app.domain.form_prepare import ProtectedSnapshot
from app.domain.workflows import WORKFLOW_KIND


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    id: uuid.UUID
    status: str
    objective: str
    revision: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    stopped_at: datetime | None
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class StepRecord:
    workflow_id: uuid.UUID
    task_id: uuid.UUID
    role: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    id: uuid.UUID
    workflow_id: uuid.UUID
    source_task_id: uuid.UUID
    document_id: uuid.UUID
    document_text_sha256: str
    kind: str
    provenance: str
    #: The candidate's own text (document text). None once purged.
    value: str | None
    value_digest: str
    preview: str
    span_start: int
    span_end: int
    disclosure_id: uuid.UUID | None
    projection_digest: str | None
    doc_ref: str | None
    quote: str | None
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValueRecord:
    """An adopted workflow value as everything but its row may see it. Never the value."""

    id: uuid.UUID
    workflow_id: uuid.UUID
    candidate_id: uuid.UUID
    adopt_action_id: uuid.UUID
    kind: str
    provenance: str
    value_digest: str
    preview: str
    created_at: datetime
    purged: bool


def _workflow(row: Row[Any]) -> WorkflowRecord:
    return WorkflowRecord(
        id=row.id,
        status=row.status,
        objective=row.objective,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
        stopped_at=row.stopped_at,
        stop_reason=row.stop_reason,
    )


def _candidate(row: Row[Any]) -> CandidateRecord:
    return CandidateRecord(
        id=row.id,
        workflow_id=row.workflow_id,
        source_task_id=row.source_task_id,
        document_id=row.document_id,
        document_text_sha256=row.document_text_sha256,
        kind=row.kind,
        provenance=row.provenance,
        value=row.value,
        value_digest=row.value_digest,
        preview=row.preview,
        span_start=row.span_start,
        span_end=row.span_end,
        disclosure_id=row.disclosure_id,
        projection_digest=row.projection_digest,
        doc_ref=row.doc_ref,
        quote=row.quote,
        status=row.status,
        created_at=row.created_at,
    )


_VALUE_COLUMNS = (
    workflow_values.c.id,
    workflow_values.c.workflow_id,
    workflow_values.c.candidate_id,
    workflow_values.c.adopt_action_id,
    workflow_values.c.kind,
    workflow_values.c.provenance,
    workflow_values.c.value_digest,
    workflow_values.c.preview,
    workflow_values.c.created_at,
    workflow_values.c.purged_at,
)


def _value(row: Row[Any]) -> ValueRecord:
    return ValueRecord(
        id=row.id,
        workflow_id=row.workflow_id,
        candidate_id=row.candidate_id,
        adopt_action_id=row.adopt_action_id,
        kind=row.kind,
        provenance=row.provenance,
        value_digest=row.value_digest,
        preview=row.preview,
        created_at=row.created_at,
        purged=row.purged_at is not None,
    )


def workflow_is_live() -> Any:
    """SQL: the workflow row is ACTIVE and unexpired. The database's clock decides."""
    return and_(workflows.c.status == "ACTIVE", workflows.c.expires_at > func.now())


class WorkflowRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- workflows ------------------------------------------------------------------------------

    async def insert(self, *, workflow_id: uuid.UUID, objective: str, ttl: timedelta) -> WorkflowRecord:
        result = await self._connection.execute(
            insert(workflows)
            .values(
                id=workflow_id,
                kind=WORKFLOW_KIND,
                status="ACTIVE",
                objective=objective,
                revision=1,
                expires_at=func.now() + ttl,
            )
            .returning(*workflows.c)
        )
        return _workflow(result.one())

    async def get(self, workflow_id: uuid.UUID, *, lock: bool = False) -> WorkflowRecord | None:
        statement = select(workflows).where(workflows.c.id == workflow_id)
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _workflow(row) if row is not None else None

    async def is_live(self, workflow_id: uuid.UUID) -> bool:
        found = await self._connection.scalar(
            select(workflows.c.id).where(workflows.c.id == workflow_id, workflow_is_live())
        )
        return found is not None

    async def latest(self) -> WorkflowRecord | None:
        row = (
            await self._connection.execute(
                select(workflows).order_by(workflows.c.created_at.desc(), workflows.c.id).limit(1)
            )
        ).one_or_none()
        return _workflow(row) if row is not None else None

    async def bump(self, workflow_id: uuid.UUID) -> WorkflowRecord | None:
        result = await self._connection.execute(
            update(workflows)
            .where(workflows.c.id == workflow_id)
            .values(revision=workflows.c.revision + 1, updated_at=func.now())
            .returning(*workflows.c)
        )
        row = result.one_or_none()
        return _workflow(row) if row is not None else None

    async def stop(self, workflow_id: uuid.UUID, *, reason: str) -> WorkflowRecord | None:
        result = await self._connection.execute(
            update(workflows)
            .where(workflows.c.id == workflow_id, workflows.c.status == "ACTIVE")
            .values(
                status="STOPPED",
                stopped_at=func.now(),
                stop_reason=reason,
                revision=workflows.c.revision + 1,
                updated_at=func.now(),
            )
            .returning(*workflows.c)
        )
        row = result.one_or_none()
        return _workflow(row) if row is not None else None

    # ---- steps ----------------------------------------------------------------------------------

    async def insert_step(self, *, workflow_id: uuid.UUID, task_id: uuid.UUID, role: str) -> StepRecord:
        result = await self._connection.execute(
            insert(workflow_steps)
            .values(id=uuid.uuid4(), workflow_id=workflow_id, task_id=task_id, role=role)
            .returning(*workflow_steps.c)
        )
        row = result.one()
        return StepRecord(workflow_id=row.workflow_id, task_id=row.task_id, role=row.role, created_at=row.created_at)

    async def steps(self, workflow_id: uuid.UUID) -> list[StepRecord]:
        result = await self._connection.execute(
            select(workflow_steps).where(workflow_steps.c.workflow_id == workflow_id).order_by(workflow_steps.c.created_at)
        )
        return [
            StepRecord(workflow_id=row.workflow_id, task_id=row.task_id, role=row.role, created_at=row.created_at)
            for row in result
        ]

    async def step(self, workflow_id: uuid.UUID, role: str) -> StepRecord | None:
        row = (
            await self._connection.execute(
                select(workflow_steps).where(workflow_steps.c.workflow_id == workflow_id, workflow_steps.c.role == role)
            )
        ).one_or_none()
        return (
            StepRecord(workflow_id=row.workflow_id, task_id=row.task_id, role=row.role, created_at=row.created_at)
            if row is not None
            else None
        )

    async def step_for_task(self, task_id: uuid.UUID) -> StepRecord | None:
        row = (
            await self._connection.execute(select(workflow_steps).where(workflow_steps.c.task_id == task_id))
        ).one_or_none()
        return (
            StepRecord(workflow_id=row.workflow_id, task_id=row.task_id, role=row.role, created_at=row.created_at)
            if row is not None
            else None
        )

    # ---- candidates -----------------------------------------------------------------------------

    async def insert_candidate(self, **values: Any) -> CandidateRecord | None:
        """Insert one candidate, or None if the same (document, provenance, kind, value) already exists."""
        result = await self._connection.execute(
            pg_insert(workflow_candidates)
            .values(id=uuid.uuid4(), source_role="documents", status="PROPOSED", **values)
            .on_conflict_do_nothing(constraint="uq_workflow_candidates_dedupe")
            .returning(*workflow_candidates.c)
        )
        row = result.one_or_none()
        return _candidate(row) if row is not None else None

    async def candidates(self, workflow_id: uuid.UUID) -> list[CandidateRecord]:
        result = await self._connection.execute(
            select(workflow_candidates)
            .where(workflow_candidates.c.workflow_id == workflow_id)
            .order_by(workflow_candidates.c.created_at, workflow_candidates.c.id)
        )
        return [_candidate(row) for row in result]

    async def count_candidates(self, workflow_id: uuid.UUID) -> int:
        return int(
            await self._connection.scalar(
                select(func.count()).select_from(workflow_candidates).where(workflow_candidates.c.workflow_id == workflow_id)
            )
            or 0
        )

    async def get_candidate(self, candidate_id: uuid.UUID, *, lock: bool = False) -> CandidateRecord | None:
        statement = select(workflow_candidates).where(workflow_candidates.c.id == candidate_id)
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _candidate(row) if row is not None else None

    async def mark_candidate(self, candidate_id: uuid.UUID, *, status: str, expected: str = "PROPOSED") -> bool:
        result = await self._connection.execute(
            update(workflow_candidates)
            .where(workflow_candidates.c.id == candidate_id, workflow_candidates.c.status == expected)
            .values(status=status)
            .returning(workflow_candidates.c.id)
        )
        return result.one_or_none() is not None

    # ---- values ---------------------------------------------------------------------------------

    async def insert_value(
        self, *, candidate: CandidateRecord, canonical: str, adopt_action_id: uuid.UUID
    ) -> ValueRecord:
        """The adopted value. Kind, digest and provenance come from the candidate row -- and the composite
        foreign key refuses any other combination, whatever a caller passes."""
        result = await self._connection.execute(
            insert(workflow_values)
            .values(
                id=uuid.uuid4(),
                workflow_id=candidate.workflow_id,
                candidate_id=candidate.id,
                adopt_action_id=adopt_action_id,
                kind=candidate.kind,
                provenance=candidate.provenance,
                value=canonical,
                value_digest=candidate.value_digest,
                preview=candidate.preview,
            )
            .returning(*_VALUE_COLUMNS)
        )
        return _value(result.one())

    async def values(self, workflow_id: uuid.UUID) -> list[ValueRecord]:
        result = await self._connection.execute(
            select(*_VALUE_COLUMNS).where(workflow_values.c.workflow_id == workflow_id).order_by(workflow_values.c.created_at)
        )
        return [_value(row) for row in result]

    async def value_for_kind(self, workflow_id: uuid.UUID, kind: str) -> ValueRecord | None:
        row = (
            await self._connection.execute(
                select(*_VALUE_COLUMNS).where(workflow_values.c.workflow_id == workflow_id, workflow_values.c.kind == kind)
            )
        ).one_or_none()
        return _value(row) if row is not None else None

    async def snapshots(
        self, workflow_id: uuid.UUID, kinds: Iterable[str], *, lock: bool = False
    ) -> dict[str, ProtectedSnapshot]:
        """Digest, preview and length of the live adopted values of `kinds` in this LIVE workflow. Never the
        value. A stopped or expired workflow, or a purged value, contributes nothing."""
        statement = (
            select(
                workflow_values.c.kind,
                workflow_values.c.value_digest,
                workflow_values.c.preview,
                func.length(workflow_values.c.value).label("length"),
            )
            .join(workflows, workflows.c.id == workflow_values.c.workflow_id)
            .where(
                workflow_values.c.workflow_id == workflow_id,
                workflow_values.c.kind.in_(list(kinds)),
                workflow_values.c.purged_at.is_(None),
                workflow_is_live(),
            )
        )
        if lock:
            statement = statement.with_for_update(read=True, of=workflow_values)
        result = await self._connection.execute(statement)
        return {
            row.kind: ProtectedSnapshot(kind=row.kind, value_digest=row.value_digest, preview=row.preview, length=row.length)
            for row in result
        }

    async def values_for_execution(self, workflow_id: uuid.UUID, kinds: Iterable[str]) -> dict[str, str]:
        """The raw adopted values of `kinds`, share-locked, **for one approved local draft** in a live workflow.

        The only method in this layer that returns an adopted value. The caller holds it in memory only,
        verifies its digest against the approved manifest before using it, and never persists or returns it.
        """
        result = await self._connection.execute(
            select(workflow_values.c.kind, workflow_values.c.value)
            .join(workflows, workflows.c.id == workflow_values.c.workflow_id)
            .where(
                workflow_values.c.workflow_id == workflow_id,
                workflow_values.c.kind.in_(list(kinds)),
                workflow_values.c.purged_at.is_(None),
                workflow_is_live(),
            )
            .with_for_update(read=True, of=workflow_values)
        )
        return {row.kind: row.value for row in result if row.value is not None}

    # ---- retention ------------------------------------------------------------------------------

    async def purge(self, *, workflow_id: uuid.UUID | None = None) -> int:
        """Drop the text of every candidate and value of a stopped or expired workflow (or of one workflow).
        Digests, previews and provenance stay as the audit record."""
        dead = select(workflows.c.id).where(or_(workflows.c.status != "ACTIVE", workflows.c.expires_at <= func.now()))
        if workflow_id is not None:
            dead = dead.where(workflows.c.id == workflow_id)
        purged = 0
        for table in (workflow_candidates, workflow_values):
            # A provider quote is document text too (S4 review finding 2): it goes with the value.
            cleared: dict[str, Any] = {"value": None, "purged_at": func.now()}
            if table is workflow_candidates:
                cleared["quote"] = None
            result = await self._connection.execute(
                update(table)
                .where(table.c.workflow_id.in_(dead), table.c.purged_at.is_(None))
                .values(**cleared)
                .returning(table.c.id)
            )
            purged += len(result.all())
        return purged


__all__ = [
    "CandidateRecord",
    "StepRecord",
    "ValueRecord",
    "WorkflowRecord",
    "WorkflowRepository",
    "workflow_is_live",
]
