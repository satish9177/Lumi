"""SQL for `file_transfer` grants, their step authorizations and the transfer manifest (Milestone 10 S2).

Every grant read filters `kind = 'file_transfer'`, and the step-authorization consuming statement checks,
inside itself, that the funding grant is a `file_transfer` grant that is ACTIVE, unexpired and at the bound
revision and scope digest -- so no other grant kind can fund a transfer step and a transfer grant can fund
nothing else. Callers own the transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import file_transfers, step_authorizations, task_grants
from app.domain.research import GrantStatus
from app.domain.transfers import FILE_TRANSFER_KIND, TransferScope

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class TransferGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: TransferScope
    scope_digest: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class TransferRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    source_url: str
    source_digest: str
    source_host: str
    dest_root_id: uuid.UUID
    dest_name: str
    max_bytes: int
    status: str
    error_code: str | None
    length: int | None
    sha256: str | None
    kind: str | None
    content_type: str | None
    quarantine_volume: int | None
    quarantine_index: int | None
    placed_volume: int | None
    placed_index: int | None
    download_action_id: uuid.UUID | None
    place_action_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    quarantined_at: datetime | None
    placed_at: datetime | None
    cleaned_at: datetime | None


def _u64(value: int | None) -> int | None:
    return None if value is None else int(value) & 0xFFFFFFFFFFFFFFFF


def _i64(value: int | None) -> int | None:
    if value is None:
        return None
    value = int(value) & 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _grant(row: Row[Any]) -> TransferGrantRecord:
    return TransferGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=int(row.revision),
        policy_version=row.policy_version,
        scope=TransferScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        expires_at=row.expires_at,
    )


def _transfer(row: Row[Any]) -> TransferRecord:
    return TransferRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        source_url=row.source_url,
        source_digest=row.source_digest,
        source_host=row.source_host,
        dest_root_id=row.dest_root_id,
        dest_name=row.dest_name,
        max_bytes=int(row.max_bytes),
        status=row.status,
        error_code=row.error_code,
        length=row.length,
        sha256=row.sha256,
        kind=row.kind,
        content_type=row.content_type,
        quarantine_volume=_u64(row.quarantine_volume),
        quarantine_index=None if row.quarantine_index is None else int(row.quarantine_index),
        placed_volume=_u64(row.placed_volume),
        placed_index=None if row.placed_index is None else int(row.placed_index),
        download_action_id=row.download_action_id,
        place_action_id=row.place_action_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        quarantined_at=row.quarantined_at,
        placed_at=row.placed_at,
        cleaned_at=row.cleaned_at,
    )


class TransferRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- the grant -----------------------------------------------------------------------------

    async def insert_grant(self, *, grant_id: uuid.UUID, scope: TransferScope) -> TransferGrantRecord:
        row = (
            await self._connection.execute(
                insert(task_grants)
                .values(
                    id=grant_id,
                    task_id=scope.task_id,
                    kind=FILE_TRANSFER_KIND,
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

    async def get_grant(self, grant_id: uuid.UUID) -> TransferGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(task_grants.c.id == grant_id, task_grants.c.kind == FILE_TRANSFER_KIND)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> TransferGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == FILE_TRANSFER_KIND,
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
    ) -> TransferGrantRecord | None:
        """PENDING/ACTIVE -> REVOKED (a person declined or stopped) or ACTIVE -> COMPLETED (both steps done)."""
        conditions = [task_grants.c.id == grant_id, task_grants.c.kind == FILE_TRANSFER_KIND]
        if status is GrantStatus.REVOKED:
            conditions.append(task_grants.c.status.in_(_OPEN))
            values: dict[str, Any] = {
                "revoked_at": func.now(),
                "confirmed_at": func.coalesce(task_grants.c.confirmed_at, func.now()),
            }
        elif status is GrantStatus.COMPLETED:
            conditions.append(task_grants.c.status == GrantStatus.ACTIVE.value)
            values = {"completed_at": func.now()}
        else:  # pragma: no cover - programming error
            raise ValueError("unsupported grant transition")
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(*conditions)
                .values(status=status.value, revision=task_grants.c.revision + 1, updated_at=func.now(), **values)
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def grant_is_usable(self, grant_id: uuid.UUID) -> bool:
        row = (
            await self._connection.execute(
                select(task_grants.c.id).where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == FILE_TRANSFER_KIND,
                    task_grants.c.status == GrantStatus.ACTIVE.value,
                    task_grants.c.expires_at > func.now(),
                )
            )
        ).one_or_none()
        return row is not None

    # ---- step authorizations -----------------------------------------------------------------

    async def insert_step_authorization(
        self,
        *,
        grant: TransferGrantRecord,
        action_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
        runtime_generation: uuid.UUID,
        ttl: timedelta,
    ) -> uuid.UUID:
        authorization_id = uuid.uuid4()
        await self._connection.execute(
            insert(step_authorizations).values(
                id=authorization_id,
                grant_id=grant.id,
                grant_revision=grant.revision,
                task_id=grant.task_id,
                action_id=action_id,
                action_revision=action_revision,
                proposal_digest=proposal_digest,
                scope_digest=grant.scope_digest,
                policy_version=grant.policy_version,
                runtime_generation=runtime_generation,
                expires_at=func.now() + ttl,
            )
        )
        return authorization_id

    async def consume_step_authorization(
        self, *, authorization_id: uuid.UUID, action_revision: int, proposal_digest: str, runtime_generation: uuid.UUID
    ) -> bool:
        funded = (
            select(task_grants.c.id)
            .where(
                task_grants.c.id == step_authorizations.c.grant_id,
                task_grants.c.kind == FILE_TRANSFER_KIND,
                task_grants.c.status == GrantStatus.ACTIVE.value,
                task_grants.c.revision == step_authorizations.c.grant_revision,
                task_grants.c.scope_digest == step_authorizations.c.scope_digest,
                or_(task_grants.c.expires_at.is_(None), task_grants.c.expires_at > func.now()),
            )
            .exists()
        )
        row = (
            await self._connection.execute(
                update(step_authorizations)
                .where(
                    step_authorizations.c.id == authorization_id,
                    step_authorizations.c.consumed_at.is_(None),
                    step_authorizations.c.action_revision == action_revision,
                    step_authorizations.c.proposal_digest == proposal_digest,
                    step_authorizations.c.runtime_generation == runtime_generation,
                    step_authorizations.c.expires_at > func.now(),
                    funded,
                )
                .values(consumed_at=func.now())
                .returning(step_authorizations.c.id)
            )
        ).one_or_none()
        return row is not None

    # ---- the manifest ---------------------------------------------------------------------------

    async def insert_transfer(self, *, scope: TransferScope, grant_id: uuid.UUID, source_digest: str, source_host: str) -> TransferRecord:
        row = (
            await self._connection.execute(
                insert(file_transfers)
                .values(
                    id=scope.transfer_id,
                    task_id=scope.task_id,
                    grant_id=grant_id,
                    source_url=scope.source_url,
                    source_digest=source_digest,
                    source_host=source_host,
                    dest_root_id=scope.dest_root_id,
                    dest_name=scope.dest_name,
                    max_bytes=scope.max_bytes,
                    status="PENDING",
                )
                .returning(*file_transfers.c)
            )
        ).one()
        return _transfer(row)

    async def get(self, transfer_id: uuid.UUID, *, lock: bool = False) -> TransferRecord | None:
        statement = select(file_transfers).where(file_transfers.c.id == transfer_id)
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _transfer(row) if row is not None else None

    async def for_task(self, task_id: uuid.UUID) -> TransferRecord | None:
        row = (await self._connection.execute(select(file_transfers).where(file_transfers.c.task_id == task_id))).one_or_none()
        return _transfer(row) if row is not None else None

    async def update(self, transfer_id: uuid.UUID, **values: Any) -> TransferRecord:
        for key in ("quarantine_volume", "placed_volume"):
            if key in values:
                values[key] = _i64(values[key])
        for key in ("quarantine_index", "placed_index"):
            if key in values and values[key] is not None:
                values[key] = str(values[key])
        row = (
            await self._connection.execute(
                update(file_transfers)
                .where(file_transfers.c.id == transfer_id)
                .values(updated_at=func.now(), **values)
                .returning(*file_transfers.c)
            )
        ).one()
        return _transfer(row)

    async def cleanable(self, *, older_than: timedelta) -> list[TransferRecord]:
        """Transfers whose quarantine directory may go, once old enough: placed, failed or cancelled, or
        quarantined under a grant that can no longer place it (revoked while downloading, expired). OUTCOME_UNKNOWN
        ones are evidence and are kept (S2 review finding 6)."""
        closed_grant = select(task_grants.c.id).where(
            task_grants.c.id == file_transfers.c.grant_id,
            task_grants.c.status.in_((GrantStatus.REVOKED.value, GrantStatus.EXPIRED.value, GrantStatus.COMPLETED.value)),
        ).exists()
        expired_grant = select(task_grants.c.id).where(
            task_grants.c.id == file_transfers.c.grant_id, task_grants.c.expires_at < func.now()
        ).exists()
        rows = await self._connection.execute(
            select(file_transfers).where(
                file_transfers.c.cleaned_at.is_(None),
                or_(
                    file_transfers.c.status.in_(("PLACED", "FAILED", "CANCELLED")),
                    and_(file_transfers.c.status == "QUARANTINED", or_(closed_grant, expired_grant)),
                ),
                and_(file_transfers.c.updated_at < func.now() - older_than),
            )
        )
        return [_transfer(row) for row in rows]
