"""SQL for the S5 visual-fallback grants and their records: capture (local-only) and vision-disclosure
(one named provider).

The single-use property is a database fact here too, exactly as `desktop_disclosure`'s repository
already establishes:

* `claim_*_grant` is one compare-and-swap, `ACTIVE` -> `COMPLETED`, guarded by the revision the caller
  read, the status and the expiry.
* `desktop_captures.grant_id`/`.task_id` and `desktop_vision_disclosures.grant_id`/`.task_id` are all
  UNIQUE, so even a bug that got past the swap could not record a second attempt for one approval.

Callers own the transaction. Nothing here ever holds a pixel: only digests, dimensions and DPI.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import desktop_captures, desktop_vision_disclosures, task_grants
from app.domain.desktop_vision import (
    DESKTOP_VISION_CAPTURE_KIND,
    DESKTOP_VISION_DISCLOSE_KIND,
    CaptureGrantScope,
    DiscloseGrantScope,
)
from app.domain.research import GrantStatus

_OPEN = (GrantStatus.PENDING.value, GrantStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class CaptureGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: CaptureGrantScope
    scope_digest: str
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None
    approval_input_tick: int | None


@dataclass(frozen=True, slots=True)
class DiscloseGrantRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    status: GrantStatus
    revision: int
    policy_version: str
    scope: DiscloseGrantScope
    scope_digest: str
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    completed_at: datetime | None
    approval_input_tick: int | None


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    surface_ref: str
    surface_epoch: int
    approval_input_tick: int
    geometry_fingerprint: str | None
    frame_digest: str | None
    width: int | None
    height: int | None
    dpi: int | None
    monitor_id: int | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class VisionDisclosureRecord:
    id: uuid.UUID
    task_id: uuid.UUID
    grant_id: uuid.UUID
    capture_id: uuid.UUID
    provider: str
    model: str
    purpose: str
    approval_input_tick: int
    geometry_fingerprint: str | None
    frame_digest: str | None
    width: int | None
    height: int | None
    dpi: int | None
    monitor_id: int | None
    candidates: list[dict[str, Any]] | None
    status: str
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None


def _capture_grant(row: Row[Any]) -> CaptureGrantRecord:
    return CaptureGrantRecord(
        id=row.id, task_id=row.task_id, status=GrantStatus(row.status), revision=row.revision,
        policy_version=row.policy_version, scope=CaptureGrantScope.model_validate(row.scope),
        scope_digest=row.scope_digest, created_at=row.created_at, updated_at=row.updated_at,
        confirmed_at=row.confirmed_at, expires_at=row.expires_at, revoked_at=row.revoked_at,
        completed_at=row.completed_at, approval_input_tick=row.approval_input_tick,
    )


def _disclose_grant(row: Row[Any]) -> DiscloseGrantRecord:
    return DiscloseGrantRecord(
        id=row.id, task_id=row.task_id, status=GrantStatus(row.status), revision=row.revision,
        policy_version=row.policy_version, scope=DiscloseGrantScope.model_validate(row.scope),
        scope_digest=row.scope_digest, created_at=row.created_at, updated_at=row.updated_at,
        confirmed_at=row.confirmed_at, expires_at=row.expires_at, revoked_at=row.revoked_at,
        completed_at=row.completed_at, approval_input_tick=row.approval_input_tick,
    )


def _capture(row: Row[Any]) -> CaptureRecord:
    return CaptureRecord(
        id=row.id, task_id=row.task_id, grant_id=row.grant_id, surface_ref=row.surface_ref,
        surface_epoch=row.surface_epoch, approval_input_tick=row.approval_input_tick,
        geometry_fingerprint=row.geometry_fingerprint,
        frame_digest=row.frame_digest, width=row.width, height=row.height, dpi=row.dpi,
        monitor_id=row.monitor_id, status=row.status, started_at=row.started_at,
        finished_at=row.finished_at, error_code=row.error_code,
    )


def _vision_disclosure(row: Row[Any]) -> VisionDisclosureRecord:
    return VisionDisclosureRecord(
        id=row.id, task_id=row.task_id, grant_id=row.grant_id, capture_id=row.capture_id,
        provider=row.provider, model=row.model, purpose=row.purpose,
        approval_input_tick=row.approval_input_tick,
        geometry_fingerprint=row.geometry_fingerprint, frame_digest=row.frame_digest,
        width=row.width, height=row.height, dpi=row.dpi, monitor_id=row.monitor_id,
        candidates=list(row.candidates) if row.candidates is not None else None,
        status=row.status, started_at=row.started_at, finished_at=row.finished_at,
        error_code=row.error_code,
    )


class DesktopCaptureRepository:
    """`desktop_vision_capture` grants and their `desktop_captures` records."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: CaptureGrantScope
    ) -> CaptureGrantRecord:
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id, task_id=task_id, kind=DESKTOP_VISION_CAPTURE_KIND,
                status=GrantStatus.PENDING.value, revision=1, policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"), scope_digest=scope.digest,
            )
            .returning(*task_grants.c)
        )
        return _capture_grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> CaptureGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_CAPTURE_KIND
                )
            )
        ).one_or_none()
        return _capture_grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> CaptureGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants)
                .where(task_grants.c.task_id == task_id, task_grants.c.kind == DESKTOP_VISION_CAPTURE_KIND)
                .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
                .limit(1)
            )
        ).one_or_none()
        return _capture_grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta,
        approval_input_tick: int,
    ) -> CaptureGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_CAPTURE_KIND,
                    task_grants.c.revision == expected_revision, task_grants.c.status == GrantStatus.PENDING.value,
                    task_grants.c.scope_digest == scope_digest,
                )
                .values(
                    status=GrantStatus.ACTIVE.value, revision=task_grants.c.revision + 1,
                    confirmed_at=func.now(), expires_at=func.now() + ttl, updated_at=func.now(),
                    approval_input_tick=approval_input_tick,
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _capture_grant(row) if row is not None else None

    async def close_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int | None = None
    ) -> CaptureGrantRecord | None:
        conditions = [
            task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_CAPTURE_KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(*conditions)
                .values(
                    status=GrantStatus.REVOKED.value, revision=task_grants.c.revision + 1,
                    confirmed_at=func.coalesce(task_grants.c.confirmed_at, func.now()),
                    revoked_at=func.now(), updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _capture_grant(row) if row is not None else None

    async def claim_grant(self, *, grant_id: uuid.UUID, expected_revision: int) -> CaptureGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_CAPTURE_KIND,
                    task_grants.c.revision == expected_revision, task_grants.c.status == GrantStatus.ACTIVE.value,
                    and_(task_grants.c.expires_at.is_not(None), task_grants.c.expires_at > func.now()),
                )
                .values(
                    status=GrantStatus.COMPLETED.value, revision=task_grants.c.revision + 1,
                    completed_at=func.now(), updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _capture_grant(row) if row is not None else None

    async def grant_is_expired(self, grant_id: uuid.UUID) -> bool:
        row = (
            await self._connection.execute(
                select(task_grants.c.expires_at <= func.now()).where(task_grants.c.id == grant_id)
            )
        ).scalar_one_or_none()
        return bool(row)

    # ---- the capture record -----------------------------------------------------------------

    async def start_capture(
        self, *, capture_id: uuid.UUID, task_id: uuid.UUID, grant_id: uuid.UUID, surface_ref: str,
        surface_epoch: int, approval_input_tick: int,
    ) -> CaptureRecord:
        result = await self._connection.execute(
            insert(desktop_captures)
            .values(
                id=capture_id, task_id=task_id, grant_id=grant_id, surface_ref=surface_ref,
                surface_epoch=surface_epoch, approval_input_tick=approval_input_tick, status="STARTED",
            )
            .returning(*desktop_captures.c)
        )
        return _capture(result.one())

    async def get_capture(self, capture_id: uuid.UUID) -> CaptureRecord | None:
        row = (
            await self._connection.execute(select(desktop_captures).where(desktop_captures.c.id == capture_id))
        ).one_or_none()
        return _capture(row) if row is not None else None

    async def capture_for_task(self, task_id: uuid.UUID) -> CaptureRecord | None:
        row = (
            await self._connection.execute(select(desktop_captures).where(desktop_captures.c.task_id == task_id))
        ).one_or_none()
        return _capture(row) if row is not None else None

    async def finish_capture_succeeded(
        self, *, capture_id: uuid.UUID, geometry_fingerprint: str, frame_digest: str,
        width: int, height: int, dpi: int, monitor_id: int,
    ) -> CaptureRecord | None:
        row = (
            await self._connection.execute(
                update(desktop_captures)
                .where(desktop_captures.c.id == capture_id, desktop_captures.c.status == "STARTED")
                .values(
                    status="SUCCEEDED", finished_at=func.now(), geometry_fingerprint=geometry_fingerprint,
                    frame_digest=frame_digest, width=width, height=height, dpi=dpi, monitor_id=monitor_id,
                )
                .returning(*desktop_captures.c)
            )
        ).one_or_none()
        return _capture(row) if row is not None else None

    async def finish_capture(self, *, capture_id: uuid.UUID, status: str, error_code: str) -> CaptureRecord | None:
        row = (
            await self._connection.execute(
                update(desktop_captures)
                .where(desktop_captures.c.id == capture_id, desktop_captures.c.status == "STARTED")
                .values(status=status, error_code=error_code, finished_at=func.now())
                .returning(*desktop_captures.c)
            )
        ).one_or_none()
        return _capture(row) if row is not None else None

    async def list_started(self, *, older_than_seconds: int | None = None) -> list[CaptureRecord]:
        statement = select(desktop_captures).where(desktop_captures.c.status == "STARTED")
        if older_than_seconds is not None:
            statement = statement.where(
                desktop_captures.c.started_at < func.now() - text(f"interval '{int(older_than_seconds)} seconds'")
            )
        return [_capture(row) for row in (await self._connection.execute(statement)).all()]


class DesktopVisionDiscloseRepository:
    """`desktop_vision_disclose` grants and their `desktop_vision_disclosures` records."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_grant(
        self, *, grant_id: uuid.UUID, task_id: uuid.UUID, scope: DiscloseGrantScope
    ) -> DiscloseGrantRecord:
        result = await self._connection.execute(
            insert(task_grants)
            .values(
                id=grant_id, task_id=task_id, kind=DESKTOP_VISION_DISCLOSE_KIND,
                status=GrantStatus.PENDING.value, revision=1, policy_version=scope.policy_version,
                scope=scope.model_dump(mode="json"), scope_digest=scope.digest,
            )
            .returning(*task_grants.c)
        )
        return _disclose_grant(result.one())

    async def get_grant(self, grant_id: uuid.UUID) -> DiscloseGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants).where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_DISCLOSE_KIND
                )
            )
        ).one_or_none()
        return _disclose_grant(row) if row is not None else None

    async def latest_grant_for_task(self, task_id: uuid.UUID) -> DiscloseGrantRecord | None:
        row = (
            await self._connection.execute(
                select(task_grants)
                .where(task_grants.c.task_id == task_id, task_grants.c.kind == DESKTOP_VISION_DISCLOSE_KIND)
                .order_by(task_grants.c.created_at.desc(), task_grants.c.id)
                .limit(1)
            )
        ).one_or_none()
        return _disclose_grant(row) if row is not None else None

    async def confirm_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int, scope_digest: str, ttl: timedelta,
        approval_input_tick: int,
    ) -> DiscloseGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_DISCLOSE_KIND,
                    task_grants.c.revision == expected_revision, task_grants.c.status == GrantStatus.PENDING.value,
                    task_grants.c.scope_digest == scope_digest,
                )
                .values(
                    status=GrantStatus.ACTIVE.value, revision=task_grants.c.revision + 1,
                    confirmed_at=func.now(), expires_at=func.now() + ttl, updated_at=func.now(),
                    approval_input_tick=approval_input_tick,
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _disclose_grant(row) if row is not None else None

    async def close_grant(
        self, *, grant_id: uuid.UUID, expected_revision: int | None = None
    ) -> DiscloseGrantRecord | None:
        conditions = [
            task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_DISCLOSE_KIND,
            task_grants.c.status.in_(_OPEN),
        ]
        if expected_revision is not None:
            conditions.append(task_grants.c.revision == expected_revision)
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(*conditions)
                .values(
                    status=GrantStatus.REVOKED.value, revision=task_grants.c.revision + 1,
                    confirmed_at=func.coalesce(task_grants.c.confirmed_at, func.now()),
                    revoked_at=func.now(), updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _disclose_grant(row) if row is not None else None

    async def claim_grant(self, *, grant_id: uuid.UUID, expected_revision: int) -> DiscloseGrantRecord | None:
        row = (
            await self._connection.execute(
                update(task_grants)
                .where(
                    task_grants.c.id == grant_id, task_grants.c.kind == DESKTOP_VISION_DISCLOSE_KIND,
                    task_grants.c.revision == expected_revision, task_grants.c.status == GrantStatus.ACTIVE.value,
                    and_(task_grants.c.expires_at.is_not(None), task_grants.c.expires_at > func.now()),
                )
                .values(
                    status=GrantStatus.COMPLETED.value, revision=task_grants.c.revision + 1,
                    completed_at=func.now(), updated_at=func.now(),
                )
                .returning(*task_grants.c)
            )
        ).one_or_none()
        return _disclose_grant(row) if row is not None else None

    async def grant_is_expired(self, grant_id: uuid.UUID) -> bool:
        row = (
            await self._connection.execute(
                select(task_grants.c.expires_at <= func.now()).where(task_grants.c.id == grant_id)
            )
        ).scalar_one_or_none()
        return bool(row)

    # ---- the disclosure record ----------------------------------------------------------------

    async def start_disclosure(
        self, *, disclosure_id: uuid.UUID, task_id: uuid.UUID, grant_id: uuid.UUID, capture_id: uuid.UUID,
        provider: str, model: str, purpose: str, approval_input_tick: int,
    ) -> VisionDisclosureRecord:
        result = await self._connection.execute(
            insert(desktop_vision_disclosures)
            .values(
                id=disclosure_id, task_id=task_id, grant_id=grant_id, capture_id=capture_id,
                provider=provider, model=model, purpose=purpose, approval_input_tick=approval_input_tick,
                status="STARTED",
            )
            .returning(*desktop_vision_disclosures.c)
        )
        return _vision_disclosure(result.one())

    async def get_disclosure(self, disclosure_id: uuid.UUID) -> VisionDisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_vision_disclosures).where(desktop_vision_disclosures.c.id == disclosure_id)
            )
        ).one_or_none()
        return _vision_disclosure(row) if row is not None else None

    async def disclosure_for_task(self, task_id: uuid.UUID) -> VisionDisclosureRecord | None:
        row = (
            await self._connection.execute(
                select(desktop_vision_disclosures).where(desktop_vision_disclosures.c.task_id == task_id)
            )
        ).one_or_none()
        return _vision_disclosure(row) if row is not None else None

    async def record_frame(
        self, *, disclosure_id: uuid.UUID, geometry_fingerprint: str, frame_digest: str,
        width: int, height: int, dpi: int, monitor_id: int,
    ) -> VisionDisclosureRecord | None:
        """Records the FRESH capture's identity on a still-`STARTED` row, independent of the later
        provider outcome: a crash before a result ever arrives still leaves an honest audit trail of
        what was captured, not just that something was attempted."""
        row = (
            await self._connection.execute(
                update(desktop_vision_disclosures)
                .where(desktop_vision_disclosures.c.id == disclosure_id, desktop_vision_disclosures.c.status == "STARTED")
                .values(
                    geometry_fingerprint=geometry_fingerprint, frame_digest=frame_digest,
                    width=width, height=height, dpi=dpi, monitor_id=monitor_id,
                )
                .returning(*desktop_vision_disclosures.c)
            )
        ).one_or_none()
        return _vision_disclosure(row) if row is not None else None

    async def finish_disclosure_succeeded(
        self, *, disclosure_id: uuid.UUID, candidates: list[dict[str, Any]],
    ) -> VisionDisclosureRecord | None:
        row = (
            await self._connection.execute(
                update(desktop_vision_disclosures)
                .where(desktop_vision_disclosures.c.id == disclosure_id, desktop_vision_disclosures.c.status == "STARTED")
                .values(status="SUCCEEDED", finished_at=func.now(), candidates=candidates)
                .returning(*desktop_vision_disclosures.c)
            )
        ).one_or_none()
        return _vision_disclosure(row) if row is not None else None

    async def finish_disclosure(
        self, *, disclosure_id: uuid.UUID, status: str, error_code: str
    ) -> VisionDisclosureRecord | None:
        row = (
            await self._connection.execute(
                update(desktop_vision_disclosures)
                .where(desktop_vision_disclosures.c.id == disclosure_id, desktop_vision_disclosures.c.status == "STARTED")
                .values(status=status, error_code=error_code, finished_at=func.now())
                .returning(*desktop_vision_disclosures.c)
            )
        ).one_or_none()
        return _vision_disclosure(row) if row is not None else None

    async def list_started(self, *, older_than_seconds: int | None = None) -> list[VisionDisclosureRecord]:
        statement = select(desktop_vision_disclosures).where(desktop_vision_disclosures.c.status == "STARTED")
        if older_than_seconds is not None:
            statement = statement.where(
                desktop_vision_disclosures.c.started_at
                < func.now() - text(f"interval '{int(older_than_seconds)} seconds'")
            )
        return [_vision_disclosure(row) for row in (await self._connection.execute(statement)).all()]
