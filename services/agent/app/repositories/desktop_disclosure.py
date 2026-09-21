"""SQL for desktop disclosure grants, disclosures and private answers (Milestone 9 S2).

The single-use property is a database fact, not a convention:

* `claim_grant` is one compare-and-swap, `ACTIVE` -> `COMPLETED`, guarded by the revision the caller
  read, the status and the expiry. A second claim finds no `ACTIVE` row.
* `desktop_disclosures.grant_id` and `.task_id` are UNIQUE, so even a bug that got past the swap could
  not record a second disclosure for one approval.

Callers own the transaction. Nothing here reads or returns desktop text except `answers`, which is
the private grounded result the user asked for.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import desktop_answers, desktop_disclosures, task_grants
from app.domain.desktop_disclosure import DESKTOP_DISCLOSE_KIND, DesktopDiscloseScope
from app.domain.research import GrantStatus

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class DesktopGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: DesktopDiscloseScope
    scope_digest: str
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DisclosureRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    observation_id: uuid.UUID
    snapshot_digest: str
    recipient: str
    model: str
    projection_digest: str
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class AnswerRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    disclosure_id: uuid.UUID
    observation_id: uuid.UUID
    classification: str
    recipient: str
    model: str
    kind: str
    answer: str | None
    reason: str | None
    evidence: list[dict[str, Any]]
    created_at: datetime


def _grant(row: Row[Any]) -> DesktopGrantRecord:
    return DesktopGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=row.revision,
        policy_version=row.policy_version,
        scope=DesktopDiscloseScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        completed_at=row.completed_at,
    )


def _disclosure(row: Row[Any]) -> DisclosureRecord:
    return DisclosureRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        observation_id=row.observation_id,
        snapshot_digest=row.snapshot_digest,
        recipient=row.recipient,
        model=row.model,
        projection_digest=row.projection_digest,
        node_count=row.node_count,
        text_bytes=row.text_bytes,
        redaction_count=row.redaction_count,
        truncated=row.truncated,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )


def _answer(row: Row[Any]) -> AnswerRecord:
    return AnswerRecord(
        id=row.id,
        task_id=row.task_id,
        disclosure_id=row.disclosure_id,
        observation_id=row.observation_id,
        classification=row.classification,
        recipient=row.recipient,
        model=row.model,
        kind=row.kind,
        answer=row.answer,
        reason=row.reason,
        evidence=list(row.evidence),
        created_at=row.created_at,
    )


class DesktopDisclosureRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- the grant ------------------------------------------------------------------------

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: DesktopDiscloseScope
    ) -> DesktopGrantRecord:
        """A PENDING grant: the card's contents. It authorises nothing at all."""
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id,
                task_id=task_id,
                kind=DESKTOP_DISCLOSE_KIND,
                status=GrantStatus.PENDING.value,
                revision=1,
                policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"),
                scope_digest=scope.digest,
            )
            .returning(*task_grants.c)
        )
        return _grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> DesktopGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_DISCLOSE_KIND
                )
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> DesktopGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants)
                .where(task_grants.c.task_id == task_id, task_grants.c.kind == DESKTOP_DISCLOSE_KIND)
                .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
                .limit(1)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> DesktopGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.task_id == task_id,
                    task_grants.c.kind == DESKTOP_DISCLOSE_KIND,
                    task_grants.c.status.in_(_OPEN),
                )
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> DesktopGrantRecord | None:
        """PENDING -> ACTIVE, only at the revision and digest the card showed."""
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DESKTOP_DISCLOSE_KIND,
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

    async def close_grant(
        self, *, grant_id: uuid.UUID, status: GrantStatus, expected_revision: int | None = None
    ) -> DesktopGrantRecord | None:
        """PENDING or ACTIVE -> REVOKED. A grant that was already claimed is never touched."""
        if status is not GrantStatus.REVOKED:
            raise ValueError("close_grant only revokes a grant")
        conditions = [
            task_grants.c.id == grant_id,
            task_grants.c.kind == DESKTOP_DISCLOSE_KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(*conditions)
                .values(
                    status=status.value,
                    revision=task_grants.c.revision + 1,
                    confirmed_at=func.coalesce(task_grants.c.confirmed_at, func.now()),
                    revoked_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def claim_grant(self, *, grant_id: uuid.UUID, expected_revision: int) -> DesktopGrantRecord | None:
        """The single-use swap: ACTIVE and unexpired -> COMPLETED, exactly once."""
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DESKTOP_DISCLOSE_KIND,
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
                select(task_grants.c.expires_at <= func.now()).where(task_grants.c.id == grant_id)
            )
        ).scalar_one_or_none()
        return bool(row)

    # ---- the disclosure ---------------------------------------------------------------------

    async def insert_disclosure(
        self,
        *,
        disclosure_id: uuid.UUID,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        observation_id: uuid.UUID,
        snapshot_digest: str,
        recipient: str,
        model: str,
        projection_digest: str,
        node_count: int,
        text_bytes: int,
        redaction_count: int,
        truncated: bool,
    ) -> DisclosureRecord:
        result = await self._connection.execute(
            insert(desktop_disclosures)
            .values(
                id=disclosure_id,
                task_id=task_id,
                grant_id=grant_id,
                observation_id=observation_id,
                snapshot_digest=snapshot_digest,
                recipient=recipient,
                model=model,
                projection_digest=projection_digest,
                node_count=node_count,
                text_bytes=text_bytes,
                redaction_count=redaction_count,
                truncated=truncated,
                status="STARTED",
            )
            .returning(*desktop_disclosures.c)
        )
        return _disclosure(result.one())

    async def get_disclosure(self, disclosure_id: uuid.UUID) -> DisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_disclosures).where(desktop_disclosures.c.id == disclosure_id)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def disclosure_for_task(self, task_id: uuid.UUID) -> DisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_disclosures).where(desktop_disclosures.c.task_id == task_id)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def finish_disclosure(
        self, *, disclosure_id: uuid.UUID, status: str, error_code: str | None
    ) -> DisclosureRecord | None:
        """STARTED -> a final status, once. Anything already final is left exactly as it is."""
        row = (
            await self._connection.execute(
                update(desktop_disclosures)
                .where(desktop_disclosures.c.id == disclosure_id, desktop_disclosures.c.status == "STARTED")
                .values(status=status, error_code=error_code, finished_at=func.now())
                .returning(*desktop_disclosures.c)
            )
        ).one_or_none()
        return _disclosure(row) if row is not None else None

    async def list_started(self, *, older_than_seconds: int | None = None) -> list[DisclosureRecord]:
        statement = select(desktop_disclosures).where(desktop_disclosures.c.status == "STARTED")
        if older_than_seconds is not None:
            statement = statement.where(
                desktop_disclosures.c.started_at < func.now() - text(f"interval '{int(older_than_seconds)} seconds'")
            )
        return [_disclosure(row) for row in (await self._connection.execute(statement)).all()]

    # ---- the private answer -----------------------------------------------------------------

    async def insert_answer(
        self,
        *,
        answer_id: uuid.UUID,
        task_id: uuid.UUID,
        disclosure_id: uuid.UUID,
        observation_id: uuid.UUID,
        recipient: str,
        model: str,
        kind: str,
        answer: str | None,
        reason: str | None,
        evidence: list[dict[str, Any]],
    ) -> AnswerRecord:
        result = await self._connection.execute(
            insert(desktop_answers)
            .values(
                id=answer_id,
                task_id=task_id,
                disclosure_id=disclosure_id,
                observation_id=observation_id,
                classification="desktop_private",
                recipient=recipient,
                model=model,
                kind=kind,
                answer=answer,
                reason=reason,
                evidence=evidence,
            )
            .returning(*desktop_answers.c)
        )
        return _answer(result.one())

    async def answer_for_task(self, task_id: uuid.UUID) -> AnswerRecord | None:
        row = (
            await self._connection.execute(select(desktop_answers).where(desktop_answers.c.task_id == task_id))
        ).one_or_none()
        return _answer(row) if row is not None else None
