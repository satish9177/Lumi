"""Milestone 11 S2: the durable orchestration controller, against real PostgreSQL.

`OrchestrationService` holds no tool of its own: a task-backed step links a task `ResearchService`'s own
boundary already created, and its resolution is read only from `ResearchService.describe`, never guessed.
These tests are about the controller's own properties -- closed catalog validation, one-composed-capability
honesty, budgets, the repeat-capability loop guard, revision/expiry fencing, and that approval is never
skipped -- not about research's own step loop, which `test_research_authorization.py` already covers.
"""

import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.desktop.registry import AppRegistry
from app.domain.orchestration import MAX_STEPS, OrchestrationRefusal
from app.domain.orchestration_resources import CAPABILITY_RESOURCE_REQUIREMENTS
from app.domain.research import ResearchAnswer, ResearchBudgets
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.browser_profiles import BrowserProfileService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.orchestration import OrchestrationService
from app.services.documents import DocumentService
from app.services.projects import ProjectService
from app.services.research_tasks import ResearchService
from app.services.tasks import TaskService
from tests.document_fixtures import make_pdf, make_text
from tests.test_documents_service import _two_documents
from tests.test_research_authorization import DISCLOSURE, OBJECTIVE, SEARCH_STEP, _envelope, build_service


@pytest.fixture
async def research(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> AsyncIterator[ResearchService]:
    yield build_service(engine, action_service, task_service, runtime_generation=runtime_generation.id)


@pytest.fixture
def project(engine: AsyncEngine, action_service: ActionService, runtime_generation: Any, tmp_path: Path) -> ProjectService:
    return ProjectService(
        engine, actions=action_service, runtime_generation=runtime_generation.id, grant_ttl_seconds=600,
        protected_folders=((), ()), run_root=str(tmp_path / "runs"), poll_seconds=0.1,
    )


@pytest.fixture
def authenticated(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any, tmp_path: Path
) -> AuthenticatedReadService:
    """Milestone 12 S3: a real `AuthenticatedReadService`, unconfigured for any real browser step -- these
    tests never call `execute_step`/`prepare` against it (see `test_orchestration_account_read.py` for that);
    `OrchestrationService` only needs it for `account_read`'s task-backed reads and `register_resource`'s
    profile check."""
    profiles = BrowserProfileService(
        engine, runtime_generation=runtime_generation.id, browser=object(),  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "browser-profiles")}),
    )
    return AuthenticatedReadService(
        engine, tasks=task_service, actions=action_service, runtime_generation=runtime_generation.id,
        worker=object(),  # type: ignore[arg-type]
        profiles=profiles, grant_ttl_seconds=600, step_ttl_seconds=120,
    )


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: Any) -> DesktopService:
    """Milestone 12 S4: a real `DesktopService` with no worker configured -- `OrchestrationService` only
    needs it for `desktop_target_ref` registration's freshness check, which these tests never exercise
    (see `test_orchestration_desktop.py` for that)."""
    return DesktopService(engine, runtime_generation=runtime_generation.id, worker=None, timeout_seconds=5.0)


@pytest.fixture
def desktop_disclosure(engine: AsyncEngine, desktop: DesktopService) -> DesktopDisclosureService:
    return DesktopDisclosureService(engine, desktop=desktop, grant_ttl_seconds=600)


@pytest.fixture
def desktop_action(engine: AsyncEngine, action_service: ActionService, task_service: TaskService, desktop: DesktopService) -> DesktopActionService:
    return DesktopActionService(engine, actions=action_service, tasks=task_service, desktop=desktop, registry=AppRegistry())


@pytest.fixture
def service(
    engine: AsyncEngine, research: ResearchService, project: ProjectService, authenticated: AuthenticatedReadService,
    desktop: DesktopService, desktop_disclosure: DesktopDisclosureService, desktop_action: DesktopActionService,
) -> OrchestrationService:
    return OrchestrationService(
        engine, research=research, project=project, authenticated=authenticated,
        desktop=desktop, desktop_disclosure=desktop_disclosure, desktop_action=desktop_action,
    )


