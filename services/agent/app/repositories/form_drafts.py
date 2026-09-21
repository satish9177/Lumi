"""SQL for local form drafts (Milestone 8b S6). Callers own the transaction.

A `form_drafts` row records what Lumi *prepared or attempted* in a form -- refs,
identity hashes, approved digests and verified-local-value hashes. It is **not** a
restorable draft: the page is browser-local and is lost on any restart, and nothing
here can bring it back. No column and no query result carries a raw value, a
selector or a locator description.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import form_drafts
from app.domain.local_form_draft import LIVE_DRAFT_STATUSES, DraftFieldRecord, DraftStatus

_FIELDS = TypeAdapter(list[DraftFieldRecord])


@dataclass(frozen=True, slots=True)
class DraftRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    profile_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    dispatch_id: uuid.UUID
    manifest_digest: str
    draft_digest: str
    observation_id: uuid.UUID
    tab: str
    document_epoch: int
    form_epoch: int
    form_ref: str
    status: DraftStatus
    revision: int
    created_at: datetime
    updated_at: datetime
    fields: tuple[DraftFieldRecord, ...]

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_DRAFT_STATUSES


def _record(row: Row[Any]) -> DraftRecord:
    return DraftRecord(
        id=row.id,
        task_id=row.task_id,
        profile_id=row.profile_id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        dispatch_id=row.dispatch_id,
        manifest_digest=row.manifest_digest,
        draft_digest=row.draft_digest,
        observation_id=row.observation_id,
        tab=row.tab,
        document_epoch=row.document_epoch,
        form_epoch=row.form_epoch,
        form_ref=row.form_ref,
        status=DraftStatus(row.status),
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
        fields=tuple(_FIELDS.validate_python(row.fields)),
    )


class FormDraftRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(
        self,
        *,
        draft_id: uuid.UUID,
        task_id: uuid.UUID,
        profile_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        dispatch_id: uuid.UUID,
        manifest_digest: str,
        draft_digest: str,
        observation_id: uuid.UUID,
        tab: str,
        document_epoch: int,
        form_epoch: int,
        form_ref: str,
        status: DraftStatus,
        fields: list[DraftFieldRecord],
    ) -> DraftRecord:
        """Record a draft. The unique live-draft indexes refuse a second one."""
        row = (
            await self._connection.execute(
                insert(form_drafts)
                .values(
                    id=draft_id,
                    task_id=task_id,
                    profile_id=profile_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    dispatch_id=dispatch_id,
                    manifest_digest=manifest_digest,
                    draft_digest=draft_digest,
                    observation_id=observation_id,
                    tab=tab,
                    document_epoch=document_epoch,
                    form_epoch=form_epoch,
                    form_ref=form_ref,
                    status=status.value,
                    fields=[item.model_dump(mode="json") for item in fields],
                )
                .returning(*form_drafts.c)
            )
        ).one()
        return _record(row)

    async def get(self, draft_id: uuid.UUID) -> DraftRecord | None:
        row = (
            await self._connection.execute(select(form_drafts).where(form_drafts.c.id == draft_id))
        ).one_or_none()
        return _record(row) if row is not None else None

    async def latest_for_task(self, task_id: uuid.UUID) -> DraftRecord | None:
        row = (
            await self._connection.execute(
                select(form_drafts)
                .where(form_drafts.c.task_id == task_id)
                .order_by(form_drafts.c.created_at.desc(), form_drafts.c.id.desc())
                .limit(1)
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def live_for_profile(self, profile_id: uuid.UUID) -> DraftRecord | None:
        row = (
            await self._connection.execute(
                select(form_drafts).where(
                    form_drafts.c.profile_id == profile_id,
                    form_drafts.c.status.in_([status.value for status in LIVE_DRAFT_STATUSES]),
                )
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def live_for_task(self, task_id: uuid.UUID) -> DraftRecord | None:
        row = (
            await self._connection.execute(
                select(form_drafts).where(
                    form_drafts.c.task_id == task_id,
                    form_drafts.c.status.in_([status.value for status in LIVE_DRAFT_STATUSES]),
                )
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def transition(
        self,
        *,
        draft_id: uuid.UUID,
        expected_revision: int | None,
        allowed_from: frozenset[DraftStatus],
        to: DraftStatus,
    ) -> DraftRecord | None:
        """One compare-and-swap: only a draft in an allowed state, at the revision the
        caller saw, moves. `None` means it did not (nothing is written)."""
        conditions = [
            form_drafts.c.id == draft_id,
            form_drafts.c.status.in_([status.value for status in allowed_from]),
        ]
        if expected_revision is not None:
            conditions.append(form_drafts.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(form_drafts)
                .where(*conditions)
                .values(status=to.value, revision=form_drafts.c.revision + 1, updated_at=func.now())
                .returning(*form_drafts.c)
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def discard_all_live(self) -> list[DraftRecord]:
        """Startup: every live draft belonged to a browser that no longer exists.

        The local page is gone, so the row is closed as `DISCARDED`. It is never
        re-filled and its approval is never reused.
        """
        rows = (
            await self._connection.execute(
                update(form_drafts)
                .where(form_drafts.c.status.in_([status.value for status in LIVE_DRAFT_STATUSES]))
                .values(
                    status=DraftStatus.DISCARDED.value,
                    revision=form_drafts.c.revision + 1,
                    updated_at=func.now(),
                )
                .returning(*form_drafts.c)
            )
        ).all()
        return [_record(row) for row in rows]
