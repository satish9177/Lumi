"""SQL for Milestone 12 S1's trusted orchestration resource-ref registry. Callers own the transaction,
exactly like every other repository in this layer.

Every lookup is scoped to `(orchestration_id, ref)`: there is no method here that resolves a ref by itself,
so a ref from another orchestration -- even one that happens to use the same opaque string, e.g. both
orchestrations' first resource being `r1` -- cannot resolve against the wrong owner. That is not a filter
applied after the fact; it is the only key `get()` accepts.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import orchestration_resources


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    id: uuid.UUID
    orchestration_id: uuid.UUID
    ref: str
    kind: str
    producing_step_id: uuid.UUID | None
    parent_resource_id: uuid.UUID | None
    revision: int
    privacy_class: str
    safe_label: str
    single_use: bool
    consumed_at: datetime | None
    expires_at: datetime | None
    binding_digest: str | None
    #: Milestone 12 S2: model-invisible backing identity. `backing_id` is the file/document id inside
    #: `orchestrations.document_task_id` (`document_ref`/`document_result_ref`); `backing_text` is the
    #: canonical, policy-checked URL (`public_url_ref`). At most one is ever set.
    backing_id: uuid.UUID | None
    backing_text: str | None
    created_at: datetime
    updated_at: datetime


def _record(row: Row[Any]) -> ResourceRecord:
    return ResourceRecord(
        id=row.id,
        orchestration_id=row.orchestration_id,
        ref=row.ref,
        kind=row.kind,
        producing_step_id=row.producing_step_id,
        parent_resource_id=row.parent_resource_id,
        revision=row.revision,
        privacy_class=row.privacy_class,
        safe_label=row.safe_label,
        single_use=row.single_use,
        consumed_at=row.consumed_at,
        expires_at=row.expires_at,
        binding_digest=row.binding_digest,
        backing_id=row.backing_id,
        backing_text=row.backing_text,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class OrchestrationResourceRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def count(self, orchestration_id: uuid.UUID) -> int:
        """How many resources this orchestration already owns -- used only to allocate the next opaque ref.
        Callers mint under the same orchestration row lock `OrchestrationService._commit_step` already holds,
        so this count-then-insert is not a race: no two mints for one orchestration ever run concurrently."""
        found = await self._connection.scalar(
            select(func.count()).select_from(orchestration_resources).where(
                orchestration_resources.c.orchestration_id == orchestration_id
            )
        )
        return int(found or 0)

    async def mint(
        self,
        *,
        orchestration_id: uuid.UUID,
        ref: str,
        kind: str,
        producing_step_id: uuid.UUID | None,
        privacy_class: str,
        safe_label: str,
        single_use: bool = False,
        parent_resource_id: uuid.UUID | None = None,
        expires_at: datetime | None = None,
        binding_digest: str | None = None,
        backing_id: uuid.UUID | None = None,
        backing_text: str | None = None,
    ) -> ResourceRecord:
        result = await self._connection.execute(
            insert(orchestration_resources)
            .values(
                id=uuid.uuid4(),
                orchestration_id=orchestration_id,
                ref=ref,
                kind=kind,
                producing_step_id=producing_step_id,
                parent_resource_id=parent_resource_id,
                privacy_class=privacy_class,
                safe_label=safe_label,
                single_use=single_use,
                expires_at=expires_at,
                binding_digest=binding_digest,
                backing_id=backing_id,
                backing_text=backing_text,
            )
            .returning(*orchestration_resources.c)
        )
        return _record(result.one())

    async def get(self, orchestration_id: uuid.UUID, ref: str) -> ResourceRecord | None:
        """The only lookup this repository offers: scoped to one orchestration's own ref. There is no
        `get_by_ref` that omits `orchestration_id` -- that omission is the whole cross-orchestration-isolation
        guarantee, not an optimization to add back later."""
        row = (
            await self._connection.execute(
                select(orchestration_resources).where(
                    orchestration_resources.c.orchestration_id == orchestration_id,
                    orchestration_resources.c.ref == ref,
                )
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def available(self, orchestration_id: uuid.UUID) -> list[ResourceRecord]:
        """Not consumed, not expired. Ordered by `ref`'s own mint order. Orchestration liveness itself is the
        caller's concern (`OrchestrationRepository.is_live`); a resource's own `expires_at`, when set, is an
        additional, narrower freshness window on top of that."""
        result = await self._connection.execute(
            select(orchestration_resources)
            .where(
                orchestration_resources.c.orchestration_id == orchestration_id,
                orchestration_resources.c.consumed_at.is_(None),
                (orchestration_resources.c.expires_at.is_(None)) | (orchestration_resources.c.expires_at > func.now()),
            )
            .order_by(orchestration_resources.c.created_at)
        )
        return [_record(row) for row in result]

    async def consume(self, resource_id: uuid.UUID) -> ResourceRecord | None:
        """Marks a single-use resource spent. `None` if it was not single-use or was already consumed -- the
        database, not an earlier read, decides; a caller cannot spend the same resource twice by racing this."""
        result = await self._connection.execute(
            update(orchestration_resources)
            .where(
                orchestration_resources.c.id == resource_id,
                orchestration_resources.c.single_use.is_(True),
                orchestration_resources.c.consumed_at.is_(None),
            )
            .values(consumed_at=func.now(), updated_at=func.now())
            .returning(*orchestration_resources.c)
        )
        row = result.one_or_none()
        return _record(row) if row is not None else None


__all__ = ["OrchestrationResourceRepository", "ResourceRecord"]
