"""Persistence for desktop dispatches (Milestone 9 S3): identifiers, refs, digests and closed codes only."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import actions, desktop_dispatches
from app.domain.action_status import ActionStatus
from app.domain.desktop_actions import TOOL_PREFIX


@dataclass(frozen=True, slots=True)
class DesktopDispatchRecord:
    id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    worker_generation: uuid.UUID
    operation: str
    surface_ref: str | None
    surface_epoch: int | None
    observation_id: uuid.UUID | None
    control_ref: str | None
    app_id: str | None
    value_ref: str | None
    option_container_ref: str | None
    invoke_effect: str | None
    input_tick: int
    status: str
    error_code: str | None
    result: dict[str, Any] | None


def _record(row: Any) -> DesktopDispatchRecord:
    return DesktopDispatchRecord(
        id=row.id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        worker_generation=row.worker_generation,
        operation=row.operation,
        surface_ref=row.surface_ref,
        surface_epoch=row.surface_epoch,
        observation_id=row.observation_id,
        control_ref=row.control_ref,
        app_id=row.app_id,
        value_ref=row.value_ref,
        option_container_ref=row.option_container_ref,
        invoke_effect=row.invoke_effect,
        input_tick=int(row.input_tick),
        status=row.status,
        error_code=row.error_code,
        result=row.result,
    )


#: A desktop action in any of these states is still live: waiting on the person, or running. An
#: `OUTCOME_UNKNOWN` focus, scroll or launch does not block a new one (each is recoverable by a fresh
#: observation and a NEW approval; a launch inspects for a running instance before it ever spawns).
_LIVE = (
    ActionStatus.PROPOSED.value,
    ActionStatus.WAITING_APPROVAL.value,
    ActionStatus.APPROVED.value,
    ActionStatus.EXECUTING.value,
)

#: S4 mutations (`set_control_value`, `select_control`, `invoke_control`) are different: their
#: `OUTCOME_UNKNOWN` DOES block, until reconciled. A value write, a selection or an invoked control is
#: a real mutation whose outcome Lumi does not know; letting a new task, a new plan or any other route
#: propose past that would be exactly the silent-retry risk the ledger exists to prevent.
_S4_MUTATION_TOOLS = ("DESKTOP_SET_VALUE", "DESKTOP_SELECT", "DESKTOP_INVOKE")
_UNRESOLVED = (ActionStatus.OUTCOME_UNKNOWN.value, ActionStatus.RECONCILING.value)


class DesktopActionRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_dispatch(
        self,
        *,
        dispatch_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        worker_generation: uuid.UUID,
        operation: str,
        input_tick: int,
        surface_ref: str | None = None,
        surface_epoch: int | None = None,
        observation_id: uuid.UUID | None = None,
        snapshot_digest: str | None = None,
        control_ref: str | None = None,
        app_id: str | None = None,
        value_ref: str | None = None,
        option_container_ref: str | None = None,
        invoke_effect: str | None = None,
    ) -> DesktopDispatchRecord:
        """The durable intent to ask the worker for ONE effect. `attempt_id` is UNIQUE."""
        row = (
            await self._connection.execute(
                insert(desktop_dispatches)
                .values(
                    id=dispatch_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    worker_generation=worker_generation,
                    operation=operation,
                    surface_ref=surface_ref,
                    surface_epoch=surface_epoch,
                    observation_id=observation_id,
                    snapshot_digest=snapshot_digest,
                    control_ref=control_ref,
                    app_id=app_id,
                    value_ref=value_ref,
                    option_container_ref=option_container_ref,
                    invoke_effect=invoke_effect,
                    input_tick=input_tick,
                    status="DISPATCHED",
                )
                .returning(*desktop_dispatches.c)
            )
        ).one()
        return _record(row)

    async def finish_dispatch(
        self, *, dispatch_id: uuid.UUID, status: str, error_code: str | None, result: dict[str, Any] | None
    ) -> DesktopDispatchRecord | None:
        """Close a dispatch exactly once: only a still-`DISPATCHED` row can be finished."""
        row = (
            await self._connection.execute(
                update(desktop_dispatches)
                .where(desktop_dispatches.c.id == dispatch_id, desktop_dispatches.c.status == "DISPATCHED")
                .values(status=status, error_code=error_code, result=result, finished_at=func.now())
                .returning(*desktop_dispatches.c)
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def close_orphaned_dispatch(self, attempt_id: uuid.UUID) -> DesktopDispatchRecord | None:
        """A dispatch still in flight for an attempt whose runtime died: its outcome is unknown."""
        row = (
            await self._connection.execute(
                update(desktop_dispatches)
                .where(desktop_dispatches.c.attempt_id == attempt_id, desktop_dispatches.c.status == "DISPATCHED")
                .values(status="OUTCOME_UNKNOWN", error_code="runtime_restart", finished_at=func.now())
                .returning(*desktop_dispatches.c)
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def get_for_attempt(self, attempt_id: uuid.UUID) -> DesktopDispatchRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_dispatches).where(desktop_dispatches.c.attempt_id == attempt_id)
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def live_desktop_actions(self, *, excluding: uuid.UUID | None = None) -> list[tuple[uuid.UUID, str, str]]:
        """`(action_id, tool_name, status)` of every desktop action that is still live, in any task."""
        query = select(actions.c.id, actions.c.tool_name, actions.c.status).where(
            actions.c.tool_name.like(f"{TOOL_PREFIX}%"), actions.c.status.in_(_LIVE)
        )
        if excluding is not None:
            query = query.where(actions.c.id != excluding)
        return [(row.id, row.tool_name, row.status) for row in (await self._connection.execute(query)).all()]

    async def executing_desktop_actions(self, *, excluding: uuid.UUID | None = None) -> int:
        query = select(func.count()).select_from(actions).where(
            actions.c.tool_name.like(f"{TOOL_PREFIX}%"), actions.c.status == ActionStatus.EXECUTING.value
        )
        if excluding is not None:
            query = query.where(actions.c.id != excluding)
        return int((await self._connection.execute(query)).scalar_one())

    async def action_for_task(self, task_id: uuid.UUID) -> uuid.UUID | None:
        """The one desktop action `_open_locked` created together with this task, if any. Milestone 12 S4:
        `OrchestrationService` links a `desktop_action` task, never an action id directly, so resolving a
        step's own linked task back to its action is how `desktop_safe_action`/`launch_registered_app` read
        the current state of the effect they proposed."""
        row = (
            await self._connection.execute(
                select(actions.c.id).where(actions.c.task_id == task_id, actions.c.tool_name.like(f"{TOOL_PREFIX}%"))
            )
        ).first()
        return row.id if row is not None else None

    async def action_for_plan(self, plan_id: uuid.UUID) -> uuid.UUID | None:
        """The execution action already opened from this plan, if any (`propose_from_plan` is
        idempotent: a plan funds at most one execution card, ever)."""
        row = (
            await self._connection.execute(
                select(actions.c.id).where(
                    actions.c.tool_name.in_(_S4_MUTATION_TOOLS),
                    actions.c.proposal["plan_id"].astext == str(plan_id),
                )
            )
        ).first()
        return row.id if row is not None else None

    async def unresolved_mutation(self) -> uuid.UUID | None:
        """An S4 mutation action still `OUTCOME_UNKNOWN` or `RECONCILING`, if one exists.

        Not scoped to a task: an unresolved effect blocks every desktop action, not only a new one in
        the same task, so a new task cannot side-step it either.
        """
        row = (
            await self._connection.execute(
                select(actions.c.id)
                .where(actions.c.tool_name.in_(_S4_MUTATION_TOOLS), actions.c.status.in_(_UNRESOLVED))
                .order_by(actions.c.created_at.desc())
                .limit(1)
            )
        ).first()
        return row.id if row is not None else None

    async def latest_desktop_action_id(self) -> uuid.UUID | None:
        row = (
            await self._connection.execute(
                select(actions.c.id)
                .where(actions.c.tool_name.like(f"{TOOL_PREFIX}%"))
                .order_by(actions.c.created_at.desc())
                .limit(1)
            )
        ).first()
        return row.id if row is not None else None
