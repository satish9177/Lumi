"""SQL for projects, recipes, runs and `project_run` grants (Milestone 10 S3).

Every grant read filters `kind = 'project_run'`, and the step-authorization consuming statement checks
inside itself that the funding grant is an ACTIVE, unexpired `project_run` grant at the bound revision and
scope digest. Callers own the transaction.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import project_recipes, project_runs, projects, step_authorizations, task_grants
from app.domain.projects import PROJECT_RUN_KIND, ProjectRunScope
from app.domain.research import GrantStatus

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)
#: A run in one of these states owns its project: no second run may start (and the index enforces it).
LIVE_RUN_STATUSES = ("STARTING", "RUNNING", "READY", "OUTCOME_UNKNOWN")


def _i64(value: int) -> int:
    value = int(value) & 0xFFFFFFFFFFFFFFFF
    return value - (1 << 64) if value >= (1 << 63) else value


def _u64(value: int) -> int:
    return int(value) & 0xFFFFFFFFFFFFFFFF


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: uuid.UUID
    label: str
    canonical_path: str
    volume_serial: int
    dir_index: int
    status: str
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecipeRecord:
    id: uuid.UUID
    project_id: uuid.UUID
    label: str
    script_name: str
    spec: dict[str, Any]
    digest: str
    status: str
    invalid_reason: str | None
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: ProjectRunScope
    scope_digest: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    recipe_id: uuid.UUID
    project_id: uuid.UUID
    recipe_digest: str
    status: str
    error_code: str | None
    pid: int | None
    creation_time: int | None
    exit_code: int | None
    action_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    resumed_at: datetime | None
    ready_at: datetime | None
    ended_at: datetime | None


def _project(row: Row[Any]) -> ProjectRecord:
    return ProjectRecord(
        id=row.id, label=row.label, canonical_path=row.canonical_path, volume_serial=_u64(row.volume_serial),
        dir_index=int(row.dir_index), status=row.status, revision=int(row.revision), created_at=row.created_at,
    )


def _recipe(row: Row[Any]) -> RecipeRecord:
    return RecipeRecord(
        id=row.id, project_id=row.project_id, label=row.label, script_name=row.script_name, spec=dict(row.spec),
        digest=row.digest, status=row.status, invalid_reason=row.invalid_reason, revision=int(row.revision),
        created_at=row.created_at,
    )


def _grant(row: Row[Any]) -> RunGrantRecord:
    return RunGrantRecord(
        id=row.id, task_id=row.task_id, status=GrantStatus(row.status), revision=int(row.revision),
        policy_version=row.policy_version, scope=ProjectRunScope.model_validate(row.scope), scope_digest=row.scope_digest,
        expires_at=row.expires_at,
    )


def _run(row: Row[Any]) -> RunRecord:
    return RunRecord(
        id=row.id, task_id=row.task_id, grant_id=row.grant_id, recipe_id=row.recipe_id, project_id=row.project_id,
        recipe_digest=row.recipe_digest, status=row.status, error_code=row.error_code, pid=row.pid,
        creation_time=None if row.creation_time is None else _u64(row.creation_time),
        exit_code=None if row.exit_code is None else int(row.exit_code), action_id=row.action_id,
        created_at=row.created_at, updated_at=row.updated_at, resumed_at=row.resumed_at, ready_at=row.ready_at,
        ended_at=row.ended_at,
    )


class ProjectRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- projects ---------------------------------------------------------------------------------------

    async def insert_project(self, *, label: str, canonical_path: str, path_key: str, volume: int, index: int) -> ProjectRecord:
        row = (
            await self._connection.execute(
                insert(projects)
                .values(
                    id=uuid.uuid4(), label=label, canonical_path=canonical_path, path_key=path_key, volume_serial=_i64(volume),
                    dir_index=str(index), status="ACTIVE", revision=1,
                )
                .returning(*projects.c)
            )
        ).one()
        return _project(row)

    async def get_project(self, project_id: uuid.UUID, *, lock: bool = False) -> ProjectRecord | None:
        statement = select(projects).where(projects.c.id == project_id)
        if lock:
            statement = statement.with_for_update()
        row = (await self._connection.execute(statement)).one_or_none()
        return _project(row) if row is not None else None

    async def active_projects(self) -> list[ProjectRecord]:
        rows = await self._connection.execute(select(projects).where(projects.c.status == "ACTIVE").order_by(projects.c.created_at))
        return [_project(row) for row in rows]

    async def revoke_project(self, project_id: uuid.UUID, *, expected_revision: int) -> ProjectRecord | None:
        row = (
            await self._connection.execute(
                update(projects)
                .where(projects.c.id == project_id, projects.c.status == "ACTIVE", projects.c.revision == expected_revision)
                .values(status="REVOKED", revoked_at=func.now(), revision=projects.c.revision + 1, updated_at=func.now())
                .returning(*projects.c)
            )
        ).one_or_none()
        if row is not None:
            await self._connection.execute(
                update(project_recipes)
                .where(project_recipes.c.project_id == project_id, project_recipes.c.status == "ACTIVE")
                .values(status="REVOKED", revision=project_recipes.c.revision + 1, updated_at=func.now())
            )
        return _project(row) if row is not None else None

    # ---- recipes ----------------------------------------------------------------------------------------

    async def insert_recipe(self, *, project_id: uuid.UUID, label: str, script_name: str, spec: dict[str, Any], digest: str) -> RecipeRecord:
        row = (
            await self._connection.execute(
                insert(project_recipes)
                .values(
                    id=uuid.uuid4(), project_id=project_id, label=label, script_name=script_name, spec=spec, digest=digest,
                    status="ACTIVE", revision=1,
                )
                .returning(*project_recipes.c)
            )
        ).one()
        return _recipe(row)

    async def get_recipe(self, recipe_id: uuid.UUID) -> RecipeRecord | None:
        row = (await self._connection.execute(select(project_recipes).where(project_recipes.c.id == recipe_id))).one_or_none()
        return _recipe(row) if row is not None else None

    async def recipes(self, project_id: uuid.UUID | None = None) -> list[RecipeRecord]:
        statement = select(project_recipes).where(project_recipes.c.status != "REVOKED")
        if project_id is not None:
            statement = statement.where(project_recipes.c.project_id == project_id)
        rows = await self._connection.execute(statement.order_by(project_recipes.c.created_at))
        return [_recipe(row) for row in rows]

    async def invalidate_recipe(self, recipe_id: uuid.UUID, *, reason: str) -> None:
        await self._connection.execute(
            update(project_recipes)
            .where(project_recipes.c.id == recipe_id, project_recipes.c.status == "ACTIVE")
            .values(status="INVALIDATED", invalid_reason=reason[:40], revision=project_recipes.c.revision + 1, updated_at=func.now())
        )

    async def revoke_recipe(self, recipe_id: uuid.UUID, *, expected_revision: int) -> RecipeRecord | None:
        row = (
            await self._connection.execute(
                update(project_recipes)
                .where(project_recipes.c.id == recipe_id, project_recipes.c.revision == expected_revision,
                       project_recipes.c.status.in_(("ACTIVE", "INVALIDATED")))
                .values(status="REVOKED", invalid_reason=None, revision=project_recipes.c.revision + 1, updated_at=func.now())
                .returning(*project_recipes.c)
            )
        ).one_or_none()
        return _recipe(row) if row is not None else None

    # ---- the grant --------------------------------------------------------------------------------------

    async def insert_grant(self, *, grant_id: uuid.UUID, scope: ProjectRunScope) -> RunGrantRecord:
        row = (
            await self._connection.execute(
                insert(task_grants)
                .values(
                    id=grant_id, task_id=scope.task_id, kind=PROJECT_RUN_KIND, status=GrantStatus.PENDING.value, revision=1,
                    policy_version=scope.policy_version, scope=scope.model_dump(mode="json"), scope_digest=scope.digest,
                )
                .returning(*task_grants.c)
            )
        ).one()
        return _grant(row)

    async def get_grant(self, grant_id: uuid.UUID) -> RunGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(task_grants.c.id == grant_id, task_grants.c.kind == PROJECT_RUN_KIND)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def grant_for_task(self, task_id: uuid.UUID) -> RunGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(task_grants.c.task_id == task_id, task_grants.c.kind == PROJECT_RUN_KIND)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta) -> RunGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id, task_grants.c.kind == PROJECT_RUN_KIND, task_grants.c.revision == expected_revision,
                    task_grants.c.status == GrantStatus.PENDING.value, task_grants.c.scope_digest == scope_digest,
                )
                .values(
                    status=GrantStatus.ACTIVE.value, revision=task_grants.c.revision + 1, confirmed_at=func.now(),
                    expires_at=func.now() + ttl, updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _grant(row) if row is not None else None

    async def close_grant(self, *, grant_id: uuid.UUID, status: GrantStatus, expected_revision: int | None = None) -> RunGrantRecord | None:
        conditions = [task_grants.c.id == grant_id, task_grants.c.kind == PROJECT_RUN_KIND]
        if status is GrantStatus.REVOKED:
            conditions.append(task_grants.c.status.in_(_OPEN))
            values: dict[str, Any] = {"revoked_at": func.now(), "confirmed_at": func.coalesce(task_grants.c.confirmed_at, func.now())}
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

    async def insert_step_authorization(
        self, *, grant: RunGrantRecord, action_id: uuid.UUID, action_revision: int, proposal_digest: str,
        runtime_generation: uuid.UUID, ttl: timedelta,
    ) -> uuid.UUID:
        authorization_id = uuid.uuid4()
        await self._connection.execute(
            insert(step_authorizations).values(
                id=authorization_id, grant_id=grant.id, grant_revision=grant.revision, task_id=grant.task_id, action_id=action_id,
                action_revision=action_revision, proposal_digest=proposal_digest, scope_digest=grant.scope_digest,
                policy_version=grant.policy_version, runtime_generation=runtime_generation, expires_at=func.now() + ttl,
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
                task_grants.c.kind == PROJECT_RUN_KIND,
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
                    step_authorizations.c.id == authorization_id, step_authorizations.c.consumed_at.is_(None),
                    step_authorizations.c.action_revision == action_revision, step_authorizations.c.proposal_digest == proposal_digest,
                    step_authorizations.c.runtime_generation == runtime_generation, step_authorizations.c.expires_at > func.now(),
                    funded,
                )
                .values(consumed_at=func.now())
                .returning(step_authorizations.c.id)
            )
        ).one_or_none()
        return row is not None

    # ---- runs -------------------------------------------------------------------------------------------

    async def insert_run(self, *, scope: ProjectRunScope, grant_id: uuid.UUID) -> RunRecord:
        """STARTING. The partial unique index refuses a second live run of the same project."""
        row = (
            await self._connection.execute(
                insert(project_runs)
                .values(
                    id=scope.run_id, task_id=scope.task_id, grant_id=grant_id, recipe_id=scope.recipe_id,
                    project_id=scope.project_id, recipe_digest=scope.recipe_digest, status="STARTING",
                )
                .returning(*project_runs.c)
            )
        ).one()
        return _run(row)

    async def run_for_task(self, task_id: uuid.UUID) -> RunRecord | None:
        row = (await self._connection.execute(select(project_runs).where(project_runs.c.task_id == task_id))).one_or_none()
        return _run(row) if row is not None else None

    async def get_run(self, run_id: uuid.UUID) -> RunRecord | None:
        row = (await self._connection.execute(select(project_runs).where(project_runs.c.id == run_id))).one_or_none()
        return _run(row) if row is not None else None

    async def live_run_for_project(self, project_id: uuid.UUID) -> RunRecord | None:
        row = (
            await self._connection.execute(
                select(project_runs).where(project_runs.c.project_id == project_id, project_runs.c.status.in_(LIVE_RUN_STATUSES))
            )
        ).one_or_none()
        return _run(row) if row is not None else None

    async def live_runs(self) -> list[RunRecord]:
        rows = await self._connection.execute(select(project_runs).where(project_runs.c.status.in_(LIVE_RUN_STATUSES)))
        return [_run(row) for row in rows]

    async def update_run(self, run_id: uuid.UUID, *, only_if: tuple[str, ...] | None = None, **values: Any) -> RunRecord | None:
        if "creation_time" in values and values["creation_time"] is not None:
            values["creation_time"] = _i64(values["creation_time"])
        conditions = [project_runs.c.id == run_id]
        if only_if is not None:
            conditions.append(project_runs.c.status.in_(only_if))
        row = (
            await self._connection.execute(
                update(project_runs).where(and_(*conditions)).values(updated_at=func.now(), **values).returning(*project_runs.c)
            )
        ).one_or_none()
        return _run(row) if row is not None else None
