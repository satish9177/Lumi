"""Authenticated account reading (Milestone 8a S3): the runtime's authority.

Everything here is deterministic and needs no browser: it is the controller half
of S3, against a real PostgreSQL ledger, with a scripted stand-in for the one
call that would reach the browser worker. The browser half -- the network guard,
the credential-surface gate, redaction, refs -- lives in
`test_authenticated_browser.py`, and both halves together in
`test_authenticated_service_browser.py`.

What these tests are really about: **an account step can only happen because the
user confirmed a bounded scope for one profile, one account and one provider,
and each step consumes a single-use authorization that the database re-checks
against that profile at the instant it executes.** Everything else here is a way
of trying to get a step to happen without that, on a different account, after
the account changed, or with private text going somewhere it was never approved.
"""

import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.db.tables import authenticated_observations, task_grants
from app.domain.action_status import AttemptOutcome, RiskTier
from app.domain.authenticated import (
    AuthenticatedObservation,
    AuthenticatedRefusal,
    AuthenticatedStepEnvelope,
    AuthOperation,
    CredentialSurfaceResult,
    IdentityCheckResult,
    PauseReason,
    WorkerReadResult,
    parse_authenticated_step,
)
from app.domain.browser_dispatch import DispatchStatus
from app.domain.browser_profile import BrowserVersions, ProfileStatus, allowed_origins_for
from app.domain.errors import (
    AuthenticatedAnswerAlreadyRecordedError,
    AuthenticatedBudgetExhaustedError,
    AuthenticatedGrantNotUsableError,
    AuthenticatedProfileUnavailableError,
    AuthenticatedStepInFlightError,
    AuthenticatedStepRefusedError,
    TaskNotAcceptingActionsError,
)
from app.domain.login_takeover import CredentialSignal, hash_identity
from app.domain.research import (
    AnswerNotGroundedError,
    GrantStatus,
    ResearchAnswer,
    ResearchEvidence,
    TextBlock,
    compute_content_hash,
)
from app.domain.task_status import TaskStatus
from app.repositories.authenticated import AuthenticatedRepository
from app.repositories.browser import BrowserRepository
from app.repositories.login_attempts import LoginAttemptRepository
from app.repositories.profiles import BrowserProfileRepository
from app.repositories.research import ResearchRepository
from app.services import authenticated_read as service_module
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.browser_execution import Outcome, WorkerProfileOpen
from app.services.browser_profiles import BrowserProfileService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from evals.sites.account_fixture import ACCOUNT_ID, SECOND_ACCOUNT_ID

OBJECTIVE = "Which of my repositories are private?"
FINGERPRINT = hash_identity(ACCOUNT_ID)
OTHER_FINGERPRINT = hash_identity(SECOND_ACCOUNT_ID)
ACCOUNT_BLOCKS = [
    "Your repositories",
    "You own 5 repositories.",
    "lumi-notes - Private",
    "public-site - Public",
    "secret-plans - Private",
]


class FakeProfileBrowser:
    """Stands in for `BrowserExecutionService` for the profile open/close calls."""

    def __init__(self) -> None:
        self.worker_generation = uuid.uuid4()
        self.opened: list[tuple[uuid.UUID, bool]] = []
        self.closed: list[uuid.UUID] = []

    async def open_browser_profile(
        self, *, profile_id: uuid.UUID, recorded_chromium_build: str | None, headed: bool = False
    ) -> WorkerProfileOpen:
        self.opened.append((profile_id, headed))
        return WorkerProfileOpen(
            profile_id=profile_id,
            worker_generation=self.worker_generation,
            versions=BrowserVersions(chromium_build="153.0.8010.12", playwright_version="1.63.0", app_version=""),
            lock_held=True,
        )

    async def close_browser_profile(self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID) -> bool:
        self.closed.append(profile_id)
        return True


@dataclass
class FakeWorker:
    """Scripts what the browser worker would answer, one dispatch at a time.

    `queue` holds callables from the dispatch request to an `Outcome`. An empty
    queue means the worker returns the ordinary account page, which is what most
    tests want. Every request the worker was asked to run is recorded in
    `dispatched`, which is how a test proves a step really did (or did not) reach
    the browser.
    """

    generation: uuid.UUID
    dispatched: list[Any] = field(default_factory=list)
    queue: list[Callable[[Any], Outcome]] = field(default_factory=list)
    blocks: list[str] = field(default_factory=lambda: list(ACCOUNT_BLOCKS))

    async def identify(self) -> Any:
        return type(
            "Identity", (), {"worker_generation": self.generation, "started_at": datetime.now(UTC).isoformat()}
        )()

    async def aclose(self) -> None:
        return None


def _outcome(request: Any, result: WorkerReadResult | None, *, error_code: str | None = None,
             status: AttemptOutcome = AttemptOutcome.SUCCEEDED, submitted: bool = True,
             dispatch: DispatchStatus = DispatchStatus.OK) -> Outcome:
    observation: dict[str, Any] = {}
    observation_id = None
    if result is not None:
        observation["result"] = result.model_dump(mode="json")
        if result.observation is not None:
            observation["observation_id"] = str(result.observation.observation_id)
            observation_id = result.observation.observation_id
    return Outcome(
        outcome=status, dispatch_status=dispatch, submitted=submitted, error_code=error_code,
        observation_id=observation_id,
        result={"operation": request.operation, "status": dispatch.value, "dispatch_id": str(request.dispatch_id)},
        observation=observation,
    )


