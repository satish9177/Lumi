"""Milestone 8a S3: the durable authority for one authenticated account read.

The shape, in one place:

    task (authenticated_read: an objective, one profile)       -- created by main
      -> prepare: check the profile *without opening a browser*, build the
         bounded scope, open a PENDING grant (the trusted card's contents; it
         authorises nothing)
      -> trusted click: confirm -> the grant becomes ACTIVE and expiring, and
         the database checks in the same statement that the profile is still
         the one the card showed
      -> for each step main's planner chooses:
           validate against the grant, the budgets and the profile
           check the ref against Lumi's own persisted observation (metadata
             only -- there is no address to resolve, the worker holds it)
           create the action, mint and consume a single-use authorization
             (the consuming statement re-checks profile, epoch, fingerprint)
           persist the attempt and the dispatch, then run one operation
           store the redacted observation with the finished attempt
      -> finish: record one answer, grounded in the stored redacted evidence,
         and complete the grant

**Where the authority sits, stated once.** The planner proposes; this module
authorises. The provider is named by the grant, never by a page or a model. The
worker checks the credential surface and the account identity before it
projects a single character, and this module turns what it reports into a
deterministic pause -- the planner is never asked whether to stop.

**What a pause is.** `login_required`, `account_changed`,
`account_identity_unknown` and `left_site_scope` each end the current run, close
the profile's browser so a human takeover can open it, and leave a task event
that says why. `account_changed` additionally bumps the profile's revoke epoch,
which makes every grant bound to the old epoch unusable in the very statement
that consumes a step authorization. Continuing after it needs a new scope card:
an old grant is never transferred to a new account.

**Nothing is retried blindly.** A GET may have reached the site before a lost
answer, and Lumi cannot tell whether the site recorded the visit. A lost step
is `OUTCOME_UNKNOWN`, never "nothing happened", and the only step allowed after
it is a fresh `observe`.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.browser.errors import BrowserWorkerError
from app.browser.protocol import DispatchRequest
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.authenticated import (
    ACCOUNT_PRIVATE,
    AUTHENTICATED_READ_SITE,
    AUTHENTICATED_READ_TASK_TYPE,
    AUTHENTICATED_TOOL_NAMES,
    TOOL_NAMES,
    AuthenticatedAnswer,
    AuthenticatedBudgets,
    AuthenticatedDisclosure,
    AuthenticatedObservation,
    AuthenticatedProposal,
    AuthenticatedReadScope,
    AuthenticatedStep,
    AuthenticatedStepEnvelope,
    AuthOperation,
    BlockTarget,
    LinkTarget,
    NavigateStep,
    ObserveStep,
    PauseReason,
    Recipient,
    RevealStep,
    TabStep,
    WorkerReadResult,
    operation_of,
)
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.browser_profile import BrowserContextKind, BrowserProfile, ProfileRefusal, ProfileStatus
from app.domain.errors import (
    ApprovalNotUsableError,
    AuthenticatedAnswerAlreadyRecordedError,
    AuthenticatedBudgetExhaustedError,
    AuthenticatedGrantNotFoundError,
    AuthenticatedGrantNotUsableError,
    AuthenticatedProfileUnavailableError,
    AuthenticatedReadNotConfiguredError,
    AuthenticatedStepInFlightError,
    AuthenticatedStepRefusedError,
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.research import (
    MAX_TAB_SLOTS,
    GrantStatus,
    parse_objective,
    verify_research_grounding,
)
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.repositories.actions import ActionRecord, ActionRepository, AttemptRecord
from app.repositories.authenticated import (
    AuthenticatedAnswerRecord,
    AuthenticatedGrantRecord,
    AuthenticatedObservationRecord,
    AuthenticatedRepository,
)
from app.repositories.browser import BrowserRepository
from app.repositories.login_attempts import LoginAttemptRepository
from app.repositories.profiles import BrowserProfileRepository
from app.repositories.tasks import TaskRecord, TaskRepository
from app.repositories.workflows import require_step_live
from app.services.actions import ActionService, ActionView
from app.services.browser_execution import (
    Outcome,
    WorkerSource,
    dispatch_and_classify,
    open_worker_client,
)
from app.services.browser_profiles import BrowserProfileService
from app.services.form_state import FormStateRegistry
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.authenticated")

#: Everything an authenticated-read task request may carry. Provenance fields
#: are written by the trusted desktop layer, never by a model.
REQUEST_KEYS = frozenset(
    {
        "type",
        "text",
        "objective",
        "source",
        "request_id",
        "voice_turn_id",
        "profile_id",
        "classification",
    }
)

#: Action statuses that mean a step of this task is not finished with -- other
#: than `OUTCOME_UNKNOWN`, which has its own rule (see `_check_step_allowed`).
IN_FLIGHT_STATUSES = frozenset(
    {
        ActionStatus.PROPOSED,
        ActionStatus.WAITING_APPROVAL,
        ActionStatus.APPROVED,
        ActionStatus.AUTHORIZED,
        ActionStatus.EXECUTING,
        ActionStatus.RECONCILING,
    }
)


def validate_request(request: dict[str, Any]) -> tuple[str, uuid.UUID]:
    """Refuse to store an authenticated task that could never be worked on.

    The classification is not a hint: a task that does not say `account_private`
    is refused, so a public-looking task can never acquire a private grant.
    """
    if not set(request) <= REQUEST_KEYS:
        raise ValueError("unknown request field")
    if request.get("classification") != ACCOUNT_PRIVATE:
        raise ValueError("an authenticated task is account_private")
    try:
        profile_id = uuid.UUID(str(request.get("profile_id")))
    except ValueError:
        raise ValueError("a profile is required") from None
    return parse_objective(request.get("objective")), profile_id


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    steps: int
    observations: int
    planner_calls: int
    active_seconds: float
    tabs: int


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """The trusted, controller-authored facts the card and the panel show."""

    id: uuid.UUID
    label: str
    site: str
    status: ProfileStatus


@dataclass(frozen=True, slots=True)
class AuthenticatedView:
    task: TaskRecord
    objective: str
    profile: ProfileSummary | None
    grant: AuthenticatedGrantRecord | None
    observations: tuple[AuthenticatedObservationRecord, ...]
    answer: AuthenticatedAnswerRecord | None
    usage: BudgetUsage
    #: Why the task is paused, if it is. Never model-chosen.
    pause_reason: PauseReason | None
    #: True while a step has no outcome Lumi can stand behind.
    unresolved_step: bool


@dataclass(frozen=True, slots=True)
class StepResult:
    view: AuthenticatedView
    action: ActionView
    observation: AuthenticatedObservationRecord | None
    outcome: AttemptOutcome
    error_code: str | None
    #: Set when this step ended the run with a deterministic pause.
    pause_reason: PauseReason | None
    replayed: bool


class _GrantAuthorizer:
    """Mints and consumes one step authorization for `start_scoped_attempt`."""

    def __init__(
        self,
        *,
        grant: AuthenticatedGrantRecord,
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
        record = await AuthenticatedRepository(connection).insert_step_authorization(
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
        claimed = await AuthenticatedRepository(connection).consume_step_authorization(
            authorization_id=authorization_id,
            action_revision=action_revision,
            proposal_digest=proposal_digest,
            runtime_generation=self._runtime_generation,
        )
        return claimed is not None


class AuthenticatedReadService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        tasks: TaskService,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
        profiles: BrowserProfileService,
        grant_ttl_seconds: int,
        step_ttl_seconds: int,
        max_tabs: int = 3,
        forms: FormStateRegistry | None = None,
    ) -> None:
        self._engine = engine
        #: Milestone 8b S6: which profile is in form preparation (opened headed) or
        #: holds a local draft (no read step may touch it).
        self._forms = forms
        self._tasks = tasks
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._profiles = profiles
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        self._step_ttl = timedelta(seconds=step_ttl_seconds)
        self._max_tabs = max_tabs

    @property
    def is_configured(self) -> bool:
        return self._worker is not None

    # ---- reading -------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> AuthenticatedView:
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> AuthenticatedView:
        tasks = TaskRepository(connection)
        task = await tasks.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
            raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
        repository = AuthenticatedRepository(connection)
        grant = await repository.open_grant_for_task(task_id)
        if grant is None:
            grant = await repository.latest_grant_for_task(task_id)
        observations = tuple(await repository.list_observations(task_id))
        profile = await self._profile_of(connection, task)
        actions = await ActionRepository(connection).list_actions(task_id, limit=500)
        usage = BudgetUsage(
            steps=await repository.count_steps(task_id),
            observations=len(observations),
            planner_calls=grant.planner_calls if grant else 0,
            active_seconds=(await repository.active_seconds(grant.id)) if grant else 0.0,
            tabs=len(observations[-1].observation.open_tabs) if observations else 0,
        )
        return AuthenticatedView(
            task=task,
            objective=str(task.request.get("objective", "")),
            profile=ProfileSummary(
                id=profile.id, label=profile.label, site=profile.site, status=profile.status
            )
            if profile is not None
            else None,
            grant=grant,
            observations=observations,
            answer=await repository.get_answer(task_id),
            usage=usage,
            pause_reason=await self._pause_reason(connection, task),
            unresolved_step=_blocking_step(actions) is not None,
        )

    async def _profile_of(
        self, connection: AsyncConnection, task: TaskRecord
    ) -> BrowserProfile | None:
        try:
            profile_id = uuid.UUID(str(task.request.get("profile_id")))
        except ValueError:
            return None
        return await BrowserProfileRepository(connection).get(profile_id)

    @staticmethod
    async def _pause_reason(connection: AsyncConnection, task: TaskRecord) -> PauseReason | None:
        if task.status is not TaskStatus.PAUSED:
            return None
        repository = TaskRepository(connection)
        start = max(0, task.last_event_sequence - 20)
        for event in reversed(await repository.list_events(task.id, after_sequence=start, limit=40)):
            if event.event_type == TaskEventType.TASK_AUTHENTICATED_PAUSED.value:
                try:
                    return PauseReason(str(event.payload.get("reason")))
                except ValueError:
                    return None
            if event.event_type == TaskEventType.TASK_AUTHENTICATED_RESUMED.value:
                return None
        return None

    # ---- the scope card ------------------------------------------------------

    def build_scope(
        self, *, profile: BrowserProfile, recipient: Recipient
    ) -> AuthenticatedReadScope:
        """The bounded scope the trusted card shows, word for word.

        The operation list is the whole capability. There is no entry for login,
        typing, form submission, upload, download, purchase, message or any
        non-GET request, and no field in which one could be added at run time.
        Everything comes from the profile row and this runtime's configuration:
        nothing here is taken from a page, a model or a renderer payload except
        the recipient, which main validated against its own configured list.
        """
        if profile.account_fingerprint is None:
            raise AuthenticatedProfileUnavailableError("account_fingerprint_unknown")
        return AuthenticatedReadScope(
            profile_id=profile.id,
            site=profile.site,
            allowed_origins=list(profile.allowed_origins),
            allowed_operations=list(AuthOperation),
            disclosure=AuthenticatedDisclosure(recipient=recipient),
            budgets=AuthenticatedBudgets(max_tabs=min(self._max_tabs, MAX_TAB_SLOTS)),
            account_fingerprint=profile.account_fingerprint,
            profile_revoke_epoch=profile.revoke_epoch,
        )

    async def _check_profile_readable(
        self, connection: AsyncConnection, profile: BrowserProfile | None
    ) -> BrowserProfile:
        """Deterministic reasons, in a fixed order, and no browser is opened."""
        if profile is None:
            raise AuthenticatedProfileUnavailableError("profile_not_found")
        if profile.is_deleted:
            raise AuthenticatedProfileUnavailableError("profile_deleted")
        if profile.status is not ProfileStatus.AUTHENTICATED:
            raise AuthenticatedProfileUnavailableError("profile_not_authenticated")
        if profile.account_fingerprint is None:
            raise AuthenticatedProfileUnavailableError("account_fingerprint_unknown")
        if await LoginAttemptRepository(connection).find_open_for_profile(profile.id) is not None:
            raise AuthenticatedProfileUnavailableError("profile_takeover_active")
        if (
            profile.lease_runtime_generation is not None
            and profile.lease_runtime_generation != self._runtime_generation
            and profile.lease_expires_at is not None
            and profile.lease_expires_at > datetime.now(UTC)
        ):
            raise AuthenticatedProfileUnavailableError("profile_in_use")
        return profile

    async def check_profile(self, profile_id: uuid.UUID) -> None:
        """Refuse, deterministically, before a task exists, and open nothing.

        Called when an authenticated task is *created*, so a profile that is not
        signed in, not identifiable, deleted, in the middle of a sign-in or in
        use elsewhere never leaves an orphan task with no permission card.
        """
        if self._worker is None:
            raise AuthenticatedReadNotConfiguredError()
        async with self._engine.connect() as connection:
            profile = await BrowserProfileRepository(connection).get(profile_id)
            await self._check_profile_readable(connection, profile)

    async def prepare(self, task_id: uuid.UUID, *, recipient: Recipient) -> AuthenticatedView:
        """Open the PENDING grant. Nothing is opened, navigated or read."""
        if self._worker is None:
            raise AuthenticatedReadNotConfiguredError()
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
                raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            # M10 final audit (Pass A, F1): a stopped workflow's form step never gets account reading back.
            await require_step_live(connection, task_id)
            repository = AuthenticatedRepository(connection)
            existing = await repository.open_grant_for_task(task_id)
            if existing is not None:
                return await self._view(connection, task_id)
            profile = await self._check_profile_readable(
                connection, await self._profile_of(connection, task)
            )
            scope = self.build_scope(profile=profile, recipient=recipient)
            grant = await repository.insert_grant(
                grant_id=uuid.uuid4(), task_id=task_id, scope=scope
            )
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_AUTHENTICATED_SCOPE_REQUESTED,
                payload={
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "policy_version": grant.policy_version,
                    "profile_id": str(profile.id),
                    "recipient": scope.disclosure.recipient,
                    "allowed_operations": [op.value for op in scope.allowed_operations],
                },
            )
            return await self._view(connection, task_id)

    async def confirm(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> AuthenticatedView:
        """The trusted click. The *only* way a grant becomes usable.

        There is no other caller, no model-reachable route and no spoken command
        that reaches this method. The compare-and-swap also checks, in SQL, that
        the profile is still the one the card was built for; if the account
        changed while the card was on screen the click confirms nothing.
        """
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            await require_step_live(connection, task_id)  # M10 final audit (Pass A, F1)
            repository = AuthenticatedRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise AuthenticatedGrantNotFoundError(task_id)
            if grant.status is not GrantStatus.PENDING:
                raise AuthenticatedGrantNotUsableError(f"it is {grant.status.value.lower()}")
            if grant.revision != expected_revision:
                raise AuthenticatedGrantNotUsableError("it changed since you reviewed it")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id,
                expected_revision=expected_revision,
                scope_digest=grant.scope_digest,
                ttl=self._grant_ttl,
            )
            if confirmed is None:
                raise AuthenticatedGrantNotUsableError(
                    "the account or its sign-in changed since you reviewed it"
                )
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_AUTHENTICATED_SCOPE_GRANTED,
                payload={
                    "grant_id": str(confirmed.id),
                    "grant_revision": confirmed.revision,
                    "grant_status": confirmed.status.value,
                    "scope_digest": confirmed.scope_digest,
                    "policy_version": confirmed.policy_version,
                    "expires_at": confirmed.expires_at.isoformat() if confirmed.expires_at else None,
                },
            )
            view = await self._view(connection, task_id)
        logger.info(
            "authenticated scope granted",
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
    ) -> AuthenticatedView:
        """Withdraw the scope and release the browser. Not reversible."""
        profile_id: uuid.UUID | None = None
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            repository = AuthenticatedRepository(connection)
            grant = (
                await repository.get_grant(grant_id)
                if grant_id is not None
                else await repository.open_grant_for_task(task_id)
            )
            if grant is None or grant.task_id != task_id:
                raise AuthenticatedGrantNotFoundError(task_id)
            profile_id = grant.profile_id
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(
                    grant_id=grant.id, status=status, expected_revision=expected_revision
                )
                if closed is None:
                    raise AuthenticatedGrantNotUsableError("it changed since you reviewed it")
            else:
                closed = grant
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_AUTHENTICATED_SCOPE_REVOKED,
                payload={
                    "grant_id": str(closed.id),
                    "grant_revision": closed.revision,
                    "grant_status": closed.status.value,
                    "reason": reason,
                },
            )
        await self._release_profile(profile_id)
        return await self.describe(task_id)

    # ---- one step ------------------------------------------------------------

    async def execute_step(
        self, task_id: uuid.UUID, envelope: AuthenticatedStepEnvelope
    ) -> StepResult:
        """Validate, authorise, persist, execute and observe exactly one step."""
        if self._worker is None:
            raise AuthenticatedReadNotConfiguredError()
        step = envelope.step
        operation = operation_of(step)
        async with self._engine.connect() as connection:
            view = await self._view(connection, task_id)
            grant = await self._usable_grant(connection, view)
            actions = await ActionRepository(connection).list_actions(task_id, limit=500)
            replay = await self._replayed_step(connection, task_id, envelope.request_id)
            profile = await self._check_profile_readable(
                connection, await self._profile_of(connection, view.task)
            )
        if replay is not None:
            return replay
        if self._forms is not None:
            # A page holding a local draft can reflect a protected value anywhere in its
            # text: no agent read may run against it, and no provider may see it.
            self._forms.assert_clean(profile.id)
        if not grant.scope.permits(operation):
            raise AuthenticatedStepRefusedError("outside_scope")
        self._check_step_allowed(actions, operation)
        self._check_budgets(view, grant, planner_calls=envelope.planner_calls)
        if isinstance(step, TabStep) and step.action == "open":
            if view.usage.tabs >= grant.scope.budgets.max_tabs:
                raise AuthenticatedBudgetExhaustedError("max_tabs")
        if profile.revoke_epoch != grant.profile_revoke_epoch or (
            profile.account_fingerprint != grant.scope.account_fingerprint
        ):
            raise AuthenticatedGrantNotUsableError("the account changed since you allowed this")

        client = await open_worker_client(self._worker, self._runtime_generation)
        try:
            worker_generation = await self._bind_worker(client)
            await self._open_profile(profile.id, worker_generation)
            source = await self._check_refs(
                task_id=task_id, step=step, worker_generation=worker_generation
            )
            async with self._engine.connect() as connection:
                sequence = await AuthenticatedRepository(connection).next_sequence(task_id)
            proposal = AuthenticatedProposal(
                operation=operation,
                step_number=view.usage.steps + 1,
                policy_version=grant.policy_version,
                grant_id=grant.id,
                scope_digest=grant.scope_digest,
                profile_id=profile.id,
                profile_revoke_epoch=grant.profile_revoke_epoch,
                step=step.model_dump(mode="json"),
                tab=getattr(step, "tab", None),
                expected_document_epoch=source.observation.document_epoch if source else None,
                source_observation_id=source.id if source else None,
                worker_generation=worker_generation,
            )
            await self._resume_if_paused(task_id)
            action_view, created = await self._start(task_id, grant, proposal, envelope)
            if not created:  # pragma: no cover - the replay check ran first.
                raise AuthenticatedStepInFlightError(action_view.action.id)
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
                        site=AUTHENTICATED_READ_SITE,
                        effect=BrowserEffect.ACCOUNT_READ,
                    )
            except Exception:
                logger.exception("could not record the authenticated dispatch")
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
                    site=AUTHENTICATED_READ_SITE,
                    profile_id=profile.id,
                    input=_worker_input(
                        step,
                        sequence=sequence,
                        grant=grant,
                        source=source,
                    ),
                ),
            )
        finally:
            await client.aclose()

        await self._close_dispatch(dispatch_id, outcome)
        if outcome.outcome is not AttemptOutcome.SUCCEEDED:
            return await self._finish_worker_failure(
                task_id, action_view, outcome=outcome, grant=grant
            )
        parsed = _worker_result(outcome, sequence=sequence, profile_id=profile.id)
        if parsed is None:
            return await self._finish_failure(
                task_id, action_view, error_code="observation_invalid",
                result=_step_summary(outcome, None),
            )
        if parsed.observation is not None:
            if self._forms is not None:
                # Recorded so a manifest built from THIS observation is executable: only an
                # observation taken in the headed preparation window can fund a local draft.
                self._forms.note_observation(profile.id, parsed.observation.observation_id)
            return await self._finish_success(
                task_id,
                action_view,
                grant=grant,
                observation=parsed.observation,
                dispatch_id=dispatch_id,
                worker_generation=worker_generation,
                result=_step_summary(outcome, parsed.observation),
            )
        if parsed.credential_surface is not None:
            # Signals only. No title, no text, no block, no link, no model call.
            return await self._finish_pause(
                task_id,
                action_view,
                grant=grant,
                reason=PauseReason.LOGIN_REQUIRED,
                result={
                    **_step_summary(outcome, None),
                    "signals": [signal.value for signal in parsed.credential_surface.signals],
                },
            )
        assert parsed.identity is not None
        return await self._finish_pause(
            task_id,
            action_view,
            grant=grant,
            reason=(
                PauseReason.ACCOUNT_CHANGED
                if parsed.identity.kind == "account_changed"
                else PauseReason.ACCOUNT_IDENTITY_UNKNOWN
            ),
            result=_step_summary(outcome, None),
        )

    async def _usable_grant(
        self, connection: AsyncConnection, view: AuthenticatedView
    ) -> AuthenticatedGrantRecord:
        grant = view.grant
        if grant is None:
            raise AuthenticatedGrantNotFoundError(view.task.id)
        if grant.status is GrantStatus.PENDING:
            raise AuthenticatedGrantNotUsableError("it has not been granted")
        if grant.status is not GrantStatus.ACTIVE:
            raise AuthenticatedGrantNotUsableError(f"it is {grant.status.value.lower()}")
        if await AuthenticatedRepository(connection).grant_is_expired(grant.id):
            raise AuthenticatedGrantNotUsableError("it has expired")
        if not accepts_actions(view.task.status):
            raise TaskNotAcceptingActionsError(view.task.id, view.task.status)
        return grant

    @staticmethod
    def _check_step_allowed(actions: list[ActionRecord], operation: AuthOperation) -> None:
        blocking = _blocking_step(actions)
        if blocking is None:
            return
        if blocking.status in IN_FLIGHT_STATUSES:
            raise AuthenticatedStepInFlightError(blocking.id)
        # OUTCOME_UNKNOWN: a request may have reached the site and Lumi cannot
        # tell what happened. Nothing repeats it; a fresh observation does.
        if operation is not AuthOperation.OBSERVE:
            raise AuthenticatedStepRefusedError("observe_required")

    async def _replayed_step(
        self, connection: AsyncConnection, task_id: uuid.UUID, request_id: str
    ) -> StepResult | None:
        """A request id that already executed returns its own stored result."""
        existing = await ActionRepository(connection).get_action_by_idempotency_key(
            task_id=task_id, idempotency_key=_idempotency_key(request_id)
        )
        if existing is None:
            return None
        repository = AuthenticatedRepository(connection)
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
        view = await self._view(connection, task_id)
        return StepResult(
            view=view,
            action=ActionView(action=existing, approval=None, attempts=tuple(attempts)),
            observation=observation,
            outcome=(last.outcome if last and last.outcome else AttemptOutcome.OUTCOME_UNKNOWN),
            error_code=last.error_code if last else None,
            pause_reason=view.pause_reason,
            replayed=True,
        )

    def _check_budgets(
        self, view: AuthenticatedView, grant: AuthenticatedGrantRecord, *, planner_calls: int
    ) -> None:
        budgets = grant.scope.budgets
        usage = view.usage
        if usage.steps >= budgets.max_steps:
            raise AuthenticatedBudgetExhaustedError("max_steps")
        if usage.observations >= budgets.max_observations:
            raise AuthenticatedBudgetExhaustedError("max_observations")
        if max(planner_calls, usage.planner_calls) > budgets.max_planner_calls:
            raise AuthenticatedBudgetExhaustedError("max_planner_calls")
        if usage.active_seconds > budgets.max_active_seconds:
            raise AuthenticatedBudgetExhaustedError("max_active_seconds")

    # ---- refs ----------------------------------------------------------------

    async def _check_refs(
        self, *, task_id: uuid.UUID, step: AuthenticatedStep, worker_generation: uuid.UUID
    ) -> AuthenticatedObservationRecord | None:
        """Check a step's ref against Lumi's own persisted observation.

        Metadata only: a ref, its tab, its document epoch and the worker
        generation that issued it. There is no address to resolve, because the
        address never left the worker. The worker checks the same ref against
        its own per-epoch table and refuses on any disagreement.
        """
        if not isinstance(step, (NavigateStep, RevealStep)):
            return None
        target = step.target
        sequence = int(target.observation[1:])
        async with self._engine.connect() as connection:
            repository = AuthenticatedRepository(connection)
            record = await repository.observation_by_sequence(task_id=task_id, sequence=sequence)
            newest = await repository.list_observations(task_id)
        if record is None:
            raise AuthenticatedStepRefusedError("unknown_observation")
        observation = record.observation
        if observation.kind != "page":
            raise AuthenticatedStepRefusedError("target_not_a_page")
        if observation.tab != step.tab:
            raise AuthenticatedStepRefusedError("wrong_tab")
        if record.worker_generation != worker_generation:
            # Issued by a browser that no longer exists. Re-observe.
            raise AuthenticatedStepRefusedError("stale_session")
        if any(
            other.observation.tab == observation.tab
            and other.worker_generation == record.worker_generation
            and other.observation.document_epoch > observation.document_epoch
            for other in newest
        ):
            raise AuthenticatedStepRefusedError("stale_target_ref")
        if isinstance(target, LinkTarget):
            if not any(link.id == target.ref for link in observation.links):
                raise AuthenticatedStepRefusedError("unknown_target_ref")
        else:
            assert isinstance(target, BlockTarget)
            if observation.block(target.ref) is None:
                raise AuthenticatedStepRefusedError("unknown_target_ref")
        return record

    # ---- the worker and its profile -----------------------------------------

    async def _bind_worker(self, client: BrowserWorkerClient) -> uuid.UUID:
        identity = await client.identify()
        async with self._engine.begin() as connection:
            repository = BrowserRepository(connection)
            if await repository.get_worker_generation(identity.worker_generation) is None:
                await repository.register_worker_generation(
                    worker_generation=identity.worker_generation,
                    runtime_generation=self._runtime_generation,
                    worker_started_at=datetime.fromisoformat(identity.started_at),
                )
        return identity.worker_generation

    async def _open_profile(self, profile_id: uuid.UUID, worker_generation: uuid.UUID) -> None:
        """The profile, headless, in the worker that is answering right now.

        Headless because an agent read is Lumi-driven and a takeover is the
        human's: the two are never the same window. A profile this runtime
        believes is open in a worker that has since restarted is closed and
        opened afresh, so no step runs against a context that no longer exists.
        """
        # Milestone 8b S6: a profile in form preparation is headed (a local draft can only
        # live in a window a person can see); every other read is headless.
        headed = self._forms is not None and self._forms.is_preparing(profile_id)
        try:
            opened = await self._profiles.open_profile(
                profile_id, kind=BrowserContextKind.AUTHENTICATED_PROFILE, headed=headed
            )
            if opened.worker_generation != worker_generation:
                await self._profiles.close_profile(profile_id)
                opened = await self._profiles.open_profile(
                    profile_id, kind=BrowserContextKind.AUTHENTICATED_PROFILE, headed=headed
                )
        except ProfileRefusal as error:
            if error.code == "profile_open_mode_mismatch":
                raise AuthenticatedProfileUnavailableError("profile_takeover_active") from None
            if error.code in ("profile_locked_by_another_process", "profile_lease_unavailable"):
                raise AuthenticatedProfileUnavailableError("profile_in_use") from None
            raise AuthenticatedProfileUnavailableError(error.code) from None
        _ = opened

    async def _release_profile(self, profile_id: uuid.UUID | None) -> None:
        """Close the headless context so a human takeover can open the profile."""
        if profile_id is None:
            return
        try:
            await self._profiles.close_profile(profile_id)
        except BrowserWorkerError:
            logger.info("could not close the profile in the worker", extra={"profile_id": str(profile_id)})

    # ---- ledger plumbing -----------------------------------------------------

    async def _start(
        self,
        task_id: uuid.UUID,
        grant: AuthenticatedGrantRecord,
        proposal: AuthenticatedProposal,
        envelope: AuthenticatedStepEnvelope,
    ) -> tuple[ActionView, bool]:
        authorizer = _GrantAuthorizer(
            grant=grant,
            task_id=task_id,
            runtime_generation=self._runtime_generation,
            ttl=self._step_ttl,
        )
        try:
            return await self._actions.start_scoped_attempt(
                task_id,
                tool_name=TOOL_NAMES[proposal.operation],
                idempotency_key=_idempotency_key(envelope.request_id),
                risk_tier=RiskTier.R1,
                proposal=proposal.model_dump(mode="json"),
                authorizer=authorizer,
            )
        except ApprovalNotUsableError:
            # The consuming statement refused. Say why, deterministically, from
            # the profile as it stands now -- the database already decided.
            raise await self._explain_unusable(grant) from None

    async def _explain_unusable(self, grant: AuthenticatedGrantRecord) -> Exception:
        async with self._engine.connect() as connection:
            profile = await BrowserProfileRepository(connection).get(grant.profile_id)
        if profile is None or profile.is_deleted:
            return AuthenticatedProfileUnavailableError("profile_deleted")
        if profile.revoke_epoch != grant.profile_revoke_epoch or (
            profile.account_fingerprint != grant.scope.account_fingerprint
        ):
            return AuthenticatedGrantNotUsableError("the account changed since you allowed this")
        if profile.status is not ProfileStatus.AUTHENTICATED:
            return AuthenticatedProfileUnavailableError("login_required")
        return AuthenticatedGrantNotUsableError("it can no longer be used")

    async def _record_step_start(self, grant: AuthenticatedGrantRecord, planner_calls: int) -> None:
        async with self._engine.begin() as connection:
            await AuthenticatedRepository(connection).record_step_start(
                grant_id=grant.id, planner_calls=planner_calls
            )

    async def _close_dispatch(self, dispatch_id: uuid.UUID, outcome: Outcome) -> None:
        async with self._engine.begin() as connection:
            await BrowserRepository(connection).finish_dispatch(
                dispatch_id=dispatch_id,
                status=outcome.dispatch_status,
                submitted=outcome.submitted,
                observation_id=outcome.observation_id,
                error_code=outcome.error_code,
                duration_ms=outcome.result.get("duration_ms"),
                # Codes and ids only. The shared summariser can carry the worker's
                # whole observation, which is account-private text and belongs in
                # `authenticated_observations` and nowhere else.
                result={
                    key: outcome.result[key]
                    for key in _DISPATCH_KEPT
                    if key in outcome.result
                },
            )

    async def _resume_if_paused(self, task_id: uuid.UUID) -> None:
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None or task.status is not TaskStatus.PAUSED:
                return
            advanced = await tasks.advance_task(
                task_id=task.id, expected_revision=task.revision, status=TaskStatus.READY
            )
            if advanced is not None:
                await tasks.append_event(
                    task=advanced,
                    event_type=TaskEventType.TASK_AUTHENTICATED_RESUMED,
                    payload={},
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
            "authenticated step did not produce an observation",
            extra={
                "task_id": str(task_id),
                "action_id": str(action_view.action.id),
                "outcome": outcome.value,
                "error_code": error_code,
            },
        )
        view = await self.describe(task_id)
        return StepResult(
            view=view,
            action=finished,
            observation=None,
            outcome=outcome,
            error_code=error_code,
            pause_reason=view.pause_reason,
            replayed=False,
        )

    async def _finish_worker_failure(
        self,
        task_id: uuid.UUID,
        action_view: ActionView,
        *,
        outcome: Outcome,
        grant: AuthenticatedGrantRecord,
    ) -> StepResult:
        if outcome.error_code == "left_site_scope":
            return await self._finish_pause(
                task_id,
                action_view,
                grant=grant,
                reason=PauseReason.LEFT_SITE_SCOPE,
                result=_step_summary(outcome, None),
            )
        # A lost answer is OUTCOME_UNKNOWN and is never retried: a GET may have
        # reached the site, and nothing can establish that it did not.
        return await self._finish_failure(
            task_id,
            action_view,
            error_code=outcome.error_code,
            outcome=outcome.outcome,
            result=_step_summary(outcome, None),
        )

    async def _finish_success(
        self,
        task_id: uuid.UUID,
        action_view: ActionView,
        *,
        grant: AuthenticatedGrantRecord,
        observation: AuthenticatedObservation,
        dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
        result: dict[str, Any],
    ) -> StepResult:
        action_id = action_view.action.id
        stored: list[AuthenticatedObservationRecord] = []

        async def record(connection: AsyncConnection, finished: AttemptRecord) -> None:
            stored.append(
                await AuthenticatedRepository(connection).insert_observation(
                    observation=observation,
                    task_id=task_id,
                    grant_id=grant.id,
                    action_id=action_id,
                    attempt_id=finished.id,
                    dispatch_id=dispatch_id,
                    worker_generation=worker_generation,
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
            },
            record=record,
        )
        logger.info(
            "authenticated step observed",
            extra={
                "task_id": str(task_id),
                "action_id": str(action_id),
                "operation": observation.operation.value,
                "observation_id": str(observation.observation_id),
                "sequence": observation.sequence,
            },
        )
        view = await self.describe(task_id)
        return StepResult(
            view=view,
            action=finished,
            observation=stored[0] if stored else None,
            outcome=AttemptOutcome.SUCCEEDED,
            error_code=None,
            pause_reason=None,
            replayed=False,
        )

    async def _finish_pause(
        self,
        task_id: uuid.UUID,
        action_view: ActionView,
        *,
        grant: AuthenticatedGrantRecord,
        reason: PauseReason,
        result: dict[str, Any],
    ) -> StepResult:
        """A deterministic pause. The planner is never consulted.

        Order matters. The action finishes first, then the authority changes
        (a bumped epoch and revoked grant for `account_changed`; a profile back
        to `NEEDS_LOGIN` for an expired session), then the task pauses, and
        only then is the browser closed so a human takeover can open the
        profile. Whatever happens after the first line, no model is called.
        """
        finished = await self._actions.finish_attempt(
            action_view.action.id,
            outcome=AttemptOutcome.FAILED,
            result=result,
            error_code=reason.value,
        )
        async with self._engine.begin() as connection:
            profiles = BrowserProfileRepository(connection)
            if reason is PauseReason.ACCOUNT_CHANGED:
                await profiles.invalidate_account(profile_id=grant.profile_id)
                await AuthenticatedRepository(connection).revoke_open_grants_for_profile(
                    grant.profile_id
                )
            elif reason is PauseReason.LOGIN_REQUIRED:
                await profiles.mark_needs_login(profile_id=grant.profile_id)
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is not None and accepts_actions(task.status):
                advanced = await tasks.advance_task(
                    task_id=task.id, expected_revision=task.revision, status=TaskStatus.PAUSED
                )
                if advanced is not None:
                    await tasks.append_event(
                        task=advanced,
                        event_type=TaskEventType.TASK_AUTHENTICATED_PAUSED,
                        payload={"reason": reason.value, "grant_id": str(grant.id)},
                    )
        await self._release_profile(grant.profile_id)
        logger.warning(
            "authenticated task paused",
            extra={"task_id": str(task_id), "reason": reason.value},
        )
        view = await self.describe(task_id)
        return StepResult(
            view=view,
            action=finished,
            observation=None,
            outcome=AttemptOutcome.FAILED,
            error_code=reason.value,
            pause_reason=reason,
            replayed=False,
        )

    # ---- the answer ----------------------------------------------------------

    async def record_answer(
        self,
        task_id: uuid.UUID,
        *,
        answer: AuthenticatedAnswer,
        provider: str,
        model: str,
        planner_calls: int,
    ) -> AuthenticatedView:
        """Store the one grounded answer, then close the scope.

        Grounding is verified here against this task's own stored, **redacted**
        observations -- the text the provider actually received -- and the
        provider must be the grant's one recipient. An answer attributed to any
        other provider is refused outright.
        """
        profile_id: uuid.UUID | None = None
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
                raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
            repository = AuthenticatedRepository(connection)
            if await repository.get_answer(task_id) is not None:
                raise AuthenticatedAnswerAlreadyRecordedError(task_id)
            grant = await repository.open_grant_for_task(task_id)
            if grant is None:
                grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise AuthenticatedGrantNotFoundError(task_id)
            if provider != grant.scope.disclosure.recipient:
                raise AuthenticatedStepRefusedError("recipient_mismatch")
            profile_id = grant.profile_id
            observations = await repository.list_observations(task_id)
            verify_research_grounding(
                {record.observation.ref: record.observation for record in observations}, answer
            )
            await repository.insert_answer(
                answer_id=uuid.uuid4(),
                task_id=task_id,
                grant_id=grant.id,
                profile_id=grant.profile_id,
                answer=answer,
                provider=provider,
                model=model,
                steps_used=await repository.count_steps(task_id),
                observations_used=len(observations),
                planner_calls=max(planner_calls, grant.planner_calls),
            )
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                await repository.close_grant(grant_id=grant.id, status=GrantStatus.COMPLETED)
            advanced = await tasks.advance_task(
                task_id=task.id,
                expected_revision=task.revision,
                status=None if not accepts_actions(task.status) else _final_status(answer),
            )
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_AUTHENTICATED_ANSWER_RECORDED,
                payload={
                    "grant_id": str(grant.id),
                    "answer_status": answer.status,
                    "stop_reason": answer.stop_reason,
                    "evidence_count": len(answer.evidence),
                    "observations_used": len(observations),
                    "provider": provider,
                },
            )
        await self._release_profile(profile_id)
        logger.info(
            "authenticated answer recorded",
            extra={"task_id": str(task_id), "answer_status": answer.status, "provider": provider},
        )
        return await self.describe(task_id)

    # ---- evidence lifecycle ----------------------------------------------------

    async def purge_task_evidence(self, task_id: uuid.UUID) -> int:
        """Remove a task's observations and answer. Idempotent."""
        async with self._engine.begin() as connection:
            return await AuthenticatedRepository(connection).delete_evidence_for_task(task_id)


