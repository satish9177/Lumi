"""Milestone 12 S4: desktop + app + project-stop composition, against real PostgreSQL.

`desktop_observe`/`desktop_reason`/`desktop_safe_action`/`launch_registered_app` all compose over the
EXISTING, unchanged M9 S1-S3 services (`DesktopService`, `DesktopDisclosureService`, `DesktopActionService`);
`project_stop` composes over the existing M10 `ProjectService.stop()`. `OrchestrationService` itself adds no
new desktop or project authority -- these tests are about the orchestration-level plumbing: the trusted
`desktop_target_ref`/`app_ref`/`project_ref` resources, the compatibility matrix, freshness at the exact
points M9 already checks it, the private-data-leak lesson from `docs/reviews/milestone-12-s3.md` reapplied to
`desktop_reason`, and the `desktop_action` task-type discriminator that keeps a `set_control_value`/
`select_control`/`invoke_control` task from ever resolving as `desktop_safe_action`/`launch_registered_app`.

The only stand-in is the desktop worker (`FakeEffectDesktop`, reused from `test_desktop_actions_service.py`):
the same fake `test_desktop_disclosure_service.py`/`test_desktop_actions_service.py` already trust for S1-S3
behaviour, so freshness re-checks here exercise the REAL `DesktopDisclosureService.create`/`DesktopActionService
.propose_focus`/`propose_scroll` freshness logic, not a re-implementation of it.
"""

import sys
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.protocol import ScrollStep
from app.desktop.registry import AppRegistry
from app.domain.orchestration import OrchestrationRefusal
from app.domain.orchestration_resources import CAPABILITY_RESOURCE_REQUIREMENTS
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionError, DesktopActionService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.orchestration import OrchestrationService
from app.services.projects import ProjectService
from app.services.research_tasks import ResearchService
from app.services.tasks import TaskService
from tests.test_desktop_actions_service import APP, FakeEffectDesktop
from tests.test_orchestration_service import authenticated, project, research  # noqa: F401 -- reused fixtures

OBJECTIVE = "What does this window say?"

#: `FakeEffectDesktop`'s own node tree names only "Editor" (u1, the window root), "Results" (u2), "Save"
#: (u3), etc, with no `text` on any node -- the quote must match a NAME `verify_grounding` can actually
#: find on a non-root control (u1 is the observation's own window root and is not addressable evidence).
GROUNDED_ANSWER = {
    "schema_version": 1,
    "kind": "answer",
    "answer": "There is a Save control, a secret nobody else approved to see.",
    "evidence": [{"control_ref": "u3", "quote": "Save"}],
}


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: Any) -> FakeEffectDesktop:
    return FakeEffectDesktop(engine, runtime_generation.id)


@pytest.fixture
def desktop_disclosure(engine: AsyncEngine, desktop: FakeEffectDesktop) -> DesktopDisclosureService:
    return DesktopDisclosureService(engine, desktop=cast(DesktopService, desktop), grant_ttl_seconds=600)


@pytest.fixture
def desktop_action(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, desktop: FakeEffectDesktop
) -> DesktopActionService:
    return DesktopActionService(
        engine, actions=action_service, tasks=task_service, desktop=cast(DesktopService, desktop),
        registry=AppRegistry([APP]),
    )


@pytest.fixture
def service(
    engine: AsyncEngine, research: ResearchService, project: ProjectService, authenticated: AuthenticatedReadService,
    desktop: FakeEffectDesktop, desktop_disclosure: DesktopDisclosureService, desktop_action: DesktopActionService,
) -> OrchestrationService:
    return OrchestrationService(
        engine, research=research, project=project, authenticated=authenticated,
        desktop=cast(DesktopService, desktop), desktop_disclosure=desktop_disclosure, desktop_action=desktop_action,
    )


def _backing_text(desktop: FakeEffectDesktop, *, surface_epoch: int | None = None) -> str:
    epoch = desktop.surface_epoch if surface_epoch is None else surface_epoch
    return f"{desktop.worker_generation}|s1|{epoch}"