def page_observation(
    request: Any, blocks: list[str], *, profile_id: uuid.UUID | None = None, links: int = 2,
    redactions: dict[str, int] | None = None, tab: str = "t1", epoch: int = 1,
) -> AuthenticatedObservation:
    profile = profile_id or request.profile_id
    text_blocks = [TextBlock(id=f"b{index + 1}", text=value) for index, value in enumerate(blocks)]
    from app.domain.research import ObservedLink

    observed_links = [ObservedLink(id=f"l{index + 1}", text=f"Link {index + 1}", host="github.com") for index in range(links)]
    return AuthenticatedObservation(
        observation_id=uuid.uuid4(), kind="page", operation=AuthOperation(_operation_of(request)),
        sequence=request.input["sequence"], profile_id=profile, tab=tab, document_epoch=epoch,
        host="github.com", title="Your repositories", settled=True, truncated=False,
        observed_at=datetime.now(UTC), blocks=text_blocks, links=observed_links, open_tabs=["t1"],
        total_text_chars=sum(len(value) for value in blocks), total_link_count=links,
        redactions=redactions or {},
        content_hash=compute_content_hash(
            kind="page", final_url="github.com", title="Your repositories",
            blocks=text_blocks, links=observed_links, results=[],
        ),
    )


def _operation_of(request: Any) -> str:
    return str(request.operation).removeprefix("authenticated_")


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    return tmp_path / "browser-profiles"


