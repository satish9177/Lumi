"""Persistence for desktop observations: the safe projection, nothing else."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import desktop_observations, desktop_worker_generations


@dataclass(frozen=True, slots=True)
class DesktopObservationRecord:
    id: uuid.UUID
    worker_generation: uuid.UUID
    surface_ref: str
    surface_epoch: int
    schema_version: int
    classification: str
    snapshot: dict[str, Any]
    snapshot_digest: str
    truncated: bool
    created_at: datetime


class DesktopRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def register_worker_generation(
        self,
        *,
        worker_generation: uuid.UUID,
        runtime_generation: uuid.UUID,
        worker_started_at: datetime,
    ) -> None:
        """Record a worker generation once. Registering is idempotent: the row is the
        worker's identity, not a claim on anything."""
        await self._connection.execute(
            pg_insert(desktop_worker_generations)
            .values(
                id=worker_generation,
                runtime_generation=runtime_generation,
                worker_started_at=worker_started_at,
            )
            .on_conflict_do_nothing(index_elements=[desktop_worker_generations.c.id])
        )

    async def insert_observation(
        self,
        *,
        observation_id: uuid.UUID,
        worker_generation: uuid.UUID,
        surface_ref: str,
        surface_epoch: int,
        schema_version: int,
        classification: str,
        snapshot: dict[str, Any],
        snapshot_digest: str,
        truncated: bool,
    ) -> None:
        await self._connection.execute(
            desktop_observations.insert().values(
                id=observation_id,
                worker_generation=worker_generation,
                surface_ref=surface_ref,
                surface_epoch=surface_epoch,
                schema_version=schema_version,
                classification=classification,
                snapshot=snapshot,
                snapshot_digest=snapshot_digest,
                truncated=truncated,
            )
        )

    async def prune(self, *, keep: int, max_age_seconds: int) -> None:
        """Keep only the newest `keep` observations, and none older than `max_age_seconds`.

        Desktop text is private and local. It is retained for a short, bounded window in both count
        and time; an idle runtime does not hold it indefinitely.
        """
        newest = (
            select(desktop_observations.c.id)
            .order_by(desktop_observations.c.created_at.desc(), desktop_observations.c.id)
            .limit(keep)
        )
        expired = desktop_observations.c.created_at < func.now() - text(f"interval '{int(max_age_seconds)} seconds'")
        await self._connection.execute(
            delete(desktop_observations).where(or_(desktop_observations.c.id.not_in(newest), expired))
        )

    async def latest_observation_id(self, worker_generation: uuid.UUID, surface_ref: str) -> uuid.UUID | None:
        """The newest observation of this surface in this worker generation. Older ones are stale for action."""
        row = (
            await self._connection.execute(
                select(desktop_observations.c.id)
                .where(
                    desktop_observations.c.worker_generation == worker_generation,
                    desktop_observations.c.surface_ref == surface_ref,
                )
                .order_by(desktop_observations.c.created_at.desc(), desktop_observations.c.id.desc())
                .limit(1)
            )
        ).first()
        return row.id if row is not None else None

    async def get_observation(self, observation_id: uuid.UUID) -> DesktopObservationRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_observations).where(desktop_observations.c.id == observation_id)
            )
        ).one_or_none()
        if row is None:
            return None
        return DesktopObservationRecord(
            id=row.id,
            worker_generation=row.worker_generation,
            surface_ref=row.surface_ref,
            surface_epoch=row.surface_epoch,
            schema_version=row.schema_version,
            classification=row.classification,
            snapshot=row.snapshot,
            snapshot_digest=row.snapshot_digest,
            truncated=row.truncated,
            created_at=row.created_at,
        )
