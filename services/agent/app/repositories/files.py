"""SQL for M10 file roots and task-owned file refs (Milestone 10 S1).

Callers own the transaction. Two records are returned per row shape: the *internal* record (with the
controller-local path, for the broker only) and nothing else -- the API layer builds its own safe view
and never receives a path from here unless it explicitly asks for `canonical_path` / `local_path`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import file_refs, file_roots


@dataclass(frozen=True, slots=True)
class FileRootRecord:
    id: uuid.UUID
    label: str
    canonical_path: str
    volume_serial: int
    dir_index: int
    can_read: bool
    can_create: bool
    can_modify: bool
    status: str
    revision: int
    created_at: datetime
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.status == "ACTIVE"


@dataclass(frozen=True, slots=True)
class FileRefRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    source: str
    root_id: uuid.UUID | None
    relative_path: str | None
    local_path: str | None
    display_name: str
    format: str
    volume_serial: int
    file_index: int
    size_bytes: int
    mtime_ns: int
    sha256: str
    created_at: datetime


def _root(row: Row[Any]) -> FileRootRecord:
    return FileRootRecord(
        id=row.id,
        label=row.label,
        canonical_path=row.canonical_path,
        volume_serial=_unsigned(row.volume_serial),
        dir_index=int(row.dir_index),
        can_read=row.can_read,
        can_create=row.can_create,
        can_modify=row.can_modify,
        status=row.status,
        revision=int(row.revision),
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def _ref(row: Row[Any]) -> FileRefRecord:
    return FileRefRecord(
        id=row.id,
        task_id=row.task_id,
        source=row.source,
        root_id=row.root_id,
        relative_path=row.relative_path,
        local_path=row.local_path,
        display_name=row.display_name,
        format=row.format,
        volume_serial=_unsigned(row.volume_serial),
        file_index=int(row.file_index),
        size_bytes=int(row.size_bytes),
        mtime_ns=int(row.mtime_ns),
        sha256=row.sha256,
        created_at=row.created_at,
    )


def _signed(value: int) -> int:
    """Windows reports a 64-bit *unsigned* volume serial; BIGINT is signed. Stored two's-complement, exactly."""
    value = int(value) & 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _unsigned(value: int) -> int:
    return int(value) & 0xFFFFFFFFFFFFFFFF


class FileRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- roots ---------------------------------------------------------------------------------

    async def insert_root(
        self,
        *,
        root_id: uuid.UUID,
        label: str,
        canonical_path: str,
        path_key: str,
        volume_serial: int,
        dir_index: int,
        can_read: bool,
        can_create: bool,
    ) -> FileRootRecord:
        row = (
            await self._connection.execute(
                insert(file_roots)
                .values(
                    id=root_id,
                    label=label,
                    canonical_path=canonical_path,
                    path_key=path_key,
                    volume_serial=_signed(volume_serial),
                    dir_index=str(dir_index),
                    can_read=can_read,
                    can_create=can_create,
                    can_modify=False,
                    status="ACTIVE",
                    revision=1,
                )
                .returning(*file_roots.c)
            )
        ).one()
        return _root(row)

    async def active_root_for_key(self, path_key: str) -> FileRootRecord | None:
        row = (
            await self._connection.execute(
                select(file_roots).where(file_roots.c.path_key == path_key, file_roots.c.status == "ACTIVE")
            )
        ).one_or_none()
        return _root(row) if row is not None else None

    async def get_root(self, root_id: uuid.UUID, *, lock: bool = False) -> FileRootRecord | None:
        statement = select(file_roots).where(file_roots.c.id == root_id)
        if lock:
            statement = statement.with_for_update(read=True)
        row = (await self._connection.execute(statement)).one_or_none()
        return _root(row) if row is not None else None

    async def list_roots(self) -> list[FileRootRecord]:
        rows = await self._connection.execute(
            select(file_roots).where(file_roots.c.status == "ACTIVE").order_by(file_roots.c.created_at, file_roots.c.id)
        )
        return [_root(row) for row in rows]

    async def revoke_root(self, root_id: uuid.UUID, *, expected_revision: int | None) -> FileRootRecord | None:
        conditions = [file_roots.c.id == root_id, file_roots.c.status == "ACTIVE"]
        if expected_revision is not None:
            conditions.append(file_roots.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(file_roots)
                .where(*conditions)
                .values(
                    status="REVOKED", revoked_at=func.now(), updated_at=func.now(), revision=file_roots.c.revision + 1
                )
                .returning(*file_roots.c)
            )
        ).one_or_none()
        return _root(row) if row is not None else None

    # ---- file refs -----------------------------------------------------------------------------

    async def insert_ref(
        self,
        *,
        ref_id: uuid.UUID,
        task_id: uuid.UUID,
        source: str,
        root_id: uuid.UUID | None,
        relative_path: str | None,
        local_path: str | None,
        display_name: str,
        format: str,
        volume_serial: int,
        file_index: int,
        size_bytes: int,
        mtime_ns: int,
        sha256: str,
    ) -> FileRefRecord:
        row = (
            await self._connection.execute(
                insert(file_refs)
                .values(
                    id=ref_id,
                    task_id=task_id,
                    source=source,
                    root_id=root_id,
                    relative_path=relative_path,
                    local_path=local_path,
                    display_name=display_name,
                    format=format,
                    volume_serial=_signed(volume_serial),
                    file_index=str(file_index),
                    size_bytes=size_bytes,
                    mtime_ns=mtime_ns,
                    sha256=sha256,
                )
                .returning(*file_refs.c)
            )
        ).one()
        return _ref(row)

    async def get_ref(self, ref_id: uuid.UUID) -> FileRefRecord | None:
        row = (await self._connection.execute(select(file_refs).where(file_refs.c.id == ref_id))).one_or_none()
        return _ref(row) if row is not None else None

    async def refs_for_task(self, task_id: uuid.UUID) -> list[FileRefRecord]:
        rows = await self._connection.execute(
            select(file_refs).where(file_refs.c.task_id == task_id).order_by(file_refs.c.created_at, file_refs.c.id)
        )
        return [_ref(row) for row in rows]