# ---- helpers ---------------------------------------------------------------------


def _blocking_step(actions: list[ActionRecord]) -> ActionRecord | None:
    """The step, if any, that leaves this task without an outcome it can stand behind.

    An action in flight blocks everything. An `OUTCOME_UNKNOWN` action blocks
    everything except a fresh observation, and stops blocking once a *later*
    authenticated action has succeeded, because that success is exactly the
    fresh look at authoritative browser state the unknown outcome called for.
    """
    ours = [action for action in actions if action.tool_name in AUTHENTICATED_TOOL_NAMES]
    for action in ours:
        if action.status in IN_FLIGHT_STATUSES:
            return action
    unknown = [i for i, action in enumerate(ours) if action.status is ActionStatus.OUTCOME_UNKNOWN]
    if not unknown:
        return None
    succeeded = [i for i, action in enumerate(ours) if action.status is ActionStatus.SUCCEEDED]
    if succeeded and max(succeeded) > max(unknown):
        return None
    return ours[max(unknown)]


_DISPATCH_KEPT = (
    "operation", "status", "worker_generation", "dispatch_id", "duration_ms",
    "submitted", "replayed", "observation_id", "reason",
)


def _idempotency_key(request_id: str) -> str:
    return f"authenticated:{request_id}"


def _step_summary(outcome: Outcome, observation: AuthenticatedObservation | None) -> dict[str, Any]:
    """Codes and counts only. Never page text, a title, a URL or an identity."""
    summary: dict[str, Any] = {
        key: outcome.result[key]
        for key in ("operation", "status", "worker_generation", "dispatch_id", "duration_ms", "replayed")
        if key in outcome.result
    }
    summary["submitted"] = outcome.submitted
    if observation is not None:
        summary.update(
            document_epoch=observation.document_epoch,
            block_count=len(observation.blocks),
            link_count=len(observation.links),
            truncated=observation.truncated,
            settled=observation.settled,
            redactions=sum(observation.redactions.values()),
            # S4: counts, the epoch and a flag. Never a name, a label or a state.
            form_count=observation.inventory.form_count,
            element_count=observation.inventory.element_count,
            option_count=observation.inventory.option_count,
            form_epoch=observation.form_epoch,
            inventory_truncated=observation.inventory.truncated,
        )
    else:
        value = outcome.observation.get("http_status")
        if isinstance(value, int) and not isinstance(value, bool):
            summary["http_status"] = value
    return summary