async def _target_ref(
    service: OrchestrationService, orchestration_id: uuid.UUID, expected_revision: int, desktop: FakeEffectDesktop,
    *, surface_epoch: int | None = None,
) -> Any:
    return await service.register_resource(
        orchestration_id, expected_revision=expected_revision,
        kind="desktop_target_ref", safe_label_text="approved desktop window: Editor",
        backing_text=_backing_text(desktop, surface_epoch=surface_epoch),
    )


async def _app_ref(service: OrchestrationService, orchestration_id: uuid.UUID, expected_revision: int) -> Any:
    return await service.register_resource(
        orchestration_id, expected_revision=expected_revision,
        kind="app_ref", safe_label_text="Fake App", backing_text=APP.app_id,
    )


async def _project_ref(service: OrchestrationService, orchestration_id: uuid.UUID, expected_revision: int, *, task_id: uuid.UUID) -> Any:
    return await service.register_resource(
        orchestration_id, expected_revision=expected_revision,
        kind="project_ref", safe_label_text="registered project run", backing_id=task_id,
    )


# ---- desktop_target_ref: trusted registration and freshness ---------------------------------------------


class TestDesktopTargetResource:
    async def test_registers_from_a_fresh_listing(self, service: OrchestrationService, desktop: FakeEffectDesktop) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        assert len(registered.resources) == 1
        resource = registered.resources[0]
        assert resource.kind == "desktop_target_ref"
        assert resource.privacy_class == "private"
        assert resource.backing_id is None
        assert resource.backing_text == _backing_text(desktop)
        # No native identity, coordinate or geometry ever reaches the planner-visible label.
        forbidden = ("hwnd", "pid", str(desktop.worker_generation).split("-")[0])
        assert not any(word in resource.safe_label.lower() for word in forbidden)

    async def test_refuses_a_recreated_window_at_registration_time(
        self, service: OrchestrationService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        desktop.surface_epoch = 2  # the window closed and reopened; the epoch moved on
        with pytest.raises(OrchestrationRefusal, match="desktop_target_unavailable"):
            await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop, surface_epoch=1)

    async def test_refuses_a_different_worker_generation(self, service: OrchestrationService, desktop: FakeEffectDesktop) -> None:
        view = await service.create(objective=OBJECTIVE)
        bogus = f"{uuid.uuid4()}|s1|1"
        with pytest.raises(OrchestrationRefusal, match="desktop_target_unavailable"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="desktop_target_ref", safe_label_text="approved desktop window", backing_text=bogus,
            )

    async def test_refuses_a_malformed_backing_text(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        for bad in ("not-shaped-at-all", "s1|1", "00000000-0000-4000-8000-000000000001|hwnd123|1"):
            with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
                await service.register_resource(
                    view.orchestration.id, expected_revision=view.orchestration.revision,
                    kind="desktop_target_ref", safe_label_text="approved desktop window", backing_text=bad,
                )


# ---- desktop_observe ----------------------------------------------------------------------------------


class TestDesktopObserve:
    async def test_requires_exactly_one_desktop_target_ref(self) -> None:
        assert CAPABILITY_RESOURCE_REQUIREMENTS["desktop_observe"] == ("desktop_target_ref",)

    async def test_mints_a_template_only_result_never_raw_observed_text(
        self, service: OrchestrationService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        # Electron main performs the real, read-only observation itself (exactly as `document_read`'s own
        # extraction is main's job), then reports ONLY a bounded fact -- never a node, a role or any text.
        observation = await desktop.observe(desktop.worker_generation, "s1", desktop.surface_epoch)
        summary = f"Desktop observation completed: {observation.node_count} element(s) observed."
        advanced = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="desktop_observe", resolved_summary=summary, resources=[ref],
        )
        assert advanced.steps[-1].status == "SUCCEEDED"
        assert len(advanced.resources) == 2
        result = next(item for item in advanced.resources if item.kind == "desktop_result_ref")
        assert result.privacy_class == "private"
        assert result.safe_label == "desktop_observe result (step 1)"

    async def test_refuses_a_resource_of_the_wrong_kind(self, service: OrchestrationService, desktop: FakeEffectDesktop) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _app_ref(service, view.orchestration.id, view.orchestration.revision)
        ref = registered.resources[0].ref
        with pytest.raises(OrchestrationRefusal, match="resource_kind_mismatch"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="desktop_observe", resolved_summary="Desktop observation completed.", resources=[ref],
            )

    async def test_refuses_with_no_resource_cited(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(OrchestrationRefusal, match="resources_not_supported"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="desktop_observe", resolved_summary="Desktop observation completed.",
            )


# ---- desktop_reason -------------------------------------------------------------------------------------


class TestDesktopReason:
    async def test_pauses_for_the_existing_disclosure_card_first(
        self, service: OrchestrationService, desktop_disclosure: DesktopDisclosureService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        read = await desktop_disclosure.create(
            objective=OBJECTIVE, recipient="scripted", model="scripted-1",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=desktop.surface_epoch,
        )
        paused = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="desktop_reason", task_id=read.task_id, resources=[ref],
        )
        assert paused.orchestration.status == "PAUSED"
        assert paused.orchestration.pause_reason == "approval_required"

    async def test_succeeds_with_a_template_only_summary_never_the_answer_text(
        self, service: OrchestrationService, desktop_disclosure: DesktopDisclosureService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        read = await desktop_disclosure.create(
            objective=OBJECTIVE, recipient="scripted", model="scripted-1",
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=desktop.surface_epoch,
        )
        assert read.card is not None
        confirmed = await desktop_disclosure.confirm(read.task_id, grant_id=read.card.grant_id, expected_revision=read.card.grant_revision)
        assert confirmed.card is not None
        context = await desktop_disclosure.claim(read.task_id)
        await desktop_disclosure.record_result(read.task_id, disclosure_id=context.disclosure_id, result=GROUNDED_ANSWER, failure=None)
        # The task is already answered before `advance()` links it, so the step resolves SUCCEEDED
        # immediately -- there is nothing further to `resume()`, exactly like `document_read`'s own
        # already-extracted case.
        advanced = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="desktop_reason", task_id=read.task_id, resources=[ref],
        )
        step = advanced.steps[-1]
        assert step.status == "SUCCEEDED"
        assert step.result_summary is not None
        assert "failing tests" not in step.result_summary
        assert "secret" not in step.result_summary
        assert step.result_summary == "Desktop reasoning finished: answer (1 quoted item of evidence)."
        result = next(item for item in advanced.resources if item.kind == "desktop_result_ref")
        assert "failing" not in result.safe_label and "secret" not in result.safe_label

    async def test_refuses_a_task_of_the_wrong_kind(
        self, service: OrchestrationService, task_service: TaskService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        other = await task_service.create_task({"type": "public_research", "text": OBJECTIVE, "objective": OBJECTIVE, "source": "text"})
        with pytest.raises(OrchestrationRefusal, match="task_kind_mismatch"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="desktop_reason", task_id=other.id, resources=[ref],
            )


# ---- desktop_safe_action --------------------------------------------------------------------------------


class TestDesktopSafeAction:
    async def test_focus_succeeds_with_a_closed_vocabulary_summary(
        self, service: OrchestrationService, desktop_action: DesktopActionService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        proposed = await desktop_action.propose_focus(
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=desktop.surface_epoch
        )
        paused = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="desktop_safe_action", task_id=proposed.task_id, resources=[ref],
        )
        assert paused.orchestration.pause_reason == "approval_required"
        await desktop_action.approve(proposed.action_id, expected_revision=proposed.revision)
        resumed = await service.resume(paused.orchestration.id, expected_revision=paused.orchestration.revision)
        step = resumed.steps[-1]
        assert step.status == "SUCCEEDED"
        assert step.result_summary == "Desktop action succeeded: focus_surface."
        assert "Editor" not in (step.result_summary or "")

    async def test_scroll_succeeds_and_may_repeat(
        self, service: OrchestrationService, desktop_action: DesktopActionService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        observation_id, targets = await desktop_action.scroll_targets(
            worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=desktop.surface_epoch
        )
        control_ref = targets[0][0]
        proposed = await desktop_action.propose_scroll(
            worker_generation=desktop.worker_generation, observation_id=observation_id, control_ref=control_ref,
            step=ScrollStep.SMALL_DOWN
        )
        paused = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="desktop_safe_action", task_id=proposed.task_id, resources=[ref],
        )
        await desktop_action.approve(proposed.action_id, expected_revision=proposed.revision)
        resumed = await service.resume(paused.orchestration.id, expected_revision=paused.orchestration.revision)
        step = resumed.steps[-1]
        assert step.status == "SUCCEEDED"
        assert step.result_summary == "Desktop action succeeded: scroll_control (small_down)."
        assert "desktop_safe_action" in resumed.available_capabilities  # repeatable, not superseded by the loop guard

    async def test_refuses_a_set_value_task_impersonating_a_safe_action(
        self, service: OrchestrationService, task_service: TaskService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        other = await task_service.create_task({"type": "desktop_action", "operation": "set_control_value"})
        with pytest.raises(OrchestrationRefusal, match="task_kind_mismatch"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="desktop_safe_action", task_id=other.id, resources=[ref],
            )

    async def test_refuses_a_recreated_window_at_propose_time(
        self, service: OrchestrationService, desktop_action: DesktopActionService, desktop: FakeEffectDesktop
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        stale_epoch = desktop.surface_epoch
        desktop.surface_epoch = stale_epoch + 1  # the window closed and reopened
        with pytest.raises(DesktopActionError, match="desktop_action_stale"):
            await desktop_action.propose_focus(worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=stale_epoch)
        assert registered.resources[0].kind == "desktop_target_ref"  # the ref itself is untouched either way


# ---- launch_registered_app -------------------------------------------------------------------------------


class TestLaunchRegisteredApp:
    async def test_requires_an_app_ref(self) -> None:
        assert CAPABILITY_RESOURCE_REQUIREMENTS["launch_registered_app"] == ("app_ref",)

    async def test_succeeds_and_names_no_path_or_argument(
        self, service: OrchestrationService, desktop_action: DesktopActionService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _app_ref(service, view.orchestration.id, view.orchestration.revision)
        ref = registered.resources[0].ref
        proposed = await desktop_action.propose_launch(app_id=APP.app_id)
        paused = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="launch_registered_app", task_id=proposed.task_id, resources=[ref],
        )
        await desktop_action.approve(proposed.action_id, expected_revision=proposed.revision)
        resumed = await service.resume(paused.orchestration.id, expected_revision=paused.orchestration.revision)
        step = resumed.steps[-1]
        assert step.status == "SUCCEEDED"
        assert step.result_summary == "Desktop action succeeded: launch_app."
        assert APP.executable not in (step.result_summary or "")

    async def test_refuses_a_focus_task_impersonating_a_launch(self, service: OrchestrationService, task_service: TaskService) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _app_ref(service, view.orchestration.id, view.orchestration.revision)
        ref = registered.resources[0].ref
        other = await task_service.create_task({"type": "desktop_action", "operation": "focus_surface"})
        with pytest.raises(OrchestrationRefusal, match="task_kind_mismatch"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="launch_registered_app", task_id=other.id, resources=[ref],
            )


class TestAppRefResource:
    async def test_registers_a_known_shape(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _app_ref(service, view.orchestration.id, view.orchestration.revision)
        resource = registered.resources[0]
        assert resource.kind == "app_ref" and resource.backing_text == APP.app_id and resource.privacy_class == "none"

    async def test_refuses_a_malformed_app_id(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="app_ref", safe_label_text="app", backing_text="C:\\evil\\path.exe",
            )


# ---- project_stop ----------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="M10 S3 project runs ship on Windows")
class TestProjectStopComposition:
    """`register_resource`'s `project_ref` branch re-confirms the run is genuinely live (Low finding from
    the adversarial review: a bare task id shape check was not enough), so these need a REAL synthetic
    project and a real long-running `node.exe` process -- the same rig `TestProjectStartCapability` uses."""

    @pytest.fixture
    async def live_run_task_id(self, project: ProjectService, tmp_path: Path) -> uuid.UUID:
        from tests.project_fixtures import make_project
        from tests.test_projects_service import _project, _recipe

        folder = tmp_path / "synthetic-project-stop"
        make_project(folder)
        project_id = await _project(project, folder)
        recipe_id = await _recipe(project, project_id, "hang", timeout=30)
        run = await project.create_run(recipe_id=recipe_id)
        assert run.grant is not None
        confirmed = await project.confirm(run.task_id, grant_id=run.grant.id, expected_revision=run.grant.revision)
        assert confirmed.phase == "approved"
        started = await project.start(run.task_id)
        assert started.phase in ("starting", "running")
        return run.task_id

    async def test_requires_a_project_ref(self) -> None:
        assert CAPABILITY_RESOURCE_REQUIREMENTS["project_stop"] == ("project_ref",)

    async def test_project_ref_registers_from_a_live_run_task_id(
        self, service: OrchestrationService, live_run_task_id: uuid.UUID
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _project_ref(service, view.orchestration.id, view.orchestration.revision, task_id=live_run_task_id)
        resource = registered.resources[0]
        assert resource.kind == "project_ref" and resource.backing_id == live_run_task_id and resource.privacy_class == "none"

    async def test_refuses_a_run_that_has_already_ended(
        self, service: OrchestrationService, project: ProjectService, live_run_task_id: uuid.UUID
    ) -> None:
        await project.stop(live_run_task_id)
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(OrchestrationRefusal, match="project_run_not_live"):
            await _project_ref(service, view.orchestration.id, view.orchestration.revision, task_id=live_run_task_id)

    async def test_mints_a_project_status_ref_from_a_caller_computed_summary(
        self, service: OrchestrationService, live_run_task_id: uuid.UUID
    ) -> None:
        # Main performs the real stop through `ProjectService.stop()`'s own existing entry point and reports
        # ONLY the resulting phase -- exactly like `project_status`, never a log line or an env value.
        view = await service.create(objective=OBJECTIVE)
        registered = await _project_ref(service, view.orchestration.id, view.orchestration.revision, task_id=live_run_task_id)
        ref = registered.resources[0].ref
        advanced = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="project_stop", resolved_summary="Project run stopped.", resources=[ref],
        )
        assert advanced.steps[-1].status == "SUCCEEDED"
        result = next(item for item in advanced.resources if item.kind == "project_status_ref")
        assert result.privacy_class == "none"

    async def test_refuses_a_resource_of_the_wrong_kind(self, service: OrchestrationService, desktop: FakeEffectDesktop) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, view.orchestration.id, view.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        with pytest.raises(OrchestrationRefusal, match="resource_kind_mismatch"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="project_stop", resolved_summary="Project run stopped.", resources=[ref],
            )


# ---- cross-orchestration replay -------------------------------------------------------------------------


class TestCrossOrchestrationReplay:
    async def test_a_desktop_target_ref_from_another_orchestration_cannot_be_cited(
        self, service: OrchestrationService, desktop: FakeEffectDesktop
    ) -> None:
        first = await service.create(objective=OBJECTIVE)
        registered = await _target_ref(service, first.orchestration.id, first.orchestration.revision, desktop)
        ref = registered.resources[0].ref
        second = await service.create(objective="a different task entirely")
        with pytest.raises(OrchestrationRefusal, match="resource_not_found"):
            await service.advance(
                second.orchestration.id, expected_revision=second.orchestration.revision,
                capability_id="desktop_observe", resolved_summary="Desktop observation completed.", resources=[ref],
            )