async def _sql(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.begin() as connection:
        return await connection.execute(text(statement), params)


async def _research_task(task_service: TaskService) -> uuid.UUID:
    task = await task_service.create_task({"type": "public_research", "text": OBJECTIVE, "objective": OBJECTIVE, "source": "text"})
    return task.id


class TestCreateAndRead:
    async def test_create_starts_running_with_only_the_composed_capabilities_available(self, service: OrchestrationService) -> None:
        view = await service.create(objective="Research the Lumi repository and summarize it")
        assert view.orchestration.status == "RUNNING"
        assert view.orchestration.revision == 1
        assert view.live is True
        assert view.steps == ()
        assert set(view.available_capabilities) == {
            "public_research", "project_status", "project_start", "document_read", "document_compare",
            "account_read", "desktop_observe", "desktop_reason", "desktop_safe_action", "launch_registered_app",
            "project_stop",
        }

    async def test_create_refuses_an_invalid_objective(self, service: OrchestrationService) -> None:
        with pytest.raises(OrchestrationRefusal, match="objective_invalid"):
            await service.create(objective="")

    async def test_describe_unknown_orchestration_is_refused(self, service: OrchestrationService) -> None:
        with pytest.raises(OrchestrationRefusal, match="orchestration_not_found"):
            await service.describe(uuid.uuid4())

    async def test_latest_is_none_when_nothing_exists_yet(self, service: OrchestrationService, engine: AsyncEngine) -> None:
        await _sql(engine, "DELETE FROM orchestration_steps")
        await _sql(engine, "DELETE FROM orchestrations")
        assert await service.latest() is None
        created = await service.create(objective="Research the Lumi repository")
        latest = await service.latest()
        assert latest is not None and latest.orchestration.id == created.orchestration.id


class TestAdvanceClosedCatalog:
    async def test_an_unknown_capability_id_is_refused_outright(self, service: OrchestrationService) -> None:
        view = await service.create(objective="do something")
        with pytest.raises(OrchestrationRefusal, match="capability_unknown"):
            await service.advance(view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="run_shell")

    async def test_a_real_but_uncomposed_catalog_id_pauses_rather_than_executing_or_crashing(self, service: OrchestrationService) -> None:
        view = await service.create(objective="prepare a form")
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="form_prepare"
        )
        assert advanced.orchestration.status == "PAUSED"
        assert advanced.orchestration.pause_reason == "capability_unavailable"
        assert advanced.steps == ()  # nothing was recorded as if it had run

    async def test_a_stale_expected_revision_is_refused(self, service: OrchestrationService) -> None:
        view = await service.create(objective="do something")
        with pytest.raises(OrchestrationRefusal, match="revision_conflict"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision + 1,
                capability_id="project_status", resolved_summary="Project run phase: running, ready.",
            )


class TestSynchronousCapability:
    async def test_project_status_succeeds_immediately_with_no_task_and_no_approval(self, service: OrchestrationService) -> None:
        view = await service.create(objective="check my project")
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert advanced.orchestration.status == "RUNNING"  # no pause: a synchronous read needs no approval
        assert len(advanced.steps) == 1
        step = advanced.steps[0]
        assert step.status == "SUCCEEDED"
        assert step.child_task_id is None
        assert step.result_handle == "project_status:1"
        assert step.result_summary == "Project run phase: running, ready."

    async def test_project_status_refuses_a_task_id(self, service: OrchestrationService) -> None:
        view = await service.create(objective="check my project")
        with pytest.raises(OrchestrationRefusal, match="task_id_not_allowed"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="project_status", task_id=uuid.uuid4(),
            )

    async def test_project_status_requires_a_summary(self, service: OrchestrationService) -> None:
        view = await service.create(objective="check my project")
        with pytest.raises(OrchestrationRefusal, match="resolved_summary_required"):
            await service.advance(view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="project_status")


