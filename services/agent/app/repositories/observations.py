"""SQL for page observations. Callers own the transaction."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import page_observations
from app.domain.page_observation import PageAnswer, PageObservation


@dataclass(frozen=True, slots=True)
class RecordedAnswer:
    answer: PageAnswer
    provider: str
    model: str
    answered_at: datetime


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observation: PageObservation
    task_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    dispatch_id: uuid.UUID
    worker_generation: uuid.UUID
    created_at: datetime
    answer: RecordedAnswer | None

    @property
    def id(self) -> uuid.UUID:
        return self.observation.observation_id


def _record(row: Row[Any]) -> ObservationRecord:
    projection: dict[str, Any] = row.projection
    observation = PageObservation(
        schema_version=row.schema_version,
        observation_id=row.id,
        provenance=row.provenance,
        requested_url=row.requested_url,
        final_url=row.final_url,
        redirects=projection["redirects"],
        title=row.title,
        document_epoch=row.document_epoch,
        settled=row.settled,
        observed_at=row.observed_at,
        blocks=projection["blocks"],
        links=projection["links"],
        truncated=row.truncated,
        total_text_chars=projection["total_text_chars"],
        total_link_count=projection["total_link_count"],
        content_hash=row.content_hash,
    )
    answer = None
    if row.answered_at is not None:
        answer = RecordedAnswer(
            answer=PageAnswer.model_validate(row.answer),
            provider=row.answer_provider,
            model=row.answer_model,
            answered_at=row.answered_at,
        )
    return ObservationRecord(
        observation=observation,
        task_id=row.task_id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        dispatch_id=row.dispatch_id,
        worker_generation=row.worker_generation,
        created_at=row.created_at,
        answer=answer,
    )


class ObservationRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(
        self,
        *,
        observation: PageObservation,
        task_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
    ) -> ObservationRecord:
        dumped = observation.model_dump(mode="json")
        result = await self._connection.execute(
            insert(page_observations)
            .values(
                id=observation.observation_id,
                task_id=task_id,
                action_id=action_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                worker_generation=worker_generation,
                schema_version=observation.schema_version,
                provenance=observation.provenance,
                requested_url=observation.requested_url,
                final_url=observation.final_url,
                title=observation.title,
                document_epoch=observation.document_epoch,
                settled=observation.settled,
                truncated=observation.truncated,
                observed_at=observation.observed_at,
                content_hash=observation.content_hash,
                projection={
                    "redirects": dumped["redirects"],
                    "blocks": dumped["blocks"],
                    "links": dumped["links"],
                    "total_text_chars": dumped["total_text_chars"],
                    "total_link_count": dumped["total_link_count"],
                },
            )
            .returning(*page_observations.c)
        )
        return _record(result.one())

    async def get(self, observation_id: uuid.UUID) -> ObservationRecord | None:
        result = await self._connection.execute(
            select(page_observations).where(page_observations.c.id == observation_id)
        )
        row = result.one_or_none()
        return _record(row) if row is not None else None

    async def for_action(self, action_id: uuid.UUID) -> ObservationRecord | None:
        result = await self._connection.execute(
            select(page_observations)
            .where(page_observations.c.action_id == action_id)
            .order_by(page_observations.c.created_at.desc(), page_observations.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _record(row) if row is not None else None

    async def latest_for_task(self, task_id: uuid.UUID) -> ObservationRecord | None:
        result = await self._connection.execute(
            select(page_observations)
            .where(page_observations.c.task_id == task_id)
            .order_by(page_observations.c.created_at.desc(), page_observations.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _record(row) if row is not None else None

    async def record_answer(
        self, *, observation_id: uuid.UUID, answer: PageAnswer, provider: str, model: str
    ) -> ObservationRecord | None:
        """Write the answer once. Returns None if one was already recorded."""
        result = await self._connection.execute(
            update(page_observations)
            .where(
                page_observations.c.id == observation_id,
                page_observations.c.answered_at.is_(None),
            )
            .values(
                answer_status=answer.status,
                answer=answer.model_dump(mode="json"),
                answer_provider=provider,
                answer_model=model,
                answered_at=func.now(),
            )
            .returning(*page_observations.c)
        )
        row = result.one_or_none()
        return _record(row) if row is not None else None