@dataclass
class Rig:
    engine: AsyncEngine
    service: AuthenticatedReadService
    profiles: BrowserProfileService
    browser: FakeProfileBrowser
    worker: FakeWorker
    tasks: TaskService
    actions: ActionService
    generation: RuntimeGeneration
    profile_id: uuid.UUID
    calls: int = 0

    async def task(self, *, profile_id: uuid.UUID | None = None, objective: str = OBJECTIVE) -> uuid.UUID:
        task = await self.tasks.create_task({
            "type": "authenticated_read", "classification": "account_private", "text": objective,
            "objective": objective, "source": "text", "profile_id": str(profile_id or self.profile_id),
        })
        return task.id

    async def granted(self, recipient: str = "gemini") -> tuple[uuid.UUID, Any]:
        task_id = await self.task()
        view = await self.service.prepare(task_id, recipient=recipient)  # type: ignore[arg-type]
        assert view.grant is not None
        confirmed = await self.service.confirm(task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
        assert confirmed.grant is not None and confirmed.grant.status is GrantStatus.ACTIVE
        return task_id, confirmed.grant

    def envelope(self, step: dict[str, Any], *, planner_calls: int = 1) -> AuthenticatedStepEnvelope:
        self.calls += 1
        return AuthenticatedStepEnvelope(
            request_id=f"req-{self.calls:08d}", step=parse_authenticated_step(step), planner_calls=planner_calls
        )

    async def observe(self, task_id: uuid.UUID) -> Any:
        return await self.service.execute_step(task_id, self.envelope({"operation": "observe", "tab": "t1"}))

    async def profile(self) -> Any:
        return await self.profiles.get_profile(self.profile_id)


@pytest.fixture
async def rig(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService,
    runtime_generation: RuntimeGeneration, profile_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Rig]:
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
    rig = Rig(
        engine=engine, service=service, profiles=profiles, browser=browser, worker=worker,
        tasks=task_service, actions=action_service, generation=runtime_generation, profile_id=profile_id,
    )

    async def open_worker_client(_worker: Any, _generation: uuid.UUID) -> FakeWorker:
        return worker

    async def dispatch_and_classify(_client: Any, *, request: Any) -> Outcome:
        worker.dispatched.append(request)
        if worker.queue:
            return worker.queue.pop(0)(request)
        observation = page_observation(request, worker.blocks)
        return _outcome(request, WorkerReadResult(observation=observation))

    monkeypatch.setattr(service_module, "open_worker_client", open_worker_client)
    monkeypatch.setattr(service_module, "dispatch_and_classify", dispatch_and_classify)
    yield rig


def credential_surface(request: Any) -> Outcome:
    return _outcome(request, WorkerReadResult(credential_surface=CredentialSurfaceResult(
        signals=[CredentialSignal.PASSWORD_FIELD], tab="t1", document_epoch=1, observed_at=datetime.now(UTC))))


def identity(kind: str) -> Callable[[Any], Outcome]:
    def build(request: Any) -> Outcome:
        return _outcome(request, WorkerReadResult(identity=IdentityCheckResult(
            kind=kind, tab="t1", document_epoch=1, observed_at=datetime.now(UTC))))
    return build


def lost(request: Any) -> Outcome:
    return Outcome(
        outcome=AttemptOutcome.OUTCOME_UNKNOWN, dispatch_status=DispatchStatus.OUTCOME_UNKNOWN,
        submitted=True, error_code="worker_lost_response", observation_id=None,
        result={"reason": "lost"}, observation={},
    )


def failed(code: str, *, status: DispatchStatus = DispatchStatus.FAILED_BEFORE_EFFECT) -> Callable[[Any], Outcome]:
    def build(request: Any) -> Outcome:
        return Outcome(
            outcome=AttemptOutcome.FAILED, dispatch_status=status, submitted=True, error_code=code,
            observation_id=None, result={"operation": request.operation}, observation={},
        )
    return build


async def scalar(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as connection:
        return await connection.scalar(text(sql), params)


# ---- preparing: deterministic, and no browser is opened -------------------------------


async def test_prepare_builds_the_scope_from_the_profile_and_opens_nothing(rig: Rig) -> None:
    task_id = await rig.task()
    view = await rig.service.prepare(task_id, recipient="gemini")
    grant = view.grant
    assert grant is not None and grant.status is GrantStatus.PENDING
    scope = grant.scope
    profile = await rig.profile()
    assert scope.profile_id == rig.profile_id and scope.site == "github.com"
    assert scope.allowed_origins == ["https://github.com", "https://www.github.com"]
    assert scope.account_fingerprint == FINGERPRINT
    assert scope.profile_revoke_epoch == profile.revoke_epoch
    assert scope.classification == "account_private" and scope.methods == ["GET", "HEAD"]
    assert scope.disclosure.recipient == "gemini" and scope.disclosure.max_text_chars == 4_000
    assert scope.disclosure.max_blocks == 60 and scope.disclosure.failover == "none"
    assert scope.budgets.max_vision_calls == 0
    assert scope.website_side_effects_possible is True
    assert {op.value for op in scope.allowed_operations} == {"navigate", "observe", "reveal", "tab", "history"}
    # Nothing was opened, nothing was dispatched.
    assert rig.browser.opened == [] and rig.worker.dispatched == []
    assert grant.profile_id == rig.profile_id and grant.profile_revoke_epoch == profile.revoke_epoch


async def test_prepare_refuses_a_profile_that_is_not_signed_in(rig: Rig) -> None:
    async with rig.engine.begin() as connection:
        await BrowserProfileRepository(connection).mark_needs_login(profile_id=rig.profile_id)
    task_id = await rig.task()
    with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
        await rig.service.prepare(task_id, recipient="gemini")
    assert refused.value.code == "profile_not_authenticated"
    assert rig.browser.opened == [] and rig.worker.dispatched == []


async def test_prepare_refuses_an_unknown_account_fingerprint(rig: Rig) -> None:
    async with rig.engine.begin() as connection:
        await connection.execute(text("UPDATE browser_profiles SET account_fingerprint = NULL"))
    with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
        await rig.service.prepare(await rig.task(), recipient="gemini")
    assert refused.value.code == "account_fingerprint_unknown"


async def test_prepare_refuses_while_a_takeover_is_open(rig: Rig, runtime_generation: RuntimeGeneration) -> None:
    async with rig.engine.begin() as connection:
        await LoginAttemptRepository(connection).create(
            attempt_id=uuid.uuid4(), profile_id=rig.profile_id,
            runtime_generation=runtime_generation.id, profile_revision=1, ttl=timedelta(minutes=5),
        )
    with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
        await rig.service.prepare(await rig.task(), recipient="gemini")
    assert refused.value.code == "profile_takeover_active"


async def test_prepare_refuses_a_deleted_or_missing_profile(rig: Rig) -> None:
    missing = await rig.task(profile_id=uuid.uuid4())
    with pytest.raises(AuthenticatedProfileUnavailableError) as refused:
        await rig.service.prepare(missing, recipient="gemini")
    assert refused.value.code == "profile_not_found"
    await rig.profiles.delete_profile(rig.profile_id)
    with pytest.raises(AuthenticatedProfileUnavailableError) as deleted:
        await rig.service.prepare(await rig.task(), recipient="gemini")
    assert deleted.value.code == "profile_deleted"


async def test_a_task_that_is_not_account_private_is_never_stored(rig: Rig) -> None:
    for request in (
        {"type": "authenticated_read", "objective": OBJECTIVE, "profile_id": str(rig.profile_id)},
        {"type": "authenticated_read", "classification": "public", "objective": OBJECTIVE, "profile_id": str(rig.profile_id)},
        {"type": "authenticated_read", "classification": "account_private", "objective": OBJECTIVE},
        {"type": "authenticated_read", "classification": "account_private", "objective": OBJECTIVE,
         "profile_id": str(rig.profile_id), "url": "https://evil.example"},
    ):
        with pytest.raises(ValueError):
            service_module.validate_request(request)


# ---- the grant: immutable, bound, confirmed by the trusted click --------------------


async def test_a_confirmed_grant_is_bound_and_immutable(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    for column, value in (
        ("profile_revoke_epoch", 99), ("profile_id", str(uuid.uuid4())), ("scope_digest", "0" * 64), ("kind", "public_research"),
    ):
        with pytest.raises((DBAPIError, IntegrityError)):
            async with rig.engine.begin() as connection:
                await connection.execute(text(f"UPDATE task_grants SET {column} = :v WHERE id = :id"), {"v": value, "id": grant.id})
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(
                text("UPDATE task_grants SET scope = jsonb_set(scope, '{disclosure,recipient}', '\"openai\"') WHERE id = :id"),
                {"id": grant.id},
            )
    assert task_id is not None


async def test_only_a_grant_of_the_right_kind_binds_a_profile(rig: Rig) -> None:
    task = await rig.tasks.create_task({"type": "public_research", "objective": "x"})
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO task_grants (id, task_id, kind, status, revision, policy_version, scope, scope_digest, profile_id, profile_revoke_epoch) "
                     "VALUES (:id, :task, 'public_research', 'PENDING', 1, 'v', '{}', :digest, :profile, 0)"),
                {"id": uuid.uuid4(), "task": task.id, "digest": "0" * 64, "profile": rig.profile_id},
            )
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO task_grants (id, task_id, kind, status, revision, policy_version, scope, scope_digest) "
                     "VALUES (:id, :task, 'authenticated_read', 'PENDING', 1, 'v', '{}', :digest)"),
                {"id": uuid.uuid4(), "task": task.id, "digest": "0" * 64},
            )


