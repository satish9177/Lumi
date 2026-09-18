"""SQL for browser worker generations and dispatches. Callers own the transaction."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Row, func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import browser_dispatches, browser_worker_generations
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus


@dataclass(frozen=True, slots=True)
class WorkerGenerationRecord:
    id: uuid.UUID
    runtime_generation: uuid.UUID
    worker_started_at: datetime
    registered_at: datetime


@dataclass(frozen=True, slots=True)
class DispatchRecord:
    id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID | None
    worker_generation: uuid.UUID
    operation: str
    site: str
    effect: BrowserEffect
    status: DispatchStatus
    submitted: bool
    observation_id: uuid.UUID | None
    error_code: str | None
    duration_ms: int | None
    result: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None


def _generation(row: Row[Any]) -> WorkerGenerationRecord:
    return WorkerGenerationRecord(
        id=row.id,
        runtime_generation=row.runtime_generation,
        worker_started_at=row.worker_started_at,
        registered_at=row.registered_at,
    )


def _dispatch(row: Row[Any]) -> DispatchRecord:
    return DispatchRecord(
        id=row.id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        worker_generation=row.worker_generation,
        operation=row.operation,
        site=row.site,
        effect=BrowserEffect(row.effect),
        status=DispatchStatus(row.status),
        submitted=row.submitted,
        observation_id=row.observation_id,
        error_code=row.error_code,
        duration_ms=row.duration_ms,
        result=row.result,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class BrowserRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- worker generations -------------------------------------------------

    async def register_worker_generation(
        self,
        *,
        worker_generation: uuid.UUID,
        runtime_generation: uuid.UUID,
        worker_started_at: datetime,
    ) -> WorkerGenerationRecord:
        result = await self._connection.execute(
            insert(browser_worker_generations)
            .values(
                id=worker_generation,
                runtime_generation=runtime_generation,
                worker_started_at=worker_started_at,
            )
            .returning(*browser_worker_generations.c)
        )
        return _generation(result.one())

    async def get_worker_generation(
        self, worker_generation: uuid.UUID
    ) -> WorkerGenerationRecord | None:
        result = await self._connection.execute(
            select(browser_worker_generations).where(
                browser_worker_generations.c.id == worker_generation
            )
        )
        row = result.one_or_none()
        return _generation(row) if row is not None else None

    # ---- dispatches ---------------------------------------------------------

    async def insert_dispatch(
        self,
        *,
        dispatch_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
        worker_generation: uuid.UUID,
        operation: str,
        site: str,
        effect: BrowserEffect,
        session_id: uuid.UUID | None = None,
    ) -> DispatchRecord:
        """Record the intent to drive a browser, before the browser is driven.

        `attempt_id` is UNIQUE, so this insert is where a second consequential
        dispatch for one execution attempt fails -- in the database, before any
        request leaves the process.
        """
        result = await self._connection.execute(
            insert(browser_dispatches)
            .values(
                id=dispatch_id,
                action_id=action_id,
                attempt_id=attempt_id,
                worker_generation=worker_generation,
                operation=operation,
                site=site,
                effect=effect.value,
                session_id=session_id,
                status=DispatchStatus.DISPATCHED.value,
                submitted=False,
            )
            .returning(*browser_dispatches.c)
        )
        return _dispatch(result.one())

    async def finish_dispatch(
        self,
        *,
        dispatch_id: uuid.UUID,
        status: DispatchStatus,
        submitted: bool,
        observation_id: uuid.UUID | None,
        error_code: str | None,
        duration_ms: int | None,
        result: dict[str, Any] | None,
    ) -> DispatchRecord | None:
        """Close a dispatch, only if it is still open."""
        updated = await self._connection.execute(
            update(browser_dispatches)
            .where(
                browser_dispatches.c.id == dispatch_id,
                browser_dispatches.c.status == DispatchStatus.DISPATCHED.value,
            )
            .values(
                status=status.value,
                submitted=submitted,
                observation_id=observation_id,
                error_code=error_code,
                duration_ms=duration_ms,
                # null(), not None: None stores a JSON null, which is not the
                # same as "no result" and fails the object CHECK.
                result=result if result is not None else null(),
                finished_at=func.now(),
            )
            .returning(*browser_dispatches.c)
        )
        row = updated.one_or_none()
        return _dispatch(row) if row is not None else None

    async def close_orphaned_dispatch(self, attempt_id: uuid.UUID) -> DispatchRecord | None:
        """Close a dispatch a dead runtime left open, as an unknown.

        The runtime that made this dispatch died before hearing back, so it
        never learned whether a submission went out. `submitted` therefore stays
        false -- not as a claim that nothing was submitted, but because nothing
        observed one. The action's own OUTCOME_UNKNOWN is where the uncertainty
        is recorded; this row only stops an audit trail claiming browser work is
        still in flight when the process that started it no longer exists.
        """
        updated = await self._connection.execute(
            update(browser_dispatches)
            .where(
                browser_dispatches.c.attempt_id == attempt_id,
                browser_dispatches.c.status == DispatchStatus.DISPATCHED.value,
            )
            .values(
                status=DispatchStatus.OUTCOME_UNKNOWN.value,
                error_code="runtime_restart",
                finished_at=func.now(),
            )
            .returning(*browser_dispatches.c)
        )
        row = updated.one_or_none()
        return _dispatch(row) if row is not None else None

    async def get_dispatch(self, dispatch_id: uuid.UUID) -> DispatchRecord | None:
        result = await self._connection.execute(
            select(browser_dispatches).where(browser_dispatches.c.id == dispatch_id)
        )
        row = result.one_or_none()
        return _dispatch(row) if row is not None else None

    async def get_dispatch_for_attempt(self, attempt_id: uuid.UUID) -> DispatchRecord | None:
        result = await self._connection.execute(
            select(browser_dispatches).where(browser_dispatches.c.attempt_id == attempt_id)
        )
        row = result.one_or_none()
        return _dispatch(row) if row is not None else None

    async def list_dispatches(self, action_id: uuid.UUID) -> list[DispatchRecord]:
        result = await self._connection.execute(
            select(browser_dispatches)
            .where(browser_dispatches.c.action_id == action_id)
            .order_by(browser_dispatches.c.started_at, browser_dispatches.c.id)
        )
        return [_dispatch(row) for row in result]

    async def count_consequential_dispatches(self, action_id: uuid.UUID) -> int:
        """How many times a browser was sent to change the world for this action."""
        total = await self._connection.scalar(
            select(func.count())
            .select_from(browser_dispatches)
            .where(
                browser_dispatches.c.action_id == action_id,
                browser_dispatches.c.effect == BrowserEffect.CONSEQUENTIAL.value,
            )
        )
        return int(total or 0)