class TestTaskBackedCapability:
    async def test_a_freshly_created_unprepared_task_pauses_for_approval(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        assert advanced.orchestration.status == "PAUSED"
        assert advanced.orchestration.pause_reason == "approval_required"
        step = advanced.steps[0]
        assert step.status == "AWAITING_APPROVAL"
        assert step.child_task_id == task_id
        assert step.result_handle is None

    async def test_public_research_refuses_a_resolved_summary(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resolved_summary_not_allowed"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="public_research", task_id=task_id, resolved_summary="not allowed here",
            )

    async def test_a_task_of_the_wrong_kind_is_refused(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        wrong = await task_service.create_task({"type": "clinic_info"})
        with pytest.raises(OrchestrationRefusal, match="task_kind_mismatch"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="public_research", task_id=wrong.id,
            )

    async def test_an_unknown_task_id_is_refused(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(OrchestrationRefusal, match="task_not_found"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="public_research", task_id=uuid.uuid4(),
            )

    async def test_a_task_already_linked_to_another_step_cannot_be_linked_twice(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        first = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        # A second orchestration cannot claim the same child task.
        other = await service.create(objective="a different objective entirely")
        with pytest.raises(OrchestrationRefusal, match="child_task_already_linked"):
            await service.advance(
                other.orchestration.id, expected_revision=other.orchestration.revision, capability_id="public_research", task_id=task_id
            )
        assert first.steps[0].child_task_id == task_id

    async def test_resume_settles_the_step_once_the_task_is_answered_and_never_before(
        self, service: OrchestrationService, research: ResearchService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        assert paused.orchestration.status == "PAUSED"

        # Nothing changed yet: resume is idempotent, not a guess.
        still_paused = await service.resume(view.orchestration.id, expected_revision=paused.orchestration.revision)
        assert still_paused.orchestration.status == "PAUSED"
        assert still_paused.orchestration.revision == paused.orchestration.revision
        assert still_paused.steps[0].status == "AWAITING_APPROVAL"

        # The research task progresses through its OWN existing boundary, outside the orchestrator: the
        # scope card is opened, then the trusted click confirms it.
        prepared = await research.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
        assert prepared.grant is not None
        await research.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)
        result = await research.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-resume-0001"))
        assert result.outcome == "SUCCEEDED"
        # "not_found" needs no grounding evidence, exactly like `test_research_authorization.py`'s own
        # honest-not-found case; the property under test is that the orchestrator reads whatever research
        # actually recorded, not that this particular search finds a citable page.
        await research.record_answer(
            task_id,
            answer=ResearchAnswer(status="not_found", stop_reason="no_evidence", answer="Lumi could not verify that from the public pages it read."),
            provider="scripted", model="scripted-1", planner_calls=1,
        )

        resumed = await service.resume(view.orchestration.id, expected_revision=still_paused.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        assert resumed.orchestration.pause_reason is None
        step = resumed.steps[0]
        assert step.status == "SUCCEEDED"
        assert step.result_handle == "research_result:1"
        assert step.result_summary is not None and "could not verify" in step.result_summary

    async def test_resume_refuses_once_the_orchestration_has_expired_rather_than_reviving_it(
        self, service: OrchestrationService, task_service: TaskService, engine: AsyncEngine
    ) -> None:
        """Regression: `resume()` must never revive a PAUSED-but-expired orchestration to RUNNING. Before
        this fix it skipped the liveness check every other write path (`_live_running`) enforces, so an
        orchestration a user came back to after its 30-minute TTL passed could be settled straight to
        RUNNING -- only to immediately refuse `orchestration_expired` on the very next call, a stuck,
        undocumented state."""
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        assert paused.orchestration.status == "PAUSED"
        await _sql(
            engine,
            "UPDATE orchestrations SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 minute' WHERE id = :id",
            id=view.orchestration.id,
        )
        with pytest.raises(OrchestrationRefusal, match="orchestration_expired"):
            await service.resume(view.orchestration.id, expected_revision=paused.orchestration.revision)

    async def test_a_declined_scope_settles_the_step_as_failed_not_stuck_forever(
        self, service: OrchestrationService, research: ResearchService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        # Declined without ever being confirmed -- the card was shown and the person pressed Decline.
        prepared = await research.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
        assert prepared.grant is not None
        await research.revoke(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision, reason="user_declined")

        latest = await service.describe(view.orchestration.id)
        resumed = await service.resume(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        assert resumed.steps[0].status == "FAILED"
        assert resumed.steps[0].result_handle is None


class TestLoopAndBudgetGuards:
    async def test_re_choosing_an_already_succeeded_capability_pauses_as_a_loop_not_a_second_step(
        self, service: OrchestrationService
    ) -> None:
        view = await service.create(objective="check my project twice")
        first = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert first.orchestration.status == "RUNNING"
        looped = await service.advance(
            first.orchestration.id, expected_revision=first.orchestration.revision,
            capability_id="project_status", resolved_summary="asking again for no reason",
        )
        assert looped.orchestration.status == "PAUSED"
        assert looped.orchestration.pause_reason == "loop_detected"
        assert len(looped.steps) == 1  # no second step was ever recorded

    async def test_the_step_budget_pauses_rather_than_silently_widening(
        self, service: OrchestrationService, engine: AsyncEngine
    ) -> None:
        view = await service.create(objective="a very long task")
        # Seed MAX_STEPS worth of prior (uncomposed-capability) step rows directly, so the loop guard
        # (which only looks at the CHOSEN capability's own history) does not fire before the budget does.
        async with engine.begin() as connection:
            for sequence in range(1, MAX_STEPS + 1):
                await connection.execute(
                    text(
                        "INSERT INTO orchestration_steps (id, orchestration_id, sequence, capability_id, status, "
                        "result_handle, result_summary) VALUES (gen_random_uuid(), :o, :seq, 'form_prepare', "
                        "'SUCCEEDED', :handle, 'seeded')"
                    ),
                    {"o": view.orchestration.id, "seq": sequence, "handle": f"form_result:{sequence}"},
                )
            await connection.execute(
                text("UPDATE orchestrations SET step_count = :n WHERE id = :o"),
                {"n": MAX_STEPS, "o": view.orchestration.id},
            )
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="one more, over budget",
        )
        assert advanced.orchestration.status == "PAUSED"
        assert advanced.orchestration.pause_reason == "budget_exhausted"
        assert len(advanced.steps) == MAX_STEPS  # the over-budget step was never inserted


class TestFinishStopExpiry:
    async def test_finish_refuses_when_nothing_has_succeeded_yet(self, service: OrchestrationService) -> None:
        view = await service.create(objective="do something")
        with pytest.raises(OrchestrationRefusal, match="nothing_to_finish"):
            await service.finish(view.orchestration.id, expected_revision=view.orchestration.revision)

    async def test_finish_after_a_succeeded_step_is_terminal(self, service: OrchestrationService) -> None:
        view = await service.create(objective="check my project")
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        finished = await service.finish(advanced.orchestration.id, expected_revision=advanced.orchestration.revision)
        assert finished.orchestration.status == "SUCCEEDED"
        assert finished.available_capabilities == ()

    async def test_stop_ends_scheduling_and_further_steps_are_refused(self, service: OrchestrationService) -> None:
        view = await service.create(objective="do something")
        stopped = await service.stop(view.orchestration.id, expected_revision=view.orchestration.revision)
        assert stopped.orchestration.status == "STOPPED"
        with pytest.raises(OrchestrationRefusal, match="orchestration_not_active"):
            await service.advance(
                stopped.orchestration.id, expected_revision=stopped.orchestration.revision, capability_id="project_status", resolved_summary="x"
            )

    async def test_stop_clears_the_pause_reason_so_a_paused_orchestration_can_still_be_stopped(
        self, service: OrchestrationService
    ) -> None:
        """Regression: `stop()` on a PAUSED orchestration must clear `pause_reason` along with the status
        change, or the write violates `ck_orchestrations_pause_reason_set` (pause_reason must be NULL
        whenever status != PAUSED) and the whole call fails -- leaving the orchestration stuck PAUSED
        forever, in exactly the state a user is most likely to want to stop from."""
        view = await service.create(objective="prepare a form")
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="form_prepare"
        )
        assert paused.orchestration.status == "PAUSED"
        assert paused.orchestration.pause_reason == "capability_unavailable"

        stopped = await service.stop(paused.orchestration.id, expected_revision=paused.orchestration.revision)
        assert stopped.orchestration.status == "STOPPED"
        assert stopped.orchestration.pause_reason is None

    async def test_an_expired_orchestration_refuses_a_new_step(self, service: OrchestrationService, engine: AsyncEngine) -> None:
        view = await service.create(objective="do something")
        # Both columns move into the past together: `expires_at > created_at` still holds (the database's
        # own invariant against ever inserting an already-expired row), while `expires_at` is before now.
        await _sql(
            engine,
            "UPDATE orchestrations SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 minute' WHERE id = :id",
            id=view.orchestration.id,
        )
        with pytest.raises(OrchestrationRefusal, match="orchestration_expired"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="project_status", resolved_summary="x"
            )


@pytest.mark.skipif(sys.platform != "win32", reason="M10 S3 project runs ship on Windows")
class TestProjectStartCapability:
    """Milestone 11 S3: project_start, against a real synthetic Node project and a real `node.exe`
    process -- the same rig `test_projects_service.py` uses. The property under test is the same one S2
    proved for research: the orchestrator only ever links a task ProjectService's own boundary already
    created, and the R3 "this executes code" warning is never skipped."""

    @pytest.fixture
    async def recipe_id(self, project: ProjectService, tmp_path: Path) -> uuid.UUID:
        from tests.project_fixtures import make_project
        from tests.test_projects_service import _project, _recipe

        folder = tmp_path / "synthetic-project"
        make_project(folder)
        project_id = await _project(project, folder)
        return await _recipe(project, project_id, "check", timeout=30)

    async def test_a_freshly_created_run_pauses_for_the_warning_card(
        self, service: OrchestrationService, project: ProjectService, recipe_id: uuid.UUID
    ) -> None:
        view = await service.create(objective="Start my registered project")
        run = await project.create_run(recipe_id=recipe_id)
        assert run.phase == "awaiting_approval"
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_start", task_id=run.task_id,
        )
        assert advanced.orchestration.status == "PAUSED"
        assert advanced.orchestration.pause_reason == "approval_required"
        assert advanced.steps[0].status == "AWAITING_APPROVAL"
        assert advanced.steps[0].result_handle is None

    async def test_resume_settles_succeeded_once_the_run_is_approved_and_started(
        self, service: OrchestrationService, project: ProjectService, recipe_id: uuid.UUID
    ) -> None:
        view = await service.create(objective="Start my registered project")
        run = await project.create_run(recipe_id=recipe_id)
        assert run.grant is not None
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_start", task_id=run.task_id,
        )
        assert paused.orchestration.status == "PAUSED"

        # The trusted click, through ProjectService's OWN boundary, exactly like a direct request.
        confirmed = await project.confirm(run.task_id, grant_id=run.grant.id, expected_revision=run.grant.revision)
        assert confirmed.phase == "approved"
        # start() is a mechanical continuation of that one approval -- idempotent, never a second approval.
        started = await project.start(run.task_id)
        assert started.phase in ("starting", "running", "succeeded")

        latest = await service.describe(view.orchestration.id)
        resumed = await service.resume(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        step = resumed.steps[0]
        assert step.status == "SUCCEEDED"
        assert step.result_handle == "project_run_result:1"
        assert step.result_summary is not None and "Project run started" in step.result_summary

    async def test_an_outcome_unknown_run_pauses_with_its_own_honest_reason_not_approval_required(
        self, service: OrchestrationService, project: ProjectService, recipe_id: uuid.UUID, engine: AsyncEngine
    ) -> None:
        """A crash between approval and a settled result leaves the run OUTCOME_UNKNOWN
        (`app/services/projects.py`'s own recovery semantics). The orchestrator must not relabel that as
        still-awaiting-approval -- a human already approved; what is missing now is proof of what happened,
        not permission."""
        view = await service.create(objective="Start my registered project")
        run = await project.create_run(recipe_id=recipe_id)
        assert run.grant is not None
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_start", task_id=run.task_id,
        )
        await project.confirm(run.task_id, grant_id=run.grant.id, expected_revision=run.grant.revision)
        started = await project.start(run.task_id)
        assert started.run is not None
        # Simulate the recovery outcome directly, the same way `test_projects_service.py` pins it: a crash
        # left this run's own fate unresolved.
        await _sql(engine, "UPDATE project_runs SET status = 'OUTCOME_UNKNOWN' WHERE id = :id", id=started.run.id)

        resumed = await service.resume(view.orchestration.id, expected_revision=paused.orchestration.revision)
        assert resumed.orchestration.status == "PAUSED"
        assert resumed.orchestration.pause_reason == "outcome_unknown"
        assert resumed.steps[0].status == "AWAITING_APPROVAL"  # still the original step row; not yet settled

    async def test_a_declined_warning_card_settles_as_failed(
        self, service: OrchestrationService, project: ProjectService, recipe_id: uuid.UUID
    ) -> None:
        view = await service.create(objective="Start my registered project")
        run = await project.create_run(recipe_id=recipe_id)
        assert run.grant is not None
        await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_start", task_id=run.task_id,
        )
        await project.revoke(run.task_id, grant_id=run.grant.id, expected_revision=run.grant.revision)

        latest = await service.describe(view.orchestration.id)
        resumed = await service.resume(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        assert resumed.steps[0].status == "FAILED"
        assert resumed.steps[0].result_handle is None

    async def test_a_task_of_the_wrong_kind_is_refused_for_project_start(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective="Start my registered project")
        wrong = await task_service.create_task({"type": "public_research", "objective": "x"})
        with pytest.raises(OrchestrationRefusal, match="task_kind_mismatch"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="project_start", task_id=wrong.id,
            )


class TestPlannerCallBudget:
    async def test_record_planner_call_increments_and_pauses_at_the_bound(self, service: OrchestrationService, engine: AsyncEngine) -> None:
        view = await service.create(objective="do something")
        await _sql(engine, "UPDATE orchestrations SET planner_calls = 20 WHERE id = :id", id=view.orchestration.id)
        latest = await service.describe(view.orchestration.id)
        counted = await service.record_planner_call(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert counted.orchestration.status == "PAUSED"
        assert counted.orchestration.pause_reason == "budget_exhausted"


class TestResourceRegistry:
    """Milestone 12 S1: the trusted resource-ref registry. `project_status` (trivial, synchronous, mints
    `project_status_ref`) plays the minting role throughout; `public_research` (task-backed, needs only a
    fresh task id -- no filesystem/recipe rig) plays the citing role, with its own
    `CAPABILITY_RESOURCE_REQUIREMENTS` entry monkeypatched per test to prove the general resolution/kind/
    freshness/consumption mechanics ahead of the real slice that gives any capability a non-empty one."""

    async def test_a_succeeded_synchronous_capability_mints_a_controller_authored_resource(
        self, service: OrchestrationService
    ) -> None:
        view = await service.create(objective="check my project")
        advanced = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert len(advanced.resources) == 1
        resource = advanced.resources[0]
        assert resource.ref == "r1"
        assert resource.kind == "project_status_ref"
        assert resource.privacy_class == "none"
        assert resource.single_use is False
        # Controller-authored template only -- never the resolved_summary's own content.
        assert "running" not in resource.safe_label
        assert resource.safe_label == "project_status result (step 1)"

    async def test_a_succeeded_task_backed_capability_mints_a_resource_via_resume(
        self, service: OrchestrationService, research: ResearchService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        paused = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision, capability_id="public_research", task_id=task_id
        )
        assert paused.resources == ()  # nothing minted yet -- the step has not resolved
        prepared = await research.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
        assert prepared.grant is not None
        await research.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)
        await research.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-resource-0001"))
        await research.record_answer(
            task_id,
            answer=ResearchAnswer(status="not_found", stop_reason="no_evidence", answer="Lumi could not verify that publicly."),
            provider="scripted", model="scripted-1", planner_calls=1,
        )
        resumed = await service.resume(view.orchestration.id, expected_revision=paused.orchestration.revision)
        assert len(resumed.resources) == 1
        resource = resumed.resources[0]
        assert resource.kind == "research_result_ref"
        assert resource.privacy_class == "public"
        # Controller-authored template only -- never the model's own grounded answer text.
        assert "verify" not in resource.safe_label
        assert resource.safe_label == "public_research result (step 1)"

    async def test_citing_any_resource_is_refused_for_a_capability_that_accepts_none_even_when_owned_and_fresh(
        self, service: OrchestrationService, task_service: TaskService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        minted = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert minted.resources[0].ref == "r1"
        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resources_not_supported"):
            await service.advance(
                minted.orchestration.id, expected_revision=minted.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r1"],
            )
        # Nothing was partially committed by the refused attempt.
        latest = await service.describe(view.orchestration.id)
        assert len(latest.steps) == 1
        assert len(latest.resources) == 1

    async def test_advance_refuses_a_malformed_resources_list_before_any_capability_check(
        self, service: OrchestrationService
    ) -> None:
        view = await service.create(objective="do something")
        for bad in (
            ["not-a-ref"],
            ["00000000-0000-4000-8000-000000000001"],  # a UUID is not an opaque ref
            ["r1", "r1"],  # duplicate
            [f"r{i}" for i in range(1, 6)],  # over the per-step bound
            "r1",  # not a list at all
        ):
            with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
                await service.advance(
                    view.orchestration.id, expected_revision=view.orchestration.revision,
                    capability_id="project_status", resolved_summary="x", resources=bad,
                )
        # None of the malformed attempts were committed.
        assert (await service.describe(view.orchestration.id)).steps == ()

    async def test_the_model_cannot_invent_a_resource_that_was_never_minted(
        self, service: OrchestrationService, task_service: TaskService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(CAPABILITY_RESOURCE_REQUIREMENTS, "public_research", ("project_status_ref",))
        view = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resource_not_found"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r99"],
            )

    async def test_a_resource_from_another_orchestration_cannot_be_cited(
        self, service: OrchestrationService, task_service: TaskService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = await service.create(objective="check my project")
        minted = await service.advance(
            owner.orchestration.id, expected_revision=owner.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert minted.resources[0].ref == "r1"

        monkeypatch.setitem(CAPABILITY_RESOURCE_REQUIREMENTS, "public_research", ("project_status_ref",))
        stranger = await service.create(objective=OBJECTIVE)
        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resource_not_found"):
            await service.advance(
                stranger.orchestration.id, expected_revision=stranger.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r1"],
            )

    async def test_a_resource_of_the_wrong_kind_is_refused_even_though_it_is_real_fresh_and_owned(
        self, service: OrchestrationService, task_service: TaskService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        minted = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert minted.resources[0].kind == "project_status_ref"

        monkeypatch.setitem(CAPABILITY_RESOURCE_REQUIREMENTS, "public_research", ("research_result_ref",))
        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resource_kind_mismatch"):
            await service.advance(
                minted.orchestration.id, expected_revision=minted.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r1"],
            )

    async def test_a_single_use_resource_that_a_capability_already_spent_cannot_be_cited_again(
        self, service: OrchestrationService, task_service: TaskService, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`public_research`'s own first citation pauses the orchestration for its approval card (a real
        task-backed step always does), so a genuine second `advance()` call in the same orchestration would
        refuse `orchestration_not_active` before ever reaching resource resolution -- an unrelated property,
        not the one under test. A single-use resource a capability already spent is set up directly here,
        the same way `test_an_expired_resource_is_refused_...` sets up an already-expired one, so the
        citation attempt itself exercises exactly the `resource_consumed` path."""
        monkeypatch.setitem(CAPABILITY_RESOURCE_REQUIREMENTS, "public_research", ("project_status_ref",))
        view = await service.create(objective=OBJECTIVE)
        minted = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        assert minted.resources[0].ref == "r1"
        await _sql(
            engine, "UPDATE orchestration_resources SET single_use = true, consumed_at = now() "
            "WHERE orchestration_id = :id AND ref = 'r1'",
            id=view.orchestration.id,
        )
        latest = await service.describe(view.orchestration.id)
        assert latest.resources == ()  # already excluded from the available list

        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resource_consumed"):
            await service.advance(
                minted.orchestration.id, expected_revision=minted.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r1"],
            )

    async def test_an_expired_resource_is_refused_even_though_it_is_real_and_owned(
        self, service: OrchestrationService, task_service: TaskService, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(CAPABILITY_RESOURCE_REQUIREMENTS, "public_research", ("project_status_ref",))
        view = await service.create(objective=OBJECTIVE)
        minted = await service.advance(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            capability_id="project_status", resolved_summary="Project run phase: running, ready.",
        )
        await _sql(
            engine, "UPDATE orchestration_resources SET expires_at = now() - interval '1 minute' WHERE orchestration_id = :id",
            id=view.orchestration.id,
        )
        latest = await service.describe(view.orchestration.id)
        assert latest.resources == ()  # already excluded from the available list

        task_id = await _research_task(task_service)
        with pytest.raises(OrchestrationRefusal, match="resource_expired"):
            await service.advance(
                minted.orchestration.id, expected_revision=minted.orchestration.revision,
                capability_id="public_research", task_id=task_id, resources=["r1"],
            )


@pytest.mark.skipif(sys.platform != "win32", reason="the M10 file broker ships on Windows")
class TestDocumentComposition:
    """Milestone 12 S2: `document_read`/`document_compare`, against a real PostgreSQL database and a real
    directory tree. Main already performs the real extraction/comparison through `DocumentService`'s own
    existing, no-new-approval methods (exactly as a direct request does) before ever calling `advance()` --
    these tests build the same fixtures `test_documents_service.py` uses to prove that."""

    @pytest.fixture
    def documents(self, engine: AsyncEngine) -> DocumentService:
        return DocumentService(engine, grant_ttl_seconds=600, protected_folders=((), ()))

    @pytest.fixture
    def folder(self, tmp_path: Path) -> Path:
        # Matches `test_documents_service.py`'s own fixture shape exactly: `_two_documents` (reused below)
        # hardcodes "resume.pdf" and "Jobs/job.txt".
        root = tmp_path / "Approved Docs"
        (root / "Jobs").mkdir(parents=True)
        (root / "resume.pdf").write_bytes(make_pdf())
        (root / "Jobs" / "job.txt").write_bytes(make_text())
        return root

    async def test_register_resource_mints_a_document_ref_and_sets_the_shared_task(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read my resume")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
        file_id = added.files[0].file_id

        view = await service.create(objective="read my resume")
        registered = await service.register_resource(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            kind="document_ref", safe_label_text="resume.pdf", backing_id=file_id, document_task_id=task.task_id,
        )
        assert registered.orchestration.document_task_id == task.task_id
        assert len(registered.resources) == 1
        resource = registered.resources[0]
        assert resource.kind == "document_ref"
        assert resource.safe_label == "resume.pdf"
        assert resource.backing_id == file_id
        assert resource.backing_text is None
        assert resource.privacy_class == "private"

    async def test_a_second_document_ref_must_use_the_same_shared_task(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task_a = await documents.create_task(objective="a")
        task_b = await documents.create_task(objective="b")
        added = await documents.add_root_file(task_a.task_id, root_id=root.root_id, relative_path="resume.pdf")

        view = await service.create(objective="x")
        registered = await service.register_resource(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            kind="document_ref", safe_label_text="resume.pdf", backing_id=added.files[0].file_id,
            document_task_id=task_a.task_id,
        )
        with pytest.raises(OrchestrationRefusal, match="document_task_mismatch"):
            await service.register_resource(
                view.orchestration.id, expected_revision=registered.orchestration.revision,
                kind="document_ref", safe_label_text="job.txt", backing_id=uuid.uuid4(),
                document_task_id=task_b.task_id,
            )

    async def test_register_resource_refuses_an_unregisterable_kind(self, service: OrchestrationService) -> None:
        view = await service.create(objective="x")
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="research_result_ref", safe_label_text="x",
            )

    async def test_register_resource_document_ref_requires_backing_id_and_task(self, service: OrchestrationService) -> None:
        view = await service.create(objective="x")
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="document_ref", safe_label_text="x",
            )

    async def test_register_resource_document_ref_refuses_backing_text(self, service: OrchestrationService) -> None:
        view = await service.create(objective="x")
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="document_ref", safe_label_text="x", backing_id=uuid.uuid4(), backing_text="oops",
                document_task_id=uuid.uuid4(),
            )

    async def test_register_resource_public_url_ref(self, service: OrchestrationService) -> None:
        view = await service.create(objective="inspect a page")
        registered = await service.register_resource(
            view.orchestration.id, expected_revision=view.orchestration.revision,
            kind="public_url_ref", safe_label_text="example.com", backing_text="https://example.com/page",
        )
        assert len(registered.resources) == 1
        resource = registered.resources[0]
        assert resource.kind == "public_url_ref"
        assert resource.backing_text == "https://example.com/page"
        assert resource.backing_id is None
        assert resource.privacy_class == "public"

    async def test_register_resource_public_url_ref_refuses_backing_id(self, service: OrchestrationService) -> None:
        view = await service.create(objective="x")
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="public_url_ref", safe_label_text="x", backing_id=uuid.uuid4(), backing_text="https://example.com",
            )

    async def _registered_document_ref(
        self, service: OrchestrationService, orchestration_id: uuid.UUID, expected_revision: int,
        *, task_id: uuid.UUID, file_id: uuid.UUID, label: str,
    ) -> Any:
        return await service.register_resource(
            orchestration_id, expected_revision=expected_revision,
            kind="document_ref", safe_label_text=label, backing_id=file_id, document_task_id=task_id,
        )

    async def test_document_read_mints_a_document_result_ref_with_a_template_only_label(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read my resume")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
        file_id = added.files[0].file_id

        view = await service.create(objective="read my resume")
        registered = await self._registered_document_ref(
            service, view.orchestration.id, view.orchestration.revision,
            task_id=task.task_id, file_id=file_id, label="resume.pdf",
        )
        extracted = await documents.extract(task.task_id, file_id=file_id)
        document_id = extracted.documents[0].document_id
        read = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="document_read", resources=["r1"], result_backing_id=document_id,
            resolved_summary=f"Extracted {extracted.documents[0].text_chars} characters from an approved document.",
        )
        assert read.orchestration.status == "RUNNING"  # no approval needed
        assert len(read.resources) == 2  # r1 (document_ref) is still available; r2 is the new result
        result = next(item for item in read.resources if item.ref == "r2")
        assert result.kind == "document_result_ref"
        assert result.backing_id == document_id
        # Controller-authored template only -- character COUNT is fine, but never any extracted text.
        assert result.safe_label == "document_read result (step 1)"
        assert "resume" not in result.safe_label.lower()

    async def test_document_read_refuses_a_document_ref_from_a_different_orchestration(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read my resume")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")

        owner = await service.create(objective="owner")
        await self._registered_document_ref(
            service, owner.orchestration.id, owner.orchestration.revision,
            task_id=task.task_id, file_id=added.files[0].file_id, label="resume.pdf",
        )
        stranger = await service.create(objective="stranger")
        with pytest.raises(OrchestrationRefusal, match="resource_not_found"):
            await service.advance(
                stranger.orchestration.id, expected_revision=stranger.orchestration.revision,
                capability_id="document_read", resources=["r1"], result_backing_id=uuid.uuid4(),
                resolved_summary="Extracted 10 characters.",
            )

    async def test_document_read_refuses_the_wrong_count_of_resources(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read my resume")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
        view = await service.create(objective="read my resume")
        registered = await self._registered_document_ref(
            service, view.orchestration.id, view.orchestration.revision,
            task_id=task.task_id, file_id=added.files[0].file_id, label="resume.pdf",
        )
        with pytest.raises(OrchestrationRefusal, match="resources_not_supported"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="document_read", resources=[], resolved_summary="Extracted 10 characters.",
            )

    async def test_document_read_requires_a_result_backing_id(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read my resume")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
        view = await service.create(objective="read my resume")
        registered = await self._registered_document_ref(
            service, view.orchestration.id, view.orchestration.revision,
            task_id=task.task_id, file_id=added.files[0].file_id, label="resume.pdf",
        )
        with pytest.raises(OrchestrationRefusal, match="result_backing_id_required"):
            await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="document_read", resources=["r1"], resolved_summary="Extracted 10 characters.",
            )

    @pytest.mark.parametrize("capability_id", ["project_status", "public_research", "project_start"])
    async def test_a_capability_with_no_backing_id_output_refuses_one(
        self, service: OrchestrationService, capability_id: str
    ) -> None:
        # The check is one blanket guard applied before any capability-specific branching, so it fires
        # identically for every capability whose own output spec does not declare `needs_backing_id` --
        # even one that also needs a task_id or resources it was never given, since this check runs first.
        view = await service.create(objective="check my project")
        with pytest.raises(OrchestrationRefusal, match="result_backing_id_not_allowed"):
            await service.advance(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                capability_id=capability_id, result_backing_id=uuid.uuid4(),
            )

    async def test_document_read_may_repeat_without_tripping_the_loop_guard(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        root = await documents.register_root(path=str(folder), label="Docs", can_read=True, can_create=False, can_modify=False)
        task = await documents.create_task(objective="read two files")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="resume.pdf")
        added = await documents.add_root_file(task.task_id, root_id=root.root_id, relative_path="Jobs/job.txt")
        view = await service.create(objective="read two files")
        registered = view
        for item in added.files:
            registered = await self._registered_document_ref(
                service, registered.orchestration.id, registered.orchestration.revision,
                task_id=task.task_id, file_id=item.file_id, label=item.display_name,
            )
        refs = [resource.ref for resource in registered.resources]
        assert len(refs) == 2
        for index, ref in enumerate(refs):
            extracted = await documents.extract(task.task_id, file_id=added.files[index].file_id)
            registered = await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="document_read", resources=[ref], result_backing_id=extracted.documents[-1].document_id,
                resolved_summary=f"Extracted {extracted.documents[-1].text_chars} characters from an approved document.",
            )
        assert registered.orchestration.status == "RUNNING"  # neither call paused as loop_detected
        assert sum(1 for step in registered.steps if step.capability_id == "document_read") == 2
        assert all(step.status == "SUCCEEDED" for step in registered.steps if step.capability_id == "document_read")

    async def test_document_compare_requires_exactly_two_document_result_refs(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        view_docs = await _two_documents(documents, folder)
        view = await service.create(objective="compare my documents")
        registered = view
        result_refs: list[str] = []
        for item in view_docs.documents:
            registered = await self._registered_document_ref(
                service, registered.orchestration.id, registered.orchestration.revision,
                task_id=view_docs.task_id, file_id=item.file_id, label="a document",
            )
            ref = registered.resources[-1].ref
            registered = await service.advance(
                registered.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="document_read", resources=[ref], result_backing_id=item.document_id,
                resolved_summary=f"Extracted {item.text_chars} characters from an approved document.",
            )
            result_refs.append(next(r.ref for r in registered.resources if r.backing_id == item.document_id))

        local = await documents.compare_local(view_docs.task_id, first_id=view_docs.documents[0].document_id, second_id=view_docs.documents[1].document_id)
        compared = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="document_compare", resources=result_refs,
            resolved_summary=f"Local comparison: {round(local.overlap * 100)}% term overlap between two approved documents.",
        )
        assert compared.orchestration.status == "RUNNING"
        step = next(s for s in compared.steps if s.capability_id == "document_compare")
        assert step.status == "SUCCEEDED"
        assert step.result_summary is not None and "%" in step.result_summary
        # Never the documents' own vocabulary.
        assert "resume" not in (step.result_summary or "").lower()

    async def test_document_compare_refuses_a_single_document_ref(
        self, service: OrchestrationService, documents: DocumentService, folder: Path
    ) -> None:
        view_docs = await _two_documents(documents, folder)
        view = await service.create(objective="compare my documents")
        registered = await self._registered_document_ref(
            service, view.orchestration.id, view.orchestration.revision,
            task_id=view_docs.task_id, file_id=view_docs.documents[0].file_id, label="a document",
        )
        ref = registered.resources[-1].ref
        read = await service.advance(
            registered.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="document_read", resources=[ref], result_backing_id=view_docs.documents[0].document_id,
            resolved_summary=f"Extracted {view_docs.documents[0].text_chars} characters from an approved document.",
        )
        result_ref = next(r.ref for r in read.resources if r.backing_id == view_docs.documents[0].document_id)
        with pytest.raises(OrchestrationRefusal, match="resources_not_supported"):
            await service.advance(
                read.orchestration.id, expected_revision=read.orchestration.revision,
                capability_id="document_compare", resources=[result_ref], resolved_summary="Local comparison: 0% overlap.",
            )
