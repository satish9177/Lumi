"""SQL for `login_attempts`. Callers own the transaction.

Every transition here is a compare-and-swap written into the `UPDATE`'s
`WHERE` clause, in the repository's existing idiom: the conditions under which
a transition is legal are part of the statement, not an earlier `SELECT`, so
two concurrent callers racing for the same attempt cannot both win.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import Row, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import login_attempts
from app.domain.login_takeover import (
    OPEN_LOGIN_ATTEMPT_STATUSES,
    LoginAttempt,
    LoginAttemptStatus,
)

_OPEN = tuple(status.value for status in OPEN_LOGIN_ATTEMPT_STATUSES)


def _attempt(row: Row[tuple[object, ...]]) -> LoginAttempt:
    mapping = row._mapping
    return LoginAttempt(
        id=mapping["id"],
        profile_id=mapping["profile_id"],
        runtime_generation=mapping["runtime_generation"],
        worker_generation=mapping["worker_generation"],
        profile_revision=mapping["profile_revision"],
        started_at=mapping["started_at"],
        expires_at=mapping["expires_at"],
        completed_at=mapping["completed_at"],
        cancelled_at=mapping["cancelled_at"],
        status=LoginAttemptStatus(mapping["status"]),
    )


class LoginAttemptRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def create(
        self,
        *,
        attempt_id: uuid.UUID,
        profile_id: uuid.UUID,
        runtime_generation: uuid.UUID,
        profile_revision: int,
        ttl: timedelta,
    ) -> LoginAttempt:
        """Open one attempt. Refused by the partial unique index if one is
        already open for this profile -- the caller turns the resulting
        integrity error into `login_attempt_already_open`."""
        result = await self._connection.execute(
            login_attempts.insert()
            .values(
                id=attempt_id,
                profile_id=profile_id,
                runtime_generation=runtime_generation,
                profile_revision=profile_revision,
                status=LoginAttemptStatus.OPEN.value,
                expires_at=func.now() + ttl,
            )
            .returning(*login_attempts.c)
        )
        return _attempt(result.one())

    async def get(self, attempt_id: uuid.UUID) -> LoginAttempt | None:
        result = await self._connection.execute(
            select(login_attempts).where(login_attempts.c.id == attempt_id)
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def find_open_for_profile(self, profile_id: uuid.UUID) -> LoginAttempt | None:
        result = await self._connection.execute(
            select(login_attempts).where(
                login_attempts.c.profile_id == profile_id,
                login_attempts.c.status.in_(_OPEN),
            )
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def list_open(self) -> list[LoginAttempt]:
        """Every attempt still in an open status, across all profiles.

        The desktop's capture guard reads this after an Electron-main restart:
        an open row is the only durable evidence that a headed, human-driven
        browser may still be on screen. Status alone decides -- an `OPEN` row
        whose `expires_at` has passed but which the sweep has not settled yet
        still counts, because the sweep is also what closes the window.
        """
        result = await self._connection.execute(
            select(login_attempts)
            .where(login_attempts.c.status.in_(_OPEN))
            .order_by(login_attempts.c.started_at)
        )
        return [_attempt(row) for row in result.all()]

    async def set_worker_generation(
        self, *, attempt_id: uuid.UUID, worker_generation: uuid.UUID
    ) -> LoginAttempt | None:
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.id == attempt_id,
                login_attempts.c.status == LoginAttemptStatus.OPEN.value,
            )
            .values(worker_generation=worker_generation, updated_at=func.now())
            .returning(*login_attempts.c)
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def begin_confirm(self, attempt_id: uuid.UUID) -> LoginAttempt | None:
        """`OPEN` -> `UNCONFIRMED`. Refused if already terminal, or expired."""
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.id == attempt_id,
                login_attempts.c.status == LoginAttemptStatus.OPEN.value,
                login_attempts.c.expires_at > func.now(),
            )
            .values(status=LoginAttemptStatus.UNCONFIRMED.value, updated_at=func.now())
            .returning(*login_attempts.c)
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def complete(self, attempt_id: uuid.UUID) -> LoginAttempt | None:
        """`UNCONFIRMED` -> `COMPLETED`, regardless of the verdict it found."""
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.id == attempt_id,
                login_attempts.c.status == LoginAttemptStatus.UNCONFIRMED.value,
            )
            .values(
                status=LoginAttemptStatus.COMPLETED.value,
                completed_at=func.now(),
                updated_at=func.now(),
            )
            .returning(*login_attempts.c)
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def cancel(self, attempt_id: uuid.UUID) -> LoginAttempt | None:
        """`OPEN` -> `CANCELLED`. Cancellation is not logout."""
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.id == attempt_id,
                login_attempts.c.status == LoginAttemptStatus.OPEN.value,
            )
            .values(
                status=LoginAttemptStatus.CANCELLED.value,
                cancelled_at=func.now(),
                updated_at=func.now(),
            )
            .returning(*login_attempts.c)
        )
        row = result.one_or_none()
        return _attempt(row) if row is not None else None

    async def expire_due(self, *, now: datetime) -> list[LoginAttempt]:
        """Every `OPEN` attempt whose hard timeout has passed, moved to
        `EXPIRED` in one statement. The caller then closes each profile's
        headed context; this method only settles the durable record."""
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.status == LoginAttemptStatus.OPEN.value,
                login_attempts.c.expires_at <= now,
            )
            .values(status=LoginAttemptStatus.EXPIRED.value, updated_at=now)
            .returning(*login_attempts.c)
        )
        return [_attempt(row) for row in result.all()]

    async def mark_interrupted_for_stale_generations(
        self, current_runtime_generation: uuid.UUID
    ) -> list[LoginAttempt]:
        """Every open attempt belonging to a runtime generation that is gone.

        A runtime holds an exclusive advisory lock for its whole life, so any
        open attempt naming a *different* generation describes a process that
        died with the takeover still active. There is no benign case for the
        attempt itself -- unlike the profile, which may still turn out to be
        genuinely authenticated on the next fresh observation.
        """
        result = await self._connection.execute(
            update(login_attempts)
            .where(
                login_attempts.c.status.in_(_OPEN),
                login_attempts.c.runtime_generation != current_runtime_generation,
            )
            .values(status=LoginAttemptStatus.INTERRUPTED.value, updated_at=func.now())
            .returning(*login_attempts.c)
        )
        return [_attempt(row) for row in result.all()]


__all__ = ["LoginAttemptRepository"]
