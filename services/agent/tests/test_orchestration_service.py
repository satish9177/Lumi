"""Milestone 11 S2: the durable orchestration controller, against real PostgreSQL.

`OrchestrationService` holds no tool of its own: a task-backed step links a task `ResearchService`'s own
boundary already created, and its resolution is read only from `ResearchService.describe`, never guessed.
These tests are about the controller's own properties -- closed catalog validation, one-composed-capability
honesty, budgets, the repeat-capability loop guard, revision/expiry fencing, and that approval is never
skipped -- not about research's own step loop, which `test_research_authorization.py` already covers.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.orchestration import MAX_STEPS, OrchestrationRefusal
from app.domain.research import ResearchAnswer, ResearchBudgets
from app.services.actions import ActionService
from app.services.orchestration import OrchestrationService
from app.services.research_tasks import ResearchService
from app.services.tasks import TaskService
from tests.test_research_authorization import DISCLOSURE, OBJECTIVE, SEARCH_STEP, _envelope, build_service


@pytest.fixture
async def research(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> AsyncIterator[ResearchService]:
    yield build_service(engine, action_service, task_service, runtime_generation=runtime_generation.id)


@pytest.fixture
def service(engine: AsyncEngine, research: ResearchService) -> OrchestrationService:
    return OrchestrationService(engine, research=research)


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
        assert set(view.available_capabilities) == {"public_research", "project_status"}

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


class TestPlannerCallBudget:
    async def test_record_planner_call_increments_and_pauses_at_the_bound(self, service: OrchestrationService, engine: AsyncEngine) -> None:
        view = await service.create(objective="do something")
        await _sql(engine, "UPDATE orchestrations SET planner_calls = 20 WHERE id = :id", id=view.orchestration.id)
        latest = await service.describe(view.orchestration.id)
        counted = await service.record_planner_call(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert counted.orchestration.status == "PAUSED"
        assert counted.orchestration.pause_reason == "budget_exhausted"
