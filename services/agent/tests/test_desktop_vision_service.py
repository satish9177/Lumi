"""Milestone 9 S5: the scoped visual-fallback service against real PostgreSQL.

Every test drives the real `DesktopVisionService`, repository and database. The only stand-in is the
desktop worker (`FakeVisionDesktop`), which records exactly how many times it was asked for a capture
and returns a scripted, deterministic `CaptureResponse` -- "how many times did the worker actually
capture pixels" is a number that must stay at exactly one per claim, and the disclosure claim's own
capture must always be a DIFFERENT `capture_id` from the first.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import CaptureRequest, CaptureResponse
from app.domain.desktop_vision import DesktopVisionRefusal
from app.repositories.desktop import DesktopRepository
from app.services.desktop import DesktopService
from app.services.desktop_vision import DesktopVisionService, DesktopVisionView
from app.services.runtime import RuntimeGeneration
from tests.desktop_disclosure_support import FakeDesktop, node, persist
from app.desktop.protocol import DesktopRole

OBJECTIVE = "Find the Settings button"
PURPOSE = "Find the exact Settings button and tell me where it is"
TITLE = "Editor - notes.txt"


class FakeVisionDesktop(FakeDesktop):
    """The desktop worker as the runtime sees it, with a scriptable capture effect."""

    def __init__(self, engine: AsyncEngine, runtime_generation: uuid.UUID) -> None:
        # A tree too sparse/unknown-heavy to be readable -- `uia_missing_required_semantics`
        # eligible by construction, so `create_capture` does not need to be separately mocked.
        super().__init__(
            engine, runtime_generation,
            make_nodes=lambda: [node(1, name=None, role=DesktopRole.UNKNOWN)],
        )
        self.baseline = 5000
        self.capture_calls: list[CaptureRequest] = []
        self.capture_error: DesktopRefusal | None = None
        self.capture_outcome: dict[str, Any] = {}

    async def input_baseline(self, worker_generation: uuid.UUID) -> int:
        async with self.engine.begin() as connection:
            await DesktopRepository(connection).register_worker_generation(
                worker_generation=self.worker_generation,
                runtime_generation=self.runtime_generation,
                worker_started_at=datetime.now(UTC),
            )
        if worker_generation != self.worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        return self.baseline

    async def capture(self, request: CaptureRequest) -> CaptureResponse:
        self.capture_calls.append(request)
        if self.capture_error is not None:
            raise self.capture_error
        defaults: dict[str, Any] = {
            "worker_generation": request.expected_worker_generation,
            "capture_id": request.capture_id,
            "surface_ref": request.surface_ref,
            "surface_epoch": request.surface_epoch,
            "geometry_fingerprint": "a" * 64,
            "frame_digest": "b" * 64,
            "width": 800,
            "height": 600,
            "dpi": 96,
            "monitor_id": 1,
            "image_base64": "QUFB",
            "input_changed": False,
        }
        defaults.update(self.capture_outcome)
        return CaptureResponse(**defaults)


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> FakeVisionDesktop:
    return FakeVisionDesktop(engine, runtime_generation.id)


@pytest.fixture
def service(engine: AsyncEngine, desktop: FakeVisionDesktop) -> DesktopVisionService:
    return DesktopVisionService(engine, desktop=cast(DesktopService, desktop), grant_ttl_seconds=600)


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


async def open_capture_card(service: DesktopVisionService, desktop: FakeVisionDesktop) -> DesktopVisionView:
    return await service.create_capture(
        objective=OBJECTIVE, worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
    )


async def captured(service: DesktopVisionService, desktop: FakeVisionDesktop) -> DesktopVisionView:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    granted = await service.confirm_capture(
        view.task_id, grant_id=view.capture_card.grant_id, expected_revision=view.capture_card.grant_revision
    )
    assert granted.phase == "approved"
    await service.claim_capture(view.task_id)
    return await service.describe(view.task_id)


def refusal_code(error: pytest.ExceptionInfo[DesktopVisionRefusal]) -> str:
    return error.value.code


# ---- create_capture: the deterministic trigger, and nothing captured yet ----------------------------


async def test_create_capture_opens_a_pending_card_and_captures_nothing(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await open_capture_card(service, desktop)
    assert view.phase == "awaiting_approval" and view.capture_card is not None
    assert view.capture_card.grant_status == "PENDING"
    assert view.capture_card.fallback_reason == "uia_missing_required_semantics"
    assert view.capture_card.application_label == "Editor" and view.capture_card.window_title == TITLE
    assert view.capture is None
    assert desktop.capture_calls == []

    task = (await rows(engine, "SELECT request, status FROM tasks"))[0]
    assert task.request == {"type": "desktop_vision", "objective": OBJECTIVE}
    assert await scalar(engine, "SELECT count(*) FROM desktop_captures") == 0
    grant = (await rows(engine, "SELECT kind, status FROM task_grants"))[0]
    assert (grant.kind, grant.status) == ("desktop_vision_capture", "PENDING")


async def test_a_sufficient_semantic_tree_refuses_before_any_task_or_grant_exists(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    desktop.make_nodes = lambda: [
        node(1, name="Window", role=DesktopRole.WINDOW),
        node(2, name="Settings", role=DesktopRole.BUTTON, parent=1),
        node(3, name="Field", role=DesktopRole.EDIT, parent=1),
        node(4, name="List", role=DesktopRole.LIST, parent=1),
        node(5, name="Item", role=DesktopRole.LIST_ITEM, parent=4),
    ]
    with pytest.raises(DesktopVisionRefusal) as info:
        await open_capture_card(service, desktop)
    assert refusal_code(info) == "fallback_not_eligible"
    assert await scalar(engine, "SELECT count(*) FROM tasks") == 0
    assert await scalar(engine, "SELECT count(*) FROM task_grants") == 0


# ---- confirm_capture / revoke_capture ----------------------------------------------------------------


async def test_confirm_capture_moves_the_grant_to_active(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    granted = await service.confirm_capture(
        view.task_id, grant_id=view.capture_card.grant_id, expected_revision=view.capture_card.grant_revision
    )
    assert granted.phase == "approved" and granted.capture_card is not None
    assert granted.capture_card.grant_status == "ACTIVE"


async def test_confirming_with_a_stale_revision_is_refused(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.confirm_capture(view.task_id, grant_id=view.capture_card.grant_id, expected_revision=999)
    assert refusal_code(info) == "grant_changed"


async def test_revoking_before_a_claim_cancels_the_task_and_captures_nothing(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    revoked = await service.revoke_capture(
        view.task_id, grant_id=view.capture_card.grant_id, expected_revision=view.capture_card.grant_revision
    )
    assert revoked.phase == "declined"
    assert desktop.capture_calls == []
    task = (await rows(engine, "SELECT status FROM tasks WHERE id = :id", id=view.task_id))[0]
    assert task.status == "CANCELLED"


# ---- claim_capture: consume the grant, then ONE real capture -----------------------------------------


async def test_claim_capture_performs_exactly_one_capture_and_persists_metadata_only(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await captured(service, desktop)
    assert len(desktop.capture_calls) == 1
    assert view.phase == "captured" and view.capture is not None
    assert view.capture.status == "SUCCEEDED"
    assert view.capture.width == 800 and view.capture.height == 600 and view.capture.dpi == 96

    row = (await rows(engine, "SELECT * FROM desktop_captures"))[0]._mapping
    assert row["status"] == "SUCCEEDED"
    assert row["geometry_fingerprint"] == "a" * 64 and row["frame_digest"] == "b" * 64
    assert row["width"] == 800 and row["height"] == 600 and row["dpi"] == 96 and row["monitor_id"] == 1
    # No column anywhere on this row can hold the image itself.
    assert set(row.keys()) == {
        "id", "task_id", "grant_id", "surface_ref", "surface_epoch", "approval_input_tick",
        "geometry_fingerprint", "frame_digest", "width", "height", "dpi", "monitor_id", "status",
        "started_at", "finished_at", "error_code", "created_at",
    }

    grant = (await rows(engine, "SELECT status FROM task_grants WHERE kind = 'desktop_vision_capture'"))[0]
    assert grant.status == "COMPLETED"


async def test_a_second_claim_on_the_same_grant_is_refused(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await captured(service, desktop)
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.claim_capture(view.task_id)
    assert refusal_code(info) == "grant_not_active"
    assert len(desktop.capture_calls) == 1


async def test_a_failed_native_capture_marks_the_capture_failed_not_the_grant_reusable(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    await service.confirm_capture(view.task_id, grant_id=view.capture_card.grant_id, expected_revision=view.capture_card.grant_revision)
    desktop.capture_error = DesktopRefusal(DesktopReason.CAPTURE_REFUSED)
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.claim_capture(view.task_id)
    assert refusal_code(info) == "capture_refused"
    final = await service.describe(view.task_id)
    assert final.phase == "capture_failed" and final.capture is not None
    assert final.capture.status == "FAILED" and final.capture.error_code == "capture_refused"
    # The grant was already claimed (COMPLETED) before the worker call: a failure does not refund it.
    grant = (await rows(engine, "SELECT status FROM task_grants WHERE kind = 'desktop_vision_capture'"))[0]
    assert grant.status == "COMPLETED"


# ---- create_disclosure: requires a SUCCEEDED capture, a SEPARATE grant --------------------------------


async def test_disclosure_requires_a_succeeded_capture(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await open_capture_card(service, desktop)
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.create_disclosure(view.task_id, recipient="gemini", model="gemini-2.5-flash", purpose=PURPOSE)
    assert refusal_code(info) == "capture_not_succeeded"


async def test_create_disclosure_opens_a_separate_pending_grant_and_sends_nothing(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await captured(service, desktop)
    disclosed = await service.create_disclosure(view.task_id, recipient="gemini", model="gemini-2.5-flash", purpose=PURPOSE)
    assert disclosed.phase == "awaiting_disclosure_approval" and disclosed.disclosure_card is not None
    assert disclosed.disclosure_card.grant_status == "PENDING"
    assert disclosed.disclosure_card.provider == "gemini" and disclosed.disclosure_card.purpose == PURPOSE
    assert len(desktop.capture_calls) == 1  # still just the first, local-only capture
    grants = await rows(engine, "SELECT kind, status FROM task_grants ORDER BY created_at")
    assert [(g.kind, g.status) for g in grants] == [
        ("desktop_vision_capture", "COMPLETED"), ("desktop_vision_disclose", "PENDING"),
    ]


async def test_claim_disclosure_takes_a_brand_new_capture_never_the_first_ones_bytes(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await captured(service, desktop)
    disclosed = await service.create_disclosure(view.task_id, recipient="gemini", model="gemini-2.5-flash", purpose=PURPOSE)
    assert disclosed.disclosure_card is not None
    granted = await service.confirm_disclosure(
        view.task_id, grant_id=disclosed.disclosure_card.grant_id, expected_revision=disclosed.disclosure_card.grant_revision
    )
    assert granted.phase == "disclosure_approved"
    context = await service.claim_disclosure(view.task_id)
    assert len(desktop.capture_calls) == 2
    assert desktop.capture_calls[0].capture_id != desktop.capture_calls[1].capture_id
    assert context.capture.capture_id == desktop.capture_calls[1].capture_id
    assert context.purpose == PURPOSE and context.recipient == "gemini" and context.model == "gemini-2.5-flash"

    row = (await rows(engine, "SELECT * FROM desktop_vision_disclosures"))[0]._mapping
    assert row["status"] == "STARTED"  # no result recorded yet
    assert row["geometry_fingerprint"] == "a" * 64  # the frame is recorded even before a result exists
    assert set(row.keys()) == {
        "id", "task_id", "grant_id", "capture_id", "provider", "model", "purpose", "approval_input_tick",
        "geometry_fingerprint", "frame_digest", "width", "height", "dpi", "monitor_id", "candidates",
        "status", "started_at", "finished_at", "error_code", "created_at",
    }


async def test_a_second_claim_disclosure_is_refused(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await captured(service, desktop)
    disclosed = await service.create_disclosure(view.task_id, recipient="gemini", model="gemini-2.5-flash", purpose=PURPOSE)
    assert disclosed.disclosure_card is not None
    await service.confirm_disclosure(
        view.task_id, grant_id=disclosed.disclosure_card.grant_id, expected_revision=disclosed.disclosure_card.grant_revision
    )
    await service.claim_disclosure(view.task_id)
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.claim_disclosure(view.task_id)
    assert refusal_code(info) == "grant_not_active"
    assert len(desktop.capture_calls) == 2


# ---- record_candidates -------------------------------------------------------------------------------


CANDIDATE = {
    "schema_version": 1, "kind": "candidate", "label": "Settings",
    "region": {"x": 0.5, "y": 0.3, "w": 0.2, "h": 0.1}, "confidence": 0.9, "observed_text": "Settings",
}


async def _disclosed_and_claimed(service: DesktopVisionService, desktop: FakeVisionDesktop) -> DesktopVisionView:
    view = await captured(service, desktop)
    disclosed = await service.create_disclosure(view.task_id, recipient="gemini", model="gemini-2.5-flash", purpose=PURPOSE)
    assert disclosed.disclosure_card is not None
    await service.confirm_disclosure(
        view.task_id, grant_id=disclosed.disclosure_card.grant_id, expected_revision=disclosed.disclosure_card.grant_revision
    )
    await service.claim_disclosure(view.task_id)
    return await service.describe(view.task_id)


async def test_recording_a_well_formed_result_stores_candidates_and_succeeds(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await _disclosed_and_claimed(service, desktop)
    disclosure_id = view.disclosure.disclosure_id  # type: ignore[union-attr]
    final = await service.record_candidates(
        view.task_id, disclosure_id=disclosure_id, result={"schema_version": 1, "candidates": [CANDIDATE]}, failure=None
    )
    assert final.phase == "candidates_ready"
    assert final.candidates == [
        {"schema_version": 1, "kind": "candidate", "label": "Settings",
         "region": {"x": 0.5, "y": 0.3, "w": 0.2, "h": 0.1}, "confidence": 0.9, "observed_text": "Settings"}
    ]
    row = (await rows(engine, "SELECT status, candidates FROM desktop_vision_disclosures"))[0]
    assert row.status == "SUCCEEDED" and len(row.candidates) == 1


async def test_a_result_that_smuggles_an_action_field_is_refused_as_invalid_output(
    service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await _disclosed_and_claimed(service, desktop)
    disclosure_id = view.disclosure.disclosure_id  # type: ignore[union-attr]
    hostile = dict(CANDIDATE, click=True)
    final = await service.record_candidates(
        view.task_id, disclosure_id=disclosure_id, result={"schema_version": 1, "candidates": [hostile]}, failure=None
    )
    assert final.phase == "disclosure_failed" and final.disclosure is not None
    assert final.disclosure.error_code == "invalid_output"


async def test_recording_a_provider_failure_ends_the_task(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await _disclosed_and_claimed(service, desktop)
    disclosure_id = view.disclosure.disclosure_id  # type: ignore[union-attr]
    final = await service.record_candidates(view.task_id, disclosure_id=disclosure_id, result=None, failure="model_unavailable")
    assert final.phase == "disclosure_failed" and final.disclosure is not None
    assert final.disclosure.error_code == "model_unavailable"


async def test_recording_twice_on_the_same_disclosure_is_refused(service: DesktopVisionService, desktop: FakeVisionDesktop) -> None:
    view = await _disclosed_and_claimed(service, desktop)
    disclosure_id = view.disclosure.disclosure_id  # type: ignore[union-attr]
    await service.record_candidates(view.task_id, disclosure_id=disclosure_id, result=None, failure="model_unavailable")
    with pytest.raises(DesktopVisionRefusal) as info:
        await service.record_candidates(view.task_id, disclosure_id=disclosure_id, result=None, failure="model_unavailable")
    assert refusal_code(info) == "disclosure_already_recorded"


# ---- recovery: a runtime that died leaves an honest OUTCOME_UNKNOWN, never a silent retry -------------


async def test_recovery_marks_an_interrupted_capture_outcome_unknown(
    engine: AsyncEngine, service: DesktopVisionService, desktop: FakeVisionDesktop
) -> None:
    view = await open_capture_card(service, desktop)
    assert view.capture_card is not None
    await service.confirm_capture(view.task_id, grant_id=view.capture_card.grant_id, expected_revision=view.capture_card.grant_revision)
    # Simulate the process dying between the claim commit and the worker call finishing: a STARTED
    # row exists with no worker call ever having been recorded as finished.
    from app.repositories.desktop_vision import DesktopCaptureRepository

    async with engine.begin() as connection:
        await DesktopCaptureRepository(connection).start_capture(
            capture_id=uuid.uuid4(), task_id=view.task_id, grant_id=view.capture_card.grant_id,
            surface_ref="s1", surface_epoch=1, approval_input_tick=1000,
        )
    marked = await service.recover_started()
    assert marked == 1
    final = await service.describe(view.task_id)
    assert final.phase == "capture_outcome_unknown" and final.capture is not None
    assert final.capture.status == "OUTCOME_UNKNOWN" and final.capture.error_code == "runtime_restart"
