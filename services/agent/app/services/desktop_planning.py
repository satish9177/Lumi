"""Milestone 9 S4: the runtime's desktop action-planning service.

This service decides **who may see an already-captured desktop snapshot in order to propose ONE
bounded action**, and records that ONE proposal. It never touches the desktop itself: the only desktop
calls it makes are S1's two read-only ones (list, observe), exactly like `DesktopDisclosureService`.
Turning a recorded proposal into something that can actually run is `DesktopActionService`'s job
(`propose_from_plan`), on the ordinary action ledger, behind its own separate exact approval.

```text
create   observe ONE surface locally (S1) + the person's own typed candidate values -> task + PENDING grant
confirm  the trusted click                            -> grant ACTIVE, bound to that exact snapshot
claim    validate + project + consume the grant       -> ONE committed transaction, THEN the provider
record   the provider's ONE proposed action, or failure -> validated proposal | FAILED | OUTCOME_UNKNOWN
```

Ordering is the safety property, identical to S2: the single-use grant is consumed and a UNIQUE plan row
is written *in one transaction that commits before* the projection is returned to Electron main, and no
database transaction is open while the provider is called. A crash after that commit leaves the outcome
`OUTCOME_UNKNOWN`, never replayed.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import task_grants as grants_table
from app.db.tables import tasks as tasks_table
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import SurfaceRecord
from app.domain.authenticated import Recipient
from app.domain.desktop_disclosure import DisplayTarget, Projection, build_projection
from app.domain.desktop_planning import (
    DESKTOP_ACTION_PLAN_TASK_TYPE,
    DESKTOP_PLAN_KIND,
    ERROR_INVALID_OUTPUT,
    ERROR_OBSERVATION_GONE,
    ERROR_RUNTIME_RESTART,
    MAX_VALUES,
    PLANNING_OBSERVATION_MAX_AGE_SECONDS,
    PROVIDER_FAILURE_CODES,
    STALE_CLAIM_SECONDS,
    DesktopPlanRefusal,
    DesktopPlanScope,
    PlannedAction,
    StoredValue,
    ValueDescriptor,
    dump_planned_action,
    observation_age_seconds,
    parse_planned_action,
    validate_model,
    validate_objective,
    validate_planned_action,
)
from app.domain.errors import (
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.repositories.desktop import DesktopObservationRecord, DesktopRepository
from app.repositories.desktop_actions import DesktopActionRepository
from app.repositories.desktop_planning import (
    DesktopPlanGrantRecord,
    DesktopPlanningRepository,
    DesktopPlanRecord,
)
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.desktop import DesktopService

logger = logging.getLogger("lumi.desktop_planning")


# ---- views ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanCardView:
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    recipient: str
    model: str
    observed_at: datetime
    application_label: str
    window_title: str
    max_nodes: int
    max_text_bytes: int
    redaction_policy: str
    observation_available: bool
    node_count: int | None
    text_bytes: int | None
    redaction_count: int | None
    truncated: bool | None
    truncation: tuple[str, ...]
    #: The person's own candidate values, shown back to them (raw) on the card. Never sent to any
    #: provider as raw text: only `ValueDescriptor` (ref, classification, length) is.
    values: tuple[StoredValue, ...]


@dataclass(frozen=True, slots=True)
class PlanStateView:
    plan_id: uuid.UUID
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool
    proposed_action: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class DesktopPlanView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: str
    card: PlanCardView | None
    plan: PlanStateView | None
    #: Set once a plan SUCCEEDED and `DesktopActionService.propose_from_plan` has opened the
    #: execution-time action. Lets the trusted UI jump straight to the second (execution) card.
    action_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class PlanProviderContext:
    """What goes to Electron main, once, after the claim has committed."""

    plan_id: uuid.UUID
    task_id: uuid.UUID
    objective: str
    recipient: str
    model: str
    observed_at: datetime
    projection: dict[str, Any]
    projection_digest: str
    value_descriptors: tuple[ValueDescriptor, ...]


# ---- the service ---------------------------------------------------------------------------


class DesktopPlanningService:
    """Task use cases. Each method is short transactions with no provider call inside.

    Deliberately independent of `DesktopActionService`: this service never opens, approves or executes
    an action. It only ever produces a validated, closed proposal that a SEPARATE service, with its own
    independent re-verification, may turn into something requiring its own separate approval.
    """

    def __init__(self, engine: AsyncEngine, *, desktop: DesktopService, grant_ttl_seconds: int) -> None:
        self._engine = engine
        self._desktop = desktop
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)

    # ---- create: local observation, then the card ---------------------------------------

    async def create(
        self,
        *,
        objective: object,
        recipient: Recipient,
        model: object,
        worker_generation: uuid.UUID,
        surface_ref: str,
        surface_epoch: int,
        values: list[tuple[str, str]],
    ) -> DesktopPlanView:
        """Observe ONE surface locally and open the planning card. Nothing is sent anywhere.

        `values` is the person's own typed candidate text, each with a short label THEY chose (never
        inferred from the desktop): `[(classification, raw_text), ...]`, at most `MAX_VALUES`. A refused
        or failed S1 observation raises before a task or a grant exists.
        """
        question = validate_objective(objective)
        model_name = validate_model(model)
        if len(values) > MAX_VALUES:
            raise DesktopPlanRefusal("too_many_values")
        try:
            # Validated and built up front, before any DB transaction or provider-relevant work: a
            # value the HTTP body schema allowed through (it only bounds length) but `StoredValue`'s
            # own stricter check rejects (control characters) must never surface as a raw, unhandled
            # `pydantic.ValidationError` -- that exception's own text embeds the offending input value,
            # and an uncaught exception here would otherwise reach a generic error handler and the
            # server's own logs carrying it.
            stored_values = tuple(
                StoredValue(value_ref=f"v{index + 1}", classification=classification, length=len(raw), raw=raw)
                for index, (classification, raw) in enumerate(values)
            )
        except ValidationError:
            raise DesktopPlanRefusal("value_invalid") from None
        async with self._engine.connect() as connection:
            unresolved = await DesktopActionRepository(connection).unresolved_mutation()
        if unresolved is not None:
            # A new plan is a new observation and a new provider call; the brief is explicit that
            # neither may side-step an unresolved mutation, so this refuses before Lumi even inspects
            # the window, not only later when the plan tries to fund an execution card.
            raise DesktopPlanRefusal("desktop_action_unresolved")
        listing = await self._desktop.list_surfaces()
        if listing.worker_generation != worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        target = _find_surface(listing.surfaces, surface_ref, surface_epoch)
        if target is None:
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        observation = await self._desktop.observe(worker_generation, surface_ref, surface_epoch)
        async with self._engine.begin() as connection:
            record = await DesktopRepository(connection).get_observation(observation.observation_id)
            if record is None:
                raise DesktopPlanRefusal("observation_unavailable")
            scope = DesktopPlanScope(
                observation_id=record.id,
                snapshot_digest=record.snapshot_digest,
                observed_at=record.created_at,
                worker_generation=record.worker_generation,
                surface_ref=record.surface_ref,
                surface_epoch=record.surface_epoch,
                recipient=recipient,
                model=model_name,
                objective=question,
                display=DisplayTarget(
                    application_label=target.application_label, window_title=target.window_title
                ),
                values=stored_values,
            )
            # Refuse now, with the same projection the provider would get, if it cannot be built.
            build_projection(record.snapshot, observed_at=record.created_at, withhold=scope.withheld())
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(),
                status=TaskStatus.WAITING_APPROVAL,
                request={"type": DESKTOP_ACTION_PLAN_TASK_TYPE, "objective": question},
            )
            await tasks.append_event(
                task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value}
            )
            grant = await DesktopPlanningRepository(connection).insert_grant(
                grant_id=uuid.uuid4(), task_id=task.id, scope=scope
            )
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_PLAN_REQUESTED,
                {
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "observation_id": str(scope.observation_id),
                    "recipient": scope.recipient,
                    "value_count": len(stored_values),
                },
            )
            return await self._view(connection, task.id)

    # ---- reading ------------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> DesktopPlanView:
        await self._expire_stale_claims()
        async with self._engine.connect() as connection:
            await self._require_plan_task(connection, task_id)
            return await self._view(connection, task_id)

    async def latest(self) -> DesktopPlanView | None:
        await self._expire_stale_claims()
        async with self._engine.connect() as connection:
            row = (await connection.execute(_LATEST_PLAN_TASK)).scalar_one_or_none()
            if row is None:
                return None
            return await self._view(connection, row)

    # ---- the trusted click ----------------------------------------------------------------------

    async def confirm(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int) -> DesktopPlanView:
        async with self._engine.begin() as connection:
            task = await self._lock_plan_task(connection, task_id)
            # Re-checked here too, not only in `create`: a DIFFERENT action can become unresolved in
            # the time between a plan being created and the person clicking to confirm it. Confirming
            # anyway would arm a grant that `claim` is about to release a real snapshot through.
            if await DesktopActionRepository(connection).unresolved_mutation() is not None:
                raise DesktopPlanRefusal("desktop_action_unresolved")
            repository = DesktopPlanningRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopPlanRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise DesktopPlanRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise DesktopPlanRefusal("grant_changed")
            record = await DesktopRepository(connection).get_observation(grant.scope.observation_id)
            self._require_observation(grant, record)
            assert record is not None
            if observation_age_seconds(record.created_at) > PLANNING_OBSERVATION_MAX_AGE_SECONDS:
                raise DesktopPlanRefusal("observation_stale")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id, expected_revision=expected_revision, scope_digest=grant.scope_digest,
                ttl=self._grant_ttl,
            )
            if confirmed is None:
                raise DesktopPlanRefusal("grant_changed")
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_GRANTED,
                {
                    "grant_id": str(confirmed.id), "grant_revision": confirmed.revision,
                    "grant_status": confirmed.status.value, "scope_digest": confirmed.scope_digest,
                },
            )
            return await self._view(connection, task_id)

    async def revoke(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None, reason: str
    ) -> DesktopPlanView:
        async with self._engine.begin() as connection:
            task = await self._lock_plan_task(connection, task_id, require_accepting=False)
            repository = DesktopPlanningRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopPlanRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(
                    grant_id=grant.id, status=GrantStatus.REVOKED, expected_revision=expected_revision
                )
                if closed is None:
                    raise DesktopPlanRefusal("grant_changed")
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_REVOKED,
                    {
                        "grant_id": str(closed.id), "grant_revision": closed.revision,
                        "grant_status": closed.status.value, "reason": reason,
                    },
                )
                current = await TaskRepository(connection).get_task(task.id)
                if current is not None and accepts_actions(current.status):
                    moved = await TaskRepository(connection).advance_task(
                        task_id=current.id, expected_revision=current.revision, status=TaskStatus.CANCELLED
                    )
                    if moved is not None:
                        await TaskRepository(connection).append_event(
                            task=moved, event_type=TaskEventType.TASK_CANCELLED,
                            payload={"from_status": current.status.value, "to_status": moved.status.value},
                        )
            return await self._view(connection, task_id)

    # ---- the claim -----------------------------------------------------------------------------

    async def claim(self, task_id: uuid.UUID) -> PlanProviderContext:
        async with self._engine.begin() as connection:
            task = await self._lock_plan_task(connection, task_id)
            # The last, and most important, place this is checked: `claim` is what actually releases
            # the redacted snapshot to a provider (the projection built and committed below). An
            # unresolved mutation must block this exactly as it blocks a new plan being created.
            if await DesktopActionRepository(connection).unresolved_mutation() is not None:
                raise DesktopPlanRefusal("desktop_action_unresolved")
            repository = DesktopPlanningRepository(connection)
            grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise DesktopPlanRefusal("grant_not_found")
            if grant.status is not GrantStatus.ACTIVE:
                raise DesktopPlanRefusal("grant_not_active")
            if await repository.grant_is_expired(grant.id):
                raise DesktopPlanRefusal("grant_expired")
            record = await DesktopRepository(connection).get_observation(grant.scope.observation_id)
            self._require_observation(grant, record)
            assert record is not None
            projection = build_projection(
                record.snapshot, observed_at=grant.scope.observed_at, max_nodes=grant.scope.max_nodes,
                max_text_bytes=grant.scope.max_text_bytes, withhold=grant.scope.withheld(),
            )
            claimed = await repository.claim_grant(grant_id=grant.id, expected_revision=grant.revision)
            if claimed is None:
                raise DesktopPlanRefusal("grant_not_active")
            plan = await repository.insert_plan(
                plan_id=uuid.uuid4(), task_id=task_id, grant_id=grant.id, observation_id=grant.scope.observation_id,
                snapshot_digest=grant.scope.snapshot_digest, recipient=grant.scope.recipient,
                model=grant.scope.model, projection_digest=projection.digest, node_count=projection.node_count,
                text_bytes=projection.text_bytes, redaction_count=projection.redaction_count,
                truncated=projection.truncated,
            )
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_STARTED,
                {
                    "grant_id": str(grant.id), "plan_id": str(plan.id), "observation_id": str(plan.observation_id),
                    "recipient": plan.recipient, "projection_digest": plan.projection_digest,
                    "node_count": plan.node_count, "text_bytes": plan.text_bytes,
                    "redaction_count": plan.redaction_count,
                },
            )
            await self._move_task(connection, task_id, TaskStatus.EXECUTING)
            objective = str(task.request.get("objective", ""))
        return PlanProviderContext(
            plan_id=plan.id, task_id=task_id, objective=objective, recipient=plan.recipient, model=plan.model,
            observed_at=grant.scope.observed_at, projection=projection.payload, projection_digest=projection.digest,
            value_descriptors=grant.scope.value_descriptors(),
        )

    # ---- recording the outcome -------------------------------------------------------------------

    async def record_result(
        self, task_id: uuid.UUID, *, plan_id: uuid.UUID, result: Any | None, failure: str | None
    ) -> DesktopPlanView:
        async with self._engine.begin() as connection:
            task = await self._lock_plan_task(connection, task_id, require_accepting=False)
            repository = DesktopPlanningRepository(connection)
            plan = await repository.get_plan(plan_id)
            if plan is None or plan.task_id != task_id:
                raise DesktopPlanRefusal("plan_not_started")
            if plan.status != "STARTED":
                raise DesktopPlanRefusal("plan_already_recorded")
            if (failure is None) == (result is None):
                raise DesktopPlanRefusal("result_malformed")
            if failure is not None:
                if failure not in PROVIDER_FAILURE_CODES:
                    raise DesktopPlanRefusal("failure_invalid")
                await self._fail(connection, task, plan, failure)
                return await self._view(connection, task_id)

            outcome = await self._validate(connection, plan, result)
            if isinstance(outcome, str):
                await self._fail(connection, task, plan, outcome)
                return await self._view(connection, task_id)
            action = outcome
            finished = await repository.finish_plan(
                plan_id=plan.id, status="SUCCEEDED", error_code=None, proposed_action=dump_planned_action(action)
            )
            assert finished is not None
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_ACTION_RECORDED,
                {"plan_id": str(plan.id), "action": action.action},
            )
            await self._move_task(connection, task_id, TaskStatus.SUCCEEDED)
            return await self._view(connection, task_id)

    async def _validate(
        self, connection: AsyncConnection, plan: DesktopPlanRecord, payload: Any
    ) -> "str | PlannedAction":
        try:
            parsed = parse_planned_action(payload)
        except DesktopPlanRefusal:
            return ERROR_INVALID_OUTPUT
        record = await DesktopRepository(connection).get_observation(plan.observation_id)
        if record is None or record.snapshot_digest != plan.snapshot_digest:
            return ERROR_OBSERVATION_GONE
        grant = await DesktopPlanningRepository(connection).get_grant(plan.grant_id)
        if grant is None:
            return ERROR_OBSERVATION_GONE
        projection = build_projection(
            record.snapshot, observed_at=grant.scope.observed_at, max_nodes=grant.scope.max_nodes,
            max_text_bytes=grant.scope.max_text_bytes, withhold=grant.scope.withheld(),
        )
        if projection.digest != plan.projection_digest:
            return ERROR_OBSERVATION_GONE
        try:
            validate_planned_action(projection, grant.scope, parsed)
        except DesktopPlanRefusal as refusal:
            return refusal.code
        return parsed

    async def _fail(
        self, connection: AsyncConnection, task: TaskRecord, plan: DesktopPlanRecord, code: str
    ) -> None:
        await DesktopPlanningRepository(connection).finish_plan(
            plan_id=plan.id, status="FAILED", error_code=code, proposed_action=None
        )
        await self._event(
            connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_FAILED, {"plan_id": str(plan.id), "error_code": code}
        )
        await self._move_task(connection, task.id, TaskStatus.FAILED)

    # ---- recovery -------------------------------------------------------------------------------

    async def recover_started(self) -> int:
        return await self._mark_unknown(older_than_seconds=None, code=ERROR_RUNTIME_RESTART)

    async def _expire_stale_claims(self) -> None:
        await self._mark_unknown(older_than_seconds=STALE_CLAIM_SECONDS, code=ERROR_RUNTIME_RESTART)

    async def _mark_unknown(self, *, older_than_seconds: int | None, code: str) -> int:
        async with self._engine.connect() as connection:
            started = await DesktopPlanningRepository(connection).list_started(older_than_seconds=older_than_seconds)
        marked = 0
        for plan in started:
            async with self._engine.begin() as connection:
                tasks = TaskRepository(connection)
                task = await tasks.lock_task(plan.task_id)
                repository = DesktopPlanningRepository(connection)
                finished = await repository.finish_plan(
                    plan_id=plan.id, status="OUTCOME_UNKNOWN", error_code=code, proposed_action=None
                )
                if finished is None or task is None:
                    continue
                marked += 1
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_PLAN_OUTCOME_UNKNOWN,
                    {"plan_id": str(plan.id), "error_code": code},
                )
                await self._move_task(connection, task.id, TaskStatus.OUTCOME_UNKNOWN)
        if marked:
            logger.warning(
                "%d desktop action plan(s) were in flight when their runtime stopped; their outcome is "
                "unknown and they will not be repeated.", marked,
            )
        return marked

    # ---- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _require_observation(grant: DesktopPlanGrantRecord, record: DesktopObservationRecord | None) -> None:
        if record is None:
            raise DesktopPlanRefusal("observation_unavailable")
        if record.id != grant.scope.observation_id or record.snapshot_digest != grant.scope.snapshot_digest:
            raise DesktopPlanRefusal("observation_changed")
        if (record.worker_generation, record.surface_ref, record.surface_epoch) != (
            grant.scope.worker_generation, grant.scope.surface_ref, grant.scope.surface_epoch,
        ):
            raise DesktopPlanRefusal("observation_changed")

    @staticmethod
    async def _require_plan_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_ACTION_PLAN_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_ACTION_PLAN_TASK_TYPE)
        return task

    @staticmethod
    async def _lock_plan_task(
        connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True
    ) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_ACTION_PLAN_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_ACTION_PLAN_TASK_TYPE)
        if require_accepting and not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        return task

    @staticmethod
    async def _event(
        connection: AsyncConnection, task_id: uuid.UUID, event_type: TaskEventType, payload: dict[str, Any]
    ) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        assert current is not None
        advanced = await tasks.advance_task(task_id=current.id, expected_revision=current.revision)
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)
        await tasks.append_event(task=advanced, event_type=event_type, payload=payload)

    @staticmethod
    async def _move_task(connection: AsyncConnection, task_id: uuid.UUID, status: TaskStatus) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        if current is None or not accepts_actions(current.status):
            return
        moved = await tasks.advance_task(task_id=current.id, expected_revision=current.revision, status=status)
        if moved is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> DesktopPlanView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        repository = DesktopPlanningRepository(connection)
        grant = await repository.latest_grant_for_task(task_id)
        plan = await repository.plan_for_task(task_id)
        card: PlanCardView | None = None
        age_stale = False
        if grant is not None:
            card = await self._card(connection, grant)
            age_stale = (
                grant.status is GrantStatus.PENDING
                and observation_age_seconds(grant.scope.observed_at) > PLANNING_OBSERVATION_MAX_AGE_SECONDS
            )
        expired_active = (
            grant is not None and grant.status is GrantStatus.ACTIVE and await repository.grant_is_expired(grant.id)
        )
        return DesktopPlanView(
            task_id=task.id, task_status=task.status.value, task_revision=task.revision,
            objective=str(task.request.get("objective", "")),
            phase=_phase(grant, plan, age_stale=age_stale, expired_active=bool(expired_active)),
            card=card,
            plan=None if plan is None else PlanStateView(
                plan_id=plan.id, status=plan.status, error_code=plan.error_code, started_at=plan.started_at,
                finished_at=plan.finished_at, node_count=plan.node_count, text_bytes=plan.text_bytes,
                redaction_count=plan.redaction_count, truncated=plan.truncated,
                proposed_action=plan.proposed_action,
            ),
            action_id=None,
        )

    @staticmethod
    async def _card(connection: AsyncConnection, grant: DesktopPlanGrantRecord) -> PlanCardView:
        scope = grant.scope
        projection: Projection | None = None
        if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
            record = await DesktopRepository(connection).get_observation(scope.observation_id)
            if record is not None and record.snapshot_digest == scope.snapshot_digest:
                projection = build_projection(
                    record.snapshot, observed_at=scope.observed_at, max_nodes=scope.max_nodes,
                    max_text_bytes=scope.max_text_bytes, withhold=scope.withheld(),
                )
        return PlanCardView(
            grant_id=grant.id, grant_revision=grant.revision, grant_status=grant.status.value,
            expires_at=grant.expires_at, recipient=scope.recipient, model=scope.model,
            observed_at=scope.observed_at, application_label=scope.display.application_label,
            window_title=scope.display.window_title, max_nodes=scope.max_nodes,
            max_text_bytes=scope.max_text_bytes, redaction_policy=scope.redaction_policy,
            observation_available=projection is not None,
            node_count=projection.node_count if projection else None,
            text_bytes=projection.text_bytes if projection else None,
            redaction_count=projection.redaction_count if projection else None,
            truncated=projection.truncated if projection else None,
            truncation=projection.truncation if projection else (),
            values=scope.values,
        )


def _find_surface(surfaces: list[SurfaceRecord], surface_ref: str, surface_epoch: int) -> SurfaceRecord | None:
    for surface in surfaces:
        if surface.surface_ref == surface_ref and surface.surface_epoch == surface_epoch:
            return surface
    return None


def _phase(
    grant: DesktopPlanGrantRecord | None, plan: DesktopPlanRecord | None, *, age_stale: bool, expired_active: bool
) -> str:
    if plan is not None:
        return {
            "STARTED": "reasoning", "SUCCEEDED": "proposed", "FAILED": "failed", "OUTCOME_UNKNOWN": "outcome_unknown",
        }[plan.status]
    if grant is None:
        return "declined"
    if grant.status is GrantStatus.PENDING:
        return "expired" if age_stale else "awaiting_approval"
    if grant.status is GrantStatus.ACTIVE:
        return "expired" if expired_active else "approved"
    return "declined"


_LATEST_PLAN_TASK = (
    select(tasks_table.c.id)
    .where(
        tasks_table.c.request["type"].astext == DESKTOP_ACTION_PLAN_TASK_TYPE,
        exists().where(grants_table.c.task_id == tasks_table.c.id, grants_table.c.kind == DESKTOP_PLAN_KIND),
    )
    .order_by(tasks_table.c.created_at.desc(), tasks_table.c.id)
    .limit(1)
)
