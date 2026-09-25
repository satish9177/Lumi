"""Milestone 12 S3: `account_read` composed through the trusted resource registry, and
`manual_handoff_required` made real, against a real PostgreSQL ledger and the same scripted stand-in for
the browser worker `test_authenticated_read.py` uses.

What these tests are really about: **`account_read` reuses the existing, already-reviewed authenticated-read
authority unchanged** (the same scope card, the same grant, the same profile/fingerprint/epoch checks, the
same credential-surface detection) -- the orchestrator only reads that authority's own state and translates
it into `manual_handoff_required` honestly. Continue never assumes a login, a CAPTCHA or an account switch
happened just because the user pressed it: every scenario here re-observes through a real, fresh
`authenticated_read` step before the orchestration is allowed to treat anything as resolved.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.domain.action_status import AttemptOutcome
from app.domain.authenticated import AuthenticatedStepEnvelope, PauseReason, WorkerReadResult, parse_authenticated_step
from app.domain.browser_profile import allowed_origins_for
from app.domain.errors import AuthenticatedGrantNotUsableError, AuthenticatedProfileUnavailableError
from app.domain.orchestration import OrchestrationRefusal
from app.domain.orchestration_resources import CAPABILITY_RESOURCE_REQUIREMENTS
from app.domain.research import ResearchAnswer, ResearchEvidence
from app.repositories.browser import BrowserRepository
from app.repositories.profiles import BrowserProfileRepository
from app.services import authenticated_read as service_module
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.browser_profiles import BrowserProfileService
from app.services.orchestration import OrchestrationService
from app.services.projects import ProjectService
from app.services.research_tasks import ResearchService
from app.services.tasks import TaskService
from tests.test_authenticated_read import (
    FINGERPRINT,
    OTHER_FINGERPRINT,
    FakeProfileBrowser,
    FakeWorker,
    _outcome,
    credential_surface,
    page_observation,
)
from tests.test_orchestration_service import project, research  # noqa: F401 -- reused fixtures

OBJECTIVE = "Which of my repositories are private?"


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    return tmp_path / "browser-profiles"


@pytest.fixture
async def authenticated_rig(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService,
    runtime_generation: Any, profile_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[AuthenticatedReadService, uuid.UUID, FakeWorker]]:
    browser = FakeProfileBrowser()
    async with engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=browser.worker_generation, runtime_generation=runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
    profiles = BrowserProfileService(
        engine, runtime_generation=runtime_generation.id, browser=browser,  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}),
    )
    profile_id = uuid.uuid4()
    async with engine.begin() as connection:
        repository = BrowserProfileRepository(connection)
        await repository.create(
            profile_id=profile_id, label="GitHub - Personal", site="github.com",
            allowed_origins=allowed_origins_for("github.com"),
        )
        await repository.mark_authenticated(profile_id=profile_id, account_fingerprint=FINGERPRINT)
    worker = FakeWorker(generation=browser.worker_generation)
    service = AuthenticatedReadService(
        engine, tasks=task_service, actions=action_service, runtime_generation=runtime_generation.id,
        worker=object(),  # type: ignore[arg-type]  # Only its presence matters: the client is faked.
        profiles=profiles, grant_ttl_seconds=600, step_ttl_seconds=120,
    )

    async def open_worker_client(_worker: Any, _generation: uuid.UUID) -> FakeWorker:
        return worker

    async def dispatch_and_classify(_client: Any, *, request: Any) -> Any:
        worker.dispatched.append(request)
        if worker.queue:
            return worker.queue.pop(0)(request)
        observation = page_observation(request, worker.blocks)
        return _outcome(request, WorkerReadResult(observation=observation))

    monkeypatch.setattr(service_module, "open_worker_client", open_worker_client)
    monkeypatch.setattr(service_module, "dispatch_and_classify", dispatch_and_classify)
    yield service, profile_id, worker


@pytest.fixture
def authenticated_service(authenticated_rig: tuple[AuthenticatedReadService, uuid.UUID, FakeWorker]) -> AuthenticatedReadService:
    return authenticated_rig[0]


@pytest.fixture
def profile_id(authenticated_rig: tuple[AuthenticatedReadService, uuid.UUID, FakeWorker]) -> uuid.UUID:
    return authenticated_rig[1]


@pytest.fixture
def worker(authenticated_rig: tuple[AuthenticatedReadService, uuid.UUID, FakeWorker]) -> FakeWorker:
    return authenticated_rig[2]


@pytest.fixture
def service(
    engine: AsyncEngine, research: ResearchService, project: ProjectService, authenticated_service: AuthenticatedReadService,
) -> OrchestrationService:
    return OrchestrationService(engine, research=research, project=project, authenticated=authenticated_service)


async def _account_task(task_service: TaskService, *, profile_id: uuid.UUID, objective: str = OBJECTIVE) -> uuid.UUID:
    task = await task_service.create_task({
        "type": "authenticated_read", "classification": "account_private", "text": objective,
        "objective": objective, "source": "text", "profile_id": str(profile_id),
    })
    return task.id


def _envelope(step: dict[str, Any], *, request_id: str, planner_calls: int = 1) -> AuthenticatedStepEnvelope:
    return AuthenticatedStepEnvelope(request_id=request_id, step=parse_authenticated_step(step), planner_calls=planner_calls)


async def _registered_account_ref(
    service: OrchestrationService, orchestration_id: uuid.UUID, expected_revision: int, *, profile_id: uuid.UUID,
) -> Any:
    return await service.register_resource(
        orchestration_id, expected_revision=expected_revision,
        kind="account_context_ref", safe_label_text="approved signed-in account context for github.com",
        backing_id=profile_id,
    )


async def _sql(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.begin() as connection:
        return await connection.execute(text(statement), params)


class TestAccountReadComposition:
    """Acceptance A: trusted account context -> orchestrator chooses account_read -> existing account-read
    approval/grant -> read-only authenticated navigation -> grounded result. No mutation anywhere in this
    path: every operation the linked task can choose is closed to GET/HEAD (`AuthOperation`, unchanged)."""

    async def test_account_read_is_available_and_requires_exactly_one_account_context_ref(
        self, service: OrchestrationService
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        assert "account_read" in view.available_capabilities
        assert CAPABILITY_RESOURCE_REQUIREMENTS["account_read"] == ("account_context_ref",)

    async def test_register_resource_mints_an_account_context_ref_for_a_signed_in_profile(
        self, service: OrchestrationService, profile_id: uuid.UUID
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        assert len(registered.resources) == 1
        resource = registered.resources[0]
        assert resource.kind == "account_context_ref"
        assert resource.backing_id == profile_id
        assert resource.backing_text is None
        assert resource.privacy_class == "private"
        # The planner-visible label names no cookie, token, path, storage or password -- see the M12 S3 plan.
        forbidden = ("cookie", "token", "password", "session", "storage", "@")
        assert not any(word in resource.safe_label.lower() for word in forbidden)

    async def test_register_resource_refuses_a_profile_that_is_not_signed_in(
        self, service: OrchestrationService, engine: AsyncEngine, profile_id: uuid.UUID
    ) -> None:
        async with engine.begin() as connection:
            await BrowserProfileRepository(connection).mark_needs_login(profile_id=profile_id)
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
            await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        assert refused.value.code == "profile_not_authenticated"

    async def test_register_resource_refuses_an_unknown_profile(self, service: OrchestrationService) -> None:
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
            await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=uuid.uuid4())
        assert refused.value.code == "profile_not_found"

    async def test_register_resource_account_context_ref_refuses_backing_text(
        self, service: OrchestrationService, profile_id: uuid.UUID
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        with pytest.raises(OrchestrationRefusal, match="resources_invalid"):
            await service.register_resource(
                view.orchestration.id, expected_revision=view.orchestration.revision,
                kind="account_context_ref", safe_label_text="x", backing_id=profile_id, backing_text="oops",
            )

    async def test_selecting_account_read_is_not_approval_it_still_pauses_for_the_scope_card(
        self, service: OrchestrationService, task_service: TaskService, profile_id: uuid.UUID
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        task_id = await _account_task(task_service, profile_id=profile_id)
        advanced = await service.advance(
            view.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
        )
        assert advanced.orchestration.status == "PAUSED"
        assert advanced.orchestration.pause_reason == "approval_required"  # not manual_handoff_required
        assert advanced.steps[0].status == "AWAITING_APPROVAL"

    async def test_a_full_read_succeeds_and_mints_a_template_only_account_result_ref(
        self, service: OrchestrationService, task_service: TaskService,
        authenticated_service: AuthenticatedReadService, profile_id: uuid.UUID,
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        task_id = await _account_task(task_service, profile_id=profile_id)
        paused = await service.advance(
            view.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
        )

        prepared = await authenticated_service.prepare(task_id, recipient="gemini")
        assert prepared.grant is not None
        await authenticated_service.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)
        result = await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-a-0001"))
        assert result.outcome is AttemptOutcome.SUCCEEDED
        assert result.observation is not None
        private_block = next(block for block in result.observation.observation.blocks if block.text.endswith("- Private"))
        await authenticated_service.record_answer(
            task_id,
            answer=ResearchAnswer(
                status="answered", stop_reason="goal_reached", answer="You own private repositories.",
                evidence=[ResearchEvidence(observation="o1", block=private_block.id, quote=private_block.text)],
            ),
            provider="gemini", model="gemini-1", planner_calls=1,
        )

        latest = await service.describe(view.orchestration.id)
        resumed = await service.resume(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        assert resumed.orchestration.pause_reason is None
        step = resumed.steps[0]
        assert step.status == "SUCCEEDED"
        assert step.result_handle == "account_result:1"
        assert step.result_summary is not None and "answered" in step.result_summary
        # Template-only, exactly like document_read's character-count summary: the answer's own closed-
        # vocabulary status and a quoted-evidence count, NEVER the answer text itself -- this is what
        # `orchestrationResultLines()` feeds to the orchestration-planning model on every later planner
        # tick, an entirely separate, independently-configured recipient from the one the user's scope-card
        # click actually approved.
        assert "1 quoted item" in step.result_summary
        for leaked in ("private repositories", "lumi-notes", "secret-plans", private_block.text):
            assert leaked not in step.result_summary
        assert len(resumed.resources) == 2  # the cited account_context_ref, plus the minted account_result_ref
        minted = next(r for r in resumed.resources if r.kind == "account_result_ref")
        assert minted.privacy_class == "private"
        # Template-only label: never the answer text, a repository name or any account content.
        assert "private" not in minted.safe_label.lower() and "repositor" not in minted.safe_label.lower()


class TestManualHandoffLogin:
    """Acceptance B (and D, CAPTCHA): account_read -> login required -> manual_handoff_required -> user logs
    in -> Continue -> Lumi re-observes -> account verified -> continue. There is no separate CAPTCHA signal
    (see `app/domain/orchestration.py`): a CAPTCHA-guarded sign-in page already shows a credential surface,
    which the existing, over-inclusive `login_required` detection already catches -- proven here by using the
    exact same scripted credential-surface outcome for both."""

    async def _paused_for_login(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker,
    ) -> tuple[uuid.UUID, uuid.UUID, int]:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        task_id = await _account_task(task_service, profile_id=profile_id)
        await service.advance(
            view.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
        )
        prepared = await authenticated_service.prepare(task_id, recipient="gemini")
        assert prepared.grant is not None
        await authenticated_service.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)
        worker.queue.append(credential_surface)
        result = await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-b-0001"))
        assert result.pause_reason is PauseReason.LOGIN_REQUIRED
        latest = await service.describe(view.orchestration.id)
        return view.orchestration.id, task_id, latest.orchestration.revision

    async def test_login_required_becomes_a_real_manual_handoff_pause_with_a_safe_instruction(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker,
    ) -> None:
        orchestration_id, _task_id, revision = await self._paused_for_login(
            service, task_service, authenticated_service, profile_id, worker
        )
        resumed = await service.resume(orchestration_id, expected_revision=revision)
        assert resumed.orchestration.status == "PAUSED"
        assert resumed.orchestration.pause_reason == "manual_handoff_required"
        step = resumed.steps[0]
        # The step's OWN status stays AWAITING_APPROVAL the whole time it is unresolved -- exactly like
        # public_research's equivalent step never becomes "PENDING" mid-flight either; only the orchestration
        # is re-labelled by each `resume()`, and only a final SUCCEEDED/FAILED changes the step's own status.
        assert step.status == "AWAITING_APPROVAL"
        assert step.result_summary is None  # never a result: the step has not resolved
        assert step.pending_note is not None
        summary = step.pending_note
        assert "Manual action required" in summary
        assert "sign in" in summary.lower()
        # D: the same instruction explicitly covers a CAPTCHA challenge -- Lumi never solves or automates one.
        assert "CAPTCHA" in summary
        assert "Continue" in summary
        # No account content of any kind leaks into the instruction.
        for private in ("lumi-notes", "secret-plans", "5 repositories"):
            assert private not in summary

    async def test_continue_before_the_user_actually_signs_in_stays_paused_never_assumes_success(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker,
    ) -> None:
        orchestration_id, task_id, revision = await self._paused_for_login(
            service, task_service, authenticated_service, profile_id, worker
        )
        first = await service.resume(orchestration_id, expected_revision=revision)
        assert first.orchestration.status == "PAUSED" and first.orchestration.pause_reason == "manual_handoff_required"

        # The user pressed Continue, but nothing about the login actually happened: the profile is still
        # NEEDS_LOGIN (the pause already flipped it), so a fresh, real step is refused before any page is
        # even opened -- proving Continue never assumes the human's part happened just because it was
        # pressed. `resume()` on its own, re-checking the SAME still-unresolved task, is equally idempotent.
        with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
            await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-b-0002"))
        assert refused.value.code == "profile_not_authenticated"

        second = await service.resume(orchestration_id, expected_revision=first.orchestration.revision)
        assert second.orchestration.status == "PAUSED"
        assert second.orchestration.pause_reason == "manual_handoff_required"
        assert second.steps[0].pending_note == first.steps[0].pending_note

    async def test_a_real_sign_in_then_continue_re_observes_and_the_task_proceeds(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker, engine: AsyncEngine,
    ) -> None:
        orchestration_id, task_id, revision = await self._paused_for_login(
            service, task_service, authenticated_service, profile_id, worker
        )
        paused = await service.resume(orchestration_id, expected_revision=revision)
        assert paused.orchestration.pause_reason == "manual_handoff_required"

        # The user actually signs back in, through the real login-takeover completion path (Milestone 8a
        # S2), which is what flips the profile back to AUTHENTICATED -- Electron main's re-observe nudge
        # never does this itself. Same account, same profile: the fingerprint the takeover recorded matches
        # what this grant already trusts.
        async with engine.begin() as connection:
            await BrowserProfileRepository(connection).mark_authenticated(profile_id=profile_id, account_fingerprint=FINGERPRINT)

        # The worker's own queue is now empty, so the next observe answers the ordinary account page again --
        # exactly what a real re-observation after a real sign-in looks like from Lumi's side.
        result = await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-b-0003"))
        assert result.outcome is AttemptOutcome.SUCCEEDED and result.pause_reason is None

        # The read succeeded, but the child task has not yet produced an ANSWER -- exactly like a
        # public_research step still in progress, the orchestration stays paused for the ordinary
        # "approval_required" reason (not manual_handoff_required: nothing outside Lumi is needed any more),
        # never RUNNING, until the task's own loop actually finishes.
        mid_flight = await service.resume(orchestration_id, expected_revision=paused.orchestration.revision)
        assert mid_flight.orchestration.status == "PAUSED"
        assert mid_flight.orchestration.pause_reason == "approval_required"
        assert mid_flight.steps[0].pending_note is None  # the stale handoff note was cleared, not left stale

        assert result.observation is not None
        private_block = next(block for block in result.observation.observation.blocks if block.text.endswith("- Private"))
        await authenticated_service.record_answer(
            task_id,
            answer=ResearchAnswer(
                status="answered", stop_reason="goal_reached", answer="You own private repositories.",
                # The credential-surface pause never stored an observation, so this successful read is the
                # task's first one, "o1" -- not "o2".
                evidence=[ResearchEvidence(observation="o1", block=private_block.id, quote=private_block.text)],
            ),
            provider="gemini", model="gemini-1", planner_calls=1,
        )

        resumed = await service.resume(orchestration_id, expected_revision=mid_flight.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"
        assert resumed.orchestration.pause_reason is None
        assert resumed.steps[0].status == "SUCCEEDED"
        summary = resumed.steps[0].result_summary
        assert summary is not None and "private repositories" not in summary and private_block.text not in summary


class TestWrongAccountAfterHandoff:
    """Acceptance C: expected account A -> manual login -> account B active -> Continue -> re-observe ->
    refuse / require appropriate re-authorization. Never silently continue."""

    async def test_a_different_account_after_the_handoff_makes_the_grant_permanently_unusable(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker, engine: AsyncEngine,
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        task_id = await _account_task(task_service, profile_id=profile_id)
        await service.advance(
            view.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
        )
        prepared = await authenticated_service.prepare(task_id, recipient="gemini")
        assert prepared.grant is not None
        await authenticated_service.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)

        # A different account is now signed in through the SAME profile -- exactly what a manual takeover
        # that logged into the wrong account leaves behind. This never happens via account_read itself.
        async with engine.begin() as connection:
            await BrowserProfileRepository(connection).mark_authenticated(profile_id=profile_id, account_fingerprint=OTHER_FINGERPRINT)

        # The re-observe nudge (Electron main's `continueAccountRead`) attempts one fresh observe under the
        # still-ACTIVE grant, which is bound to the OLD (account A) fingerprint. The database itself refuses
        # before any page is opened: the grant is never silently reused for a different account.
        with pytest.raises(AuthenticatedGrantNotUsableError) as refused:
            await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-c-0001"))
        assert "account changed" in str(refused.value).lower()

        # Electron main's own nudge (see `AgentTaskController.continueAccountRead`) revokes a grant that a
        # fresh re-observe found dead, so the linked task reaches a clean terminal state instead of being
        # stuck forever retrying against a grant that can never work again.
        await authenticated_service.revoke(task_id, reason="grant_unusable")

        latest = await service.describe(view.orchestration.id)
        resumed = await service.resume(view.orchestration.id, expected_revision=latest.orchestration.revision)
        assert resumed.orchestration.status == "RUNNING"  # the orchestration itself is not stuck
        step = resumed.steps[0]
        assert step.status == "FAILED"  # refused, not silently continued
        assert step.result_summary is not None and "not continued" in step.result_summary.lower()

        # Re-authorization means a genuinely NEW scope card, never the old one silently revived: preparing
        # again opens a fresh PENDING grant the user must confirm from scratch, distinct from the revoked one.
        reprepared = await authenticated_service.prepare(task_id, recipient="gemini")
        assert reprepared.grant is not None
        assert reprepared.grant.id != prepared.grant.id
        assert reprepared.grant.status.value == "PENDING"


class TestCrossTaskAndExpiryProtection:
    async def test_an_account_context_ref_from_one_orchestration_cannot_be_cited_by_another(
        self, service: OrchestrationService, task_service: TaskService, profile_id: uuid.UUID,
    ) -> None:
        owner = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, owner.orchestration.id, owner.orchestration.revision, profile_id=profile_id)
        ref = registered.resources[0].ref

        other = await service.create(objective="a different task entirely")
        task_id = await _account_task(task_service, profile_id=profile_id)
        with pytest.raises(OrchestrationRefusal, match="resource_not_found"):
            await service.advance(
                other.orchestration.id, expected_revision=other.orchestration.revision,
                capability_id="account_read", task_id=task_id, resources=[ref],
            )

    async def test_an_expired_account_context_ref_is_refused_even_though_it_is_real_and_owned(
        self, service: OrchestrationService, task_service: TaskService, profile_id: uuid.UUID, engine: AsyncEngine,
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        ref = registered.resources[0].ref
        await _sql(
            engine, "UPDATE orchestration_resources SET expires_at = now() - interval '1 minute' "
            "WHERE orchestration_id = :id AND ref = :ref", id=view.orchestration.id, ref=ref,
        )
        task_id = await _account_task(task_service, profile_id=profile_id)
        with pytest.raises(OrchestrationRefusal, match="resource_expired"):
            await service.advance(
                view.orchestration.id, expected_revision=registered.orchestration.revision,
                capability_id="account_read", task_id=task_id, resources=[ref],
            )

    async def test_a_stopped_orchestration_refuses_to_continue_a_manual_handoff(
        self, service: OrchestrationService, task_service: TaskService, authenticated_service: AuthenticatedReadService,
        profile_id: uuid.UUID, worker: FakeWorker,
    ) -> None:
        view = await service.create(objective=OBJECTIVE)
        registered = await _registered_account_ref(service, view.orchestration.id, view.orchestration.revision, profile_id=profile_id)
        task_id = await _account_task(task_service, profile_id=profile_id)
        paused = await service.advance(
            view.orchestration.id, expected_revision=registered.orchestration.revision,
            capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
        )
        prepared = await authenticated_service.prepare(task_id, recipient="gemini")
        assert prepared.grant is not None
        await authenticated_service.confirm(task_id, grant_id=prepared.grant.id, expected_revision=prepared.grant.revision)
        worker.queue.append(credential_surface)
        await authenticated_service.execute_step(task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-x-0001"))
        handoff = await service.resume(view.orchestration.id, expected_revision=paused.orchestration.revision)
        assert handoff.orchestration.pause_reason == "manual_handoff_required"

        stopped = await service.stop(view.orchestration.id, expected_revision=handoff.orchestration.revision)
        assert stopped.orchestration.status == "STOPPED"
        assert stopped.orchestration.pause_reason is None  # never left "manual_handoff_required" once stopped

        # Stop is terminal: `resume()` is idempotent and never revives it, and a fresh step is refused
        # outright -- there is no path back to `manual_handoff_required`, and nothing here is continued.
        untouched = await service.resume(view.orchestration.id, expected_revision=stopped.orchestration.revision)
        assert untouched.orchestration.status == "STOPPED"
        with pytest.raises(OrchestrationRefusal, match="orchestration_not_active"):
            await service.advance(
                view.orchestration.id, expected_revision=stopped.orchestration.revision,
                capability_id="account_read", task_id=task_id, resources=[registered.resources[0].ref],
            )
