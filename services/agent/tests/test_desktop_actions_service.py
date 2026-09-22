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
    InvokeEffect,
    InvokeRequest,
    InvokeResponse,
    LaunchRequest,
    LaunchResponse,
    ScrollRequest,
    ScrollResponse,
    ScrollStep,
    SelectRequest,
    SelectResponse,
    SetValueRequest,
    SetValueResponse,
)
from app.desktop.registry import AppRegistry, RegisteredApp
from app.domain.action_status import ActionStatus
from app.repositories.desktop import DesktopRepository
from app.services.actions import ActionService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionError, DesktopActionService, DesktopActionView
from app.services.recovery import RecoveryService
from app.services.runtime import RuntimeGeneration
from app.domain.desktop_planning import DesktopPlanRefusal
from app.services.desktop_planning import DesktopPlanningService
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
            node(4, name="Field", role=DesktopRole.EDIT, parent=1).model_copy(update={"patterns": [DesktopPattern.VALUE]}),
            node(5, name="Combo", role=DesktopRole.COMBO_BOX, parent=1),
            node(6, name="Option A", role=DesktopRole.LIST_ITEM, parent=5).model_copy(
                update={"patterns": [DesktopPattern.SELECTION_ITEM]}
            ),
            node(7, name="Show details", role=DesktopRole.BUTTON, parent=1).model_copy(
                update={"patterns": [DesktopPattern.INVOKE]}
            ),
            # A LIST-rooted single-select container, exactly the shape a command-palette-style
            # "quick pick" widget uses (a `list`/`list_item` pair with the SelectionItem pattern,
            # not a `combo_box`): selecting an item like this is commonly activation, not mere
            # highlighting, so `select_control` must refuse it (`SAFE_SELECT_CONTAINER_ROLES`).
            node(8, name="Commands", role=DesktopRole.LIST, parent=1),
            node(9, name="Delete Everything", role=DesktopRole.LIST_ITEM, parent=8).model_copy(
                update={"patterns": [DesktopPattern.SELECTION_ITEM]}
            ),
        ])
        self.baseline = 5000
        self.calls: list[str] = []
        self.requests: list[Any] = []
        self.baseline_error: DesktopRefusal | None = None
        self.effect_error: BaseException | None = None
        self.focus_outcome = "focused"
        self.scroll_outcome = "scrolled"
        self.set_value_outcome = "set"
        self.select_outcome = "selected"
        self.invoke_outcome = "invoked"
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

    async def set_value(self, request: SetValueRequest) -> SetValueResponse:
        await self._effect("set_value", request)
        return SetValueResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=request.dispatch_id,
            outcome=cast(Any, self.set_value_outcome),
            input_changed=False,
        )

    async def select(self, request: SelectRequest) -> SelectResponse:
        await self._effect("select", request)
        return SelectResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=request.dispatch_id,
            outcome=cast(Any, self.select_outcome),
            input_changed=False,
        )

    async def invoke(self, request: InvokeRequest) -> InvokeResponse:
        await self._effect("invoke", request)
        return InvokeResponse(
            worker_generation=request.expected_worker_generation,
            dispatch_id=request.dispatch_id,
            outcome=cast(Any, self.invoke_outcome),
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


@pytest.fixture
def planning(engine: AsyncEngine, desktop: FakeEffectDesktop) -> DesktopPlanningService:
    return DesktopPlanningService(engine, desktop=cast(DesktopService, desktop), grant_ttl_seconds=600)


async def build_plan(
    planning: DesktopPlanningService,
    desktop: FakeEffectDesktop,
    *,
    result: dict[str, Any],
    values: list[tuple[str, str]] | None = None,
) -> uuid.UUID:
    """Drive `DesktopPlanningService` through create -> confirm -> claim -> record_result, exactly as
    Electron main would after ONE (here, hand-supplied) provider attempt. Returns the plan id."""
    created = await planning.create(
        objective="Fill in the field", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
        values=values or [],
    )
    assert created.card is not None
    granted = await planning.confirm(created.task_id, grant_id=created.card.grant_id, expected_revision=created.card.grant_revision)
    assert granted.phase == "approved"
    context = await planning.claim(created.task_id)
    recorded = await planning.record_result(created.task_id, plan_id=context.plan_id, result=result, failure=None)
    assert recorded.phase == "proposed", recorded.plan.error_code if recorded.plan else None
    return context.plan_id


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


# ---- S4: from a validated plan to a SEPARATE execution approval ------------------------------------------


async def test_propose_from_plan_opens_an_invoke_card_and_performs_no_effect(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    assert view.status is ActionStatus.WAITING_APPROVAL
    assert view.operation.value == "invoke_control"
    assert desktop.calls == []
    assert await scalar(engine, "SELECT count(*) FROM desktop_dispatches") == 0


async def test_propose_from_plan_opens_a_set_value_card_with_the_resolved_trusted_value(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(
        planning, desktop,
        result={"schema_version": 1, "action": "set_value", "control_ref": "u4", "value_ref": "v1"},
        values=[("search text", "hello world")],
    )
    view = await service.propose_from_plan(plan_id)
    assert view.operation.value == "set_control_value"
    proposal = view.proposal
    assert proposal.value == "hello world"  # type: ignore[union-attr]


async def test_the_raw_set_value_text_is_never_persisted_in_the_durable_action_row(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    """The final M9 cross-slice audit's disposition of the S4-deferred raw-value-ledger residual
    (`docs/reviews/milestone-9-s4.md` section 13 finding 3): the trusted text a `SetValue` will write
    lives durably in exactly one place, `task_grants.scope` (S4's own immutable plan-grant scope), never
    a second time in the ordinary `actions` row. A later, separate read (simulating a status poll long
    after the card was opened, and again after the effect has actually run) must still show the exact
    approved text -- proving it is re-resolved fresh each time, not merely absent by accident."""
    secret = "trusted text 9f2a should never sit in actions.proposal"
    plan_id = await build_plan(
        planning, desktop,
        result={"schema_version": 1, "action": "set_value", "control_ref": "u4", "value_ref": "v1"},
        values=[("field", secret)],
    )
    view = await service.propose_from_plan(plan_id)

    (persisted,) = await rows(engine, "SELECT proposal FROM actions WHERE id = :a", a=view.action_id)
    assert secret not in str(persisted.proposal)
    assert persisted.proposal.get("value_ref") == "v1"
    assert "value" not in persisted.proposal

    # A later, separate read must still resolve the real text -- not a cached in-memory copy from the
    # call above, a fresh reconstruction from the ledger row plus the immutable grant scope.
    reread = await service.get(view.action_id)
    assert reread.proposal.value == secret  # type: ignore[union-attr]

    done = await service.approve(view.action_id, expected_revision=reread.revision)
    assert done.status is ActionStatus.SUCCEEDED
    assert desktop.requests[0].value == secret

    # Post-effect, the grant is COMPLETED but its row and scope are never deleted: resolution still works.
    after = await service.get(view.action_id)
    assert after.proposal.value == secret  # type: ignore[union-attr]
    (dispatch,) = await rows(engine, "SELECT result FROM desktop_dispatches WHERE action_id = :a", a=view.action_id)
    assert secret not in str(dispatch.result)


async def test_propose_from_plan_opens_a_select_card(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(
        planning, desktop, result={"schema_version": 1, "action": "select", "container_ref": "u5", "option_ref": "u6"}
    )
    view = await service.propose_from_plan(plan_id)
    assert view.operation.value == "select_control"


async def test_approving_a_plan_derived_card_performs_exactly_one_effect(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop, engine: AsyncEngine
) -> None:
    plan_id = await build_plan(
        planning, desktop,
        result={"schema_version": 1, "action": "set_value", "control_ref": "u4", "value_ref": "v1"},
        values=[("greeting", "hi")],
    )
    view = await service.propose_from_plan(plan_id)
    done = await service.approve(view.action_id, expected_revision=view.revision)
    assert done.status is ActionStatus.SUCCEEDED
    assert desktop.calls == ["set_value"]
    request = desktop.requests[0]
    assert request.value == "hi"
    # The raw value never reaches the durable dispatch row: only the opaque ref does.
    (dispatch,) = await rows(engine, "SELECT operation, value_ref, result FROM desktop_dispatches")
    assert dispatch.value_ref == "v1"
    assert "hi" not in str(dispatch.result)


async def test_a_plan_proposing_an_unknown_control_is_refused_before_any_execution_card(
    planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_ungrounded_plan(planning, desktop, {"schema_version": 1, "action": "invoke", "control_ref": "u999"})
    assert plan_id is None


async def test_a_select_naming_a_list_rooted_container_is_refused_as_unreviewed(
    planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """M9 final cross-slice audit finding (Pass B, High): `select_control` had no equivalent of
    `invoke_control`'s `SAFE_INVOKE_LABELS` allowlist -- a `list`/`pane`-rooted single-select
    container is exactly the shape a command-palette-style "quick pick" widget uses, where
    `SelectionItem.Select` is commonly activation, not mere highlighting. Only `combo_box` and
    `radio_button` containers (ordinary, closed-set value pickers) are reviewed as safe; a `list`
    container is refused before any execution card exists, the same way an unreviewed Invoke
    label already was."""
    plan_id = await build_ungrounded_plan(
        planning, desktop, {"schema_version": 1, "action": "select", "container_ref": "u8", "option_ref": "u9"}
    )
    assert plan_id is None


async def test_a_select_naming_a_combo_box_container_is_still_accepted(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """A false-positive guard: the reviewed container roles are not refused."""
    plan_id = await build_plan(
        planning, desktop, result={"schema_version": 1, "action": "select", "container_ref": "u5", "option_ref": "u6"}
    )
    view = await service.propose_from_plan(plan_id)
    assert view.operation.value == "select_control"


async def test_a_plan_naming_an_unoffered_value_ref_is_refused(planning: DesktopPlanningService, desktop: FakeEffectDesktop) -> None:
    plan_id = await build_ungrounded_plan(
        planning, desktop, {"schema_version": 1, "action": "set_value", "control_ref": "u4", "value_ref": "v9"}
    )
    assert plan_id is None


async def test_propose_from_plan_is_idempotent_a_plan_funds_at_most_one_execution_card(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    first = await service.propose_from_plan(plan_id)
    with pytest.raises(DesktopActionError):
        await service.propose_from_plan(plan_id)
    # The action from the first call is still exactly there, untouched.
    again = await service.get(first.action_id)
    assert again.action_id == first.action_id and again.revision == first.revision


async def test_propose_from_plan_refuses_a_plan_that_never_succeeded(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    created = await planning.create(
        objective="Fill in the field", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1, values=[],
    )
    with pytest.raises(DesktopActionError):
        await service.propose_from_plan(uuid.uuid4())  # not even a real plan
    assert created.plan is None  # still STARTED-or-absent; never SUCCEEDED


# ---- S4: an unresolved mutation blocks every new desktop action --------------------------------------------


async def test_an_unresolved_mutation_blocks_a_new_focus_scroll_launch_or_plan_proposal(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("the answer never came back")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN

    with pytest.raises(DesktopActionError) as info:
        await propose_focus(service, desktop)
    assert info.value.code == "desktop_action_unresolved"

    with pytest.raises(DesktopActionError):
        await service.propose_launch(app_id="fake")

    # A new PLAN (a new observation, a new provider call) is refused too -- before Lumi even inspects
    # the window -- not only later when it would try to fund an execution card.
    with pytest.raises(DesktopPlanRefusal) as plan_info:
        await planning.create(
            objective="Fill in the field", recipient="openai", model="gpt-test",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1, values=[],
        )
    assert plan_info.value.code == "desktop_action_unresolved"


async def test_reconcile_succeeded_unblocks_every_other_desktop_action(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    resolved = await service.reconcile(stuck.action_id, expected_revision=stuck.revision, outcome="succeeded")
    assert resolved.status is ActionStatus.SUCCEEDED
    # Now an ordinary proposal works again.
    fresh = await propose_focus(service, desktop)
    assert fresh.status is ActionStatus.WAITING_APPROVAL


async def test_reconcile_still_unknown_leaves_the_block_in_place(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    still = await service.reconcile(stuck.action_id, expected_revision=stuck.revision, outcome="still_unknown")
    assert still.status is ActionStatus.OUTCOME_UNKNOWN
    with pytest.raises(DesktopActionError):
        await propose_focus(service, desktop)


async def test_reconcile_never_retries_the_effect(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    await service.reconcile(stuck.action_id, expected_revision=stuck.revision, outcome="failed")
    assert desktop.calls == ["invoke"], "reconciliation must never call the worker again"


async def test_reconcile_refuses_an_outcome_unknown_focus_it_is_not_the_escape_hatch_for(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    """Focus/scroll/launch never block on their own `OUTCOME_UNKNOWN` (S3, unchanged), so `reconcile`
    -- the escape hatch for S4 mutations specifically -- refuses to touch one even if a caller tries."""
    view = await propose_focus(service, desktop)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN
    with pytest.raises(DesktopActionError):
        await service.reconcile(stuck.action_id, expected_revision=stuck.revision, outcome="succeeded")


async def test_reconcile_refuses_an_action_that_is_not_outcome_unknown(
    service: DesktopActionService, desktop: FakeEffectDesktop
) -> None:
    view = await propose_focus(service, desktop)
    with pytest.raises(DesktopActionError):
        await service.reconcile(view.action_id, expected_revision=view.revision, outcome="succeeded")


# ---- S4: a verification read that itself fails is unknown, never a known non-effect ------------------------


async def test_a_set_value_whose_verifying_read_itself_fails_is_outcome_unknown_not_failed(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """Found by an independent adversarial review of M9 S4: the write itself may have gone through --
    only the RE-READ that verifies it failed. Reporting that as a known `not_set`/FAILED would let a
    write whose real effect is genuinely unknown skip the `OUTCOME_UNKNOWN` block entirely."""
    plan_id = await build_plan(
        planning, desktop,
        result={"schema_version": 1, "action": "set_value", "control_ref": "u4", "value_ref": "v1"},
        values=[("greeting", "hi")],
    )
    view = await service.propose_from_plan(plan_id)
    desktop.set_value_outcome = "uncertain"
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN
    with pytest.raises(DesktopActionError) as info:
        await propose_focus(service, desktop)
    assert info.value.code == "desktop_action_unresolved"


async def test_a_select_whose_verifying_read_itself_fails_is_outcome_unknown_not_failed(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    plan_id = await build_plan(
        planning, desktop, result={"schema_version": 1, "action": "select", "container_ref": "u5", "option_ref": "u6"}
    )
    view = await service.propose_from_plan(plan_id)
    desktop.select_outcome = "uncertain"
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN


# ---- S4: the unresolved-mutation lock also covers confirm/claim, not only plan creation ---------------------


async def test_an_unresolved_mutation_blocks_confirming_an_already_created_plan(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """Found by the same review: the lock was originally checked only at plan `create` and at
    `propose_from_plan`. A DIFFERENT action can become unresolved in the time between a plan being
    created and the person clicking to confirm it; confirming anyway would arm a grant `claim` is about
    to release a real snapshot through."""
    created = await planning.create(
        objective="Fill in the field", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1, values=[],
    )
    assert created.card is not None
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN

    with pytest.raises(DesktopPlanRefusal) as info:
        await planning.confirm(
            created.task_id, grant_id=created.card.grant_id, expected_revision=created.card.grant_revision
        )
    assert info.value.code == "desktop_action_unresolved"


async def test_an_unresolved_mutation_blocks_claiming_an_already_confirmed_plan(
    service: DesktopActionService, planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """`claim` is what actually releases the redacted snapshot to a provider; it is the last, and most
    important, place this must be checked."""
    created = await planning.create(
        objective="Fill in the field", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1, values=[],
    )
    assert created.card is not None
    granted = await planning.confirm(
        created.task_id, grant_id=created.card.grant_id, expected_revision=created.card.grant_revision
    )
    assert granted.phase == "approved"

    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN

    with pytest.raises(DesktopPlanRefusal) as info:
        await planning.claim(created.task_id)
    assert info.value.code == "desktop_action_unresolved"


# ---- S4: a value the HTTP schema allows but the domain rejects never surfaces raw ----------------------------


async def test_recovery_unsticks_an_action_a_dead_process_left_mid_reconciliation(
    service: DesktopActionService,
    planning: DesktopPlanningService,
    desktop: FakeEffectDesktop,
    action_service: ActionService,
    engine: AsyncEngine,
) -> None:
    """Found by an independent adversarial review of M9 S4: `begin_reconciliation` and
    `finish_reconciliation` are two separate committed transactions. A crash between them left an
    action stuck at `RECONCILING` -- a status the ordinary `reconcile` entrypoint refuses to touch
    (only `OUTCOME_UNKNOWN` is accepted) and startup recovery never looked for -- so it could never be
    unstuck by any route. This drives an action to `RECONCILING` and stops, exactly where a crash
    would leave it, then runs the new startup recovery step directly."""
    plan_id = await build_plan(planning, desktop, result={"schema_version": 1, "action": "invoke", "control_ref": "u7"})
    view = await service.propose_from_plan(plan_id)
    desktop.effect_error = RuntimeError("lost")
    stuck = await service.approve(view.action_id, expected_revision=view.revision)
    assert stuck.status is ActionStatus.OUTCOME_UNKNOWN

    # `service.reconcile` would call both steps; only the first is driven here, simulating the crash.
    await action_service.begin_reconciliation(stuck.action_id, expected_revision=stuck.revision)
    (mid_status,) = await rows(engine, "SELECT status FROM actions WHERE id = :id", id=stuck.action_id)
    assert mid_status.status == "RECONCILING"

    recovered = await RecoveryService(engine).recover_interrupted_reconciliations()
    assert stuck.action_id in recovered

    after = await service.get(stuck.action_id)
    assert after.status is ActionStatus.OUTCOME_UNKNOWN
    # The ordinary reconciliation route works again -- it was refused while stuck at RECONCILING.
    resolved = await service.reconcile(stuck.action_id, expected_revision=after.revision, outcome="succeeded")
    assert resolved.status is ActionStatus.SUCCEEDED


async def test_a_value_with_control_characters_is_refused_without_leaking_it_in_an_exception(
    planning: DesktopPlanningService, desktop: FakeEffectDesktop
) -> None:
    """Found by the same review: `PlanValueBody` only bounds length, so a value containing a control
    character (a newline, say) reaches `StoredValue`'s own stricter validator, whose raw
    `pydantic.ValidationError` embeds the offending text. That must never escape as an unhandled
    exception -- it would otherwise reach a generic error handler and the server's own logs."""
    with pytest.raises(DesktopPlanRefusal) as info:
        await planning.create(
            objective="Fill in the field", recipient="openai", model="gpt-test",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
            values=[("note", "RAW_CANARY_ORCHID\n")],
        )
    assert info.value.code == "value_invalid"
    assert "RAW_CANARY_ORCHID" not in str(info.value)


async def build_ungrounded_plan(
    planning: DesktopPlanningService, desktop: FakeEffectDesktop, result: dict[str, Any]
) -> uuid.UUID | None:
    """Drive the plan to `record_result` and assert it was refused (FAILED), never SUCCEEDED."""
    created = await planning.create(
        objective="Fill in the field", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
        values=[("search text", "hello")],
    )
    assert created.card is not None
    await planning.confirm(created.task_id, grant_id=created.card.grant_id, expected_revision=created.card.grant_revision)
    context = await planning.claim(created.task_id)
    recorded = await planning.record_result(created.task_id, plan_id=context.plan_id, result=result, failure=None)
    assert recorded.phase == "failed"
    return None
