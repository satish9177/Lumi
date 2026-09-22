"""Milestone 9 S5: the runtime's scoped desktop visual-fallback service.

Two grant kinds, two claims, one task:

```text
create_capture   fresh S1 observe (silent, as always) -> deterministic eligibility classification
                 -> task + PENDING desktop_vision_capture grant (the capture card). No pixels yet.
confirm_capture  the trusted click                     -> grant ACTIVE, bound to that classification.
claim_capture    consume the grant, THEN call the worker ONCE -> ONE screenshot, for LOCAL use only.
                 Never sent anywhere; metadata (digests/dimensions/DPI) is durable, the image is not.

create_disclosure   names ONE provider/model/purpose for the SAME task's already-succeeded capture
                    -> a SECOND, separate PENDING desktop_vision_disclose grant. Still no pixels sent.
confirm_disclosure  the trusted click                  -> grant ACTIVE.
claim_disclosure    consume the grant, THEN call the worker for a BRAND NEW screenshot (never the
                    first capture's own bytes) -> returned once for exactly ONE provider attempt.
record_candidates   the provider's closed evidence list, or the failure, or OUTCOME_UNKNOWN.
```

Ordering is the safety property, exactly as in `desktop_disclosure`: a grant is consumed (a durable
compare-and-swap) and a STARTED record is written *before* the sensitive call it authorises, and no
database transaction is open while that call runs. If the process dies after the STARTED row commits,
Lumi cannot know whether the screenshot was taken or the image reached a provider, so the attempt is
`OUTCOME_UNKNOWN` and is never replayed: a retry needs a brand new observation/capture and a brand new
trusted approval.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import CaptureRequest, SurfaceRecord
from app.domain.authenticated import Recipient
from app.domain.desktop_disclosure import DisplayTarget, validate_objective
from app.domain.desktop_vision import (
    DESKTOP_VISION_TASK_TYPE,
    MAX_CLASSIFYING_OBSERVATION_AGE_SECONDS,
    CaptureGrantScope,
    DesktopVisionRefusal,
    DiscloseGrantScope,
    classify_fallback_eligibility,
    observation_age_seconds,
    parse_vision_result,
    validate_provider_model,
    validate_purpose,
)
from app.domain.errors import (
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.repositories.desktop import DesktopRepository
from app.repositories.desktop_vision import (
    CaptureGrantRecord,
    CaptureRecord,
    DesktopCaptureRepository,
    DesktopVisionDiscloseRepository,
    DiscloseGrantRecord,
    VisionDisclosureRecord,
)
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.desktop import DesktopService

logger = logging.getLogger("lumi.desktop_vision")

#: A STARTED capture/disclosure whose runtime never heard back is not left claiming to be in flight.
STALE_CLAIM_SECONDS: Final = 5 * 60
ERROR_RUNTIME_RESTART: Final = "runtime_restart"
ERROR_CAPTURE_LOST: Final = "capture_lost"
PROVIDER_FAILURE_CODES: Final = frozenset({"model_unavailable", "invalid_output"})


# ---- views ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureCardView:
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    application_label: str
    window_title: str
    fallback_reason: str


@dataclass(frozen=True, slots=True)
class CaptureStateView:
    capture_id: uuid.UUID
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    width: int | None
    height: int | None
    dpi: int | None


@dataclass(frozen=True, slots=True)
class DisclosureCardView:
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    application_label: str
    window_title: str
    provider: str
    model: str
    purpose: str


@dataclass(frozen=True, slots=True)
class DisclosureStateView:
    disclosure_id: uuid.UUID
    status: str
    error_code: str | None
    started_at: datetime
    finished_at: datetime | None
    candidate_count: int | None


@dataclass(frozen=True, slots=True)
class DesktopVisionView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: str
    capture_card: CaptureCardView | None
    capture: CaptureStateView | None
    disclosure_card: DisclosureCardView | None
    disclosure: DisclosureStateView | None
    candidates: list[dict[str, Any]] | None


@dataclass(frozen=True, slots=True)
class RawCapture:
    """What `claim_capture`/`claim_disclosure` hand to Electron main, exactly once, for local use
    (OCR/display) or for the one provider call. Never persisted anywhere as a whole."""

    capture_id: uuid.UUID
    image_base64: str
    width: int
    height: int
    dpi: int


@dataclass(frozen=True, slots=True)
class DisclosureProviderContext:
    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    purpose: str
    recipient: str
    model: str
    capture: RawCapture


# ---- the service ---------------------------------------------------------------------------


class DesktopVisionService:
    def __init__(self, engine: AsyncEngine, *, desktop: DesktopService, grant_ttl_seconds: int) -> None:
        self._engine = engine
        self._desktop = desktop
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)

    # ---- create_capture: a fresh, silent S1 read, then the card, if eligible ------------------

    async def create_capture(
        self,
        *,
        objective: object,
        worker_generation: uuid.UUID,
        surface_ref: str,
        surface_epoch: int,
        target_hint: str | None = None,
    ) -> DesktopVisionView:
        """Observe ONE surface locally (S1, silent, as always) and open the capture card, but only if
        deterministic code finds UIA insufficient. Nothing is captured yet."""
        question = validate_objective(objective)
        listing = await self._desktop.list_surfaces()
        if listing.worker_generation != worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        target = _find_surface(listing.surfaces, surface_ref, surface_epoch)
        if target is None:
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        observation = await self._desktop.observe(worker_generation, surface_ref, surface_epoch)
        reason = classify_fallback_eligibility(observation, target_hint=target_hint)
        if reason is None:
            raise DesktopVisionRefusal("fallback_not_eligible")
        async with self._engine.begin() as connection:
            record = await DesktopRepository(connection).get_observation(observation.observation_id)
            if record is None:
                raise DesktopVisionRefusal("observation_unavailable")
            scope = CaptureGrantScope(
                worker_generation=record.worker_generation,
                surface_ref=record.surface_ref,
                surface_epoch=record.surface_epoch,
                classifying_observation_id=record.id,
                fallback_reason=reason,
                display=DisplayTarget(application_label=target.application_label, window_title=target.window_title),
            )
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(),
                status=TaskStatus.WAITING_APPROVAL,
                request={"type": DESKTOP_VISION_TASK_TYPE, "objective": question},
            )
            await tasks.append_event(
                task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value}
            )
            grant = await DesktopCaptureRepository(connection).insert_grant(
                grant_id=uuid.uuid4(), task_id=task.id, scope=scope
            )
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DESKTOP_CAPTURE_REQUESTED,
                {
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "fallback_reason": reason.value,
                },
            )
            return await self._view(connection, task.id)

    # ---- reading --------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> DesktopVisionView:
        await self._expire_stale_claims()
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            return await self._view(connection, task_id)

    # ---- the trusted click: capture --------------------------------------------------------

    async def confirm_capture(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> DesktopVisionView:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopCaptureRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise DesktopVisionRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise DesktopVisionRefusal("grant_changed")
            record = await DesktopRepository(connection).get_observation(grant.scope.classifying_observation_id)
            self._require_classifying_observation(grant, record)
            assert record is not None
            if observation_age_seconds(record.created_at) > MAX_CLASSIFYING_OBSERVATION_AGE_SECONDS:
                raise DesktopVisionRefusal("observation_stale")
            scope = grant.scope
            scope_digest = grant.scope_digest
        # The transaction above committed without mutating anything: it only checked that the grant
        # is still exactly what the person is about to approve. This IS the trusted-click moment, so
        # the human-input tick is read now, from outside any open transaction (same rule as the actual
        # capture call below), and is carried into the compare-and-swap that follows. A later claim --
        # which may happen much later, from a separate process -- compares against THIS reading, never
        # one freshly taken at claim time, which would trivially always match "now" and detect nothing.
        baseline = await self._desktop.input_baseline(scope.worker_generation)
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopCaptureRepository(connection)
            confirmed = await repository.confirm_grant(
                grant_id=grant_id, expected_revision=expected_revision,
                scope_digest=scope_digest, ttl=self._grant_ttl, approval_input_tick=baseline,
            )
            if confirmed is None:
                raise DesktopVisionRefusal("grant_changed")
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_CAPTURE_GRANTED,
                {"grant_id": str(confirmed.id), "grant_revision": confirmed.revision, "grant_status": confirmed.status.value},
            )
            return await self._view(connection, task_id)

    async def revoke_capture(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None
    ) -> DesktopVisionView:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id, require_accepting=False)
            repository = DesktopCaptureRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(grant_id=grant.id, expected_revision=expected_revision)
                if closed is None:
                    raise DesktopVisionRefusal("grant_changed")
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_CAPTURE_REVOKED,
                    {"grant_id": str(closed.id), "grant_revision": closed.revision, "grant_status": closed.status.value},
                )
                await self._cancel_if_open(connection, task.id)
            return await self._view(connection, task_id)

    # ---- claim_capture: consume the grant, THEN one real screenshot --------------------------

    async def claim_capture(self, task_id: uuid.UUID) -> RawCapture:
        capture_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopCaptureRepository(connection)
            grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status is not GrantStatus.ACTIVE:
                raise DesktopVisionRefusal("grant_not_active")
            if await repository.grant_is_expired(grant.id):
                raise DesktopVisionRefusal("grant_expired")
            # `confirm_grant` always sets this before a grant can reach ACTIVE (see `confirm_capture`).
            assert grant.approval_input_tick is not None
            claimed = await repository.claim_grant(grant_id=grant.id, expected_revision=grant.revision)
            if claimed is None:
                raise DesktopVisionRefusal("grant_not_active")
            capture = await repository.start_capture(
                capture_id=capture_id, task_id=task_id, grant_id=grant.id,
                surface_ref=grant.scope.surface_ref, surface_epoch=grant.scope.surface_epoch,
                approval_input_tick=grant.approval_input_tick,
            )
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_CAPTURE_STARTED,
                {"grant_id": str(grant.id), "capture_id": str(capture.id)},
            )
            await self._move_task(connection, task_id, TaskStatus.EXECUTING)
            scope = grant.scope
            approval_input_tick = grant.approval_input_tick
        # The transaction has committed: the claim is durable before the worker is ever called. The
        # input tick compared here is the one read at the trusted click in `confirm_capture`, never a
        # fresh one -- a human-input takeover between approval and claim is still detected.
        try:
            response = await self._desktop.capture(
                CaptureRequest(
                    expected_worker_generation=scope.worker_generation,
                    capture_id=capture_id,
                    surface_ref=scope.surface_ref,
                    surface_epoch=scope.surface_epoch,
                    input_tick=approval_input_tick,
                )
            )
        except DesktopRefusal as refusal:
            await self._fail_capture(task_id, capture_id, refusal.code.value)
            raise DesktopVisionRefusal(refusal.code.value) from None
        async with self._engine.begin() as connection:
            await DesktopCaptureRepository(connection).finish_capture_succeeded(
                capture_id=capture_id, geometry_fingerprint=response.geometry_fingerprint,
                frame_digest=response.frame_digest, width=response.width, height=response.height,
                dpi=response.dpi, monitor_id=response.monitor_id,
            )
            await self._event(
                connection, task_id, TaskEventType.TASK_DESKTOP_CAPTURE_SUCCEEDED,
                {
                    "capture_id": str(capture_id), "width": response.width, "height": response.height,
                    "dpi": response.dpi, "geometry_fingerprint": response.geometry_fingerprint,
                    "frame_digest": response.frame_digest,
                },
            )
            # READY, not SUCCEEDED: a captured-but-not-disclosed task is not necessarily done. The
            # person may still request a SEPARATE vision disclosure on this SAME task; only recording
            # a disclosure result (or a capture failure, with nothing left to try) ends the task.
            await self._move_task(connection, task_id, TaskStatus.READY)
        return RawCapture(
            capture_id=capture_id, image_base64=response.image_base64,
            width=response.width, height=response.height, dpi=response.dpi,
        )

    async def _fail_capture(self, task_id: uuid.UUID, capture_id: uuid.UUID, code: str) -> None:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id, require_accepting=False)
            await DesktopCaptureRepository(connection).finish_capture(
                capture_id=capture_id, status="FAILED", error_code=code
            )
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_CAPTURE_FAILED,
                {"capture_id": str(capture_id), "error_code": code},
            )
            await self._move_task(connection, task.id, TaskStatus.FAILED)

    # ---- create_disclosure: names ONE provider for the SAME task's succeeded capture ----------

    async def create_disclosure(
        self, task_id: uuid.UUID, *, recipient: Recipient, model: object, purpose: object
    ) -> DesktopVisionView:
        """A capture-only task offering ONE more, SEPARATE approval: sending a FRESH image (not the
        capture's own bytes) to ONE named provider. Requires that task's capture to have SUCCEEDED."""
        model_name = validate_provider_model(model)
        purpose_text = validate_purpose(purpose)
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            capture_repository = DesktopCaptureRepository(connection)
            capture_grant = await capture_repository.latest_grant_for_task(task_id)
            capture = await capture_repository.capture_for_task(task_id)
            if capture_grant is None or capture is None or capture.status != "SUCCEEDED":
                raise DesktopVisionRefusal("capture_not_succeeded")
            disclose_repository = DesktopVisionDiscloseRepository(connection)
            existing = await disclose_repository.latest_grant_for_task(task_id)
            if existing is not None and existing.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                raise DesktopVisionRefusal("disclosure_already_open")
            scope = DiscloseGrantScope(
                worker_generation=capture_grant.scope.worker_generation,
                surface_ref=capture_grant.scope.surface_ref,
                surface_epoch=capture_grant.scope.surface_epoch,
                capture_id=capture.id,
                recipient=recipient,
                model=model_name,
                purpose=purpose_text,
                display=capture_grant.scope.display,
            )
            grant = await disclose_repository.insert_grant(grant_id=uuid.uuid4(), task_id=task_id, scope=scope)
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_REQUESTED,
                {
                    "grant_id": str(grant.id), "grant_revision": grant.revision, "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest, "recipient": recipient, "capture_id": str(capture.id),
                },
            )
            return await self._view(connection, task_id)

    async def confirm_disclosure(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> DesktopVisionView:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopVisionDiscloseRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise DesktopVisionRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise DesktopVisionRefusal("grant_changed")
            scope = grant.scope
            scope_digest = grant.scope_digest
        # Same reasoning as `confirm_capture`: this is the trusted-click moment, so the human-input
        # tick is read now, outside any open transaction, and carried into the compare-and-swap that
        # follows -- never re-read fresh at claim time, when the later, BRAND NEW screenshot is taken.
        baseline = await self._desktop.input_baseline(scope.worker_generation)
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopVisionDiscloseRepository(connection)
            confirmed = await repository.confirm_grant(
                grant_id=grant_id, expected_revision=expected_revision,
                scope_digest=scope_digest, ttl=self._grant_ttl, approval_input_tick=baseline,
            )
            if confirmed is None:
                raise DesktopVisionRefusal("grant_changed")
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_GRANTED,
                {"grant_id": str(confirmed.id), "grant_revision": confirmed.revision, "grant_status": confirmed.status.value},
            )
            return await self._view(connection, task_id)

    async def revoke_disclosure(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None
    ) -> DesktopVisionView:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id, require_accepting=False)
            repository = DesktopVisionDiscloseRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(grant_id=grant.id, expected_revision=expected_revision)
                if closed is None:
                    raise DesktopVisionRefusal("grant_changed")
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_REVOKED,
                    {"grant_id": str(closed.id), "grant_revision": closed.revision, "grant_status": closed.status.value},
                )
            return await self._view(connection, task_id)

    # ---- claim_disclosure: consume the grant, THEN a BRAND NEW screenshot ---------------------

    async def claim_disclosure(self, task_id: uuid.UUID) -> DisclosureProviderContext:
        disclosure_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id)
            repository = DesktopVisionDiscloseRepository(connection)
            grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise DesktopVisionRefusal("grant_not_found")
            if grant.status is not GrantStatus.ACTIVE:
                raise DesktopVisionRefusal("grant_not_active")
            if await repository.grant_is_expired(grant.id):
                raise DesktopVisionRefusal("grant_expired")
            # `confirm_grant` always sets this before a grant can reach ACTIVE (see `confirm_disclosure`).
            assert grant.approval_input_tick is not None
            claimed = await repository.claim_grant(grant_id=grant.id, expected_revision=grant.revision)
            if claimed is None:
                raise DesktopVisionRefusal("grant_not_active")
            disclosure = await repository.start_disclosure(
                disclosure_id=disclosure_id, task_id=task_id, grant_id=grant.id, capture_id=grant.scope.capture_id,
                provider=grant.scope.recipient, model=grant.scope.model, purpose=grant.scope.purpose,
                approval_input_tick=grant.approval_input_tick,
            )
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_STARTED,
                {"grant_id": str(grant.id), "disclosure_id": str(disclosure.id), "recipient": disclosure.provider},
            )
            await self._move_task(connection, task_id, TaskStatus.EXECUTING)
            scope = grant.scope
            approval_input_tick = grant.approval_input_tick
        # The transaction has committed: the claim is durable before the SECOND, fresh capture runs.
        # The input tick compared here is the one read at the trusted click in `confirm_disclosure`.
        try:
            capture_id = uuid.uuid4()
            response = await self._desktop.capture(
                CaptureRequest(
                    expected_worker_generation=scope.worker_generation,
                    capture_id=capture_id,
                    surface_ref=scope.surface_ref,
                    surface_epoch=scope.surface_epoch,
                    input_tick=approval_input_tick,
                )
            )
        except DesktopRefusal as refusal:
            await self._fail_disclosure(task_id, disclosure_id, refusal.code.value)
            raise DesktopVisionRefusal(refusal.code.value) from None
        # The frame's own identity is recorded now, independent of whatever the provider later says:
        # an honest audit trail of what was captured exists even if the process dies before a result
        # (or a failure) is ever recorded against this disclosure.
        async with self._engine.begin() as connection:
            await DesktopVisionDiscloseRepository(connection).record_frame(
                disclosure_id=disclosure_id, geometry_fingerprint=response.geometry_fingerprint,
                frame_digest=response.frame_digest, width=response.width, height=response.height,
                dpi=response.dpi, monitor_id=response.monitor_id,
            )
        return DisclosureProviderContext(
            disclosure_id=disclosure_id,
            task_id=task_id,
            purpose=scope.purpose,
            recipient=scope.recipient,
            model=scope.model,
            capture=RawCapture(
                capture_id=capture_id, image_base64=response.image_base64,
                width=response.width, height=response.height, dpi=response.dpi,
            ),
        )

    async def _fail_disclosure(self, task_id: uuid.UUID, disclosure_id: uuid.UUID, code: str) -> None:
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id, require_accepting=False)
            await DesktopVisionDiscloseRepository(connection).finish_disclosure(
                disclosure_id=disclosure_id, status="FAILED", error_code=code
            )
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_FAILED,
                {"disclosure_id": str(disclosure_id), "error_code": code},
            )
            await self._move_task(connection, task.id, TaskStatus.FAILED)

    # ---- recording the provider's closed evidence list -----------------------------------------

    async def record_candidates(
        self, task_id: uuid.UUID, *, disclosure_id: uuid.UUID, result: Any | None, failure: str | None,
    ) -> DesktopVisionView:
        """Record the ONE provider attempt's outcome. A failure ends the task; a well-formed result is
        parsed strictly (`parse_vision_result`) before it is ever stored: a reply that tries to smuggle
        an action, a click or a coordinate is refused as `invalid_output`, not stripped and not stored."""
        async with self._engine.begin() as connection:
            task = await self._lock_task(connection, task_id, require_accepting=False)
            repository = DesktopVisionDiscloseRepository(connection)
            disclosure = await repository.get_disclosure(disclosure_id)
            if disclosure is None or disclosure.task_id != task_id:
                raise DesktopVisionRefusal("disclosure_not_started")
            if disclosure.status != "STARTED":
                raise DesktopVisionRefusal("disclosure_already_recorded")
            if (failure is None) == (result is None):
                raise DesktopVisionRefusal("result_malformed")
            if failure is not None:
                if failure not in PROVIDER_FAILURE_CODES:
                    raise DesktopVisionRefusal("failure_invalid")
                await repository.finish_disclosure(disclosure_id=disclosure_id, status="FAILED", error_code=failure)
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_FAILED,
                    {"disclosure_id": str(disclosure_id), "error_code": failure},
                )
                await self._move_task(connection, task.id, TaskStatus.FAILED)
                return await self._view(connection, task_id)
            try:
                parsed = parse_vision_result(result)
            except DesktopVisionRefusal:
                await repository.finish_disclosure(
                    disclosure_id=disclosure_id, status="FAILED", error_code="invalid_output"
                )
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_FAILED,
                    {"disclosure_id": str(disclosure_id), "error_code": "invalid_output"},
                )
                await self._move_task(connection, task.id, TaskStatus.FAILED)
                return await self._view(connection, task_id)
            candidates = [candidate.model_dump(mode="json") for candidate in parsed.candidates]
            await repository.finish_disclosure_succeeded(disclosure_id=disclosure_id, candidates=candidates)
            await self._event(
                connection, task.id, TaskEventType.TASK_DESKTOP_VISION_CANDIDATES_RECORDED,
                {"disclosure_id": str(disclosure_id), "candidate_count": len(candidates)},
            )
            await self._move_task(connection, task_id, TaskStatus.SUCCEEDED)
            return await self._view(connection, task_id)

    # ---- recovery -------------------------------------------------------------------------------

    async def recover_started(self) -> int:
        """Startup: a capture or disclosure still STARTED belongs to a runtime that died. Its outcome
        is unknown -- never automatically retried, and never marked `FAILED` (which would claim
        knowledge Lumi does not have)."""
        return await self._mark_capture_unknown(older_than_seconds=None) + await self._mark_disclosure_unknown(
            older_than_seconds=None
        )

    async def _expire_stale_claims(self) -> None:
        await self._mark_capture_unknown(older_than_seconds=STALE_CLAIM_SECONDS)
        await self._mark_disclosure_unknown(older_than_seconds=STALE_CLAIM_SECONDS)

    async def _mark_capture_unknown(self, *, older_than_seconds: int | None) -> int:
        async with self._engine.connect() as connection:
            started = await DesktopCaptureRepository(connection).list_started(older_than_seconds=older_than_seconds)
        marked = 0
        for capture in started:
            async with self._engine.begin() as connection:
                tasks = TaskRepository(connection)
                task = await tasks.lock_task(capture.task_id)
                finished = await DesktopCaptureRepository(connection).finish_capture(
                    capture_id=capture.id, status="OUTCOME_UNKNOWN", error_code=ERROR_RUNTIME_RESTART
                )
                if finished is None or task is None:
                    continue
                marked += 1
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_CAPTURE_OUTCOME_UNKNOWN,
                    {"capture_id": str(capture.id), "error_code": ERROR_RUNTIME_RESTART},
                )
                await self._move_task(connection, task.id, TaskStatus.OUTCOME_UNKNOWN)
        if marked:
            logger.warning(
                "%d desktop capture(s) were in flight when their runtime stopped; their outcome is "
                "unknown and they will not be repeated.", marked,
            )
        return marked

    async def _mark_disclosure_unknown(self, *, older_than_seconds: int | None) -> int:
        async with self._engine.connect() as connection:
            started = await DesktopVisionDiscloseRepository(connection).list_started(
                older_than_seconds=older_than_seconds
            )
        marked = 0
        for disclosure in started:
            async with self._engine.begin() as connection:
                tasks = TaskRepository(connection)
                task = await tasks.lock_task(disclosure.task_id)
                finished = await DesktopVisionDiscloseRepository(connection).finish_disclosure(
                    disclosure_id=disclosure.id, status="OUTCOME_UNKNOWN", error_code=ERROR_RUNTIME_RESTART
                )
                if finished is None or task is None:
                    continue
                marked += 1
                await self._event(
                    connection, task.id, TaskEventType.TASK_DESKTOP_VISION_DISCLOSURE_OUTCOME_UNKNOWN,
                    {"disclosure_id": str(disclosure.id), "error_code": ERROR_RUNTIME_RESTART},
                )
                await self._move_task(connection, task.id, TaskStatus.OUTCOME_UNKNOWN)
        if marked:
            logger.warning(
                "%d desktop vision disclosure(s) were in flight when their runtime stopped; their "
                "outcome is unknown and they will not be repeated.", marked,
            )
        return marked

    # ---- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _require_classifying_observation(grant: CaptureGrantRecord, record: Any | None) -> None:
        if record is None:
            raise DesktopVisionRefusal("observation_unavailable")
        if record.id != grant.scope.classifying_observation_id:
            raise DesktopVisionRefusal("observation_changed")

    @staticmethod
    async def _require_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_VISION_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_VISION_TASK_TYPE)
        return task

    @staticmethod
    async def _lock_task(
        connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True
    ) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DESKTOP_VISION_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DESKTOP_VISION_TASK_TYPE)
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

    @staticmethod
    async def _cancel_if_open(connection: AsyncConnection, task_id: uuid.UUID) -> None:
        current = await TaskRepository(connection).get_task(task_id)
        if current is not None and accepts_actions(current.status):
            moved = await TaskRepository(connection).advance_task(
                task_id=current.id, expected_revision=current.revision, status=TaskStatus.CANCELLED
            )
            if moved is not None:
                await TaskRepository(connection).append_event(
                    task=moved, event_type=TaskEventType.TASK_CANCELLED,
                    payload={"from_status": current.status.value, "to_status": moved.status.value},
                )

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> DesktopVisionView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        capture_repository = DesktopCaptureRepository(connection)
        disclose_repository = DesktopVisionDiscloseRepository(connection)
        capture_grant = await capture_repository.latest_grant_for_task(task_id)
        capture = await capture_repository.capture_for_task(task_id)
        disclosure_grant = await disclose_repository.latest_grant_for_task(task_id)
        disclosure = await disclose_repository.disclosure_for_task(task_id)
        capture_card = None
        if capture_grant is not None:
            capture_card = CaptureCardView(
                grant_id=capture_grant.id, grant_revision=capture_grant.revision,
                grant_status=capture_grant.status.value, expires_at=capture_grant.expires_at,
                application_label=capture_grant.scope.display.application_label,
                window_title=capture_grant.scope.display.window_title,
                fallback_reason=capture_grant.scope.fallback_reason.value,
            )
        capture_state = None
        if capture is not None:
            capture_state = CaptureStateView(
                capture_id=capture.id, status=capture.status, error_code=capture.error_code,
                started_at=capture.started_at, finished_at=capture.finished_at,
                width=capture.width, height=capture.height, dpi=capture.dpi,
            )
        disclosure_card = None
        if disclosure_grant is not None:
            disclosure_card = DisclosureCardView(
                grant_id=disclosure_grant.id, grant_revision=disclosure_grant.revision,
                grant_status=disclosure_grant.status.value, expires_at=disclosure_grant.expires_at,
                application_label=disclosure_grant.scope.display.application_label,
                window_title=disclosure_grant.scope.display.window_title,
                provider=disclosure_grant.scope.recipient, model=disclosure_grant.scope.model,
                purpose=disclosure_grant.scope.purpose,
            )
        disclosure_state = None
        candidates: list[dict[str, Any]] | None = None
        if disclosure is not None:
            disclosure_state = DisclosureStateView(
                disclosure_id=disclosure.id, status=disclosure.status, error_code=disclosure.error_code,
                started_at=disclosure.started_at, finished_at=disclosure.finished_at,
                candidate_count=len(disclosure.candidates) if disclosure.candidates is not None else None,
            )
            candidates = disclosure.candidates
        return DesktopVisionView(
            task_id=task.id, task_status=task.status.value, task_revision=task.revision,
            objective=str(task.request.get("objective", "")),
            phase=_phase(capture_grant, capture, disclosure_grant, disclosure),
            capture_card=capture_card, capture=capture_state,
            disclosure_card=disclosure_card, disclosure=disclosure_state, candidates=candidates,
        )


def _find_surface(surfaces: list[SurfaceRecord], surface_ref: str, surface_epoch: int) -> SurfaceRecord | None:
    for surface in surfaces:
        if surface.surface_ref == surface_ref and surface.surface_epoch == surface_epoch:
            return surface
    return None


def _phase(
    capture_grant: CaptureGrantRecord | None,
    capture: CaptureRecord | None,
    disclosure_grant: DiscloseGrantRecord | None,
    disclosure: VisionDisclosureRecord | None,
) -> str:
    if disclosure is not None:
        return {
            "STARTED": "sending_to_provider",
            "SUCCEEDED": "candidates_ready",
            "FAILED": "disclosure_failed",
            "OUTCOME_UNKNOWN": "disclosure_outcome_unknown",
        }[disclosure.status]
    if disclosure_grant is not None and disclosure_grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
        return "awaiting_disclosure_approval" if disclosure_grant.status is GrantStatus.PENDING else "disclosure_approved"
    if capture is not None:
        return {
            "STARTED": "capturing",
            "SUCCEEDED": "captured",
            "FAILED": "capture_failed",
            "OUTCOME_UNKNOWN": "capture_outcome_unknown",
        }[capture.status]
    if capture_grant is None:
        return "declined"
    if capture_grant.status is GrantStatus.PENDING:
        return "awaiting_approval"
    if capture_grant.status is GrantStatus.ACTIVE:
        return "approved"
    return "declined"
