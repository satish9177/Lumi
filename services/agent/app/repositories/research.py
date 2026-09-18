"""SQL for research grants, step authorizations, sessions, observations and the
final answer. Callers own the transaction, as everywhere else in this layer.

Two statements here are load-bearing rather than convenient:

* `confirm_grant` and `revoke_grant` are compare-and-swap on the grant
  revision, so a stale trusted click cannot confirm a scope the user did not
  see, and a revocation cannot be lost to a concurrent write.
* `consume_step_authorization` re-checks *every* binding -- grant status, grant
  revision, action revision, proposal digest, scope digest, policy version and
  its own expiry -- inside the statement that consumes it. The database, not an
  earlier read, decides whether a step may execute.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import (
    actions,
    research_answers,
    research_observations,
    research_sessions,
    step_authorizations,
    task_grants,
)
from app.domain.research import (
    RESEARCH_TOOL_NAMES,
    GrantStatus,
    ResearchAnswer,
    ResearchObservation,
    ResearchScope,
)


@dataclass(frozen=True, slots=True)
class GrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    kind: str
    status: GrantStatus
    revision: int
    policy_version: str
    scope: ResearchScope
    scope_digest: str
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None
    first_step_at: datetime | None
    planner_calls: int


@dataclass(frozen=True, slots=True)
class StepAuthorizationRecord:
    id: uuid.UUID
    grant_id: uuid.UUID
    grant_revision: int
    task_id: uuid.UUID
    action_id: uuid.UUID
    action_revision: int
    proposal_digest: str
    scope_digest: str
    policy_version: str
    runtime_generation: uuid.UUID
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    worker_generation: uuid.UUID
    runtime_generation: uuid.UUID
    status: str
    created_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observation: ResearchObservation
    task_id: uuid.UUID
    grant_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    dispatch_id: uuid.UUID | None
    session_id: uuid.UUID | None
    worker_generation: uuid.UUID | None
    #: ref -> the address it resolves to. The controller's table; never sent to
    #: a model, and never rendered as a link by the UI.
    targets: dict[str, str]
    created_at: datetime

    @property
    def id(self) -> uuid.UUID:
        return self.observation.observation_id


@dataclass(frozen=True, slots=True)
class AnswerRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    answer: ResearchAnswer
    provider: str
    model: str
    steps_used: int
    observations_used: int
    planner_calls: int
    created_at: datetime


def _grant(row: Row[Any]) -> GrantRecord:
    return GrantRecord(
        id=row.id,
        task_id=row.task_id,
        kind=row.kind,
        status=GrantStatus(row.status),
        revision=row.revision,
        policy_version=row.policy_version,
        scope=ResearchScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        completed_at=row.completed_at,
        first_step_at=row.first_step_at,
        planner_calls=row.planner_calls,
    )


def _authorization(row: Row[Any]) -> StepAuthorizationRecord:
    return StepAuthorizationRecord(
        id=row.id,
        grant_id=row.grant_id,
        grant_revision=row.grant_revision,
        task_id=row.task_id,
        action_id=row.action_id,
        action_revision=row.action_revision,
        proposal_digest=row.proposal_digest,
        scope_digest=row.scope_digest,
        policy_version=row.policy_version,
        runtime_generation=row.runtime_generation,
        created_at=row.created_at,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
    )


def _session(row: Row[Any]) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        worker_generation=row.worker_generation,
        runtime_generation=row.runtime_generation,
        status=row.status,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _observation(row: Row[Any]) -> ObservationRecord:
    projection: dict[str, Any] = row.projection
    observation = ResearchObservation(
        schema_version=row.schema_version,
        observation_id=row.id,
        provenance=row.provenance,
        kind=row.kind,
        operation=row.operation,
        sequence=row.sequence,
        session_id=row.session_id,
        tab=row.tab,
        document_epoch=row.document_epoch,
        query=row.query,
        requested_url=row.requested_url,
        final_url=row.final_url,
        final_host=row.final_host,
        redirects=projection["redirects"],
        title=row.title,
        settled=row.settled,
        truncated=row.truncated,
        observed_at=row.observed_at,
        blocks=projection["blocks"],
        links=projection["links"],
        results=projection["results"],
        open_tabs=projection["open_tabs"],
        total_text_chars=projection["total_text_chars"],
        total_link_count=projection["total_link_count"],
        content_hash=row.content_hash,
    )
    return ObservationRecord(
        observation=observation,
        task_id=row.task_id,
        grant_id=row.grant_id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        dispatch_id=row.dispatch_id,
        session_id=row.session_id,
        worker_generation=row.worker_generation,
        targets=dict(row.targets),
        created_at=row.created_at,
    )


def _answer(row: Row[Any]) -> AnswerRecord:
    return AnswerRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        answer=ResearchAnswer.model_validate(row.answer),
        provider=row.provider,
        model=row.model,
        steps_used=row.steps_used,
        observations_used=row.observations_used,
        planner_calls=row.planner_calls,
        created_at=row.created_at,
    )


_OPEN = [GrantStatus.PENDING.value, GrantStatus.ACTIVE.value]


class ResearchRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- grants -------------------------------------------------------------

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: ResearchScope
    ) -> GrantRecord:
        """A PENDING grant: the card's contents. It authorises nothing at all."""
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id,
                task_id=task_id,
                kind=scope.kind,
                status=GrantStatus.PENDING.value,
                revision=1,
                policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"),
                scope_digest=scope.digest,
            )
            .returning(*task_grants.c)
        )
        return _grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> GrantRecord | None:
        result = await self._connection.execute(
            select(task_grants).where(task_grants.c.id == grant_id)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> GrantRecord | None:
        """The one PENDING or ACTIVE grant, if any (a partial unique index)."""
        result = await self._connection.execute(
            select(task_grants).where(
                task_grants.c.task_id == task_id, task_grants.c.status.in_(_OPEN)
            )
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> GrantRecord | None:
        result = await self._connection.execute(
            select(task_grants)
            .where(task_grants.c.task_id == task_id)
            .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> GrantRecord | None:
        """PENDING -> ACTIVE. The trusted click, bound to what it showed."""
        result = await self._connection.execute(
            update(task_grants)
            .where(
                task_grants.c.id == grant_id,
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
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def close_grant(
        self,
        *,
        grant_id: uuid.UUID,
        status: GrantStatus,
        expected_revision: int | None = None,
    ) -> GrantRecord | None:
        """Withdraw or complete a grant. Never reversible (trigger-enforced)."""
        if status not in (GrantStatus.REVOKED, GrantStatus.EXPIRED, GrantStatus.COMPLETED):
            raise ValueError("close_grant only closes a grant")
        conditions = [task_grants.c.id == grant_id, task_grants.c.status.in_(_OPEN)]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        values: dict[str, Any] = {
            "status": status.value,
            "revision": task_grants.c.revision + 1,
            "updated_at": func.now(),
        }
        if status is GrantStatus.REVOKED:
            values["revoked_at"] = func.now()
        if status is GrantStatus.COMPLETED:
            values["completed_at"] = func.now()
        # A never-confirmed grant that is withdrawn still needs `confirmed_at`
        # to satisfy the "not pending implies confirmed" CHECK; record the
        # moment it stopped being reviewable rather than inventing a consent.
        values["confirmed_at"] = func.coalesce(task_grants.c.confirmed_at, func.now())
        result = await self._connection.execute(
            update(task_grants).where(*conditions).values(**values).returning(*task_grants.c)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def grant_is_expired(self, grant_id: uuid.UUID) -> bool:
        """Ask the database, not the application clock."""
        return bool(
            await self._connection.scalar(
                select(
                    and_(
                        task_grants.c.expires_at.is_not(None),
                        task_grants.c.expires_at <= func.now(),
                    )
                ).where(task_grants.c.id == grant_id)
            )
        )

    async def record_step_start(
        self, *, grant_id: uuid.UUID, planner_calls: int
    ) -> GrantRecord | None:
        """Stamp the first step (active-time budget) and the planner-call high water mark."""
        result = await self._connection.execute(
            update(task_grants)
            .where(task_grants.c.id == grant_id, task_grants.c.status == GrantStatus.ACTIVE.value)
            .values(
                first_step_at=func.coalesce(task_grants.c.first_step_at, func.now()),
                planner_calls=func.greatest(task_grants.c.planner_calls, planner_calls),
                updated_at=func.now(),
            )
            .returning(*task_grants.c)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def active_seconds(self, grant_id: uuid.UUID) -> float:
        """How long this grant has been executing steps, by the database clock."""
        value = await self._connection.scalar(
            select(
                func.coalesce(
                    func.extract(
                        "epoch", func.now() - func.coalesce(task_grants.c.first_step_at, func.now())
                    ),
                    0,
                )
            ).where(task_grants.c.id == grant_id)
        )
        return float(value or 0.0)

    # ---- step authorizations ------------------------------------------------

    async def insert_step_authorization(
        self,
        *,
        authorization_id: uuid.UUID,
        grant: GrantRecord,
        task_id: uuid.UUID,
        action_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
        runtime_generation: uuid.UUID,
        ttl: timedelta,
    ) -> StepAuthorizationRecord:
        result = await self._connection.execute(
            insert(step_authorizations)
            .values(
                id=authorization_id,
                grant_id=grant.id,
                grant_revision=grant.revision,
                task_id=task_id,
                action_id=action_id,
                action_revision=action_revision,
                proposal_digest=proposal_digest,
                scope_digest=grant.scope_digest,
                policy_version=grant.policy_version,
                runtime_generation=runtime_generation,
                expires_at=func.now() + ttl,
            )
            .returning(*step_authorizations.c)
        )
        return _authorization(result.one())

    async def get_step_authorization(
        self, authorization_id: uuid.UUID
    ) -> StepAuthorizationRecord | None:
        result = await self._connection.execute(
            select(step_authorizations).where(step_authorizations.c.id == authorization_id)
        )
        row = result.one_or_none()
        return _authorization(row) if row is not None else None

    async def authorization_for_action(
        self, action_id: uuid.UUID
    ) -> StepAuthorizationRecord | None:
        result = await self._connection.execute(
            select(step_authorizations).where(step_authorizations.c.action_id == action_id)
        )
        row = result.one_or_none()
        return _authorization(row) if row is not None else None

    async def consume_step_authorization(
        self,
        *,
        authorization_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
        runtime_generation: uuid.UUID,
    ) -> StepAuthorizationRecord | None:
        """Consume it, or return None if it may not be used right now.

        Single-use, and every binding is re-checked here: the grant must still
        be ACTIVE, unexpired and at the revision the authorization was minted
        against; the action must still be at its bound revision with the bound
        proposal digest; the authorization must be unconsumed, unexpired, and
        belong to this runtime process.
        """
        grant = (
            select(task_grants.c.id)
            .where(
                task_grants.c.id == step_authorizations.c.grant_id,
                task_grants.c.status == GrantStatus.ACTIVE.value,
                task_grants.c.revision == step_authorizations.c.grant_revision,
                task_grants.c.scope_digest == step_authorizations.c.scope_digest,
                or_(task_grants.c.expires_at.is_(None), task_grants.c.expires_at > func.now()),
            )
            .exists()
        )
        result = await self._connection.execute(
            update(step_authorizations)
            .where(
                step_authorizations.c.id == authorization_id,
                step_authorizations.c.consumed_at.is_(None),
                step_authorizations.c.expires_at > func.now(),
                step_authorizations.c.action_revision == action_revision,
                step_authorizations.c.proposal_digest == proposal_digest,
                step_authorizations.c.runtime_generation == runtime_generation,
                grant,
            )
            .values(consumed_at=func.now())
            .returning(*step_authorizations.c)
        )
        row = result.one_or_none()
        return _authorization(row) if row is not None else None

    # ---- sessions -----------------------------------------------------------

    async def insert_session(
        self,
        *,
        session_id: uuid.UUID,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        worker_generation: uuid.UUID,
        runtime_generation: uuid.UUID,
    ) -> SessionRecord:
        result = await self._connection.execute(
            insert(research_sessions)
            .values(
                id=session_id,
                task_id=task_id,
                grant_id=grant_id,
                worker_generation=worker_generation,
                runtime_generation=runtime_generation,
                status="OPEN",
            )
            .returning(*research_sessions.c)
        )
        return _session(result.one())

    async def open_session_for_task(self, task_id: uuid.UUID) -> SessionRecord | None:
        result = await self._connection.execute(
            select(research_sessions)
            .where(research_sessions.c.task_id == task_id, research_sessions.c.status == "OPEN")
            .order_by(research_sessions.c.created_at.desc(), research_sessions.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _session(row) if row is not None else None

    async def close_session(self, *, session_id: uuid.UUID, status: str) -> SessionRecord | None:
        if status not in ("CLOSED", "STALE"):
            raise ValueError("a session closes as CLOSED or STALE")
        result = await self._connection.execute(
            update(research_sessions)
            .where(research_sessions.c.id == session_id, research_sessions.c.status == "OPEN")
            .values(status=status, closed_at=func.now())
            .returning(*research_sessions.c)
        )
        row = result.one_or_none()
        return _session(row) if row is not None else None

    async def stale_sessions_from_other_generations(
        self, current_generation: uuid.UUID
    ) -> list[SessionRecord]:
        """Open sessions a previous runtime process left behind.

        Their native browser contexts are gone with that process, so every
        semantic ref they issued has to stop resolving.
        """
        result = await self._connection.execute(
            select(research_sessions)
            .where(
                research_sessions.c.status == "OPEN",
                research_sessions.c.runtime_generation != current_generation,
            )
            .order_by(research_sessions.c.created_at, research_sessions.c.id)
        )
        return [_session(row) for row in result]

    # ---- observations -------------------------------------------------------

    async def next_sequence(self, task_id: uuid.UUID) -> int:
        highest = await self._connection.scalar(
            select(func.max(research_observations.c.sequence)).where(
                research_observations.c.task_id == task_id
            )
        )
        return int(highest or 0) + 1

    async def insert_observation(
        self,
        *,
        observation: ResearchObservation,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        dispatch_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        worker_generation: uuid.UUID | None,
        targets: dict[str, str],
    ) -> ObservationRecord:
        dumped = observation.model_dump(mode="json")
        result = await self._connection.execute(
            insert(research_observations)
            .values(
                id=observation.observation_id,
                task_id=task_id,
                grant_id=grant_id,
                action_id=action_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                session_id=session_id,
                worker_generation=worker_generation,
                sequence=observation.sequence,
                schema_version=observation.schema_version,
                provenance=observation.provenance,
                kind=observation.kind,
                operation=observation.operation.value,
                tab=observation.tab,
                document_epoch=observation.document_epoch,
                query=observation.query,
                requested_url=observation.requested_url,
                final_url=observation.final_url,
                final_host=observation.final_host,
                title=observation.title,
                settled=observation.settled,
                truncated=observation.truncated,
                observed_at=observation.observed_at,
                content_hash=observation.content_hash,
                projection={
                    "redirects": dumped["redirects"],
                    "blocks": dumped["blocks"],
                    "links": dumped["links"],
                    "results": dumped["results"],
                    "open_tabs": dumped["open_tabs"],
                    "total_text_chars": dumped["total_text_chars"],
                    "total_link_count": dumped["total_link_count"],
                },
                targets=targets,
            )
            .returning(*research_observations.c)
        )
        return _observation(result.one())

    async def list_observations(
        self, task_id: uuid.UUID, *, limit: int = 100
    ) -> list[ObservationRecord]:
        result = await self._connection.execute(
            select(research_observations)
            .where(research_observations.c.task_id == task_id)
            .order_by(research_observations.c.sequence)
            .limit(limit)
        )
        return [_observation(row) for row in result]

    async def observation_by_sequence(
        self, *, task_id: uuid.UUID, sequence: int
    ) -> ObservationRecord | None:
        result = await self._connection.execute(
            select(research_observations).where(
                research_observations.c.task_id == task_id,
                research_observations.c.sequence == sequence,
            )
        )
        row = result.one_or_none()
        return _observation(row) if row is not None else None

    async def count_observations(self, task_id: uuid.UUID) -> int:
        value = await self._connection.scalar(
            select(func.count())
            .select_from(research_observations)
            .where(research_observations.c.task_id == task_id)
        )
        return int(value or 0)

    async def count_steps(self, task_id: uuid.UUID) -> int:
        """Research actions recorded for this task, executed or not."""
        value = await self._connection.scalar(
            select(func.count())
            .select_from(actions)
            .where(actions.c.task_id == task_id, actions.c.tool_name.in_(RESEARCH_TOOL_NAMES))
        )
        return int(value or 0)

    # ---- the answer ---------------------------------------------------------

    async def insert_answer(
        self,
        *,
        answer_id: uuid.UUID,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        answer: ResearchAnswer,
        provider: str,
        model: str,
        steps_used: int,
        observations_used: int,
        planner_calls: int,
    ) -> AnswerRecord:
        result = await self._connection.execute(
            insert(research_answers)
            .values(
                id=answer_id,
                task_id=task_id,
                grant_id=grant_id,
                status=answer.status,
                stop_reason=answer.stop_reason,
                answer=answer.model_dump(mode="json"),
                provider=provider,
                model=model,
                steps_used=steps_used,
                observations_used=observations_used,
                planner_calls=planner_calls,
            )
            .returning(*research_answers.c)
        )
        return _answer(result.one())

    async def get_answer(self, task_id: uuid.UUID) -> AnswerRecord | None:
        result = await self._connection.execute(
            select(research_answers).where(research_answers.c.task_id == task_id)
        )
        row = result.one_or_none()
        return _answer(row) if row is not None else None
