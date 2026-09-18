"""Scoped research authorization, against a real PostgreSQL ledger.

What these tests are really about: **a research step can only happen because
the user confirmed a bounded scope in the trusted UI, and each step consumes a
single-use authorization derived from it.** Everything else here is a way of
trying to get a step to happen without that, or to get a second step out of one
authorization.

No browser is involved. The steps that reach execution are `public_search`,
which the runtime performs itself; browser steps are refused before any worker
is contacted because the checks that refuse them come first.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.tables import action_attempts, actions, step_authorizations, task_grants
from app.domain.action_status import ActionStatus
from app.domain.errors import (
    ResearchAnswerAlreadyRecordedError,
    ResearchBudgetExhaustedError,
    ResearchGrantNotFoundError,
    ResearchGrantNotUsableError,
    ResearchNotConfiguredError,
    ResearchStepRefusedError,
)
from app.domain.public_url import RESEARCH_POLICY_VERSION, PublicUrlPolicy
from app.domain.research import (
    AnswerNotGroundedError,
    GrantStatus,
    ResearchAnswer,
    ResearchBudgets,
    ResearchDisclosure,
    ResearchEvidence,
    ResearchOperation,
    ResearchStepEnvelope,
    parse_step,
)
from app.domain.task_status import TaskStatus
from app.repositories.research import ResearchRepository
from app.services.actions import ActionService
from app.services.research_search import RawResult, SearchProvider
from app.services.research_tasks import ResearchService
from app.services.tasks import TaskService

OBJECTIVE = "Find the Lumi project page and tell me how many contributors it has"
DISCLOSURE = ResearchDisclosure(recipients=["scripted"], max_text_chars=10_000)


class StubSearch:
    """A search provider with a fixed answer. No network, no credential."""

    def __init__(self, *, configured: bool = True) -> None:
        self._configured = configured
        self.queries: list[str] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def search(self, query: str) -> list[RawResult]:
        self.queries.append(query)
        return [
            RawResult(
                title="Lumi projects directory",
                url="https://example.com/research/hub",
                host="example.com",
                snippet="An index of projects named Lumi",
            ),
            RawResult(
                title="lumi-coffee-grinder",
                url="https://example.com/research/decoy",
                host="example.com",
                snippet="Stars: 9,912",
            ),
        ]


def build_service(
    engine: AsyncEngine,
    actions_service: ActionService,
    tasks: TaskService,
    *,
    runtime_generation: uuid.UUID,
    search: SearchProvider | None = None,
    worker: Any = None,
    grant_ttl_seconds: int = 600,
) -> ResearchService:
    return ResearchService(
        engine,
        tasks=tasks,
        actions=actions_service,
        runtime_generation=runtime_generation,
        worker=worker,
        policy=PublicUrlPolicy(
            version=RESEARCH_POLICY_VERSION, allow_any_public_host=True
        ),
        search=search if search is not None else StubSearch(),
        grant_ttl_seconds=grant_ttl_seconds,
        step_ttl_seconds=120,
    )


@pytest.fixture
async def service(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> AsyncIterator[ResearchService]:
    yield build_service(
        engine, action_service, task_service, runtime_generation=runtime_generation.id
    )


@pytest.fixture
async def task_id(task_service: TaskService) -> uuid.UUID:
    task = await task_service.create_task(
        {"type": "public_research", "text": OBJECTIVE, "objective": OBJECTIVE, "source": "text"}
    )
    return task.id


def _envelope(step: dict[str, Any], *, request_id: str, planner_calls: int = 1) -> ResearchStepEnvelope:
    return ResearchStepEnvelope(
        request_id=request_id, step=parse_step(step), planner_calls=planner_calls
    )


SEARCH_STEP = {"operation": "public_search", "query": "lumi project contributors"}


async def _granted(service: ResearchService, task_id: uuid.UUID, **prepare: Any) -> Any:
    view = await service.prepare(
        task_id,
        disclosure=prepare.pop("disclosure", DISCLOSURE),
        budgets=prepare.pop("budgets", ResearchBudgets()),
    )
    assert view.grant is not None
    return await service.confirm(
        task_id, grant_id=view.grant.id, expected_revision=view.grant.revision
    )


# ---- nothing happens before the trusted click ------------------------------------


async def test_a_step_without_any_scope_is_refused(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    with pytest.raises(ResearchGrantNotFoundError):
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))


async def test_a_step_under_a_pending_scope_is_refused(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    """The card is on screen. Nobody has pressed anything. Nothing may run."""
    view = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert view.grant is not None and view.grant.status is GrantStatus.PENDING
    with pytest.raises(ResearchGrantNotUsableError) as refused:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    assert "not been granted" in refused.value.reason


async def test_preparing_twice_shows_the_same_card_rather_than_a_second_scope(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    first = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    second = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert first.grant is not None and second.grant is not None
    assert first.grant.id == second.grant.id
    assert first.grant.scope_digest == second.grant.scope_digest


async def test_confirming_a_scope_the_user_did_not_see_is_refused(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    view = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert view.grant is not None
    with pytest.raises(ResearchGrantNotUsableError):
        await service.confirm(
            task_id, grant_id=view.grant.id, expected_revision=view.grant.revision + 1
        )


async def test_a_scope_cannot_be_confirmed_for_another_task(
    service: ResearchService, task_id: uuid.UUID, task_service: TaskService
) -> None:
    view = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert view.grant is not None
    other = await task_service.create_task(
        {"type": "public_research", "text": OBJECTIVE, "objective": OBJECTIVE}
    )
    with pytest.raises(ResearchGrantNotFoundError):
        await service.confirm(
            other.id, grant_id=view.grant.id, expected_revision=view.grant.revision
        )


async def test_the_scope_card_lists_what_is_allowed_and_what_is_not(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    view = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert view.grant is not None
    scope = view.grant.scope
    assert ResearchOperation.SEARCH in scope.allowed_operations
    assert scope.methods == ["GET", "HEAD"]
    for forbidden in ("login", "forms_and_typing", "uploads_and_downloads", "files", "private_network"):
        assert forbidden in scope.forbidden
    assert scope.policy_version == RESEARCH_POLICY_VERSION


# ---- a step under an active scope -------------------------------------------------


async def test_a_granted_scope_lets_one_step_run_and_records_its_evidence(
    service: ResearchService, task_id: uuid.UUID, engine: AsyncEngine
) -> None:
    await _granted(service, task_id)
    result = await service.execute_step(
        task_id, _envelope(SEARCH_STEP, request_id="req-00000001")
    )
    assert result.replayed is False
    assert result.observation is not None
    observation = result.observation.observation
    assert observation.kind == "search_results"
    assert observation.sequence == 1 and observation.ref == "o1"
    assert [entry.id for entry in observation.results] == ["r1", "r2"]
    # Refs resolve to addresses only in the controller's own table.
    assert result.observation.targets["r1"].endswith("/research/hub")
    assert observation.provenance == "untrusted_environment"
    assert result.action.action.status is ActionStatus.SUCCEEDED
    assert result.action.action.tool_name == "research_search"

    async with engine.connect() as connection:
        attempt = (
            await connection.execute(
                select(action_attempts).where(
                    action_attempts.c.action_id == result.action.action.id
                )
            )
        ).one()
    # The load-bearing assertion: this attempt was funded by a scoped
    # authorization, not by an approval of this exact step.
    assert attempt.approval_id is None
    assert attempt.step_authorization_id is not None


async def test_the_timeline_says_authorized_not_approved(
    service: ResearchService, task_id: uuid.UUID, task_service: TaskService
) -> None:
    await _granted(service, task_id)
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    events = await task_service.list_events(task_id, after_sequence=0, limit=100)
    types = [event.event_type for event in events]
    assert "task.research_scope_requested" in types
    assert "task.research_scope_granted" in types
    assert "action.authorized" in types
    # Lumi must never claim the user reviewed this exact step.
    assert "action.approved" not in types
    assert "action.approval_requested" not in types
    proposed = next(event for event in events if event.event_type == "action.proposed")
    assert proposed.payload["requires_approval"] is False
    assert proposed.payload["authorization"] == "task_grant"
    authorized = next(event for event in events if event.event_type == "action.authorized")
    assert authorized.payload["authorization"] == "task_grant"
    assert authorized.payload["scope_digest"]


async def test_replaying_a_request_id_executes_nothing_and_returns_the_stored_step(
    service: ResearchService, task_id: uuid.UUID, engine: AsyncEngine
) -> None:
    """A duplicated planner request must not become a second step."""
    await _granted(service, task_id)
    first = await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    again = await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    assert again.replayed is True
    assert again.action.action.id == first.action.action.id
    assert again.observation is not None
    assert again.observation.id == first.observation.id  # type: ignore[union-attr]
    async with engine.connect() as connection:
        steps = (
            await connection.execute(
                select(actions).where(actions.c.task_id == task_id)
            )
        ).all()
        authorizations = (
            await connection.execute(
                select(step_authorizations).where(step_authorizations.c.task_id == task_id)
            )
        ).all()
    assert len(steps) == 1
    assert len(authorizations) == 1


async def test_a_step_authorization_is_consumed_once_and_never_again(
    service: ResearchService, task_id: uuid.UUID, engine: AsyncEngine
) -> None:
    await _granted(service, task_id)
    result = await service.execute_step(
        task_id, _envelope(SEARCH_STEP, request_id="req-00000001")
    )
    async with engine.begin() as connection:
        repository = ResearchRepository(connection)
        authorization = await repository.authorization_for_action(result.action.action.id)
        assert authorization is not None and authorization.consumed_at is not None
        # Every re-use is refused by the statement that would consume it.
        assert (
            await repository.consume_step_authorization(
                authorization_id=authorization.id,
                action_revision=authorization.action_revision,
                proposal_digest=authorization.proposal_digest,
                runtime_generation=authorization.runtime_generation,
            )
        ) is None


async def test_an_authorization_will_not_fund_a_different_action_or_generation(
    service: ResearchService, task_id: uuid.UUID, engine: AsyncEngine, runtime_generation: Any
) -> None:
    await _granted(service, task_id)
    view = await service.describe(task_id)
    assert view.grant is not None
    async with engine.begin() as connection:
        repository = ResearchRepository(connection)
        grant = await repository.get_grant(view.grant.id)
        assert grant is not None
        action_id = uuid.uuid4()
        await connection.execute(
            actions.insert().values(
                id=action_id,
                task_id=task_id,
                idempotency_key="research:fake",
                tool_name="research_observe",
                risk_tier="R1",
                proposal={"kind": "public_research_step"},
                proposal_digest="a" * 64,
                status=ActionStatus.PROPOSED.value,
                revision=1,
            )
        )
        minted = await repository.insert_step_authorization(
            authorization_id=uuid.uuid4(),
            grant=grant,
            task_id=task_id,
            action_id=action_id,
            action_revision=1,
            proposal_digest="a" * 64,
            runtime_generation=runtime_generation.id,
            ttl=timedelta(seconds=120),
        )
        # A different action revision, a different digest, or a different
        # runtime process: each one refuses on its own.
        assert (
            await repository.consume_step_authorization(
                authorization_id=minted.id,
                action_revision=2,
                proposal_digest="a" * 64,
                runtime_generation=runtime_generation.id,
            )
        ) is None
        assert (
            await repository.consume_step_authorization(
                authorization_id=minted.id,
                action_revision=1,
                proposal_digest="b" * 64,
                runtime_generation=runtime_generation.id,
            )
        ) is None
        assert (
            await repository.consume_step_authorization(
                authorization_id=minted.id,
                action_revision=1,
                proposal_digest="a" * 64,
                runtime_generation=uuid.uuid4(),
            )
        ) is None
        assert (
            await repository.consume_step_authorization(
                authorization_id=minted.id,
                action_revision=1,
                proposal_digest="a" * 64,
                runtime_generation=runtime_generation.id,
            )
        ) is not None


async def test_an_expired_scope_authorises_nothing(
    service: ResearchService, task_id: uuid.UUID, engine: AsyncEngine
) -> None:
    await _granted(service, task_id)
    view = await service.describe(task_id)
    assert view.grant is not None
    async with engine.begin() as connection:
        await connection.execute(
            # Move the whole window into the past, so the "an active grant has
            # a window" constraint still holds and only its expiry has passed.
            update(task_grants)
            .where(task_grants.c.id == view.grant.id)
            .values(
                confirmed_at=text("now() - interval '2 seconds'"),
                expires_at=text("now() - interval '1 second'"),
            )
        )
    with pytest.raises(ResearchGrantNotUsableError) as refused:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    assert "expired" in refused.value.reason


async def test_a_revoked_scope_authorises_nothing_and_cannot_be_revived(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    revoked = await service.revoke(task_id, reason="user_stopped")
    assert revoked.grant is not None and revoked.grant.status is GrantStatus.REVOKED
    with pytest.raises(ResearchGrantNotUsableError):
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    # Confirming it again is refused: a new scope needs a new card and click.
    with pytest.raises(ResearchGrantNotUsableError):
        await service.confirm(
            task_id, grant_id=revoked.grant.id, expected_revision=revoked.grant.revision
        )


async def test_cancelling_the_task_withdraws_the_scope_and_stops_research(
    service: ResearchService, task_id: uuid.UUID, task_service: TaskService
) -> None:
    await _granted(service, task_id)
    await task_service.cancel_task(task_id)
    view = await service.describe(task_id)
    # The scope is closed in the same transaction that cancelled the task, so
    # a cancelled task never leaves a live grant behind.
    assert view.grant is not None and view.grant.status is GrantStatus.REVOKED
    events = await task_service.list_events(task_id, after_sequence=0, limit=100)
    revoked = next(event for event in events if event.event_type == "task.research_scope_revoked")
    assert revoked.payload["reason"] == "task_cancelled"
    with pytest.raises(Exception) as refused:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    assert refused.type.__name__ in (
        "TaskNotAcceptingActionsError",
        "ResearchGrantNotUsableError",
    )


# ---- the scope is the capability -------------------------------------------------


async def test_an_operation_the_scope_does_not_list_is_refused(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService,
    runtime_generation: Any, task_id: uuid.UUID
) -> None:
    """A scope without search refuses a search, however the planner asks."""
    service = build_service(
        engine,
        action_service,
        task_service,
        runtime_generation=runtime_generation.id,
        search=StubSearch(configured=False),
    )
    view = await service.prepare(task_id, disclosure=DISCLOSURE, budgets=ResearchBudgets())
    assert view.grant is not None
    assert ResearchOperation.SEARCH not in view.grant.scope.allowed_operations
    await service.confirm(
        task_id, grant_id=view.grant.id, expected_revision=view.grant.revision
    )
    with pytest.raises(ResearchStepRefusedError) as refused:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    assert refused.value.code == "outside_scope"


async def test_a_browser_step_without_a_worker_is_refused_rather_than_faked(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    with pytest.raises(ResearchNotConfiguredError):
        await service.execute_step(
            task_id, _envelope({"operation": "observe", "tab": "t1"}, request_id="req-00000009")
        )


async def test_navigating_to_an_unknown_observation_or_ref_is_refused(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    for step, code in (
        (
            {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}},
            "unknown_seed",
        ),
    ):
        with pytest.raises(ResearchStepRefusedError) as refused:
            await service.execute_step(task_id, _envelope(step, request_id="req-0000001x"))
        assert refused.value.code == code


# ---- budgets ----------------------------------------------------------------------


async def test_the_step_budget_stops_the_task(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id, budgets=ResearchBudgets(max_steps=1))
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    with pytest.raises(ResearchBudgetExhaustedError) as exhausted:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    assert exhausted.value.limit == "max_steps"


async def test_the_observation_budget_stops_the_task(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id, budgets=ResearchBudgets(max_observations=1))
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    with pytest.raises(ResearchBudgetExhaustedError) as exhausted:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    assert exhausted.value.limit == "max_observations"


async def test_the_planner_call_budget_stops_the_task(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id, budgets=ResearchBudgets(max_planner_calls=2))
    await service.execute_step(
        task_id, _envelope(SEARCH_STEP, request_id="req-00000001", planner_calls=2)
    )
    with pytest.raises(ResearchBudgetExhaustedError) as exhausted:
        await service.execute_step(
            task_id, _envelope(SEARCH_STEP, request_id="req-00000002", planner_calls=3)
        )
    assert exhausted.value.limit == "max_planner_calls"


# ---- the answer -------------------------------------------------------------------


async def test_an_ungrounded_answer_is_refused_and_nothing_is_stored(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    with pytest.raises(AnswerNotGroundedError):
        await service.record_answer(
            task_id,
            answer=ResearchAnswer(
                status="answered",
                stop_reason="goal_reached",
                answer="It has 7 contributors.",
                evidence=[ResearchEvidence(observation="o1", block="b1", quote="Contributors 7")],
            ),
            provider="scripted",
            model="scripted-research",
            planner_calls=2,
        )
    view = await service.describe(task_id)
    assert view.answer is None
    assert view.grant is not None and view.grant.status is GrantStatus.ACTIVE


async def test_an_honest_not_found_answer_completes_the_task_and_the_scope(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    view = await service.record_answer(
        task_id,
        answer=ResearchAnswer(
            status="not_found",
            stop_reason="no_evidence",
            answer="Lumi could not verify that from the public pages it read.",
        ),
        provider="scripted",
        model="scripted-research",
        planner_calls=2,
    )
    assert view.answer is not None and view.answer.answer.status == "not_found"
    assert view.grant is not None and view.grant.status is GrantStatus.COMPLETED
    assert view.task.status is TaskStatus.SUCCEEDED
    with pytest.raises(ResearchAnswerAlreadyRecordedError):
        await service.record_answer(
            task_id,
            answer=ResearchAnswer(
                status="not_found", stop_reason="no_evidence", answer="Again."
            ),
            provider="scripted",
            model="scripted-research",
            planner_calls=3,
        )


async def test_a_completed_scope_authorises_no_further_step(
    service: ResearchService, task_id: uuid.UUID
) -> None:
    await _granted(service, task_id)
    await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000001"))
    await service.record_answer(
        task_id,
        answer=ResearchAnswer(
            status="not_found", stop_reason="no_evidence", answer="Nothing public showed it."
        ),
        provider="scripted",
        model="scripted-research",
        planner_calls=2,
    )
    with pytest.raises(Exception) as refused:
        await service.execute_step(task_id, _envelope(SEARCH_STEP, request_id="req-00000002"))
    assert refused.type.__name__ in (
        "ResearchGrantNotUsableError",
        "TaskNotAcceptingActionsError",
    )


async def test_a_search_query_that_would_carry_data_out_never_reaches_the_provider(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService,
    runtime_generation: Any, task_id: uuid.UUID
) -> None:
    search = StubSearch()
    service = build_service(
        engine, action_service, task_service, runtime_generation=runtime_generation.id, search=search
    )
    await _granted(service, task_id)
    with pytest.raises(Exception):
        await service.execute_step(
            task_id,
            _envelope(
                {"operation": "public_search", "query": "lumi contributors satish@example.com"},
                request_id="req-00000001",
            ),
        )
    assert search.queries == []
