"""SQL for desktop action-planning grants and plans (Milestone 9 S4).

Mirrors `app.repositories.desktop_disclosure` exactly: the single-use property is a database fact
(one compare-and-swap `ACTIVE` -> `COMPLETED`, `grant_id`/`task_id` UNIQUE on the plan row), not a
convention. The one shape difference is `desktop_action_plans.proposed_action`: the closed action a
provider proposed, in place of S2's separate `desktop_answers` table, because a plan produces at most
one small JSON object rather than free-form evidence.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, null, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import desktop_action_plans, task_grants
from app.domain.desktop_planning import DESKTOP_PLAN_KIND, DesktopPlanScope
from app.domain.research import GrantStatus

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class DesktopPlanGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: DesktopPlanScope
    scope_digest: str
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class DesktopPlanRecord:
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
    proposed_action: dict[str, Any] | None
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


def _grant(row: Row[Any]) -> DesktopPlanGrantRecord:
    return DesktopPlanGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=row.revision,
        policy_version=row.policy_version,
        scope=DesktopPlanScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        completed_at=row.completed_at,
    )


def _plan(row: Row[Any]) -> DesktopPlanRecord:
    return DesktopPlanRecord(
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
        proposed_action=row.proposed_action,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error_code=row.error_code,
    )


class DesktopPlanningRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- the grant ------------------------------------------------------------------------

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: DesktopPlanScope
    ) -> DesktopPlanGrantRecord:
        """A PENDING grant: the card's contents. It authorises nothing at all."""
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id,
                task_id=task_id,
                kind=DESKTOP_PLAN_KIND,
                status=GrantStatus.PENDING.value,
                revision=1,
                policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"),
                scope_digest=scope.digest,
            )
            .returning(*task_grants.c)
        )
        return _grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> DesktopPlanGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_PLAN_KIND)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> DesktopPlanGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants)
                .where(task_grants.c.task_id == task_id, task_grants.c.kind == DESKTOP_PLAN_KIND)
                .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
                .limit(1)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> DesktopPlanGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.task_id == task_id,
                    task_grants.c.kind == DESKTOP_PLAN_KIND,
                    task_grants.c.status.in_(_OPEN),
                )
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> DesktopPlanGrantRecord | None:
        """PENDING -> ACTIVE, only at the revision and digest the card showed."""
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DESKTOP_PLAN_KIND,
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
    ) -> DesktopPlanGrantRecord | None:
        """PENDING or ACTIVE -> REVOKED. A grant that was already claimed is never touched."""
        if status is not GrantStatus.REVOKED:
            raise ValueError("close_grant only revokes a grant")
        conditions = [
            task_grants.c.id == grant_id,
            task_grants.c.kind == DESKTOP_PLAN_KIND,
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

    async def claim_grant(self, *, grant_id: uuid.UUID, expected_revision: int) -> DesktopPlanGrantRecord | None:
        """The single-use swap: ACTIVE and unexpired -> COMPLETED, exactly once."""
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id,
                    task_grants.c.kind == DESKTOP_PLAN_KIND,
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

    # ---- the plan ---------------------------------------------------------------------------

    async def insert_plan(
        self,
        *,
        plan_id: uuid.UUID,
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
    ) -> DesktopPlanRecord:
        result = await self._connection.execute(
            insert(desktop_action_plans)
            .values(
                id=plan_id,
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
            .returning(*desktop_action_plans.c)
        )
        return _plan(result.one())

    async def get_plan(self, plan_id: uuid.UUID) -> DesktopPlanRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_action_plans).where(desktop_action_plans.c.id == plan_id)
            )
        ).one_or_none()
        return _plan(row) if row is not None else None

    async def plan_for_task(self, task_id: uuid.UUID) -> DesktopPlanRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_action_plans).where(desktop_action_plans.c.task_id == task_id)
            )
        ).one_or_none()
        return _plan(row) if row is not None else None

    async def finish_plan(
        self,
        *,
        plan_id: uuid.UUID,
        status: str,
        error_code: str | None,
        proposed_action: dict[str, Any] | None,
    ) -> DesktopPlanRecord | None:
        """STARTED -> a final status, once. Anything already final is left exactly as it is."""
        row = (
            await self._connection.execute(
                update(desktop_action_plans)
                .where(desktop_action_plans.c.id == plan_id, desktop_action_plans.c.status == "STARTED")
                .values(
                    status=status,
                    error_code=error_code,
                    # null(), not None: None would store a JSON null, which is not the same as
                    # "no proposal" and fails the object CHECK.
                    proposed_action=proposed_action if proposed_action is not None else null(),
                    finished_at=func.now(),
                )
                .returning(*desktop_action_plans.c)
            )
        ).one_or_none()
        return _plan(row) if row is not None else None

    async def list_started(self, *, older_than_seconds: int | None = None) -> list[DesktopPlanRecord]:
        statement = select(desktop_action_plans).where(desktop_action_plans.c.status == "STARTED")
        if older_than_seconds is not None:
            statement = statement.where(
                desktop_action_plans.c.started_at
                < func.now() - text(f"interval '{int(older_than_seconds)} seconds'")
            )
        return [_plan(row) for row in (await self._connection.execute(statement)).all()]
