"""SQL for authenticated-read grants, step authorizations, evidence and answers.

Callers own the transaction, as everywhere else in this layer. This is the
authenticated twin of `ResearchRepository`, kept separate for the same reason
the tables are: a query over public research evidence must not be able to reach
account-private rows, and the reverse.

Two statements are load-bearing:

* `confirm_grant` is a compare-and-swap that also checks, **inside the
  statement**, that the profile the scope was shown for is still the profile the
  user saw: still `AUTHENTICATED`, still at the same `revoke_epoch`, still
  carrying the same account fingerprint. A trusted click that arrives after the
  account changed cannot activate a scope for an account nobody reviewed.
* `consume_step_authorization` re-checks every binding the research variant
  does **and** the same profile conditions, in the one `UPDATE` that consumes
  the authorization. There is no `SELECT`, decide, `UPDATE` window: the database
  decides whether a step may execute at the instant it executes.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import (
    actions,
    authenticated_answers,
    authenticated_observations,
    browser_profiles,
    step_authorizations,
    task_grants,
)
from app.domain.authenticated import (
    AUTHENTICATED_TOOL_NAMES,
    AuthenticatedAnswer,
    AuthenticatedObservation,
    AuthenticatedReadScope,
)
from app.domain.browser_profile import ProfileStatus
from app.domain.research import GrantStatus
from app.repositories.research import StepAuthorizationRecord, _authorization

KIND = "authenticated_read"
_OPEN = [GrantStatus.PENDING.value, GrantStatus.ACTIVE.value]


@dataclass(frozen=True, slots=True)
class AuthenticatedGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: AuthenticatedReadScope
    scope_digest: str
    profile_id: uuid.UUID
    profile_revoke_epoch: int
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None
    first_step_at: datetime | None
    planner_calls: int

    @property
    def kind(self) -> str:
        return KIND


@dataclass(frozen=True, slots=True)
class AuthenticatedObservationRecord:
    observation: AuthenticatedObservation
    task_id: uuid.UUID
    grant_id: uuid.UUID
    profile_id: uuid.UUID
    action_id: uuid.UUID
    attempt_id: uuid.UUID
    dispatch_id: uuid.UUID | None
    worker_generation: uuid.UUID | None
    created_at: datetime

    @property
    def id(self) -> uuid.UUID:
        return self.observation.observation_id


@dataclass(frozen=True, slots=True)
class AuthenticatedAnswerRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    profile_id: uuid.UUID
    classification: str
    answer: AuthenticatedAnswer
    provider: str
    model: str
    steps_used: int
    observations_used: int
    planner_calls: int
    created_at: datetime


def _grant(row: Row[Any]) -> AuthenticatedGrantRecord:
    return AuthenticatedGrantRecord(
        id=row.id,
        task_id=row.task_id,
        status=GrantStatus(row.status),
        revision=row.revision,
        policy_version=row.policy_version,
        scope=AuthenticatedReadScope.model_validate(row.scope),
        scope_digest=row.scope_digest,
        profile_id=row.profile_id,
        profile_revoke_epoch=row.profile_revoke_epoch,
        created_at=row.created_at,
        updated_at=row.updated_at,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        completed_at=row.completed_at,
        first_step_at=row.first_step_at,
        planner_calls=row.planner_calls,
    )


def _observation(row: Row[Any]) -> AuthenticatedObservationRecord:
    projection: dict[str, Any] = row.projection
    observation = AuthenticatedObservation(
        schema_version=row.schema_version,
        observation_id=row.id,
        provenance=row.provenance,
        classification=row.classification,
        kind=row.kind,
        operation=row.operation,
        sequence=row.sequence,
        profile_id=row.profile_id,
        tab=row.tab,
        document_epoch=row.document_epoch,
        host=row.host,
        title=row.title,
        settled=row.settled,
        truncated=row.truncated,
        observed_at=row.observed_at,
        blocks=projection["blocks"],
        links=projection["links"],
        open_tabs=projection["open_tabs"],
        total_text_chars=projection["total_text_chars"],
        total_link_count=projection["total_link_count"],
        redactions=projection["redactions"],
        content_hash=row.content_hash,
    )
    return AuthenticatedObservationRecord(
        observation=observation,
        task_id=row.task_id,
        grant_id=row.grant_id,
        profile_id=row.profile_id,
        action_id=row.action_id,
        attempt_id=row.attempt_id,
        dispatch_id=row.dispatch_id,
        worker_generation=row.worker_generation,
        created_at=row.created_at,
    )


def _answer(row: Row[Any]) -> AuthenticatedAnswerRecord:
    return AuthenticatedAnswerRecord(
        id=row.id,
        task_id=row.task_id,
        grant_id=row.grant_id,
        profile_id=row.profile_id,
        classification=row.classification,
        answer=AuthenticatedAnswer.model_validate(row.answer),
        provider=row.provider,
        model=row.model,
        steps_used=row.steps_used,
        observations_used=row.observations_used,
        planner_calls=row.planner_calls,
        created_at=row.created_at,
    )


def _profile_still_matches_grant() -> Any:
    """The profile is the one the user saw: same epoch, same account, signed in.

    Written once and used by both the confirming and the consuming statement,
    so the two can never disagree about what "still the same account" means.
    """
    return (
        select(browser_profiles.c.id)
        .where(
            browser_profiles.c.id == task_grants.c.profile_id,
            browser_profiles.c.status == ProfileStatus.AUTHENTICATED.value,
            browser_profiles.c.revoke_epoch == task_grants.c.profile_revoke_epoch,
            browser_profiles.c.account_fingerprint
            == task_grants.c.scope["account_fingerprint"].astext,
        )
        .exists()
    )


class AuthenticatedRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    # ---- grants -------------------------------------------------------------

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: AuthenticatedReadScope
    ) -> AuthenticatedGrantRecord:
        """A PENDING grant: the card's contents. It authorises nothing at all."""
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id,
                task_id=task_id,
                kind=KIND,
                status=GrantStatus.PENDING.value,
                revision=1,
                policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"),
                scope_digest=scope.digest,
                profile_id=scope.profile_id,
                profile_revoke_epoch=scope.profile_revoke_epoch,
            )
            .returning(*task_grants.c)
        )
        return _grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> AuthenticatedGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants).where(task_grants.c.id == grant_id, task_grants.c.kind == KIND)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def open_grant_for_task(self, task_id: uuid.UUID) -> AuthenticatedGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants).where(
                task_grants.c.task_id == task_id,
                task_grants.c.kind == KIND,
                task_grants.c.status.in_(_OPEN),
            )
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> AuthenticatedGrantRecord | None:
        result = await self._connection.execute(
            select(task_grants)
            .where(task_grants.c.task_id == task_id, task_grants.c.kind == KIND)
            .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
            .limit(1)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta
    ) -> AuthenticatedGrantRecord | None:
        """PENDING -> ACTIVE, only for the profile state the card showed."""
        result = await self._connection.execute(
            update(task_grants)
            .where(
                task_grants.c.id == grant_id,
                task_grants.c.kind == KIND,
                task_grants.c.revision == expected_revision,
                task_grants.c.status == GrantStatus.PENDING.value,
                task_grants.c.scope_digest == scope_digest,
                _profile_still_matches_grant(),
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
    ) -> AuthenticatedGrantRecord | None:
        """Withdraw or complete a grant. Never reversible (trigger-enforced)."""
        if status not in (GrantStatus.REVOKED, GrantStatus.EXPIRED, GrantStatus.COMPLETED):
            raise ValueError("close_grant only closes a grant")
        conditions = [
            task_grants.c.id == grant_id,
            task_grants.c.kind == KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        values: dict[str, Any] = {
            "status": status.value,
            "revision": task_grants.c.revision + 1,
            "updated_at": func.now(),
            "confirmed_at": func.coalesce(task_grants.c.confirmed_at, func.now()),
        }
        if status is GrantStatus.REVOKED:
            values["revoked_at"] = func.now()
        if status is GrantStatus.COMPLETED:
            values["completed_at"] = func.now()
        result = await self._connection.execute(
            update(task_grants).where(*conditions).values(**values).returning(*task_grants.c)
        )
        row = result.one_or_none()
        return _grant(row) if row is not None else None

    async def revoke_open_grants_for_profile(self, profile_id: uuid.UUID) -> int:
        """Close every PENDING or ACTIVE grant bound to a profile.

        Used when the profile's account changes or the profile is deleted. The
        epoch bump already makes those grants unusable; closing them as well
        makes the state say so.
        """
        result = await self._connection.execute(
            update(task_grants)
            .where(
                task_grants.c.profile_id == profile_id,
                task_grants.c.kind == KIND,
                task_grants.c.status.in_(_OPEN),
            )
            .values(
                status=GrantStatus.REVOKED.value,
                revision=task_grants.c.revision + 1,
                revoked_at=func.now(),
                confirmed_at=func.coalesce(task_grants.c.confirmed_at, func.now()),
                updated_at=func.now(),
            )
        )
        return result.rowcount or 0

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
    ) -> AuthenticatedGrantRecord | None:
        result = await self._connection.execute(
            update(task_grants)
            .where(
                task_grants.c.id == grant_id,
                task_grants.c.kind == KIND,
                task_grants.c.status == GrantStatus.ACTIVE.value,
            )
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
        grant: AuthenticatedGrantRecord,
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

    async def consume_step_authorization(
        self,
        *,
        authorization_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
        runtime_generation: uuid.UUID,
    ) -> StepAuthorizationRecord | None:
        """Consume it, or return None if it may not be used right now.

        Everything the research variant checks -- ACTIVE, unexpired grant at
        the revision it was minted against, the bound action revision and
        proposal digest, an unconsumed, unexpired authorization of this runtime
        -- plus: the grant is an `authenticated_read` grant, and its profile is
        `AUTHENTICATED` at exactly the revoke epoch and account fingerprint the
        grant was confirmed against.
        """
        grant = (
            select(task_grants.c.id)
            .where(
                task_grants.c.id == step_authorizations.c.grant_id,
                task_grants.c.kind == KIND,
                task_grants.c.status == GrantStatus.ACTIVE.value,
                task_grants.c.revision == step_authorizations.c.grant_revision,
                task_grants.c.scope_digest == step_authorizations.c.scope_digest,
                or_(task_grants.c.expires_at.is_(None), task_grants.c.expires_at > func.now()),
                _profile_still_matches_grant(),
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

    # ---- evidence -----------------------------------------------------------

    async def next_sequence(self, task_id: uuid.UUID) -> int:
        highest = await self._connection.scalar(
            select(func.max(authenticated_observations.c.sequence)).where(
                authenticated_observations.c.task_id == task_id
            )
        )
        return int(highest or 0) + 1

    async def insert_observation(
        self,
        *,
        observation: AuthenticatedObservation,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        dispatch_id: uuid.UUID | None,
        worker_generation: uuid.UUID | None,
    ) -> AuthenticatedObservationRecord:
        dumped = observation.model_dump(mode="json")
        result = await self._connection.execute(
            insert(authenticated_observations)
            .values(
                id=observation.observation_id,
                task_id=task_id,
                grant_id=grant_id,
                profile_id=observation.profile_id,
                action_id=action_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                worker_generation=worker_generation,
                sequence=observation.sequence,
                schema_version=observation.schema_version,
                classification=observation.classification,
                provenance=observation.provenance,
                kind=observation.kind,
                operation=observation.operation.value,
                tab=observation.tab,
                document_epoch=observation.document_epoch,
                host=observation.host,
                title=observation.title,
                settled=observation.settled,
                truncated=observation.truncated,
                observed_at=observation.observed_at,
                content_hash=observation.content_hash,
                projection={
                    "blocks": dumped["blocks"],
                    "links": dumped["links"],
                    "open_tabs": dumped["open_tabs"],
                    "total_text_chars": dumped["total_text_chars"],
                    "total_link_count": dumped["total_link_count"],
                    "redactions": dumped["redactions"],
                },
            )
            .returning(*authenticated_observations.c)
        )
        return _observation(result.one())

    async def list_observations(
        self, task_id: uuid.UUID, *, limit: int = 100
    ) -> list[AuthenticatedObservationRecord]:
        result = await self._connection.execute(
            select(authenticated_observations)
            .where(authenticated_observations.c.task_id == task_id)
            .order_by(authenticated_observations.c.sequence)
            .limit(limit)
        )
        return [_observation(row) for row in result]

    async def observation_by_sequence(
        self, *, task_id: uuid.UUID, sequence: int
    ) -> AuthenticatedObservationRecord | None:
        result = await self._connection.execute(
            select(authenticated_observations).where(
                authenticated_observations.c.task_id == task_id,
                authenticated_observations.c.sequence == sequence,
            )
        )
        row = result.one_or_none()
        return _observation(row) if row is not None else None

    async def count_steps(self, task_id: uuid.UUID) -> int:
        """Authenticated actions recorded for this task, executed or not."""
        value = await self._connection.scalar(
            select(func.count())
            .select_from(actions)
            .where(actions.c.task_id == task_id, actions.c.tool_name.in_(AUTHENTICATED_TOOL_NAMES))
        )
        return int(value or 0)

    async def delete_evidence_for_task(self, task_id: uuid.UUID) -> int:
        removed = 0
        for table in (authenticated_answers, authenticated_observations):
            result = await self._connection.execute(delete(table).where(table.c.task_id == task_id))
            removed += result.rowcount or 0
        return removed

    async def delete_evidence_for_profile(self, profile_id: uuid.UUID) -> int:
        removed = 0
        for table in (authenticated_answers, authenticated_observations):
            result = await self._connection.execute(
                delete(table).where(table.c.profile_id == profile_id)
            )
            removed += result.rowcount or 0
        return removed

    # ---- the answer ---------------------------------------------------------

    async def insert_answer(
        self,
        *,
        answer_id: uuid.UUID,
        task_id: uuid.UUID,
        grant_id: uuid.UUID,
        profile_id: uuid.UUID,
        answer: AuthenticatedAnswer,
        provider: str,
        model: str,
        steps_used: int,
        observations_used: int,
        planner_calls: int,
    ) -> AuthenticatedAnswerRecord:
        result = await self._connection.execute(
            insert(authenticated_answers)
            .values(
                id=answer_id,
                task_id=task_id,
                grant_id=grant_id,
                profile_id=profile_id,
                classification="account_private",
                status=answer.status,
                stop_reason=answer.stop_reason,
                answer=answer.model_dump(mode="json"),
                provider=provider,
                model=model,
                steps_used=steps_used,
                observations_used=observations_used,
                planner_calls=planner_calls,
            )
            .returning(*authenticated_answers.c)
        )
        return _answer(result.one())

    async def get_answer(self, task_id: uuid.UUID) -> AuthenticatedAnswerRecord | None:
        result = await self._connection.execute(
            select(authenticated_answers).where(authenticated_answers.c.task_id == task_id)
        )
        row = result.one_or_none()
        return _answer(row) if row is not None else None


__all__ = [
    "KIND",
    "AuthenticatedAnswerRecord",
    "AuthenticatedGrantRecord",
    "AuthenticatedObservationRecord",
    "AuthenticatedRepository",
]
