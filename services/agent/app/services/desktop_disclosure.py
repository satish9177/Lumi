"""Milestone 9 S2: the runtime's desktop disclosure service.

This service decides **who may see an already-captured desktop snapshot**. It never touches the
desktop: the only desktop calls it makes are S1's two read-only ones (list, observe), and it has no
way to ask for anything else.

```text
create   observe ONE surface locally (S1)            -> task + PENDING grant (the card). No provider.
confirm  the trusted click                            -> grant ACTIVE, bound to that exact snapshot.
claim    validate + project + consume the grant       -> ONE committed transaction, THEN the provider.
record   the provider's read-only result, or failure  -> grounded answer | FAILED | (never a replay).
```

Ordering is the safety property. The single-use grant is consumed and a UNIQUE disclosure row is
written *in one transaction that commits before* the projection is returned to Electron main, and no
database transaction is open while the provider is called. If the process dies anywhere after that
commit, Lumi cannot know whether the provider received the snapshot, so the disclosure is
`OUTCOME_UNKNOWN` and is never replayed: a retry needs a new observation and a new trusted approval.

The provider projection is recomputed, deterministically, from the persisted observation when a
result is recorded, so grounding checks a quote against exactly the text the provider was allowed to
see (redacted, bounded) and never against the raw observation.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import task_grants as grants_table
from app.db.tables import tasks as tasks_table
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import SurfaceRecord
from app.domain.desktop_disclosure import (
    DESKTOP_DISCLOSE_KIND,
    DESKTOP_READ_TASK_TYPE,
    ERROR_INVALID_OUTPUT,
    ERROR_NOT_GROUNDED,
    ERROR_OBSERVATION_GONE,
    ERROR_RUNTIME_RESTART,
    MAX_OBSERVATION_AGE_SECONDS,
    PROVIDER_FAILURE_CODES,
    STALE_CLAIM_SECONDS,
    AnswerNotGroundedError,
    DesktopAnswer,
    DesktopDisclosureRefusal,
    DesktopDiscloseScope,
    DesktopReadResult,
    DisplayTarget,
    Projection,
    build_projection,
    observation_age_seconds,
    parse_read_result,
    validate_model,
    validate_objective,
    verify_grounding,
)
from app.domain.authenticated import Recipient
from app.domain.errors import (
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.repositories.desktop import DesktopObservationRecord, DesktopRepository
from app.repositories.desktop_disclosure import (
    AnswerRecord,
    DesktopDisclosureRepository,
    DesktopGrantRecord,
    DisclosureRecord,
)
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.desktop import DesktopService

logger = logging.getLogger("lumi.desktop_disclosure")


# ---- views ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CardView:
    """What the trusted card shows. The provider, snapshot and limits are the runtime's, not the renderer's."""

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


@dataclass(frozen=True, slots=True)
class DisclosureStateView:
    disclosure_id: uuid.UUID
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    node_count: int
    text_bytes: int
    redaction_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class DesktopReadView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: str
    card: CardView | None
    disclosure: DisclosureStateView | None
    answer: AnswerRecord | None


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """What goes to Electron main, once, after the claim has committed."""

    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    objective: str
    recipient: str
    model: str
    observed_at: datetime
    projection: dict[str, Any]
    projection_digest: str


# ---- the service ---------------------------------------------------------------------------


class DesktopDisclosureService:
    """Task use cases. Each method is short transactions with no provider call inside."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        desktop: DesktopService,
        grant_ttl_seconds: int,
    ) -> None:
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
    ) -> DesktopReadView:
        """Observe ONE surface locally and open the disclosure card. Nothing is sent anywhere.

        A refused or failed S1 observation raises before a task or a grant exists, so a credential
        surface, an elevated window or a changed surface can never reach a disclosure card.
        """
        question = validate_objective(objective)
        model_name = validate_model(model)
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
                raise DesktopDisclosureRefusal("observation_unavailable")
            scope = DesktopDiscloseScope(
                observation_id=record.id,
                snapshot_digest=record.snapshot_digest,
                observed_at=record.created_at,
                worker_generation=record.worker_generation,
                surface_ref=record.surface_ref,
                surface_epoch=record.surface_epoch,
                recipient=recipient,
                model=model_name,
                display=DisplayTarget(
                    application_label=target.application_label, window_title=target.window_title
                ),
            )
            # Refuse now, with the same projection the provider would get, if it cannot be built.
            build_projection(record.snapshot, observed_at=record.created_at, withhold=scope.withheld())
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(),
                status=TaskStatus.WAITING_APPROVAL,
                request={"type": DESKTOP_READ_TASK_TYPE, "objective": question},
            )
            await tasks.append_event(
                task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value}
            )
            grant = await DesktopDisclosureRepository(connection).insert_grant(
                grant_id=uuid.uuid4(), task_id=task.id, scope=scope
            )
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_DISCLOSURE_REQUESTED,
                {
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "observation_id": str(scope.observation_id),
                    "recipient": scope.recipient,
                },
            )
            return await self._view(connection, task.id)

    # ---- reading ------------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> DesktopReadView:
        await self._expire_stale_claims()
        async with self._engine.connect() as connection:
            await self._require_desktop_task(connection, task_id)
            return await self._view(connection, task_id)

    async def latest(self) -> DesktopReadView | None:
        await self._expire_stale_claims()
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(_LATEST_DESKTOP_TASK)
            ).scalar_one_or_none()
            if row is None:
                return None
            return await self._view(connection, row)

    # ---- the trusted click ----------------------------------------------------------------------

    async def confirm(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> DesktopReadView:
        """The only way a disclosure grant becomes usable. Names the grant and the revision shown."""
        async with self._engine.begin() as connection:
            task = await self._lock_desktop_task(connection, task_id)
            repository = DesktopDisclosureRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopDisclosureRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise DesktopDisclosureRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise DesktopDisclosureRefusal("grant_changed")
            record = await DesktopRepository(connection).get_observation(grant.scope.observation_id)
            self._require_observation(grant, record)
            assert record is not None
            if observation_age_seconds(record.created_at) > MAX_OBSERVATION_AGE_SECONDS:
                raise DesktopDisclosureRefusal("observation_stale")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id,
                expected_revision=expected_revision,
                scope_digest=grant.scope_digest,
                ttl=self._grant_ttl,
            )
            if confirmed is None:
                raise DesktopDisclosureRefusal("grant_changed")
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_DISCLOSURE_GRANTED,
                {
                    "grant_id": str(confirmed.id),
                    "grant_revision": confirmed.revision,
                    "grant_status": confirmed.status.value,
                    "scope_digest": confirmed.scope_digest,
                },
            )
            return await self._view(connection, task_id)

    async def revoke(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None, reason: str
    ) -> DesktopReadView:
        """Decline or withdraw. Before the claim nothing was sent; after it, this cannot un-send."""
        async with self._engine.begin() as connection:
            task = await self._lock_desktop_task(connection, task_id, require_accepting=False)
            repository = DesktopDisclosureRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopDisclosureRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(
                    grant_id=grant.id, status=GrantStatus.REVOKED, expected_revision=expected_revision
                )
                if closed is None:
                    raise DesktopDisclosureRefusal("grant_changed")
                await self._event(
                    connection,
                    task.id,
                    TaskEventType.TASK_DESKTOP_DISCLOSURE_REVOKED,
                    {
                        "grant_id": str(closed.id),
                        "grant_revision": closed.revision,
                        "grant_status": closed.status.value,
                        "reason": reason,
                    },
                )
                current = await TaskRepository(connection).get_task(task.id)
                if current is not None and accepts_actions(current.status):
                    moved = await TaskRepository(connection).advance_task(
                        task_id=current.id, expected_revision=current.revision, status=TaskStatus.CANCELLED
                    )
                    if moved is not None:
                        await TaskRepository(connection).append_event(
                            task=moved,
                            event_type=TaskEventType.TASK_CANCELLED,
                            payload={"from_status": current.status.value, "to_status": moved.status.value},
                        )
            return await self._view(connection, task_id)

    # ---- the claim -----------------------------------------------------------------------------

    async def claim(self, task_id: uuid.UUID) -> ProviderContext:
        """Consume the grant and open the disclosure, then return the projection for ONE provider call.

        One transaction, under the task lock, that commits before this method returns:

        1. the grant must be ACTIVE, unexpired, and bound to an observation that still exists with
           exactly the digest that was approved;
        2. the deterministic redacted projection is built from THAT observation;
        3. the grant is swapped ACTIVE -> COMPLETED (compare-and-swap) and a disclosure row is written
           (`grant_id` UNIQUE), so nothing can claim the same approval twice.

        Whatever happens next (crash, timeout, garbage), this approval is spent.
        """
        async with self._engine.begin() as connection:
            task = await self._lock_desktop_task(connection, task_id)
            repository = DesktopDisclosureRepository(connection)
            grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise DesktopDisclosureRefusal("grant_not_found")
            if grant.status is not GrantStatus.ACTIVE:
                raise DesktopDisclosureRefusal("grant_not_active")
            if await repository.grant_is_expired(grant.id):
                raise DesktopDisclosureRefusal("grant_expired")
            record = await DesktopRepository(connection).get_observation(grant.scope.observation_id)
            self._require_observation(grant, record)
            assert record is not None
            projection = build_projection(
                record.snapshot,
                observed_at=grant.scope.observed_at,
                max_nodes=grant.scope.max_nodes,
                max_text_bytes=grant.scope.max_text_bytes,
                withhold=grant.scope.withheld(),
            )
            claimed = await repository.claim_grant(grant_id=grant.id, expected_revision=grant.revision)
            if claimed is None:
                raise DesktopDisclosureRefusal("grant_not_active")
            disclosure = await repository.insert_disclosure(
                disclosure_id=uuid.uuid4(),
                task_id=task_id,
                grant_id=grant.id,
                observation_id=grant.scope.observation_id,
                snapshot_digest=grant.scope.snapshot_digest,
                recipient=grant.scope.recipient,
                model=grant.scope.model,
                projection_digest=projection.digest,
                node_count=projection.node_count,
                text_bytes=projection.text_bytes,
                redaction_count=projection.redaction_count,
                truncated=projection.truncated,
            )
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_DISCLOSURE_STARTED,
                {
                    "grant_id": str(grant.id),
                    "disclosure_id": str(disclosure.id),
                    "observation_id": str(disclosure.observation_id),
                    "recipient": disclosure.recipient,
                    "projection_digest": disclosure.projection_digest,
                    "node_count": disclosure.node_count,
                    "text_bytes": disclosure.text_bytes,
                    "redaction_count": disclosure.redaction_count,
                },
            )
            await self._move_task(connection, task_id, TaskStatus.EXECUTING)
            objective = str(task.request.get("objective", ""))
        # The transaction has committed: the claim is durable before any provider is contacted.
        return ProviderContext(
            disclosure_id=disclosure.id,
            task_id=task_id,
            objective=objective,
            recipient=disclosure.recipient,
            model=disclosure.model,
            observed_at=grant.scope.observed_at,
            projection=projection.payload,
            projection_digest=projection.digest,
        )

    # ---- recording the outcome -------------------------------------------------------------------

    async def record_result(
        self,
        task_id: uuid.UUID,
        *,
        disclosure_id: uuid.UUID,
        result: Any | None,
        failure: str | None,
    ) -> DesktopReadView:
        """Record the ONE provider attempt's outcome. A failure or an ungrounded answer ends the task.

        Nothing here can start another provider call: a spent approval cannot be claimed again, and a
        failed or ungrounded attempt is recorded as final. Only a disclosure still `STARTED` may be
        finished, so a result that arrives after Lumi already decided the outcome was unknown is
        refused rather than merged into a record that says otherwise.
        """
        async with self._engine.begin() as connection:
            task = await self._lock_desktop_task(connection, task_id, require_accepting=False)
            repository = DesktopDisclosureRepository(connection)
            disclosure = await repository.get_disclosure(disclosure_id)
            if disclosure is None or disclosure.task_id != task_id:
                raise DesktopDisclosureRefusal("disclosure_not_started")
            if disclosure.status != "STARTED":
                raise DesktopDisclosureRefusal("disclosure_already_recorded")
            if (failure is None) == (result is None):
                raise DesktopDisclosureRefusal("result_malformed")
            if failure is not None:
                if failure not in PROVIDER_FAILURE_CODES:
                    raise DesktopDisclosureRefusal("failure_invalid")
                await self._fail(connection, task, disclosure, failure)
                return await self._view(connection, task_id)

            grounded = await self._ground(connection, disclosure, result)
            if isinstance(grounded, str):
                await self._fail(connection, task, disclosure, grounded)
                return await self._view(connection, task_id)
            parsed, quotes = grounded
            evidence = (
                [
                    {"control_ref": item.control_ref, "quote": quote}
                    for item, quote in zip(parsed.evidence, quotes, strict=True)
                ]
                if isinstance(parsed, DesktopAnswer)
                else []
            )
            try:
                # A savepoint: a database refusal of the private answer must not abort the transaction that
                # records the failure, and must not surface its statement parameters (the answer text).
                async with connection.begin_nested():
                    answer = await repository.insert_answer(
                        answer_id=uuid.uuid4(),
                        task_id=task_id,
                        disclosure_id=disclosure.id,
                        observation_id=disclosure.observation_id,
                        recipient=disclosure.recipient,
                        model=disclosure.model,
                        kind=parsed.kind,
                        answer=parsed.answer if isinstance(parsed, DesktopAnswer) else None,
                        reason=None if isinstance(parsed, DesktopAnswer) else parsed.reason,
                        evidence=evidence,
                    )
            except SQLAlchemyError:
                logger.error("a desktop answer could not be stored")
                await self._fail(connection, task, disclosure, ERROR_INVALID_OUTPUT)
                return await self._view(connection, task_id)
            await repository.finish_disclosure(disclosure_id=disclosure.id, status="SUCCEEDED", error_code=None)
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_ANSWER_RECORDED,
                {
                    "disclosure_id": str(disclosure.id),
                    "answer_id": str(answer.id),
                    "kind": answer.kind,
                    "evidence_count": len(evidence),
                },
            )
            await self._move_task(connection, task_id, TaskStatus.SUCCEEDED)
            return await self._view(connection, task_id)

    async def _ground(
        self, connection: AsyncConnection, disclosure: DisclosureRecord, payload: Any
    ) -> "str | tuple[DesktopReadResult, list[str]]":
        """The parsed result and the evidence to store, or the closed failure code that ends the attempt."""
        try:
            parsed = parse_read_result(payload)
        except DesktopDisclosureRefusal:
            return ERROR_INVALID_OUTPUT
        record = await DesktopRepository(connection).get_observation(disclosure.observation_id)
        if record is None or record.snapshot_digest != disclosure.snapshot_digest:
            return ERROR_OBSERVATION_GONE
        # The recomputed projection must be exactly the one the provider was given: same observation,
        # same disclosed `observed_at`, same limits, so the audit digest matches.
        grant = await DesktopDisclosureRepository(connection).get_grant(disclosure.grant_id)
        if grant is None:
            return ERROR_OBSERVATION_GONE
        projection = build_projection(
            record.snapshot,
            observed_at=grant.scope.observed_at,
            max_nodes=grant.scope.max_nodes,
            max_text_bytes=grant.scope.max_text_bytes,
            withhold=grant.scope.withheld(),
        )
        if projection.digest != disclosure.projection_digest:
            return ERROR_NOT_GROUNDED
        try:
            quotes = verify_grounding(projection, parsed)
        except AnswerNotGroundedError:
            return ERROR_NOT_GROUNDED
        return parsed, quotes

    async def _fail(
        self, connection: AsyncConnection, task: TaskRecord, disclosure: DisclosureRecord, code: str
    ) -> None:
        await DesktopDisclosureRepository(connection).finish_disclosure(
            disclosure_id=disclosure.id, status="FAILED", error_code=code
        )
        await self._event(
            connection,
            task.id,
            TaskEventType.TASK_DESKTOP_DISCLOSURE_FAILED,
            {"disclosure_id": str(disclosure.id), "error_code": code},
        )
        await self._move_task(connection, task.id, TaskStatus.FAILED)

    # ---- recovery -------------------------------------------------------------------------------

    async def recover_started(self) -> int:
        """Startup: a disclosure still STARTED belongs to a runtime that died. Its outcome is unknown.

        Lumi cannot tell whether the approved provider received the private snapshot, so the row is
        `OUTCOME_UNKNOWN` (never `FAILED`, which would claim knowledge Lumi does not have) and the
        provider is never called again automatically. A retry is a new observation and a new approval.
        """
        return await self._mark_unknown(older_than_seconds=None, code=ERROR_RUNTIME_RESTART)

    async def _expire_stale_claims(self) -> None:
        """A claim whose result never arrived within any provider deadline is not left "in flight"."""
        await self._mark_unknown(older_than_seconds=STALE_CLAIM_SECONDS, code=ERROR_RUNTIME_RESTART)

    async def _mark_unknown(self, *, older_than_seconds: int | None, code: str) -> int:
        async with self._engine.connect() as connection:
            started = await DesktopDisclosureRepository(connection).list_started(
                older_than_seconds=older_than_seconds
            )
        marked = 0
        for disclosure in started:
            async with self._engine.begin() as connection:
                tasks = TaskRepository(connection)
                task = await tasks.lock_task(disclosure.task_id)
                repository = DesktopDisclosureRepository(connection)
                finished = await repository.finish_disclosure(
                    disclosure_id=disclosure.id, status="OUTCOME_UNKNOWN", error_code=code
                )
                if finished is None or task is None:
                    continue
                marked += 1
                await self._event(
                    connection,
                    task.id,
                    TaskEventType.TASK_DESKTOP_DISCLOSURE_OUTCOME_UNKNOWN,
                    {"disclosure_id": str(disclosure.id), "error_code": code},
                )
                await self._move_task(connection, task.id, TaskStatus.OUTCOME_UNKNOWN)
        if marked:
            logger.warning(
                "%d desktop disclosure(s) were in flight when their runtime stopped; their outcome is "
                "unknown and they will not be repeated.",
                marked,
            )
        return marked

    # ---- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _require_observation(grant: DesktopGrantRecord, record: DesktopObservationRecord | None) -> None:
        if record is None:
            raise DesktopDisclosureRefusal("observation_unavailable")
        if record.id != grant.scope.observation_id or record.snapshot_digest != grant.scope.snapshot_digest:
            raise DesktopDisclosureRefusal("observation_changed")
        if (record.worker_generation, record.surface_ref, record.surface_epoch) != (
            grant.scope.worker_generation,
            grant.scope.surface_ref,
            grant.scope.surface_epoch,
        ):
            raise DesktopDisclosureRefusal("observation_changed")

    @staticmethod
    async def _require_desktop_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_READ_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_READ_TASK_TYPE)
        return task

    @staticmethod
    async def _lock_desktop_task(
        connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True
    ) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_READ_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_READ_TASK_TYPE)
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

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> DesktopReadView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        repository = DesktopDisclosureRepository(connection)
        grant = await repository.latest_grant_for_task(task_id)
        disclosure = await repository.disclosure_for_task(task_id)
        answer = await repository.answer_for_task(task_id)
        card: CardView | None = None
        age_stale = False
        if grant is not None:
            card = await self._card(connection, grant)
            age_stale = (
                grant.status is GrantStatus.PENDING
                and observation_age_seconds(grant.scope.observed_at) > MAX_OBSERVATION_AGE_SECONDS
            )
        expired_active = (
            grant is not None
            and grant.status is GrantStatus.ACTIVE
            and await repository.grant_is_expired(grant.id)
        )
        return DesktopReadView(
            task_id=task.id,
            task_status=task.status.value,
            task_revision=task.revision,
            objective=str(task.request.get("objective", "")),
            phase=_phase(grant, disclosure, age_stale=age_stale, expired_active=bool(expired_active)),
            card=card,
            disclosure=None
            if disclosure is None
            else DisclosureStateView(
                disclosure_id=disclosure.id,
                status=disclosure.status,
                error_code=disclosure.error_code,
                started_at=disclosure.started_at,
                finished_at=disclosure.finished_at,
                node_count=disclosure.node_count,
                text_bytes=disclosure.text_bytes,
                redaction_count=disclosure.redaction_count,
                truncated=disclosure.truncated,
            ),
            answer=answer,
        )

    @staticmethod
    async def _card(connection: AsyncConnection, grant: DesktopGrantRecord) -> CardView:
        scope = grant.scope
        projection: Projection | None = None
        if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
            record = await DesktopRepository(connection).get_observation(scope.observation_id)
            if record is not None and record.snapshot_digest == scope.snapshot_digest:
                projection = build_projection(
                    record.snapshot,
                    observed_at=scope.observed_at,
                    max_nodes=scope.max_nodes,
                    max_text_bytes=scope.max_text_bytes,
                    withhold=scope.withheld(),
                )
        return CardView(
            grant_id=grant.id,
            grant_revision=grant.revision,
            grant_status=grant.status.value,
            expires_at=grant.expires_at,
            recipient=scope.recipient,
            model=scope.model,
            observed_at=scope.observed_at,
            application_label=scope.display.application_label,
            window_title=scope.display.window_title,
            max_nodes=scope.max_nodes,
            max_text_bytes=scope.max_text_bytes,
            redaction_policy=scope.redaction_policy,
            observation_available=projection is not None,
            node_count=projection.node_count if projection else None,
            text_bytes=projection.text_bytes if projection else None,
            redaction_count=projection.redaction_count if projection else None,
            truncated=projection.truncated if projection else None,
            truncation=projection.truncation if projection else (),
        )