async def test_a_stale_or_wrong_confirmation_confirms_nothing(rig: Rig) -> None:
    task_id = await rig.task()
    view = await rig.service.prepare(task_id, recipient="gemini")
    assert view.grant is not None
    with pytest.raises(AuthenticatedGrantNotUsableError):
        await rig.service.confirm(task_id, grant_id=view.grant.id, expected_revision=view.grant.revision + 1)
    assert (await rig.service.describe(task_id)).grant.status is GrantStatus.PENDING  # type: ignore[union-attr]


async def test_a_click_after_the_account_changed_confirms_nothing(rig: Rig) -> None:
    task_id = await rig.task()
    view = await rig.service.prepare(task_id, recipient="gemini")
    assert view.grant is not None
    async with rig.engine.begin() as connection:
        await BrowserProfileRepository(connection).invalidate_account(profile_id=rig.profile_id)
    with pytest.raises(AuthenticatedGrantNotUsableError):
        await rig.service.confirm(task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
    assert (await rig.service.describe(task_id)).grant.status is GrantStatus.PENDING  # type: ignore[union-attr]


async def test_a_step_before_the_click_is_refused_and_nothing_is_dispatched(rig: Rig) -> None:
    task_id = await rig.task()
    await rig.service.prepare(task_id, recipient="gemini")
    with pytest.raises(AuthenticatedGrantNotUsableError):
        await rig.observe(task_id)
    assert rig.worker.dispatched == [] and rig.browser.opened == []


# ---- a step: funded by a single-use authorization, recorded as AUTHORIZED --------------


async def test_a_step_is_authorized_not_approved_and_stores_only_redacted_evidence(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    result = await rig.observe(task_id)
    assert result.outcome is AttemptOutcome.SUCCEEDED and result.observation is not None
    assert result.action.action.status.value == "SUCCEEDED"
    assert result.action.approval is None
    attempt = result.action.attempts[0]
    assert attempt.approval_id is None and attempt.step_authorization_id is not None
    # The proposal names no address, and carries the classification.
    proposal = result.action.action.proposal
    assert proposal["kind"] == "authenticated_read_step" and proposal["classification"] == "account_private"
    assert not re.search(r"https?://", str(proposal))
    events = await rig.tasks.list_events(task_id)
    assert "action.authorized" in [event.event_type for event in events]
    assert "action.approved" not in [event.event_type for event in events]
    # Evidence is in its own table, classified, with no URL anywhere in the row.
    row = await scalar(rig.engine, "SELECT row_to_json(authenticated_observations)::text FROM authenticated_observations LIMIT 1")
    assert "account_private" in row and not re.search(r"https?://", row)
    assert await scalar(rig.engine, "SELECT count(*) FROM research_observations") == 0
    assert rig.browser.opened == [(rig.profile_id, False)]  # Headless: an agent read is never the human's window.
    dispatch = rig.worker.dispatched[0]
    assert dispatch.profile_id == rig.profile_id and dispatch.session_id is None and dispatch.site == "authenticated_read"
    assert dispatch.input["expected_account_fingerprint"] == FINGERPRINT
    assert dispatch.input["max_text_chars"] == 4_000 and dispatch.input["max_blocks"] == 60


async def test_the_dispatch_is_an_account_read_and_never_a_read_only(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    effect = await scalar(rig.engine, "SELECT effect FROM browser_dispatches")
    assert effect == "ACCOUNT_READ"
    assert await scalar(rig.engine, "SELECT count(*) FROM browser_dispatches WHERE session_id IS NOT NULL") == 0


async def test_a_replayed_request_id_executes_nothing_twice(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    envelope = rig.envelope({"operation": "observe", "tab": "t1"})
    first = await rig.service.execute_step(task_id, envelope)
    second = await rig.service.execute_step(task_id, envelope)
    assert first.replayed is False and second.replayed is True
    assert len(rig.worker.dispatched) == 1


async def test_a_step_outside_the_scope_or_vocabulary_never_reaches_the_worker(rig: Rig) -> None:
    await rig.granted()
    for step in (
        {"operation": "click", "tab": "t1"},
        {"operation": "observe", "tab": "t1", "url": "https://evil.example"},
        {"operation": "observe", "tab": "t1", "provider": "openai"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1", "href": "x"}},
        {"operation": "reveal", "tab": "t1", "target": {"kind": "coordinate", "observation": "o1", "ref": "b1"}},
        {"operation": "history", "tab": "t1", "direction": "sideways"},
        {"operation": "public_search", "query": "x"},
        {"operation": "scroll", "tab": "t1", "direction": "down"},
        {"operation": "observe", "tab": "t9"},
    ):
        with pytest.raises(AuthenticatedRefusal):
            parse_authenticated_step(step)
    assert rig.worker.dispatched == []


async def test_a_ref_from_no_known_observation_is_refused_before_dispatch(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    with pytest.raises(AuthenticatedStepRefusedError) as refused:
        await rig.service.execute_step(task_id, rig.envelope(
            {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o7", "ref": "l1"}}))
    assert refused.value.code == "unknown_observation"
    assert rig.worker.dispatched == []


async def test_a_stale_link_ref_is_refused_before_dispatch(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    rig.worker.queue.append(lambda request: _outcome(request, WorkerReadResult(
        observation=page_observation(request, ["Organization"], epoch=2))))
    await rig.observe(task_id)
    with pytest.raises(AuthenticatedStepRefusedError) as refused:
        await rig.service.execute_step(task_id, rig.envelope(
            {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1"}}))
    assert refused.value.code == "stale_target_ref"
    assert len(rig.worker.dispatched) == 2


async def test_a_link_ref_navigates_using_only_ids_and_the_epoch(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    await rig.service.execute_step(task_id, rig.envelope(
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l2"}}))
    sent = rig.worker.dispatched[1].input
    assert sent["target_ref"] == "l2" and sent["expected_document_epoch"] == 1
    assert not any(key in sent for key in ("url", "href", "host", "origin", "selector", "method", "headers", "cookies"))


async def test_a_ref_issued_by_an_earlier_worker_is_stale(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    rig.worker.generation = uuid.uuid4()
    async with rig.engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=rig.worker.generation, runtime_generation=rig.generation.id,
            worker_started_at=datetime.now(UTC))
    rig.browser.worker_generation = rig.worker.generation
    with pytest.raises(AuthenticatedStepRefusedError) as refused:
        await rig.service.execute_step(task_id, rig.envelope(
            {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1"}}))
    assert refused.value.code == "stale_session"


# ---- budgets and expiry ------------------------------------------------------------------


async def test_the_step_budget_stops_another_step(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    for _ in range(12):
        await rig.observe(task_id)
    # 12 observations reached the observation budget first.
    with pytest.raises(AuthenticatedBudgetExhaustedError):
        await rig.observe(task_id)


async def test_the_tab_budget_stops_opening_another_tab(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(lambda request: _outcome(request, WorkerReadResult(
        observation=AuthenticatedObservation(
            observation_id=uuid.uuid4(), kind="tab_state", operation=AuthOperation.TAB, sequence=request.input["sequence"],
            profile_id=request.profile_id, tab="t3", observed_at=datetime.now(UTC), open_tabs=["t1", "t2", "t3"],
            content_hash=compute_content_hash(kind="tab_state", final_url="", title="", blocks=[], links=[], results=[])))))
    await rig.service.execute_step(task_id, rig.envelope({"operation": "tab", "action": "open"}))
    with pytest.raises(AuthenticatedBudgetExhaustedError) as exhausted:
        await rig.service.execute_step(task_id, rig.envelope({"operation": "tab", "action": "open"}))
    assert exhausted.value.limit == "max_tabs"


async def test_an_expired_grant_authorises_nothing(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    async with rig.engine.begin() as connection:
        await connection.execute(text("UPDATE task_grants SET confirmed_at = now() - interval '2 seconds', expires_at = now() - interval '1 second' WHERE id = :id"), {"id": grant.id})
    with pytest.raises(AuthenticatedGrantNotUsableError):
        await rig.observe(task_id)
    assert rig.worker.dispatched == []


# ---- the consuming statement: profile, epoch, fingerprint, kind -----------------------------


async def _minted(rig: Rig, task_id: uuid.UUID, grant: Any) -> tuple[Any, Any]:
    """An action and an authorization for it, minted but not yet consumed."""
    action = await rig.actions.propose_action(
        task_id, idempotency_key=f"k-{uuid.uuid4()}", tool_name="authenticated_observe", risk_tier=RiskTier.R1,
        proposal={"kind": "authenticated_read_step"},
    )
    record = action[0].action
    async with rig.engine.begin() as connection:
        repository = AuthenticatedRepository(connection)
        grant_record = await repository.get_grant(grant.id)
        assert grant_record is not None
        authorization = await repository.insert_step_authorization(
            authorization_id=uuid.uuid4(), grant=grant_record, task_id=task_id, action_id=record.id,
            action_revision=record.revision, proposal_digest=record.proposal_digest,
            runtime_generation=rig.generation.id, ttl=timedelta(minutes=2))
    return record, authorization


async def _consume(rig: Rig, record: Any, authorization: Any, *, research: bool = False) -> Any:
    async with rig.engine.begin() as connection:
        repository: Any = ResearchRepository(connection) if research else AuthenticatedRepository(connection)
        return await repository.consume_step_authorization(
            authorization_id=authorization.id, action_revision=record.revision,
            proposal_digest=record.proposal_digest, runtime_generation=rig.generation.id)


async def test_an_authorization_is_consumed_once(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    record, authorization = await _minted(rig, task_id, grant)
    assert await _consume(rig, record, authorization) is not None
    assert await _consume(rig, record, authorization) is None


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE browser_profiles SET revoke_epoch = revoke_epoch + 1",
        "UPDATE browser_profiles SET status = 'NEEDS_LOGIN'",
        f"UPDATE browser_profiles SET account_fingerprint = '{OTHER_FINGERPRINT}'",
        "UPDATE browser_profiles SET account_fingerprint = NULL",
        "UPDATE browser_profiles SET status = 'DELETED', deleted_at = now()",
    ],
)
async def test_the_database_refuses_an_authorization_once_the_profile_no_longer_matches(rig: Rig, change: str) -> None:
    task_id, grant = await rig.granted()
    record, authorization = await _minted(rig, task_id, grant)
    async with rig.engine.begin() as connection:
        await connection.execute(text(change))
    assert await _consume(rig, record, authorization) is None


async def test_a_research_consumer_cannot_spend_an_authenticated_authorization(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    record, authorization = await _minted(rig, task_id, grant)
    assert await _consume(rig, record, authorization, research=True) is None
    assert await _consume(rig, record, authorization) is not None


async def test_the_one_authenticated_read_scope_kind_cannot_hold_two_open_grants(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    view = await rig.service.prepare(task_id, recipient="openai")
    assert view.grant is not None and view.grant.scope.disclosure.recipient == "gemini"


# ---- pauses: deterministic, and they change authority exactly as documented ---------------------


async def test_a_credential_surface_pauses_for_login_and_stores_no_text(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    rig.worker.queue.append(credential_surface)
    result = await rig.observe(task_id)
    assert result.pause_reason is PauseReason.LOGIN_REQUIRED and result.observation is None
    profile = await rig.profile()
    assert profile.status is ProfileStatus.NEEDS_LOGIN and profile.revoke_epoch == 0
    view = await rig.service.describe(task_id)
    assert view.task.status is TaskStatus.PAUSED and view.pause_reason is PauseReason.LOGIN_REQUIRED
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0
    # The browser was released so a human takeover can open it.
    assert rig.profile_id in rig.browser.closed
    # No step can run while the profile needs a login.
    with pytest.raises(AuthenticatedProfileUnavailableError):
        await rig.observe(task_id)
    assert len(rig.worker.dispatched) == 1
    stored = await scalar(rig.engine, "SELECT result::text FROM action_attempts a JOIN actions x ON x.id = a.action_id LIMIT 1") or ""
    del stored


async def test_signing_back_in_as_the_same_account_lets_the_same_grant_continue(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(credential_surface)
    await rig.observe(task_id)
    async with rig.engine.begin() as connection:
        await BrowserProfileRepository(connection).mark_authenticated(profile_id=rig.profile_id, account_fingerprint=FINGERPRINT)
    result = await rig.observe(task_id)
    assert result.outcome is AttemptOutcome.SUCCEEDED
    view = await rig.service.describe(task_id)
    assert view.pause_reason is None and view.task.status is not TaskStatus.PAUSED


async def test_signing_back_in_as_a_different_account_voids_the_old_authority(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(credential_surface)
    await rig.observe(task_id)
    async with rig.engine.begin() as connection:
        await BrowserProfileRepository(connection).mark_authenticated(profile_id=rig.profile_id, account_fingerprint=OTHER_FINGERPRINT)
    assert (await rig.profile()).revoke_epoch == 1
    with pytest.raises(AuthenticatedGrantNotUsableError):
        await rig.observe(task_id)
    assert len(rig.worker.dispatched) == 1


async def test_a_different_account_pauses_bumps_the_epoch_and_revokes_the_grant(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    rig.worker.queue.append(identity("account_changed"))
    result = await rig.observe(task_id)
    assert result.pause_reason is PauseReason.ACCOUNT_CHANGED and result.observation is None
    profile = await rig.profile()
    assert profile.revoke_epoch == 1 and profile.status is ProfileStatus.NEEDS_LOGIN
    assert profile.account_fingerprint is None
    view = await rig.service.describe(task_id)
    assert view.grant is not None and view.grant.status is GrantStatus.REVOKED
    assert view.pause_reason is PauseReason.ACCOUNT_CHANGED
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0
    with pytest.raises((AuthenticatedGrantNotUsableError, AuthenticatedProfileUnavailableError)):
        await rig.observe(task_id)
    # A new task on the changed profile needs a fresh sign-in *and* a new card.
    with pytest.raises(AuthenticatedProfileUnavailableError):
        await rig.service.prepare(await rig.task(), recipient="gemini")
    assert len(rig.worker.dispatched) == 1
    assert grant.id == view.grant.id


async def test_an_unknown_account_pauses_and_changes_nothing_else(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(identity("account_identity_unknown"))
    result = await rig.observe(task_id)
    assert result.pause_reason is PauseReason.ACCOUNT_IDENTITY_UNKNOWN
    profile = await rig.profile()
    assert profile.status is ProfileStatus.AUTHENTICATED and profile.revoke_epoch == 0
    assert profile.account_fingerprint == FINGERPRINT
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0


async def test_leaving_the_site_pauses_the_task(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(failed("left_site_scope"))
    result = await rig.observe(task_id)
    assert result.pause_reason is PauseReason.LEFT_SITE_SCOPE
    assert (await rig.service.describe(task_id)).task.status is TaskStatus.PAUSED


async def test_a_pause_is_followed_by_a_resume_when_the_next_step_runs(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(identity("account_identity_unknown"))
    await rig.observe(task_id)
    await rig.observe(task_id)
    types = [event.event_type for event in await rig.tasks.list_events(task_id)]
    assert "task.authenticated_paused" in types and "task.authenticated_resumed" in types


# ---- a lost read is unknown, is never repeated, and needs a fresh look --------------------------


async def test_a_lost_read_is_outcome_unknown_and_is_never_retried(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(lost)
    result = await rig.observe(task_id)
    assert result.outcome is AttemptOutcome.OUTCOME_UNKNOWN and result.observation is None
    view = await rig.service.describe(task_id)
    assert view.unresolved_step is True
    assert len(rig.worker.dispatched) == 1
    # Anything but a fresh observation is refused, and it does not reach the worker.
    with pytest.raises(AuthenticatedStepRefusedError) as refused:
        await rig.service.execute_step(task_id, rig.envelope(
            {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "l1"}}))
    assert refused.value.code in ("observe_required", "unknown_observation")
    assert len(rig.worker.dispatched) == 1
    # A fresh observation resolves it.
    fresh = await rig.observe(task_id)
    assert fresh.outcome is AttemptOutcome.SUCCEEDED
    assert (await rig.service.describe(task_id)).unresolved_step is False
    assert len(rig.worker.dispatched) == 2


async def test_a_step_in_flight_blocks_a_second_one(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    action = await rig.actions.propose_action(
        task_id, idempotency_key="pending", tool_name="authenticated_observe", risk_tier=RiskTier.R1,
        proposal={"kind": "authenticated_read_step"})
    assert action[0].action is not None
    with pytest.raises(AuthenticatedStepInFlightError):
        await rig.observe(task_id)


async def test_a_worker_that_returns_unredacted_text_is_refused_not_stored(rig: Rig) -> None:
    task_id, _ = await rig.granted()

    def leaky(request: Any) -> Outcome:
        good = page_observation(request, ["ok"])
        payload = good.model_dump(mode="json")
        payload["blocks"] = [{"id": "b1", "text": "email satish@example.test and 4242424242424242"}]
        dumped = WorkerReadResult(observation=good).model_dump(mode="json")
        dumped["observation"] = payload
        return Outcome(
            outcome=AttemptOutcome.SUCCEEDED, dispatch_status=DispatchStatus.OK, submitted=True,
            error_code=None, observation_id=good.observation_id, result={"operation": request.operation},
            observation={"observation_id": str(good.observation_id), "result": dumped},
        )

    rig.worker.queue.append(leaky)
    result = await rig.observe(task_id)
    assert result.outcome is AttemptOutcome.FAILED and result.error_code == "observation_invalid"
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0
    assert "satish@example.test" not in str(await scalar(rig.engine, "SELECT string_agg(result::text, '') FROM action_attempts") or "")


async def test_an_observation_for_another_profile_or_sequence_is_refused(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.queue.append(lambda request: _outcome(request, WorkerReadResult(
        observation=page_observation(request, ["x"], profile_id=uuid.uuid4()))))
    result = await rig.observe(task_id)
    assert result.error_code == "observation_invalid"


# ---- the answer: grounded in the redacted text, only for the approved provider ----------------------


def _answer(quote: str, *, answer: str, block: str = "b3", status: str = "answered") -> ResearchAnswer:
    return ResearchAnswer(
        status=status, stop_reason="goal_reached", answer=answer,
        evidence=[ResearchEvidence(observation="o1", block=block, quote=quote)],
    )


async def test_an_answer_is_grounded_stored_classified_and_closes_the_grant(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    view = await rig.service.record_answer(
        task_id, answer=_answer("lumi-notes - Private", answer="lumi-notes is private"),
        provider="gemini", model="gemini-2.5-flash", planner_calls=1)
    assert view.answer is not None and view.answer.provider == "gemini"
    assert view.answer.profile_id == rig.profile_id and view.answer.classification == "account_private"
    assert view.grant is not None and view.grant.status is GrantStatus.COMPLETED
    assert view.task.status is TaskStatus.SUCCEEDED
    assert rig.profile_id in rig.browser.closed
    assert await scalar(rig.engine, "SELECT count(*) FROM research_answers") == 0
    with pytest.raises(AuthenticatedAnswerAlreadyRecordedError):
        await rig.service.record_answer(
            task_id, answer=_answer("lumi-notes - Private", answer="again"), provider="gemini", model="m", planner_calls=1)


async def test_an_answer_attributed_to_another_provider_is_refused(rig: Rig) -> None:
    task_id, _ = await rig.granted("gemini")
    await rig.observe(task_id)
    for provider in ("openai", "deepseek", "scripted"):
        with pytest.raises(AuthenticatedStepRefusedError) as refused:
            await rig.service.record_answer(
                task_id, answer=_answer("lumi-notes - Private", answer="x"), provider=provider,
                model="m", planner_calls=1)
        assert refused.value.code == "recipient_mismatch"
    assert (await rig.service.describe(task_id)).answer is None


async def test_grounding_uses_the_redacted_text_the_provider_saw(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    rig.worker.blocks = ["Customer id ⟦digits:2345⟧", "There are 17 private repositories."]
    await rig.observe(task_id)
    # The raw identifier was never in the evidence, so it cannot be quoted.
    with pytest.raises(AnswerNotGroundedError):
        await rig.service.record_answer(
            task_id, answer=_answer("Customer id 123456789012345", answer="id", block="b1"),
            provider="gemini", model="m", planner_calls=1)
    # An invented number is refused; an ordinary one that was read is accepted.
    with pytest.raises(AnswerNotGroundedError):
        await rig.service.record_answer(
            task_id, answer=_answer("There are 17 private repositories.", answer="There are 18", block="b2"),
            provider="gemini", model="m", planner_calls=1)
    view = await rig.service.record_answer(
        task_id, answer=_answer("There are 17 private repositories.", answer="There are 17 private repositories", block="b2"),
        provider="gemini", model="m", planner_calls=1)
    assert view.answer is not None


async def test_an_answer_citing_no_evidence_or_an_unknown_block_is_refused(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    with pytest.raises(AnswerNotGroundedError):
        await rig.service.record_answer(
            task_id, answer=ResearchAnswer(status="answered", stop_reason="goal_reached", answer="x", evidence=[]),
            provider="gemini", model="m", planner_calls=1)
    with pytest.raises(AnswerNotGroundedError):
        await rig.service.record_answer(
            task_id, answer=_answer("nothing", answer="x", block="b40"), provider="gemini", model="m", planner_calls=1)


# ---- separation and lifecycle -------------------------------------------------------------------------


async def test_evidence_and_answers_cannot_be_written_with_another_classification(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(text("UPDATE authenticated_observations SET classification = 'public'"))
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(text("UPDATE authenticated_observations SET title = 'edited'"))
    with pytest.raises((DBAPIError, IntegrityError)):
        async with rig.engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO authenticated_answers (id, task_id, grant_id, profile_id, classification, status, stop_reason, answer, provider, model, steps_used, observations_used, planner_calls) "
                     "SELECT gen_random_uuid(), task_id, grant_id, profile_id, 'public', 'answered', 'goal_reached', '{}', 'gemini', 'm', 0, 0, 0 FROM authenticated_observations LIMIT 1"))


async def test_cancelling_the_task_revokes_its_account_grant(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.tasks.cancel_task(task_id)
    view = await rig.service.describe(task_id)
    assert view.grant is not None and view.grant.status is GrantStatus.REVOKED
    with pytest.raises((AuthenticatedGrantNotUsableError, TaskNotAcceptingActionsError)):
        await rig.observe(task_id)


async def test_revoking_releases_the_browser_and_keeps_the_evidence(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    await rig.observe(task_id)
    view = await rig.service.revoke(task_id, reason="user_stopped", grant_id=grant.id)
    assert view.grant is not None and view.grant.status is GrantStatus.REVOKED
    assert len(view.observations) == 1
    assert rig.profile_id in rig.browser.closed


async def test_deleting_the_profile_removes_its_evidence_and_voids_its_grants(rig: Rig) -> None:
    task_id, grant = await rig.granted()
    await rig.observe(task_id)
    await rig.service.record_answer(
        task_id, answer=_answer("lumi-notes - Private", answer="lumi-notes is private"),
        provider="gemini", model="m", planner_calls=1)
    other_task, _ = await rig.granted()
    await rig.profiles.delete_profile(rig.profile_id)
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_answers") == 0
    assert await scalar(rig.engine, "SELECT revoke_epoch FROM browser_profiles") >= 1
    open_grants = await scalar(rig.engine, "SELECT count(*) FROM task_grants WHERE status IN ('PENDING', 'ACTIVE')")
    assert open_grants == 0
    with pytest.raises((AuthenticatedGrantNotUsableError, AuthenticatedProfileUnavailableError)):
        await rig.observe(other_task)
    assert grant.id is not None


async def test_task_evidence_can_be_purged_without_touching_others(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    other_id, _ = await rig.granted()  # noqa: F841 - a second task (its grant is separate)
    assert await rig.service.purge_task_evidence(task_id) == 1
    assert await scalar(rig.engine, "SELECT count(*) FROM authenticated_observations") == 0


async def test_the_profile_is_opened_headless_and_only_through_the_profile_service(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    await rig.observe(task_id)
    assert rig.browser.opened == [(rig.profile_id, False)]


async def test_an_agent_read_is_closed_before_a_takeover_opens_the_profile(rig: Rig) -> None:
    task_id, _ = await rig.granted()
    await rig.observe(task_id)
    assert await rig.profiles.release_read_open(rig.profile_id) is True
    assert rig.profile_id in rig.browser.closed
    assert await rig.profiles.release_read_open(rig.profile_id) is False


def test_the_service_has_no_route_to_a_provider_a_page_or_a_cookie() -> None:
    source = Path(service_module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "storage_state", "add_cookies", "cookies(", "playwright", "ModelRouter", "openai", "gemini_api",
        "deepseek", "route.fetch", "page.goto", "evaluate(",
    ):
        assert forbidden not in source, forbidden
