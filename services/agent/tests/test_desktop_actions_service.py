"""Milestone 9 S3: the desktop action ledger against real PostgreSQL.

The real `DesktopActionService`, `ActionService`, repositories, migrations and constraints. The only
stand-in is the desktop worker (`FakeEffectDesktop`), which records exactly what it was asked and can be
scripted to refuse, lose its answer or answer as another dispatch. "How many times did the desktop get an
effect request" is a number that must stay at one.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    DesktopObservation,
    DesktopPattern,
    DesktopRole,
    FocusRequest,
    FocusResponse,
    LaunchRequest,
    LaunchResponse,
    ScrollRequest,
    ScrollResponse,
    ScrollStep,
)
from app.desktop.registry import AppRegistry, RegisteredApp
from app.domain.action_status import ActionStatus
from app.repositories.desktop import DesktopRepository
from app.services.actions import ActionService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionError, DesktopActionService, DesktopActionView
from app.services.recovery import RecoveryService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from tests.desktop_disclosure_support import FakeDesktop, node, observation, persist

APP = RegisteredApp(app_id="fake", label="Fake App", executable="C:\\Program Files\\Fake\\fake.exe")


class FakeEffectDesktop(FakeDesktop):
    """The desktop worker as the runtime sees it, with scriptable effects."""

    def __init__(self, engine: AsyncEngine, runtime_generation: uuid.UUID) -> None:
        super().__init__(engine, runtime_generation, make_nodes=lambda: [
            node(1, name="Editor", role=DesktopRole.WINDOW),
            node(2, name="Results", role=DesktopRole.LIST, parent=1).model_copy(update={"patterns": [DesktopPattern.SCROLL]}),
            node(3, name="Save", role=DesktopRole.BUTTON, parent=1),
        ])
        self.baseline = 5000
        self.calls: list[str] = []
        self.requests: list[Any] = []
        self.baseline_error: DesktopRefusal | None = None
        self.effect_error: BaseException | None = None
        self.focus_outcome = "focused"
        self.scroll_outcome = "scrolled"
        self.wrong_dispatch = False
        self.gate: asyncio.Event | None = None

    async def input_baseline(self, worker_generation: uuid.UUID) -> int:
        # What `DesktopService._bind` does the first time it talks to a worker.
        async with self.engine.begin() as connection:
            await DesktopRepository(connection).register_worker_generation(
                worker_generation=self.worker_generation,
                runtime_generation=self.runtime_generation,
                worker_started_at=datetime.now(UTC),
            )
        if self.baseline_error is not None:
            raise self.baseline_error
        if worker_generation != self.worker_generation:
            raise DesktopRefusal(DesktopReason.STALE_WORKER_GENERATION)
        return self.baseline

    async def _effect(self, name: str, request: Any) -> None:
        self.calls.append(name)
        self.requests.append(request)
        if self.gate is not None:
            await self.gate.wait()
        if self.effect_error is not None:
            raise self.effect_error

    async def focus(self, request: FocusRequest) -> FocusResponse:
        await self._effect("focus", request)
        return FocusResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=uuid.uuid4() if self.wrong_dispatch else request.dispatch_id,
            surface_ref=request.surface_ref,
            surface_epoch=request.surface_epoch,
            outcome=cast(Any, self.focus_outcome),
            input_changed=False,
        )

    async def scroll(self, request: ScrollRequest) -> ScrollResponse:
        await self._effect("scroll", request)
        return ScrollResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=request.dispatch_id,
            outcome=cast(Any, self.scroll_outcome),
            percent_before=0.0,
            percent_after=40.0 if self.scroll_outcome == "scrolled" else 0.0,
            input_changed=False,
        )

    async def launch(self, request: LaunchRequest) -> LaunchResponse:
        await self._effect("launch", request)
        return LaunchResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=request.dispatch_id,
            app_id=request.app_id,
            outcome="launched",
            surface_ref="s2",
            surface_epoch=1,
            focused=False,
            input_changed=False,
        )


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> FakeEffectDesktop:
    return FakeEffectDesktop(engine, runtime_generation.id)


@pytest.fixture
def service(
    engine: AsyncEngine, desktop: FakeEffectDesktop, action_service: ActionService, task_service: TaskService
) -> DesktopActionService:
    return DesktopActionService(
        engine,
        actions=action_service,
        tasks=task_service,
        desktop=cast(DesktopService, desktop),
        registry=AppRegistry([APP]),
    )


async def rows(engine: AsyncEngine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return (await connection.execute(text(sql), params)).scalar()


async def propose_focus(service: DesktopActionService, desktop: FakeEffectDesktop) -> DesktopActionView:
    return await service.propose_focus(worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1)


async def observed_scroll(
    service: DesktopActionService, desktop: FakeEffectDesktop, step: ScrollStep = ScrollStep.PAGE_DOWN
) -> tuple[DesktopObservation, DesktopActionView]:
    seen = await desktop.observe(desktop.worker_generation, "s1", 1)
    view = await service.propose_scroll(
        worker_generation=desktop.worker_generation, observation_id=seen.observation_id, control_ref="u2", step=step
    )
    return seen, view


# ---- focus: the whole ledger order ---------------------------------------------------------------------------


async def test_a_proposal_alone_performs_no_effect_and_shows_an_exact_card(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    assert view.status is ActionStatus.WAITING_APPROVAL
    assert (view.proposal.application_label, view.proposal.window_title) == ("Editor", "Editor - notes.txt")  # type: ignore[union-attr]
    assert desktop.calls == []
    assert await scalar(engine, "SELECT count(*) FROM desktop_dispatches") == 0
    assert await scalar(engine, "SELECT count(*) FROM action_attempts") == 0


async def test_the_exact_approval_performs_exactly_one_effect_with_a_durable_dispatch_first(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.SUCCEEDED
    assert desktop.calls == ["focus"]
    request = desktop.requests[0]
    assert (request.surface_ref, request.surface_epoch, request.input_tick) == ("s1", 1, desktop.baseline)
    (dispatch,) = await rows(engine, "SELECT id, status, operation, input_tick, finished_at FROM desktop_dispatches")
    assert (dispatch.status, dispatch.operation, dispatch.input_tick) == ("OK", "focus_surface", desktop.baseline)
    assert request.dispatch_id == dispatch.id, "the worker was asked under the dispatch that was made durable first"
    assert await scalar(engine, "SELECT status FROM approvals") == "CONSUMED"
    assert await scalar(engine, "SELECT count(*) FROM action_attempts") == 1


async def test_an_approval_is_single_use(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    view = await propose_focus(service, desktop)
    await service.approve(view.action_id, expected_revision=view.revision)
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=view.revision)
    assert desktop.calls == ["focus"]


async def test_two_racing_approvals_produce_one_effect(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    view = await propose_focus(service, desktop)
    results = await asyncio.gather(
        service.approve(view.action_id, expected_revision=view.revision),
        service.approve(view.action_id, expected_revision=view.revision),
        return_exceptions=True,
    )
    assert sum(isinstance(item, DesktopActionView) for item in results) == 1
    assert desktop.calls == ["focus"]


async def test_a_wrong_revision_cannot_approve(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    view = await propose_focus(service, desktop)
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=view.revision + 1)
    assert desktop.calls == []


async def test_a_declined_action_can_never_run(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    view = await propose_focus(service, desktop)
    declined = await service.decline(view.action_id, expected_revision=view.revision)
    assert declined.status is ActionStatus.REJECTED
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=declined.revision)
    assert desktop.calls == []


async def test_a_newer_proposal_supersedes_an_unanswered_card_which_can_then_never_run(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    first = await propose_focus(service, desktop)
    second = await propose_focus(service, desktop)
    assert (await service.get(first.action_id)).status is ActionStatus.REJECTED
    with pytest.raises(DesktopActionError):
        await service.approve(first.action_id, expected_revision=first.revision)
    assert desktop.calls == []
    await service.approve(second.action_id, expected_revision=second.revision)
    assert desktop.calls == ["focus"]


async def test_a_dead_worker_generation_rejects_the_card_and_performs_nothing(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    view = await propose_focus(service, desktop)
    desktop.worker_generation = uuid.uuid4()  # the worker was replaced after the card was shown
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=view.revision)
    assert (await service.get(view.action_id)).status is ActionStatus.REJECTED
    assert desktop.calls == []


async def test_a_stale_surface_cannot_even_be_proposed(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    with pytest.raises(DesktopActionError):
        await service.propose_focus(worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=9)
    with pytest.raises(DesktopActionError):
        await service.propose_focus(worker_generation=uuid.uuid4(), surface_ref="s1", surface_epoch=1)


# ---- failure classification ----------------------------------------------------------------------------------


async def test_a_refusal_before_any_effect_is_a_known_failure_and_is_not_retried(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    desktop.effect_error = DesktopRefusal(DesktopReason.HUMAN_INPUT_DETECTED)
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.FAILED and done.error_code == "human_input_detected"
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "FAILED_BEFORE_EFFECT"
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=done.revision)
    assert desktop.calls == ["focus"]


async def test_focus_the_worker_verified_did_not_happen_is_a_known_failure_with_a_truthful_dispatch(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    desktop.focus_outcome = "not_focused"
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.FAILED and done.error_code == "not_focused"
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "OK"


@pytest.mark.parametrize(
    "error",
    [
        DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE),
        DesktopRefusal(DesktopReason.OBSERVATION_TIMEOUT),
        DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN),
        RuntimeError("garbled"),
    ],
)
async def test_a_lost_or_uncertain_answer_is_outcome_unknown_and_never_retried(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine, error: Exception
) -> None:
    desktop.effect_error = error
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.OUTCOME_UNKNOWN
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "OUTCOME_UNKNOWN"
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=done.revision)
    assert desktop.calls == ["focus"], "nothing repeats an uncertain effect"
    # A NEW card (a new decision by the person) is possible; it never inherits the old approval.
    desktop.effect_error = None
    again = await propose_focus(service, desktop)
    assert again.action_id != view.action_id
    assert desktop.calls == ["focus"]


async def test_an_answer_addressed_to_another_dispatch_is_never_believed(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    desktop.wrong_dispatch = True

    async def stale(request: FocusRequest) -> FocusResponse:
        await desktop._effect("focus", request)
        raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN)  # what DesktopService reports for StaleDesktopResult

    desktop.focus = stale  # type: ignore[method-assign]
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.OUTCOME_UNKNOWN


async def test_a_crash_between_the_durable_dispatch_and_the_answer_is_unknown_after_restart_and_never_repeated(
    service: DesktopActionService,
    desktop: FakeEffectDesktop,
    engine: AsyncEngine,
    runtime_generation: RuntimeGeneration,
) -> None:
    class Crash(BaseException):
        pass

    desktop.effect_error = Crash()
    view = await propose_focus(service, desktop)
    with pytest.raises(Crash):
        await service.approve(view.action_id, expected_revision=view.revision)
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "DISPATCHED"
    assert (await service.get(view.action_id)).status is ActionStatus.EXECUTING

    recovered = await RecoveryService(engine).recover_unfinished_attempts(uuid.uuid4())
    assert [item.action_id for item in recovered] == [view.action_id]
    assert (await service.get(view.action_id)).status is ActionStatus.OUTCOME_UNKNOWN
    (dispatch,) = await rows(engine, "SELECT status, error_code, finished_at FROM desktop_dispatches")
    assert (dispatch.status, dispatch.error_code) == ("OUTCOME_UNKNOWN", "runtime_restart") and dispatch.finished_at is not None
    assert desktop.calls == ["focus"]
    with pytest.raises(DesktopActionError):
        await service.approve(view.action_id, expected_revision=view.revision)
    assert desktop.calls == ["focus"]


async def test_a_running_effect_blocks_every_other_desktop_action(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    desktop.gate = asyncio.Event()
    view = await propose_focus(service, desktop)
    running = asyncio.create_task(service.approve(view.action_id, expected_revision=view.revision))
    for _ in range(100):
        if desktop.calls:
            break
        await asyncio.sleep(0.1)
    assert desktop.calls == ["focus"]
    with pytest.raises(DesktopActionError) as error:
        await propose_focus(service, desktop)
    assert error.value.code == "desktop_action_open"
    desktop.gate.set()
    assert (await running).status is ActionStatus.SUCCEEDED


# ---- scroll ---------------------------------------------------------------------------------------------------


async def test_scroll_is_a_separate_exact_approval_that_invalidates_the_observation_and_reads_again(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    seen, view = await observed_scroll(service, desktop)
    assert desktop.observe_calls == 1
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.SUCCEEDED
    assert done.result is not None and done.result["observation_invalidated"] is True
    assert desktop.observe_calls == 2, "a fresh S1 observation followed the scroll"
    follow_up = done.result["follow_up_observation_id"]
    assert follow_up and follow_up != str(seen.observation_id)
    request = desktop.requests[0]
    assert (request.control_ref, request.step, request.observation_id) == ("u2", ScrollStep.PAGE_DOWN, seen.observation_id)
    (dispatch,) = await rows(engine, "SELECT operation, control_ref, snapshot_digest, status FROM desktop_dispatches")
    assert dispatch.operation == "scroll_control" and dispatch.control_ref == "u2" and dispatch.status == "OK"
    assert len(dispatch.snapshot_digest) == 64


async def test_scroll_targets_lists_only_scrollable_controls_from_a_local_observation(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    observation_id, targets = await service.scroll_targets(
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1
    )
    assert targets == [("u2", "list", "Results")]
    view = await service.propose_scroll(
        worker_generation=desktop.worker_generation, observation_id=observation_id, control_ref="u2",
        step=ScrollStep.SMALL_DOWN,
    )
    assert view.status is ActionStatus.WAITING_APPROVAL and desktop.calls == []


async def test_scroll_that_moved_nothing_is_a_known_failure(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    desktop.scroll_outcome = "unchanged"
    _, view = await observed_scroll(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.FAILED and done.error_code == "scroll_no_change"


async def test_a_scroll_cannot_be_proposed_from_a_stale_superseded_or_non_scrollable_observation(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> None:
    first = await desktop.observe(desktop.worker_generation, "s1", 1)
    await desktop.observe(desktop.worker_generation, "s1", 1)  # a newer observation supersedes the first
    with pytest.raises(DesktopActionError) as superseded:
        await service.propose_scroll(
            worker_generation=desktop.worker_generation, observation_id=first.observation_id,
            control_ref="u2", step=ScrollStep.SMALL_DOWN,
        )
    assert superseded.value.code == "desktop_action_observation_stale"

    latest = await desktop.observe(desktop.worker_generation, "s1", 1)
    with pytest.raises(DesktopActionError) as not_scrollable:
        await service.propose_scroll(
            worker_generation=desktop.worker_generation, observation_id=latest.observation_id,
            control_ref="u3", step=ScrollStep.SMALL_DOWN,  # a button
        )
    assert not_scrollable.value.code == "desktop_action_invalid"
    with pytest.raises(DesktopActionError):
        await service.propose_scroll(
            worker_generation=desktop.worker_generation, observation_id=latest.observation_id,
            control_ref="u99", step=ScrollStep.SMALL_DOWN,
        )

    old = await desktop.observe(desktop.worker_generation, "s1", 1)
    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE desktop_observations SET created_at = :at WHERE id = :id"),
            {"at": datetime.now(UTC) - timedelta(minutes=5), "id": old.observation_id},
        )
    with pytest.raises(DesktopActionError):
        await service.propose_scroll(
            worker_generation=desktop.worker_generation, observation_id=old.observation_id,
            control_ref="u2", step=ScrollStep.SMALL_DOWN,
        )
    assert desktop.calls == []


async def test_an_observation_that_goes_stale_between_the_card_and_the_click_is_refused_and_nothing_is_written(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    _, view = await observed_scroll(service, desktop)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE desktop_observations SET created_at = now() - interval '5 minutes'"))
    with pytest.raises(DesktopActionError) as error:
        await service.approve(view.action_id, expected_revision=view.revision)
    assert error.value.code == "desktop_action_observation_stale"
    assert desktop.calls == []
    assert await scalar(engine, "SELECT count(*) FROM action_attempts") == 0, "a refused guard writes nothing"
    assert await scalar(engine, "SELECT status FROM approvals") == "PENDING"


# ---- launch ---------------------------------------------------------------------------------------------------


async def test_launch_names_only_a_registered_app_and_the_card_label_comes_from_the_registry(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    for unregistered in ("cmd", "notepad", "powershell"):
        with pytest.raises(DesktopRefusal) as error:
            await service.propose_launch(app_id=unregistered)
        assert error.value.code is DesktopReason.APP_NOT_REGISTERED
    view = await service.propose_launch(app_id="fake")
    assert view.proposal.application_label == "Fake App"
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.SUCCEEDED and done.result is not None and done.result["outcome"] == "launched"
    request = desktop.requests[0]
    assert set(type(request).model_fields) == {"expected_worker_generation", "dispatch_id", "app_id", "input_tick"}
    (dispatch,) = await rows(engine, "SELECT operation, app_id, surface_ref, control_ref FROM desktop_dispatches")
    assert (dispatch.operation, dispatch.app_id, dispatch.surface_ref, dispatch.control_ref) == ("launch_app", "fake", None, None)


# ---- what is stored and what may leave -----------------------------------------------------------------------


async def test_no_title_path_or_text_reaches_the_dispatch_events_or_result_rows(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    await service.approve(view.action_id, expected_revision=view.revision)
    for table in ("desktop_dispatches", "action_attempts", "task_events"):
        dump = " ".join(str(row) for row in await rows(engine, f"SELECT * FROM {table}"))
        assert "notes.txt" not in dump and "Editor - " not in dump, table
        assert "fake.exe" not in dump and "Program Files" not in dump, table


async def test_the_database_itself_enforces_one_dispatch_per_attempt_and_the_identity_each_operation_needs(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    await service.approve(view.action_id, expected_revision=view.revision)
    attempt = await scalar(engine, "SELECT id FROM action_attempts")
    generation = desktop.worker_generation
    insert = (
        "INSERT INTO desktop_dispatches (id, action_id, attempt_id, worker_generation, operation, surface_ref, "
        "surface_epoch, control_ref, app_id, input_tick, status) VALUES (:id, :action, :attempt, :gen, :op, "
        ":sref, :sepoch, :cref, :app, 1, 'DISPATCHED')"
    )
    base = {"action": view.action_id, "attempt": attempt, "gen": generation, "sref": "s1", "sepoch": 1, "cref": None, "app": None}
    with pytest.raises(IntegrityError):  # a second dispatch for one attempt
        async with engine.begin() as connection:
            await connection.execute(text(insert), {**base, "id": uuid.uuid4(), "op": "focus_surface"})
    new_attempt_needed = {**base, "id": uuid.uuid4()}
    for bad in (
        {"op": "focus_surface", "cref": "u1"},  # a focus that names a control
        {"op": "focus_surface", "app": "fake"},  # a focus that names an app
        {"op": "launch_app", "sref": "s1", "app": "fake"},  # a launch that names a surface
        {"op": "launch_app", "sref": None, "sepoch": None, "app": None},  # a launch with no app
        {"op": "click", "app": None},  # an operation that does not exist
        {"op": "scroll_control", "cref": "u2"},  # a scroll without an observation
    ):
        with pytest.raises((IntegrityError, DBAPIError)):
            async with engine.begin() as connection:
                await connection.execute(text(insert), {**new_attempt_needed, **bad, "attempt": uuid.uuid4()})


async def test_the_persisted_proposal_is_immutable_so_the_approved_values_are_the_executed_values(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    with pytest.raises((IntegrityError, DBAPIError)):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE actions SET proposal = jsonb_set(proposal, '{surface_ref}', '\"s2\"') WHERE id = :id"),
                {"id": view.action_id},
            )
    await service.approve(view.action_id, expected_revision=view.revision)
    assert desktop.requests[0].surface_ref == "s1"


# ---- independent review (S3) ---------------------------------------------------------------------------------


async def test_a_backend_failure_from_an_effect_call_is_unknown_never_a_known_failure(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    desktop.effect_error = DesktopRefusal(DesktopReason.BACKEND_FAILED)
    view = await propose_focus(service, desktop)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.OUTCOME_UNKNOWN
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "OUTCOME_UNKNOWN"


async def test_a_cancelled_request_cannot_leave_an_action_executing(
    service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    desktop.gate = asyncio.Event()
    view = await propose_focus(service, desktop)
    request = asyncio.create_task(service.approve(view.action_id, expected_revision=view.revision))
    for _ in range(100):
        if desktop.calls:
            break
        await asyncio.sleep(0.1)
    request.cancel()
    await asyncio.sleep(0.1)
    desktop.gate.set()  # the worker answers after the caller has gone
    for _ in range(100):
        if (await service.get(view.action_id)).status is not ActionStatus.EXECUTING:
            break
        await asyncio.sleep(0.1)
    assert (await service.get(view.action_id)).status is ActionStatus.SUCCEEDED
    assert await scalar(engine, "SELECT status FROM desktop_dispatches") == "OK"
    assert desktop.calls == ["focus"]


async def test_two_concurrent_proposals_leave_one_live_card(service: DesktopActionService, desktop: FakeEffectDesktop) -> None:
    first, second = await asyncio.gather(propose_focus(service, desktop), propose_focus(service, desktop))
    statuses = sorted([(await service.get(first.action_id)).status.value, (await service.get(second.action_id)).status.value])
    assert statuses == ["REJECTED", "WAITING_APPROVAL"]


async def test_the_generic_action_routes_can_never_touch_a_desktop_action(
    client: Any, service: DesktopActionService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    view = await propose_focus(service, desktop)
    before = [tuple(row) for row in await rows(engine, "SELECT status, revision FROM actions WHERE id = :id", id=view.action_id)]
    action = str(view.action_id)
    for path in (
        f"/actions/{action}/approval-request", f"/actions/{action}/approve",
        f"/actions/{action}/attempts", f"/actions/{action}/attempts/finish",
        f"/actions/{action}/reconciliation", f"/actions/{action}/reconciliation/finish",
    ):
        response = await client.post(path, json={})
        assert response.status_code in (404, 405, 409, 422), (path, response.status_code)
    after = [tuple(row) for row in await rows(engine, "SELECT status, revision FROM actions WHERE id = :id", id=view.action_id)]
    assert after == before
    assert await scalar(engine, "SELECT count(*) FROM action_attempts") == 0
    # Declining through the generic route is harmless (a rejected action can never run) and stays available as Stop.
    approve = await client.post(f"/actions/{action}/approve", json={})
    assert approve.status_code == 409 and approve.json()["error"]["reason"] == "use_desktop_route"
    # And nobody can plant a desktop action through the generic proposal route.
    created = await client.post("/tasks", json={"request": {"type": "note", "text": "x"}})
    task_id = created.json()["id"]
    for tool in ("DESKTOP_LAUNCH", "desktop_launch", "Desktop_Focus"):
        planted = await client.post(
            f"/tasks/{task_id}/actions",
            json={"idempotency_key": "k", "tool_name": tool, "risk_tier": "R2", "proposal": {"app_id": "x"}},
        )
        # Uppercase names fail the generic body pattern outright; any spelling that gets through is refused.
        assert planted.status_code in (409, 422), (tool, planted.status_code)
        if planted.status_code == 409:
            assert planted.json()["error"]["reason"] == "use_desktop_route"
    assert await scalar(engine, "SELECT count(*) FROM actions WHERE upper(tool_name) LIKE 'DESKTOP%' AND task_id = :t", t=uuid.UUID(task_id)) == 0
    rejected = await client.post(f"/actions/{action}/reject", json={})
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    assert desktop.calls == [] and await scalar(engine, "SELECT count(*) FROM action_attempts") == 0


async def test_the_generic_action_reads_do_not_return_another_programs_window_text(
    client: Any, service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    view = await propose_focus(service, desktop)
    response = await client.get(f"/actions/{view.action_id}")
    body = response.json()
    assert response.status_code == 200
    assert "notes.txt" not in response.text and "window_title" not in body["proposal"]
    assert body["proposal"]["surface_ref"] == "s1"