def _find_surface(surfaces: list[SurfaceRecord], surface_ref: str, surface_epoch: int) -> SurfaceRecord | None:
    for surface in surfaces:
        if surface.surface_ref == surface_ref and surface.surface_epoch == surface_epoch:
            return surface
    return None


def _phase(
    grant: DesktopGrantRecord | None,
    disclosure: DisclosureRecord | None,
    *,
    age_stale: bool,
    expired_active: bool,
) -> str:
    if disclosure is not None:
        return {
            "STARTED": "reasoning",
            "SUCCEEDED": "answered",
            "FAILED": "failed",
            "OUTCOME_UNKNOWN": "outcome_unknown",
        }[disclosure.status]
    if grant is None:
        return "declined"
    if grant.status is GrantStatus.PENDING:
        return "expired" if age_stale else "awaiting_approval"
    if grant.status is GrantStatus.ACTIVE:
        return "expired" if expired_active else "approved"
    return "declined"


_LATEST_DESKTOP_TASK = (
    select(tasks_table.c.id)
    .where(
        tasks_table.c.request["type"].astext == DESKTOP_READ_TASK_TYPE,
        # Only a task that really carries a disclosure grant: a task another route merely *named*
        # desktop_read must not hide the real card.
        exists().where(grants_table.c.task_id == tasks_table.c.id, grants_table.c.kind == DESKTOP_DISCLOSE_KIND),
    )
    .order_by(tasks_table.c.created_at.desc(), tasks_table.c.id)
    .limit(1)
)
