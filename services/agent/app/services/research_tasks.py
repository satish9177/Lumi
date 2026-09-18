"""Milestone 7b: the durable authority for one public web research task.

The shape of the milestone, in one place:

    task (public_research: an objective the user typed)     -- created by main
      -> prepare: build the bounded scope and open a PENDING grant
         (the trusted card's contents; it authorises nothing)
      -> trusted click: confirm -> the grant becomes ACTIVE and expiring
      -> for each step main's planner chooses:
           validate against the grant and the budgets
           resolve the semantic ref to an address, from Lumi's own records
           bind the worker, ensure the task-owned session
           create the action, mint and consume a single-use authorization,
           persist the attempt and the dispatch
           execute exactly one operation
           store the bounded observation with the finished attempt
      -> finish: record one answer, grounded in those observations, and
         complete the grant

Where the authority sits, stated once:

* **The planner proposes; this module authorises.** A step arrives as a
  validated member of a closed union. Whether it may run is decided here, by
  code, from the persisted grant -- never from anything the planner said, and
  never from anything a web page said.
* **Refs resolve from Lumi's records, not from the model.** `l5 of o3` becomes
  an address by looking it up in the observation Lumi stored. The worker
  independently looks it up in its own table for the same document. The model
  never sees an address, so it cannot supply one.
* **One operation at a time.** A task with an unresolved research action
  refuses the next step. There is no queue and no parallel browsing.
* **Nothing is retried blindly.** A lost answer leaves the action
  OUTCOME_UNKNOWN; the way forward is to re-observe the tab and plan again,
  because the browser may have moved even though nothing consequential
  happened. A public read has no external effect to reconcile.
* **Budgets stop loops.** Steps, observations, planner calls, tabs and active
  time are all counted here, from the database, and reaching one ends the task
  with an honest partial answer rather than another step.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.browser.errors import (
    BrowserWorkerError,
    BrowserWorkerRejectedError,
)
from app.browser.protocol import DispatchRequest, SessionRequest
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.errors import (
    ResearchAnswerAlreadyRecordedError,
    ResearchBudgetExhaustedError,
    ResearchGrantNotFoundError,
    ResearchGrantNotUsableError,
    ResearchNotConfiguredError,
    ResearchSessionUnavailableError,
    ResearchStepInFlightError,
    ResearchStepRefusedError,
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.public_url import PublicUrlPolicy, UrlPolicyError
from app.domain.research import (
    BROWSER_OPERATIONS,
    PUBLIC_RESEARCH_SITE,
    PUBLIC_RESEARCH_TASK_TYPE,
    RESEARCH_TOOL_NAMES,
    TOOL_NAMES,
    GrantStatus,
    LinkTarget,
    NavigateStep,
    ResearchAnswer,
    ResearchBudgets,
    ResearchDisclosure,
    ResearchObservation,
    ResearchOperation,
    ResearchProposal,
    ResearchScope,
    ResearchStep,
    ResearchStepEnvelope,
    ResultTarget,
    SearchResult,
    SeedTarget,
    TabStep,
    compute_content_hash,
    extract_seeds,
    operation_of,
    parse_objective,
    parse_search_query,
    verify_research_grounding,
)
from app.domain.task_status import TaskEventType, accepts_actions
from app.repositories.actions import ActionRecord, ActionRepository, AttemptRecord
from app.repositories.browser import BrowserRepository
from app.repositories.research import (
    AnswerRecord,
    GrantRecord,
    ObservationRecord,
    ResearchRepository,
    SessionRecord,
)
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.actions import ActionService, ActionView
from app.services.browser_execution import (
    Outcome,
    WorkerSource,
    dispatch_and_classify,
    open_worker_client,
)
from app.services.research_search import (
    SearchFailedError,
    SearchNotConfiguredError,
    SearchProvider,
)
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.research")

#: Everything a public-research task request may carry. Provenance fields are
#: written by the trusted desktop layer, never by a model.
REQUEST_KEYS = frozenset(
    {"type", "text", "objective", "source", "request_id", "voice_turn_id"}
)

#: Action statuses that mean a step of this task is not finished with.
UNRESOLVED_STATUSES = frozenset(
    {
        ActionStatus.PROPOSED,
        ActionStatus.WAITING_APPROVAL,
        ActionStatus.APPROVED,
        ActionStatus.AUTHORIZED,
        ActionStatus.EXECUTING,
        ActionStatus.OUTCOME_UNKNOWN,
        ActionStatus.RECONCILING,
    }
)


def validate_request(request: dict[str, Any]) -> str:
    """Refuse to store a research task that could never be worked on."""
    if not set(request) <= REQUEST_KEYS:
        raise ValueError("unknown request field")
    return parse_objective(request.get("objective"))


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int
    observations: int
    planner_calls: int
    active_seconds: float
    tabs: int


@dataclass(frozen=True, slots=True)
class ResearchView:
    """Everything the desktop needs to draw the research task. No addresses
    beyond the pages Lumi actually visited, which are its cited sources."""

    task: TaskRecord
    objective: str
    grant: GrantRecord | None
    session: SessionRecord | None
    observations: tuple[ObservationRecord, ...]
    answer: AnswerRecord | None
    usage: BudgetUsage
    search_configured: bool
    #: True when a step of this task has no outcome Lumi can stand behind --
    #: it is EXECUTING, or it ended OUTCOME_UNKNOWN. The task refuses further
    #: steps while that is true, and the desktop says so rather than showing
    #: progress that is not happening.
    unresolved_step: bool


@dataclass(frozen=True, slots=True)
class StepResult:
    view: ResearchView
    action: ActionView
    observation: ObservationRecord | None
    outcome: AttemptOutcome
    error_code: str | None
    #: True when this `request_id` had already been executed and nothing new
    #: happened. A duplicated planner request is a no-op, not a second step.
    replayed: bool


class _GrantAuthorizer:
    """Mints and consumes one step authorization for `start_scoped_attempt`."""

    def __init__(
        self,
        *,
        grant: GrantRecord,
        task_id: uuid.UUID,
        runtime_generation: uuid.UUID,
        ttl: timedelta,
    ) -> None:
        self._grant = grant
        self._task_id = task_id
        self._runtime_generation = runtime_generation
        self._ttl = ttl

    def authorization_payload(self) -> dict[str, Any]:
        return {
            "grant_id": str(self._grant.id),
            "grant_revision": self._grant.revision,
            "scope_digest": self._grant.scope_digest,
            "policy_version": self._grant.policy_version,
            "authorization": "task_grant",
        }

    async def mint(self, connection: AsyncConnection, *, action: ActionRecord) -> uuid.UUID:
        record = await ResearchRepository(connection).insert_step_authorization(
            authorization_id=uuid.uuid4(),
            grant=self._grant,
            task_id=self._task_id,
            action_id=action.id,
            action_revision=action.revision,
            proposal_digest=action.proposal_digest,
            runtime_generation=self._runtime_generation,
            ttl=self._ttl,
        )
        return record.id

    async def consume(
        self,
        connection: AsyncConnection,
        *,
        authorization_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
    ) -> bool:
        claimed = await ResearchRepository(connection).consume_step_authorization(
            authorization_id=authorization_id,
            action_revision=action_revision,
            proposal_digest=proposal_digest,
            runtime_generation=self._runtime_generation,
        )
        return claimed is not None


class ResearchService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        tasks: TaskService,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
        policy: PublicUrlPolicy,
        search: SearchProvider,
        grant_ttl_seconds: int,
        step_ttl_seconds: int,
        max_tabs: int = 5,
    ) -> None:
        self._engine = engine
        self._tasks = tasks
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._policy = policy
        self._search = search
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        self._step_ttl = timedelta(seconds=step_ttl_seconds)
        self._max_tabs = max_tabs

    @property
    def policy(self) -> PublicUrlPolicy:
        return self._policy

    @property
    def is_configured(self) -> bool:
        return self._policy.configured and self._worker is not None

    # ---- reading -------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> ResearchView:
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> ResearchView:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != PUBLIC_RESEARCH_TASK_TYPE:
            raise TaskKindMismatchError(task_id, PUBLIC_RESEARCH_TASK_TYPE)
        repository = ResearchRepository(connection)
        grant = await repository.open_grant_for_task(task_id)
        if grant is None:
            grant = await repository.latest_grant_for_task(task_id)
        observations = tuple(await repository.list_observations(task_id))
        session = await repository.open_session_for_task(task_id)
        unresolved = any(
            action.tool_name in RESEARCH_TOOL_NAMES and action.status in UNRESOLVED_STATUSES
            for action in await ActionRepository(connection).list_actions(task_id, limit=500)
        )
        usage = BudgetUsage(
            steps=await repository.count_steps(task_id),
            observations=len(observations),
            planner_calls=grant.planner_calls if grant else 0,
            active_seconds=(await repository.active_seconds(grant.id)) if grant else 0.0,
            tabs=len(observations[-1].observation.open_tabs) if observations else 0,
        )
        return ResearchView(
            task=task,
            objective=str(task.request.get("objective", "")),
            grant=grant,
            session=session,
            observations=observations,
            answer=await repository.get_answer(task_id),
            usage=usage,
            search_configured=self._search.configured,
            unresolved_step=unresolved,
        )

    # ---- the scope card ------------------------------------------------------

    def build_scope(
        self, *, objective: str, disclosure: ResearchDisclosure, budgets: ResearchBudgets
    ) -> ResearchScope:
        """The bounded scope the trusted card shows, word for word.

        The operation list is the whole capability. There is no entry for
        login, typing, form submission, upload, download, file access,
        purchase, message or any non-GET request, and no field in which one
        could be added at run time.
        """
        allowed = [
            ResearchOperation.NAVIGATE,
            ResearchOperation.OBSERVE,
            ResearchOperation.SCROLL,
            ResearchOperation.HISTORY,
            ResearchOperation.TAB,
        ]
        if self._search.configured:
            allowed.insert(0, ResearchOperation.SEARCH)
        hosts: Any = "any_public"
        if not self._policy.allow_any_public_host:
            hosts = sorted({*self._policy.allowed_hosts, *self._policy.test_origins})
        schemes: list[Any] = ["https"]
        if self._policy.test_origins:
            schemes.append("http_test_origin")
        return ResearchScope(
            policy_version=self._policy.version,
            allowed_operations=allowed,
            schemes=schemes,
            hosts=hosts,
            budgets=budgets.model_copy(update={"max_tabs": min(budgets.max_tabs, self._max_tabs)}),
            disclosure=disclosure,
            seeds=[
                seed
                for seed in extract_seeds(objective)
                if self._seed_is_allowed(seed)
            ],
        )

    def _seed_is_allowed(self, seed: str) -> bool:
        try:
            self._policy.check(seed)
        except UrlPolicyError:
            return False
        return True

    async def prepare(
        self, task_id: uuid.UUID, *, disclosure: ResearchDisclosure, budgets: ResearchBudgets
    ) -> ResearchView:
        """Open the PENDING grant. Nothing is searched, opened or read."""
        if not self._policy.configured:
            raise ResearchNotConfiguredError()
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != PUBLIC_RESEARCH_TASK_TYPE:
                raise TaskKindMismatchError(task_id, PUBLIC_RESEARCH_TASK_TYPE)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            repository = ResearchRepository(connection)
            existing = await repository.open_grant_for_task(task_id)
            if existing is not None:
                # A duplicate request shows the same card, and an active scope
                # is never replaced by a fresh one behind the user's back.
                return await self._view(connection, task_id)
            objective = parse_objective(task.request.get("objective"))
            scope = self.build_scope(
                objective=objective, disclosure=disclosure, budgets=budgets
            )
            grant = await repository.insert_grant(
                grant_id=uuid.uuid4(), task_id=task_id, scope=scope
            )
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_RESEARCH_SCOPE_REQUESTED,
                payload={
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "policy_version": grant.policy_version,
                    "allowed_operations": [
                        operation.value for operation in scope.allowed_operations
                    ],
                    "seed_count": len(scope.seeds),
                },
            )
            return await self._view(connection, task_id)

    async def confirm(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> ResearchView:
        """The trusted click. The *only* way a grant becomes usable.

        There is no other caller, no model-reachable route, and no spoken
        command that reaches this method: voice and typed text can create the
        task and focus the card, and that is all.
        """
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            repository = ResearchRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise ResearchGrantNotFoundError(task_id)
            if grant.status is not GrantStatus.PENDING:
                raise ResearchGrantNotUsableError(f"it is {grant.status.value.lower()}")
            if grant.revision != expected_revision:
                raise ResearchGrantNotUsableError("it changed since you reviewed it")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id,
                expected_revision=expected_revision,
                scope_digest=grant.scope_digest,
                ttl=self._grant_ttl,
            )
            if confirmed is None:
                raise ResearchGrantNotUsableError("it changed since you reviewed it")
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_RESEARCH_SCOPE_GRANTED,
                payload={
                    "grant_id": str(confirmed.id),
                    "grant_revision": confirmed.revision,
                    "grant_status": confirmed.status.value,
                    "scope_digest": confirmed.scope_digest,
                    "policy_version": confirmed.policy_version,
                    "expires_at": confirmed.expires_at.isoformat()
                    if confirmed.expires_at
                    else None,
                },
            )
            view = await self._view(connection, task_id)
        logger.info(
            "research scope granted",
            extra={"task_id": str(task_id), "grant_id": str(grant_id)},
        )
        return view

    async def revoke(
        self,
        task_id: uuid.UUID,
        *,
        reason: str,
        grant_id: uuid.UUID | None = None,
        expected_revision: int | None = None,
        status: GrantStatus = GrantStatus.REVOKED,
    ) -> ResearchView:
        """Withdraw the scope and drop the browser session. Not reversible."""
        session: SessionRecord | None = None
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            repository = ResearchRepository(connection)
            grant = (
                await repository.get_grant(grant_id)
                if grant_id is not None
                else await repository.open_grant_for_task(task_id)
            )
            if grant is None or grant.task_id != task_id:
                raise ResearchGrantNotFoundError(task_id)
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(
                    grant_id=grant.id, status=status, expected_revision=expected_revision
                )
                if closed is None:
                    raise ResearchGrantNotUsableError("it changed since you reviewed it")
            else:
                closed = grant
            session = await repository.open_session_for_task(task_id)
            if session is not None:
                await repository.close_session(session_id=session.id, status="CLOSED")
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_RESEARCH_SCOPE_REVOKED,
                payload={
                    "grant_id": str(closed.id),
                    "grant_revision": closed.revision,
                    "grant_status": closed.status.value,
                    "reason": reason,
                },
            )
        # Outside the transaction: telling the worker to drop its context is
        # best effort, and a worker that never hears it loses the context with
        # its own process anyway.
        if session is not None:
            await self._close_worker_session(session)
        return await self.describe(task_id)

    # ---- one step ------------------------------------------------------------

    async def execute_step(
        self, task_id: uuid.UUID, envelope: ResearchStepEnvelope
    ) -> StepResult:
        """Validate, authorise, persist, execute and observe exactly one step."""
        step = envelope.step
        operation = operation_of(step)
        async with self._engine.connect() as connection:
            view = await self._view(connection, task_id)
            repository = ResearchRepository(connection)
            grant = await self._usable_grant(repository, view)
            await self._check_no_unresolved_step(connection, task_id)
            replay = await self._replayed_step(connection, task_id, envelope.request_id)
        if replay is not None:
            return replay
        if not grant.scope.permits(operation):
            raise ResearchStepRefusedError("outside_scope")
        self._check_budgets(view, grant, planner_calls=envelope.planner_calls)

        if operation is ResearchOperation.SEARCH:
            return await self._execute_search(task_id, envelope, grant=grant, view=view)
        if operation not in BROWSER_OPERATIONS:  # pragma: no cover - the union is closed.
            raise ResearchStepRefusedError("unsupported_operation")
        return await self._execute_browser_step(task_id, envelope, grant=grant, view=view)

    async def _usable_grant(
        self, repository: ResearchRepository, view: ResearchView
    ) -> GrantRecord:
        grant = view.grant
        if grant is None:
            raise ResearchGrantNotFoundError(view.task.id)
        if grant.status is GrantStatus.PENDING:
            # The card is on screen and nobody has pressed anything.
            raise ResearchGrantNotUsableError("it has not been granted")
        if grant.status is not GrantStatus.ACTIVE:
            raise ResearchGrantNotUsableError(f"it is {grant.status.value.lower()}")
        if await repository.grant_is_expired(grant.id):
            raise ResearchGrantNotUsableError("it has expired")
        if not accepts_actions(view.task.status):
            raise TaskNotAcceptingActionsError(view.task.id, view.task.status)
        return grant

    async def _check_no_unresolved_step(
        self, connection: AsyncConnection, task_id: uuid.UUID
    ) -> None:
        for action in await ActionRepository(connection).list_actions(task_id, limit=500):
            if action.tool_name in RESEARCH_TOOL_NAMES and action.status in UNRESOLVED_STATUSES:
                raise ResearchStepInFlightError(action.id)

    async def _replayed_step(
        self, connection: AsyncConnection, task_id: uuid.UUID, request_id: str
    ) -> StepResult | None:
        """A request id that already executed returns its own stored result."""
        existing = await ActionRepository(connection).get_action_by_idempotency_key(
            task_id=task_id, idempotency_key=_idempotency_key(request_id)
        )
        if existing is None:
            return None
        repository = ResearchRepository(connection)
        observation = next(
            (
                record
                for record in await repository.list_observations(task_id)
                if record.action_id == existing.id
            ),
            None,
        )
        attempts = await ActionRepository(connection).list_attempts(existing.id)
        last = attempts[-1] if attempts else None
        return StepResult(
            view=await self._view(connection, task_id),
            action=ActionView(action=existing, approval=None, attempts=tuple(attempts)),
            observation=observation,
            outcome=(last.outcome if last and last.outcome else AttemptOutcome.OUTCOME_UNKNOWN),
            error_code=last.error_code if last else None,
            replayed=True,
        )

    def _check_budgets(
        self, view: ResearchView, grant: GrantRecord, *, planner_calls: int
    ) -> None:
        budgets = grant.scope.budgets
        usage = view.usage
        if usage.steps >= budgets.max_steps:
            raise ResearchBudgetExhaustedError("max_steps")
        if usage.observations >= budgets.max_observations:
            raise ResearchBudgetExhaustedError("max_observations")
        if max(planner_calls, usage.planner_calls) > budgets.max_planner_calls:
            raise ResearchBudgetExhaustedError("max_planner_calls")
        if usage.active_seconds > budgets.max_active_seconds:
            raise ResearchBudgetExhaustedError("max_active_seconds")

    # ---- search --------------------------------------------------------------

    async def _execute_search(
        self,
        task_id: uuid.UUID,
        envelope: ResearchStepEnvelope,
        *,
        grant: GrantRecord,
        view: ResearchView,
    ) -> StepResult:
        query = parse_search_query(getattr(envelope.step, "query", None))
        async with self._engine.connect() as connection:
            sequence = await ResearchRepository(connection).next_sequence(task_id)
            step_number = view.usage.steps + 1
        proposal = ResearchProposal(
            operation=ResearchOperation.SEARCH,
            step_number=step_number,
            policy_version=grant.policy_version,
            grant_id=grant.id,
            scope_digest=grant.scope_digest,
            step={"operation": ResearchOperation.SEARCH.value, "query": query},
        )
        action_view, created = await self._start(task_id, grant, proposal, envelope)
        if not created:  # pragma: no cover - the replay check ran first.
            raise ResearchStepInFlightError(action_view.action.id)
        attempt = next(a for a in action_view.attempts if a.finished_at is None)
        await self._record_step_start(grant, envelope.planner_calls)

        observation_id = uuid.uuid4()
        try:
            results = await self._search.search(query)
        except SearchNotConfiguredError:
            return await self._finish_failure(
                task_id, action_view, error_code="search_not_configured"
            )
        except SearchFailedError as error:
            return await self._finish_failure(task_id, action_view, error_code=error.code)

        refs = [
            SearchResult(
                id=f"r{index + 1}", title=result.title, host=result.host, snippet=result.snippet
            )
            for index, result in enumerate(results)
        ]
        targets = {f"r{index + 1}": result.url for index, result in enumerate(results)}
        observation = ResearchObservation(
            observation_id=observation_id,
            kind="search_results",
            operation=ResearchOperation.SEARCH,
            sequence=sequence,
            query=query,
            observed_at=_now(),
            results=refs,
            content_hash=compute_content_hash(
                kind="search_results", final_url="", title="", blocks=[], links=[], results=refs
            ),
        )
        return await self._finish_success(
            task_id,
            action_view,
            attempt=attempt,
            grant=grant,
            observation=observation,
            targets=targets,
            dispatch_id=None,
            session_id=None,
            worker_generation=None,
            result={"operation": TOOL_NAMES[ResearchOperation.SEARCH], "result_count": len(refs)},
        )

    # ---- a browser step ------------------------------------------------------

    async def _execute_browser_step(
        self,
        task_id: uuid.UUID,
        envelope: ResearchStepEnvelope,
        *,
        grant: GrantRecord,
        view: ResearchView,
    ) -> StepResult:
        step = envelope.step
        operation = operation_of(step)
        # Resolve the ref first: a refused ref costs nothing and tells the
        # planner something useful, and no browser has been contacted yet.
        destination = await self._resolve_destination(task_id=task_id, grant=grant, step=step)
        if isinstance(step, TabStep) and step.action == "open":
            self._check_tab_budget(view, grant)
        if self._worker is None:
            raise ResearchNotConfiguredError()
        client = await open_worker_client(self._worker, self._runtime_generation)
        try:
            worker_generation = await self._bind_worker(client)
            session = await self._ensure_session(
                client, worker_generation=worker_generation, task_id=task_id, grant=grant
            )
            if (
                destination.source_session_id is not None
                and destination.source_session_id != session.id
            ):
                # The ref was issued by a session that no longer exists (a
                # worker or runtime restart). Re-observe rather than guess.
                raise ResearchStepRefusedError("stale_session")
            async with self._engine.connect() as connection:
                sequence = await ResearchRepository(connection).next_sequence(task_id)
            proposal = ResearchProposal(
                operation=operation,
                step_number=view.usage.steps + 1,
                policy_version=grant.policy_version,
                grant_id=grant.id,
                scope_digest=grant.scope_digest,
                step=step.model_dump(mode="json"),
                destination_url=destination.url,
                destination_host=destination.host,
                session_id=session.id,
                tab=getattr(step, "tab", None),
                expected_document_epoch=destination.document_epoch,
                source_observation_id=destination.observation_id,
            )
            action_view, created = await self._start(task_id, grant, proposal, envelope)
            if not created:  # pragma: no cover - the replay check ran first.
                raise ResearchStepInFlightError(action_view.action.id)
            attempt = next(a for a in action_view.attempts if a.finished_at is None)
            await self._record_step_start(grant, envelope.planner_calls)

            dispatch_id = uuid.uuid4()
            try:
                async with self._engine.begin() as connection:
                    await BrowserRepository(connection).insert_dispatch(
                        dispatch_id=dispatch_id,
                        action_id=action_view.action.id,
                        attempt_id=attempt.id,
                        worker_generation=worker_generation,
                        operation=TOOL_NAMES[operation],
                        site=PUBLIC_RESEARCH_SITE,
                        effect=BrowserEffect.READ_ONLY,
                        session_id=session.id,
                    )
            except Exception:
                logger.exception("could not record the research dispatch")
                return await self._finish_failure(
                    task_id, action_view, error_code="dispatch_not_recorded"
                )
            # --- committed. Nothing below holds a transaction -----------------
            outcome = await dispatch_and_classify(
                client,
                request=DispatchRequest(
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    action_id=action_view.action.id,
                    attempt_id=attempt.id,
                    operation=TOOL_NAMES[operation],
                    site=PUBLIC_RESEARCH_SITE,
                    session_id=session.id,
                    input=_worker_input(step, sequence=sequence, destination=destination),
                ),
            )
        finally:
            await client.aclose()

        if _lost_session(outcome):
            await self._mark_session_stale(session)
        observation, outcome = _validated_observation(
            outcome, sequence=sequence, session_id=session.id
        )
        await self._close_dispatch(dispatch_id, outcome)
        if observation is None:
            return await self._finish_failure(
                task_id,
                action_view,
                error_code=outcome.error_code,
                outcome=outcome.outcome,
                result=_step_summary(outcome, None),
            )
        return await self._finish_success(
            task_id,
            action_view,
            attempt=attempt,
            grant=grant,
            observation=observation,
            targets=_targets_of(outcome),
            dispatch_id=dispatch_id,
            session_id=session.id,
            worker_generation=worker_generation,
            result=_step_summary(outcome, observation),
        )

    def _check_tab_budget(self, view: ResearchView, grant: GrantRecord) -> None:
        if view.usage.tabs >= grant.scope.budgets.max_tabs:
            raise ResearchBudgetExhaustedError("max_tabs")

    # ---- destinations --------------------------------------------------------

    async def _resolve_destination(
        self, *, task_id: uuid.UUID, grant: GrantRecord, step: ResearchStep
    ) -> "_Destination":
        """Turn a semantic ref into an address, from Lumi's own records.

        A model chose a ref. Only this method turns one into an address, and
        only from an observation Lumi stored, for the document that issued it,
        in the session that is live now. Nothing here reads a URL out of a
        planner's output, because there is nowhere in the step vocabulary for
        one to be.
        """
        if not isinstance(step, NavigateStep):
            return _Destination(None, None, None, None, None)
        target = step.target
        if isinstance(target, SeedTarget):
            seed = grant.scope.seed(target.ref)
            if seed is None:
                raise ResearchStepRefusedError("unknown_seed")
            checked = self._checked(seed)
            return _Destination(checked.url, checked.host, None, None, None)

        sequence = int(target.observation[1:])
        async with self._engine.connect() as connection:
            repository = ResearchRepository(connection)
            record = await repository.observation_by_sequence(
                task_id=task_id, sequence=sequence
            )
            if record is None:
                raise ResearchStepRefusedError("unknown_observation")
            newest = await repository.list_observations(task_id)
        address = record.targets.get(target.ref)
        if address is None:
            raise ResearchStepRefusedError("unknown_target_ref")
        if isinstance(target, ResultTarget):
            if record.observation.kind != "search_results":
                raise ResearchStepRefusedError("target_not_search_results")
            checked = self._checked(address)
            return _Destination(checked.url, checked.host, None, record.id, None)

        assert isinstance(target, LinkTarget)
        if record.observation.kind != "page":
            raise ResearchStepRefusedError("target_not_a_page")
        if record.observation.tab != step.tab:
            raise ResearchStepRefusedError("wrong_tab")
        epoch = record.observation.document_epoch
        if any(
            other.observation.tab == record.observation.tab
            and other.session_id == record.session_id
            and other.observation.document_epoch > epoch
            for other in newest
        ):
            raise ResearchStepRefusedError("stale_target_ref")
        checked = self._checked(address)
        return _Destination(checked.url, checked.host, epoch, record.id, record.session_id)

    def _checked(self, address: str) -> Any:
        try:
            return self._policy.check(address)
        except UrlPolicyError as error:
            raise ResearchStepRefusedError(error.code) from None

    # ---- the worker and its session -----------------------------------------

    async def _bind_worker(self, client: BrowserWorkerClient) -> uuid.UUID:
        identity = await client.identify()
        async with self._engine.begin() as connection:
            repository = BrowserRepository(connection)
            if await repository.get_worker_generation(identity.worker_generation) is None:
                from datetime import datetime

                await repository.register_worker_generation(
                    worker_generation=identity.worker_generation,
                    runtime_generation=self._runtime_generation,
                    worker_started_at=datetime.fromisoformat(identity.started_at),
                )
        return identity.worker_generation

    async def _ensure_session(
        self,
        client: BrowserWorkerClient,
        *,
        worker_generation: uuid.UUID,
        task_id: uuid.UUID,
        grant: GrantRecord,
    ) -> SessionRecord:
        """The task's live session, or a fresh one when the old one is gone.

        A session row from a different worker or runtime generation describes a
        context that no longer exists. It becomes STALE, and because every
        semantic ref is checked against the live session id, nothing issued
        under it can be followed.
        """
        async with self._engine.connect() as connection:
            existing = await ResearchRepository(connection).open_session_for_task(task_id)
        if existing is not None and (
            existing.worker_generation != worker_generation
            or existing.runtime_generation != self._runtime_generation
        ):
            await self._mark_session_stale(existing)
            existing = None
        session_id = existing.id if existing is not None else uuid.uuid4()
        try:
            await client.open_session(
                SessionRequest(
                    session_id=session_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                )
            )
        except BrowserWorkerRejectedError as error:
            raise ResearchSessionUnavailableError(_short(error.reason)) from None
        except BrowserWorkerError as error:
            raise ResearchSessionUnavailableError(error.code) from None
        if existing is not None:
            return existing
        async with self._engine.begin() as connection:
            return await ResearchRepository(connection).insert_session(
                session_id=session_id,
                task_id=task_id,
                grant_id=grant.id,
                worker_generation=worker_generation,
                runtime_generation=self._runtime_generation,
            )

    async def _mark_session_stale(self, session: SessionRecord) -> None:
        async with self._engine.begin() as connection:
            await ResearchRepository(connection).close_session(
                session_id=session.id, status="STALE"
            )

    async def _close_worker_session(self, session: SessionRecord) -> None:
        if self._worker is None:
            return
        try:
            client = await open_worker_client(self._worker, self._runtime_generation)
        except BrowserWorkerError:
            return
        try:
            await client.close_session(
                SessionRequest(
                    session_id=session.id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=session.worker_generation,
                )
            )
        except BrowserWorkerError:
            logger.info("could not close the research session in the worker")
        finally:
            await client.aclose()

    # ---- ledger plumbing -----------------------------------------------------

    async def _start(
        self,
        task_id: uuid.UUID,
        grant: GrantRecord,
        proposal: ResearchProposal,
        envelope: ResearchStepEnvelope,
    ) -> tuple[ActionView, bool]:
        authorizer = _GrantAuthorizer(
            grant=grant,
            task_id=task_id,
            runtime_generation=self._runtime_generation,
            ttl=self._step_ttl,
        )
        return await self._actions.start_scoped_attempt(
            task_id,
            tool_name=TOOL_NAMES[proposal.operation],
            idempotency_key=_idempotency_key(envelope.request_id),
            risk_tier=RiskTier.R1,
            proposal=proposal.model_dump(mode="json"),
            authorizer=authorizer,
        )

    async def _record_step_start(self, grant: GrantRecord, planner_calls: int) -> None:
        async with self._engine.begin() as connection:
            await ResearchRepository(connection).record_step_start(
                grant_id=grant.id, planner_calls=planner_calls
            )

    async def _close_dispatch(self, dispatch_id: uuid.UUID, outcome: Outcome) -> None:
        async with self._engine.begin() as connection:
            await BrowserRepository(connection).finish_dispatch(
                dispatch_id=dispatch_id,
                status=outcome.dispatch_status,
                submitted=False,
                observation_id=outcome.observation_id,
                error_code=outcome.error_code,
                duration_ms=outcome.result.get("duration_ms"),
                result=outcome.result,
            )

    async def _finish_failure(
        self,
        task_id: uuid.UUID,
        action_view: ActionView,
        *,
        error_code: str | None,
        outcome: AttemptOutcome = AttemptOutcome.FAILED,
        result: dict[str, Any] | None = None,
    ) -> StepResult:
        finished = await self._actions.finish_attempt(
            action_view.action.id, outcome=outcome, result=result, error_code=error_code
        )
        logger.info(
            "research step did not produce an observation",
            extra={
                "task_id": str(task_id),
                "action_id": str(action_view.action.id),
                "outcome": outcome.value,
                "error_code": error_code,
            },
        )
        return StepResult(
            view=await self.describe(task_id),
            action=finished,
            observation=None,
            outcome=outcome,
            error_code=error_code,
            replayed=False,
        )

    async def _finish_success(
        self,
        task_id: uuid.UUID,
        action_view: ActionView,
        *,
        attempt: AttemptRecord,
        grant: GrantRecord,
        observation: ResearchObservation,
        targets: dict[str, str],
        dispatch_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        worker_generation: uuid.UUID | None,
        result: dict[str, Any],
    ) -> StepResult:
        action_id = action_view.action.id
        stored: list[ObservationRecord] = []

        async def record(connection: AsyncConnection, finished: AttemptRecord) -> None:
            stored.append(
                await ResearchRepository(connection).insert_observation(
                    observation=observation,
                    task_id=task_id,
                    grant_id=grant.id,
                    action_id=action_id,
                    attempt_id=finished.id,
                    dispatch_id=dispatch_id,
                    session_id=session_id,
                    worker_generation=worker_generation,
                    targets=targets,
                )
            )

        finished = await self._actions.finish_attempt(
            action_id,
            outcome=AttemptOutcome.SUCCEEDED,
            result={
                **result,
                "observation_id": str(observation.observation_id),
                "observation_sequence": observation.sequence,
                "content_hash": observation.content_hash,
                **({"final_url": observation.final_url} if observation.final_url else {}),
            },
            record=record,
        )
        _ = attempt
        logger.info(
            "research step observed",
            extra={
                "task_id": str(task_id),
                "action_id": str(action_id),
                "operation": observation.operation.value,
                "observation_id": str(observation.observation_id),
                "sequence": observation.sequence,
                "session_id": str(session_id) if session_id else None,
            },
        )
        return StepResult(
            view=await self.describe(task_id),
            action=finished,
            observation=stored[0] if stored else None,
            outcome=AttemptOutcome.SUCCEEDED,
            error_code=None,
            replayed=False,
        )

    # ---- the answer ----------------------------------------------------------

    async def record_answer(
        self,
        task_id: uuid.UUID,
        *,
        answer: ResearchAnswer,
        provider: str,
        model: str,
        planner_calls: int,
    ) -> ResearchView:
        """Store the one grounded answer, then close the scope.

        Grounding is verified here, against this task's own observations, even
        though main verified it: this is the authority that stores it. An
        answer citing an observation from another task, a block that does not
        exist, a quote the block never showed, or a number no quote contains is
        refused outright.
        """
        session: SessionRecord | None = None
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != PUBLIC_RESEARCH_TASK_TYPE:
                raise TaskKindMismatchError(task_id, PUBLIC_RESEARCH_TASK_TYPE)
            repository = ResearchRepository(connection)
            existing = await repository.get_answer(task_id)
            if existing is not None:
                raise ResearchAnswerAlreadyRecordedError(task_id)
            grant = await repository.open_grant_for_task(task_id)
            if grant is None:
                grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise ResearchGrantNotFoundError(task_id)
            observations = await repository.list_observations(task_id)
            verify_research_grounding(
                {record.observation.ref: record.observation for record in observations}, answer
            )
            await repository.insert_answer(
                answer_id=uuid.uuid4(),
                task_id=task_id,
                grant_id=grant.id,
                answer=answer,
                provider=provider,
                model=model,
                steps_used=await repository.count_steps(task_id),
                observations_used=len(observations),
                planner_calls=max(planner_calls, grant.planner_calls),
            )
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                await repository.close_grant(grant_id=grant.id, status=GrantStatus.COMPLETED)
            session = await repository.open_session_for_task(task_id)
            if session is not None:
                await repository.close_session(session_id=session.id, status="CLOSED")
            advanced = await tasks.advance_task(
                task_id=task.id,
                expected_revision=task.revision,
                status=None if not accepts_actions(task.status) else _final_status(answer),
            )
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_RESEARCH_ANSWER_RECORDED,
                payload={
                    "grant_id": str(grant.id),
                    "answer_status": answer.status,
                    "stop_reason": answer.stop_reason,
                    "evidence_count": len(answer.evidence),
                    "observations_used": len(observations),
                    "provider": provider,
                },
            )
        if session is not None:
            await self._close_worker_session(session)
        logger.info(
            "research answer recorded",
            extra={
                "task_id": str(task_id),
                "answer_status": answer.status,
                "stop_reason": answer.stop_reason,
                "provider": provider,
            },
        )
        return await self.describe(task_id)

    # ---- recovery ------------------------------------------------------------

    async def invalidate_stale_sessions(self) -> list[uuid.UUID]:
        """Startup: every session a previous runtime left open is gone.

        Called before anything may plan again. The rows become STALE, so the
        refs they issued stop resolving and the task has to re-observe
        authoritative browser state instead of assuming the old one holds.
        """
        async with self._engine.connect() as connection:
            stale = await ResearchRepository(
                connection
            ).stale_sessions_from_other_generations(self._runtime_generation)
        invalidated: list[uuid.UUID] = []
        for session in stale:
            await self._mark_session_stale(session)
            invalidated.append(session.id)
        if invalidated:
            logger.warning(
                "Invalidated %d research browser session(s) from a previous runtime; "
                "their semantic references no longer resolve.",
                len(invalidated),
            )
        return invalidated


# ---- helpers ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Destination:
    url: str | None
    host: str | None
    document_epoch: int | None
    observation_id: uuid.UUID | None
    #: The session that issued the ref, when a ref was involved. Compared with
    #: the live session after it is bound: a ref from a session that no longer
    #: exists is stale, whatever else about it still matches.
    source_session_id: uuid.UUID | None


def _idempotency_key(request_id: str) -> str:
    return f"research:{request_id}"


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _short(reason: str) -> str:
    code = reason.split(":", 1)[0].strip()
    return code if code.replace("_", "").isalnum() and len(code) <= 64 else "session_refused"


def _lost_session(outcome: Outcome) -> bool:
    reason = str(outcome.result.get("reason", ""))
    return "unknown_session" in reason or outcome.error_code == "unknown_session"


def _targets_of(outcome: Outcome) -> dict[str, str]:
    raw = outcome.observation.get("targets")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _validated_observation(
    outcome: Outcome, *, sequence: int, session_id: uuid.UUID
) -> tuple[ResearchObservation | None, Outcome]:
    """Parse what the worker returned, or downgrade to a known failure."""
    if outcome.outcome is not AttemptOutcome.SUCCEEDED:
        return None, outcome
    try:
        observation = ResearchObservation.model_validate(outcome.observation.get("observation"))
        if observation.sequence != sequence or observation.session_id != session_id:
            raise ValueError("the observation does not answer this step")
        if observation.observation_id != outcome.observation_id:
            raise ValueError("the observation does not answer this dispatch")
    except (ValidationError, ValueError):
        return None, Outcome(
            outcome=AttemptOutcome.FAILED,
            dispatch_status=DispatchStatus.FAILED_BEFORE_EFFECT,
            submitted=False,
            error_code="observation_invalid",
            observation_id=None,
            result={**outcome.result, "status": DispatchStatus.FAILED_BEFORE_EFFECT.value},
        )
    return observation, outcome


#: Refusal details a failed step may keep: stable codes and numbers only.
_KEPT_FAILURE_FIELDS = ("http_status", "redirect_refusal", "refusal")


def _step_summary(outcome: Outcome, observation: ResearchObservation | None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        key: outcome.result[key]
        for key in ("operation", "status", "worker_generation", "dispatch_id", "duration_ms", "replayed")
        if key in outcome.result
    }
    summary["submitted"] = False
    if observation is not None:
        summary.update(
            document_epoch=observation.document_epoch,
            block_count=len(observation.blocks),
            link_count=len(observation.links),
            truncated=observation.truncated,
            settled=observation.settled,
        )
    else:
        for key in _KEPT_FAILURE_FIELDS:
            value = outcome.observation.get(key)
            if isinstance(value, (int, str)) and not isinstance(value, bool) and len(str(value)) <= 64:
                summary[key] = value
    return summary


def _worker_input(
    step: ResearchStep, *, sequence: int, destination: _Destination
) -> dict[str, Any]:
    """The typed payload for one worker operation. No field for anything else."""
    operation = operation_of(step)
    if operation is ResearchOperation.NAVIGATE:
        assert isinstance(step, NavigateStep)
        assert destination.url is not None
        return {
            "sequence": sequence,
            "tab": step.tab,
            "url": destination.url,
            "target_kind": step.target.kind,
            "target_ref": getattr(step.target, "ref", None),
            "expected_document_epoch": destination.document_epoch,
        }
    if operation is ResearchOperation.OBSERVE:
        return {"sequence": sequence, "tab": step.tab}  # type: ignore[union-attr]
    if operation is ResearchOperation.SCROLL:
        return {"sequence": sequence, "tab": step.tab, "direction": step.direction}  # type: ignore[union-attr]
    if operation is ResearchOperation.HISTORY:
        return {"sequence": sequence, "tab": step.tab, "direction": step.direction}  # type: ignore[union-attr]
    assert isinstance(step, TabStep)
    return {"sequence": sequence, "action": step.action, "tab": step.tab}


def _final_status(answer: ResearchAnswer) -> Any:
    from app.domain.task_status import TaskStatus

    # A research task that reached an honest conclusion succeeded, whatever the
    # conclusion was: "could not verify this publicly" is a real answer. Only
    # a task that was stopped or blocked before answering is a failure.
    if answer.stop_reason in ("user_stopped", "blocked", "planner_failed", "outside_scope"):
        return TaskStatus.FAILED
    return TaskStatus.SUCCEEDED


__all__ = [
    "REQUEST_KEYS",
    "BudgetUsage",
    "ResearchService",
    "ResearchView",
    "StepResult",
    "validate_request",
]