def _worker_result(
    outcome: Outcome, *, sequence: int, profile_id: uuid.UUID
) -> WorkerReadResult | None:
    """Parse what the worker returned, or refuse it. The worker is not trusted
    to have redacted: the observation model refuses unredacted identifiers."""
    try:
        parsed = WorkerReadResult.model_validate(outcome.observation.get("result"))
    except ValidationError:
        return None
    observation = parsed.observation
    if observation is not None and (
        observation.sequence != sequence
        or observation.profile_id != profile_id
        or observation.observation_id != outcome.observation_id
    ):
        return None
    return parsed


def _worker_input(
    step: AuthenticatedStep,
    *,
    sequence: int,
    grant: AuthenticatedGrantRecord,
    source: AuthenticatedObservationRecord | None,
) -> dict[str, Any]:
    """The typed payload for one worker operation. No field for anything else."""
    common: dict[str, Any] = {
        "sequence": sequence,
        "site": grant.scope.site,
        "expected_account_fingerprint": grant.scope.account_fingerprint,
        "max_text_chars": grant.scope.disclosure.max_text_chars,
        "max_blocks": grant.scope.disclosure.max_blocks,
    }
    if isinstance(step, NavigateStep):
        assert source is not None
        return {
            **common,
            "tab": step.tab,
            "target_ref": step.target.ref,
            "expected_document_epoch": source.observation.document_epoch,
        }
    if isinstance(step, RevealStep):
        assert source is not None
        return {
            **common,
            "tab": step.tab,
            "target_kind": step.target.kind,
            "target_ref": step.target.ref,
            "expected_document_epoch": source.observation.document_epoch,
        }
    if isinstance(step, ObserveStep):
        return {**common, "tab": step.tab}
    if isinstance(step, TabStep):
        return {**common, "action": step.action, "tab": step.tab}
    return {**common, "tab": step.tab, "direction": step.direction}  # HistoryStep


def _final_status(answer: AuthenticatedAnswer) -> TaskStatus:
    if answer.stop_reason in ("user_stopped", "blocked", "planner_failed", "outside_scope"):
        return TaskStatus.FAILED
    return TaskStatus.SUCCEEDED


__all__ = [
    "REQUEST_KEYS",
    "AuthenticatedReadService",
    "AuthenticatedView",
    "BudgetUsage",
    "ProfileSummary",
    "StepResult",
    "validate_request",
]
