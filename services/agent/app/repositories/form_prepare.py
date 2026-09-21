"""SQL for saved details and form-planning grants (Milestone 8b S5).

Callers own the transaction, as everywhere else in this layer.

Two things are load-bearing here:

* `ProtectedValueRepository` returns a saved value from exactly one method,
  `values_for_execution` (Milestone 8b S6), which exists so the trusted runtime can
  hold the *exact approved bytes* in memory for one frozen local draft. Every other
  read returns only `kind`, `preview`, the digest and a length. In S5 the value did
  not leave its row; in S6 an approved value may flow only through trusted runtime
  and worker memory into the locally frozen browser -- never into a query result
  shown to a model, a renderer, a log line, a task event or a ledger row.
* `FormPrepareRepository.confirm_grant` is one compare-and-swap that also checks,
  inside the statement, that the profile is still the one the card showed **and**
  that the account-reading grant the planning grew out of is still active. There
  is no SELECT-decide-UPDATE window.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, Uuid, and_, cast, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import protected_values, task_grants
from app.domain.form_prepare import FORM_PREPARE_KIND, FormPrepareScope, ProtectedSnapshot
from app.domain.research import GrantStatus
from app.repositories.authenticated import profile_still_matches_grant

_OPEN = [GrantStatus.PENDING.value, GrantStatus.ACTIVE.value]


@dataclass(frozen=True, slots=True)
class SavedDetail:
    """What a saved detail looks like to everything except the row itself."""

    kind: str
    preview: str
    updated_at: datetime


class ProtectedValueRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def upsert(
        self, *, kind: str, canonical: str, digest: str, preview: str
    ) -> SavedDetail:
        """Replace the one value of this kind. Value, digest and preview move together."""
        insert_statement = pg_insert(protected_values).values(
            id=uuid.uuid4(), kind=kind, value=canonical, value_digest=digest, preview=preview
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[protected_values.c.kind],
            set_={
                "value": insert_statement.excluded.value,
                "value_digest": insert_statement.excluded.value_digest,
                "preview": insert_statement.excluded.preview,
                "updated_at": func.now(),
            },
        ).returning(protected_values.c.kind, protected_values.c.preview, protected_values.c.updated_at)
        row = (await self._connection.execute(statement)).one()
        return SavedDetail(kind=row.kind, preview=row.preview, updated_at=row.updated_at)

    async def list_details(self) -> list[SavedDetail]:
        result = await self._connection.execute(
            select(protected_values.c.kind, protected_values.c.preview, protected_values.c.updated_at)
        )
        return [SavedDetail(kind=row.kind, preview=row.preview, updated_at=row.updated_at) for row in result]

    async def values_for_execution(self, kinds: Iterable[str]) -> dict[str, str]:
        """The raw saved values of `kinds`, share-locked, **for one approved local draft**.

        The only method in this layer that returns a value. The caller must hold it in
        memory only, verify its digest against the approved manifest before using it,
        and never persist, log, echo or return it.
        """
        result = await self._connection.execute(
            select(protected_values.c.kind, protected_values.c.value)
            .where(protected_values.c.kind.in_(list(kinds)))
            .with_for_update(read=True)
        )
        return {row.kind: row.value for row in result}

    async def snapshots(
        self, kinds: Iterable[str], *, lock: bool = False
    ) -> dict[str, ProtectedSnapshot]:
        """Digest, preview and length for the kinds that exist. **Never the value.**

        `lock=True` takes a share lock so a concurrent update of the same rows
        waits for the transaction that is deciding on them.
        """
        statement = select(
            protected_values.c.kind,
            protected_values.c.value_digest,
            protected_values.c.preview,
            func.length(protected_values.c.value).label("length"),
        ).where(protected_values.c.kind.in_(list(kinds)))
        if lock:
            statement = statement.with_for_update(read=True)
        result = await self._connection.execute(statement)
        return {
            row.kind: ProtectedSnapshot(
                kind=row.kind, value_digest=row.value_digest, preview=row.preview, length=row.length
            )
            for row in result
        }


@dataclass(frozen=True, slots=True)
class FormPrepareGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: FormPrepareScope
    scope_digest: str
    profile_id: uuid.UUID
    profile_revoke_epoch: int
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @property
    def kind(self) -> str:
        return FORM_PREPARE_KIND


def _grant(row: Row[Any]) -> FormPrepareGrantRecord:
    return FormPrepareGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=row.revision,
        policy_version=row.policy_version,
        scope=FormPrepareScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        profile_id=row.profile_id,
        profile_revoke_epoch=row.profile_revoke_epoch,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _source_grant_usable(*, statuses: tuple[str, ...]) -> Any:
    """The account-reading grant this planning grew out of is still in `statuses`."""
    source = task_grants.alias("source_grant")
    return (
        select(source.c.id)
        .where(
            source.c.id == cast(task_grants.c.scope["source_authenticated_grant_id"].astext, Uuid()),
            source.c.kind == "authenticated_read",
            source.c.task_id == task_grants.c.task_id,
            source.c.profile_id == task_grants.c.profile_id,
            source.c.profile_revoke_epoch == task_grants.c.profile_revoke_epoch,
            source.c.status.in_(statuses),
            or_(source.c.status != GrantStatus.ACTIVE.value, source.c.expires_at > func.now()),
        )
        .exists()
    )


class FormPrepareRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: FormPrepareScope
    ) -> FormPrepareGrantRecord:
        """A PENDING grant: the card's contents. It authorises nothing at all."""
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id,
                task_id=task_id,
                kind=FORM_PREPARE_KIND,
                status=GrantStatus.PENDING.value,
                revision=1,
                policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"),
                scope_digest=scope.digest,
                profile_id=scope.profile_id,
                profile_revoke_epoch=scope.profile_revoke_epoch,
            )
            .returning(*task_grants.c)
        )
        return _grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> FormPrepareGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants).where(
                task_grants.c.id == grant_id, task_grants.c.kind == FORM_PREPARE_KIND
            )
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> FormPrepareGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants).where(
                task_grants.c.task_id == task_id,
                task_grants.c.kind == FORM_PREPARE_KIND,
                task_grants.c.status.in_(_OPEN),
            )
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> FormPrepareGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants)
            .where(task_grants.c.task_id == task_id, task_grants.c.kind == FORM_PREPARE_KIND)
            .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> FormPrepareGrantRecord | None:
        """PENDING -> ACTIVE, only for the profile the card showed and a live source grant."""
        result = await self._connection.execute(
            update(task_grants)
            .where(
                task_grants.c.id == grant_id,
                task_grants.c.kind == FORM_PREPARE_KIND,
                task_grants.c.revision == expected_revision,
                task_grants.c.status == GrantStatus.PENDING.value,
                task_grants.c.scope_digest == scope_digest,
                profile_still_matches_grant(),
                _source_grant_usable(statuses=(GrantStatus.ACTIVE.value,)),
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
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def close_grant(
        self, *, grant_id: uuid.UUID, status: GrantStatus, expected_revision: int | None = None
    ) -> FormPrepareGrantRecord | None:
        if status not in (GrantStatus.REVOKED, GrantStatus.EXPIRED, GrantStatus.COMPLETED):
            raise ValueError("close_grant only closes a grant")
        conditions = [
            task_grants.c.id == grant_id,
            task_grants.c.kind == FORM_PREPARE_KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        values: dict[str, Any] = {
            "status": status.value,
            "revision": task_grants.c.revision + 1,
            "updated_at": func.now(),
            "confirmed_at": func.coalesce(task_grants.c.confirmed_at, func.now()),
        }
        if status is GrantStatus.REVOKED:
            values["revoked_at"] = func.now()
        if status is GrantStatus.COMPLETED:
            values["completed_at"] = func.now()
        result = await self._connection.execute(
            update(task_grants).where(*conditions).values(**values).returning(*task_grants.c)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def usable_now(self, grant_id: uuid.UUID) -> bool:
        """Is this grant ACTIVE, unexpired, on the same account and backed by its source?

        One statement, so the database -- not an earlier read -- decides. The
        source may be ACTIVE or already COMPLETED (an answer closes it), never
        revoked or expired.
        """
        found = await self._connection.scalar(
            select(task_grants.c.id).where(
                task_grants.c.id == grant_id,
                task_grants.c.kind == FORM_PREPARE_KIND,
                task_grants.c.status == GrantStatus.ACTIVE.value,
                and_(task_grants.c.expires_at.is_not(None), task_grants.c.expires_at > func.now()),
                profile_still_matches_grant(),
                _source_grant_usable(
                    statuses=(GrantStatus.ACTIVE.value, GrantStatus.COMPLETED.value)
                ),
            )
        )
        return found is not None

    async def grant_is_expired(self, grant_id: uuid.UUID) -> bool:
        return bool(
            await self._connection.scalar(
                select(
                    and_(
                        task_grants.c.expires_at.is_not(None),
                        task_grants.c.expires_at <= func.now(),
                    )
                ).where(task_grants.c.id == grant_id)
            )
        )


__all__ = [
    "FormPrepareGrantRecord",
    "FormPrepareRepository",
    "ProtectedValueRepository",
    "SavedDetail",
]
