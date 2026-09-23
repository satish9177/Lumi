"""Execute an approved booking through the browser, and reconcile it afterwards.

This is the module where Milestone 2's durable ledger meets Milestone 3's real
side effect, and the whole design is about the order of two things:

    COMMIT the intent to execute
        ...then...
    talk to the browser

Never the reverse, and never both at once. No database transaction is open while
a browser is being driven. A transaction held across a page load would pin a row
lock for the length of a network round trip, and -- far worse -- a crash would
roll back the record that Lumi had decided to act, while the click had already
happened. The attempt must be durable *before* the button can be pressed, so
that a process that dies immediately afterwards leaves behind evidence that
something may have occurred.

The second theme is refusing to claim knowledge. Every path out of a dispatch is
classified by what Lumi can actually establish:

* the worker verified a postcondition            -> SUCCEEDED
* the worker showed nothing reached the site     -> FAILED
* the worker could not tell, or never answered   -> OUTCOME_UNKNOWN

There is no fourth path, and in particular there is no path where a timeout
becomes a failure.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.browser.managed import ManagedBrowserWorker
from app.browser.errors import (
    BrowserExecutionNotSupportedError,
    BrowserWorkerError,
    BrowserWorkerLostResponseError,
    BrowserWorkerNotConfiguredError,
    BrowserWorkerRejectedError,
    BrowserWorkerUnavailableError,
    StaleWorkerResultError,
)
from app.browser.protocol import (
    DispatchRequest,
    DispatchResponse,
    ProfileSessionRequest,
    TakeoverConfirmRequest,
    TakeoverStartRequest,
)
from app.domain.action_status import ActionStatus, AttemptOutcome
from app.domain.booking import booking_reference, parse_booking_proposal
from app.domain.effects import (
    RECONCILIATION_REGISTRY,
    AbsenceAuthority,
    EffectKind,
    EffectLockedError,
    ReconciliationLimitedError,
    lookup_allowance,
)
from app.domain.browser_profile import BrowserVersions
from app.domain.browser_dispatch import (
    BrowserEffect,
    DispatchStatus,
    LookupStatus,
    attempt_outcome_for,
)
from app.domain.errors import (
    ActionNotFoundError,
    DestinationNotAllowedError,
    InvalidActionTransitionError,
    PublicInspectionNotConfiguredError,
    StaleActionRevisionError,
)
from app.domain.page_observation import INSPECT_PUBLIC_PAGE, PUBLIC_WEB_SITE, PageObservation
from app.domain.transfers import DOWNLOAD_TO_QUARANTINE
from app.domain.public_url import PublicUrlPolicy, UrlPolicyError
from app.domain.sites import site_trust
from app.repositories.actions import ActionRepository, AttemptRecord
from app.repositories.browser import BrowserRepository
from app.repositories.observations import ObservationRepository
from app.repositories.tasks import TaskRepository
from app.services.actions import ActionService, ActionView

logger = logging.getLogger("lumi.browser.execution")

#: The one tool this milestone executes for real.
COMMIT_BOOKING = "commit_booking"
LOOKUP_BOOKING = "lookup_booking"


@dataclass(frozen=True, slots=True)
class BrowserWorkerConfig:
    base_url: str
    token: SecretStr
    timeout_seconds: float


WorkerSource = BrowserWorkerConfig | ManagedBrowserWorker


async def open_worker_client(
    worker: WorkerSource | None, runtime_generation: uuid.UUID
) -> BrowserWorkerClient:
    """A client for the current worker. A managed worker is (re)started here."""
    if worker is None:
        raise BrowserWorkerNotConfiguredError()
    if isinstance(worker, ManagedBrowserWorker):
        endpoint = await worker.endpoint()
        base_url, token, timeout = endpoint.base_url, endpoint.token, endpoint.timeout_seconds
    else:
        base_url, token, timeout = worker.base_url, worker.token, worker.timeout_seconds
    return BrowserWorkerClient(
        base_url=base_url,
        token=token,
        runtime_generation=runtime_generation,
        timeout_seconds=timeout,
    )


@dataclass(frozen=True, slots=True)
class WorkerProfileOpen:
    """What the worker reported after opening a persistent browser profile.

    Note the absence: no path, no directory, no cookie, no storage. The only
    thing that comes back is which browser opened it and whether this worker
    holds the exclusive OS handle on the directory.
    """

    profile_id: uuid.UUID
    worker_generation: uuid.UUID
    versions: BrowserVersions
    lock_held: bool


@dataclass(frozen=True, slots=True)
class TakeoverCheckOutcome:
    """What the worker's deterministic post-takeover check established.

    Note the absence: no page text, no title, no URL, no raw account
    identity. `account_fingerprint`, if present, is already a hash.
    """

    status: str
    scope: str
    credential_surface: bool
    signals: tuple[str, ...]
    account_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a dispatch established, before it is written anywhere."""

    outcome: AttemptOutcome
    dispatch_status: DispatchStatus
    submitted: bool
    error_code: str | None
    observation_id: uuid.UUID | None
    result: dict[str, Any]
    #: The worker's raw observation. Only a reviewed reader (the inspection
    #: path) validates and stores it; the booking path never looks at it.
    observation: dict[str, Any] = field(default_factory=dict)


class BrowserExecutionService:
    """Drives `commit_booking` and its reconciliation. Owns no browser itself."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
        public_policy: PublicUrlPolicy | None = None,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._public_policy = public_policy or PublicUrlPolicy()

    @property
    def is_configured(self) -> bool:
        return self._worker is not None

    # ---- worker identity ----------------------------------------------------

    async def _client(self) -> BrowserWorkerClient:
        return await open_worker_client(self._worker, self._runtime_generation)

    async def _bind_worker(self, client: BrowserWorkerClient) -> uuid.UUID:
        """Handshake, then persist the worker generation we are about to use.

        Done *before* the approval is claimed. If the worker has restarted, or
        is not there at all, that is discovered while nothing is yet at stake --
        no attempt started, no approval consumed, and nothing to reconcile.
        """
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

    # ---- persistent browser profiles (Milestone 8a S1) ----------------------

    async def open_browser_profile(
        self,
        *,
        profile_id: uuid.UUID,
        recorded_chromium_build: str | None,
        headed: bool = False,
    ) -> "WorkerProfileOpen":
        """Have the worker open one persistent profile, and report what opened it.

        Deliberately not an execution attempt and not a dispatch: opening a
        profile touches no site, changes nothing outside the machine, and has
        nothing to reconcile. It never reaches the action ledger.

        The request carries an id and the build the database last recorded. It
        carries no path -- the worker derives the directory from the id -- and
        the response carries none either. `headed` is Milestone 8a S2's manual
        login: a visible Chromium window for the human to drive.
        """
        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)
            answer = await client.open_profile(
                ProfileSessionRequest(
                    profile_id=profile_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    recorded_chromium_build=recorded_chromium_build,
                    headed=headed,
                )
            )
        finally:
            await client.aclose()
        return WorkerProfileOpen(
            profile_id=answer.profile_id,
            worker_generation=answer.worker_generation,
            versions=BrowserVersions(
                chromium_build=answer.chromium_build or "",
                playwright_version=answer.playwright_version or "",
                app_version="",
            ),
            lock_held=answer.lock_held,
        )

    async def close_browser_profile(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID
    ) -> bool:
        """Close the persistent context and release its exclusive OS handle."""
        client = await self._client()
        try:
            answer = await client.close_profile(
                ProfileSessionRequest(
                    profile_id=profile_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                )
            )
        finally:
            await client.aclose()
        return answer.status == "CLOSED"

    # ---- manual login takeover (Milestone 8a S2) -----------------------------

    async def start_takeover(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str
    ) -> str:
        """Navigate the profile's headed tab to its own site. No planner call,
        no observation, no provider call is reachable from this method."""
        client = await self._client()
        try:
            answer = await client.start_takeover(
                TakeoverStartRequest(
                    profile_id=profile_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    site=site,
                )
            )
        finally:
            await client.aclose()
        return answer.status

    async def confirm_takeover(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str
    ) -> "TakeoverCheckOutcome":
        """The one deterministic check run when a takeover ends."""
        client = await self._client()
        try:
            answer = await client.confirm_takeover(
                TakeoverConfirmRequest(
                    profile_id=profile_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    site=site,
                )
            )
        finally:
            await client.aclose()
        return TakeoverCheckOutcome(
            status=answer.status,
            scope=answer.scope,
            credential_surface=answer.credential_surface,
            signals=tuple(answer.signals),
            account_fingerprint=answer.account_fingerprint,
        )

    # ---- execution ----------------------------------------------------------

    async def execute(self, action_id: uuid.UUID, *, expected_revision: int | None = None) -> ActionView:
        """Select the reviewed executor for this action's tool, or refuse.

        A closed mapping, not a registry lookup by caller-supplied name: the
        tool name comes from the immutable persisted action.
        """
        view = await self._actions.get_action(action_id)
        tool_name = view.action.tool_name
        if tool_name == COMMIT_BOOKING:
            return await self.execute_booking(action_id, expected_revision=expected_revision)
        if tool_name == INSPECT_PUBLIC_PAGE:
            return await self.execute_inspection(action_id, expected_revision=expected_revision)
        raise BrowserExecutionNotSupportedError(action_id, tool_name)

    async def execute_booking(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Approve -> attempt -> browser -> outcome, for one booking action.

        `expected_revision` is checked early here for a cheap refusal, and again
        by `start_attempt` inside the transaction that claims the approval, which
        is the check that actually binds the execution to the reviewed revision.
        """
        view = await self._actions.get_action(action_id)
        action = view.action
        if action.tool_name != COMMIT_BOOKING:
            raise BrowserExecutionNotSupportedError(action_id, action.tool_name)
        if expected_revision is not None and action.revision != expected_revision:
            raise StaleActionRevisionError(action_id, expected_revision, action.revision)
        if action.status is not ActionStatus.APPROVED:
            raise InvalidActionTransitionError(action_id, action.status, ActionStatus.EXECUTING)
        # Parsed from the *persisted* proposal, so the values that execute are
        # the values the approval was bound to, byte for byte. Nothing the
        # worker or the page says later can edit them.
        proposal = parse_booking_proposal(action_id, action.proposal)
        reference = booking_reference(action_id)

        client = await self._client()
        try:
            # Everything that can go wrong harmlessly goes wrong here, before
            # the approval is claimed: no attempt started, nothing to reconcile.
            worker_generation = await self._bind_worker(client)

            # --- the last database work before the outside world -------------
            try:
                view = await self._actions.start_attempt(
                    action_id, expected_revision=expected_revision
                )
            except EffectLockedError:
                # M10 S5 review finding 2: an approval given while another consequential effect was in flight or
                # unknown must not stay spendable after that one is settled. It is withdrawn here; a booking
                # needs a fresh card and a fresh approval once the earlier outcome is known.
                await self._withdraw_locked_approval(action_id)
                raise
            attempt = next(a for a in view.attempts if a.finished_at is None)
            dispatch_id = uuid.uuid4()
            try:
                async with self._engine.begin() as connection:
                    await BrowserRepository(connection).insert_dispatch(
                        dispatch_id=dispatch_id,
                        action_id=action_id,
                        attempt_id=attempt.id,
                        worker_generation=worker_generation,
                        operation=COMMIT_BOOKING,
                        site=proposal.site,
                        effect=BrowserEffect.CONSEQUENTIAL,
                    )
            except Exception:
                # The attempt exists but no browser was ever contacted, so this
                # is a failure Lumi can stand behind. Closing it here beats
                # leaving the action EXECUTING until the next restart recovers
                # it as an unknown it does not have to be.
                logger.exception("could not record the browser dispatch")
                return await self._actions.finish_attempt(
                    action_id,
                    outcome=AttemptOutcome.FAILED,
                    error_code="dispatch_not_recorded",
                )
            # --- committed. Nothing below holds a transaction ----------------

            outcome = await self._dispatch(
                client,
                request=DispatchRequest(
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    action_id=action_id,
                    attempt_id=attempt.id,
                    operation=COMMIT_BOOKING,
                    site=proposal.site,
                    input={
                        "reference": reference,
                        "proposal": proposal.model_dump(mode="json"),
                    },
                ),
            )
        finally:
            await client.aclose()

        await self._close_dispatch(dispatch_id, outcome)
        logger.info(
            "browser execution finished",
            extra={
                "action_id": str(action_id),
                "attempt_id": str(attempt.id),
                "runtime_generation": str(self._runtime_generation),
                "worker_generation": str(worker_generation),
                "dispatch_id": str(dispatch_id),
                "operation": COMMIT_BOOKING,
                "observation_id": str(outcome.observation_id) if outcome.observation_id else None,
                "outcome": outcome.outcome.value,
                "outcome_is_known": outcome.outcome is not AttemptOutcome.OUTCOME_UNKNOWN,
                "submitted": outcome.submitted,
                "error_code": outcome.error_code,
            },
        )
        return await self._actions.finish_attempt(
            action_id,
            outcome=outcome.outcome,
            result=outcome.result,
            error_code=outcome.error_code,
        )

    async def execute_inspection(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Approve -> attempt -> isolated read -> stored observation, for one page.

        Same order as a booking: everything that can fail harmlessly is checked
        before the approval is claimed, the attempt and dispatch are committed
        before the worker is contacted, and no transaction is open while the
        page loads. The difference is what success means -- a validated,
        hashed observation stored in the transaction that finishes the attempt.
        """
        from app.services.page_inspection import parse_inspection_proposal

        view = await self._actions.get_action(action_id)
        action = view.action
        if action.tool_name != INSPECT_PUBLIC_PAGE:
            raise BrowserExecutionNotSupportedError(action_id, action.tool_name)
        if expected_revision is not None and action.revision != expected_revision:
            raise StaleActionRevisionError(action_id, expected_revision, action.revision)
        if action.status is not ActionStatus.APPROVED:
            raise InvalidActionTransitionError(action_id, action.status, ActionStatus.EXECUTING)
        proposal = parse_inspection_proposal(action_id, action.proposal)
        # The runtime policy may have narrowed since approval. Refused before
        # the approval is claimed, so nothing is consumed.
        if not self._public_policy.configured:
            raise PublicInspectionNotConfiguredError()
        try:
            if self._public_policy.check(proposal.url).url != proposal.url:
                raise DestinationNotAllowedError("not_canonical")
        except UrlPolicyError as error:
            raise DestinationNotAllowedError(error.code) from None

        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)

            # --- the last database work before the outside world -------------
            view = await self._actions.start_attempt(action_id, expected_revision=expected_revision)
            attempt = next(a for a in view.attempts if a.finished_at is None)
            dispatch_id = uuid.uuid4()
            try:
                async with self._engine.begin() as connection:
                    await BrowserRepository(connection).insert_dispatch(
                        dispatch_id=dispatch_id,
                        action_id=action_id,
                        attempt_id=attempt.id,
                        worker_generation=worker_generation,
                        operation=INSPECT_PUBLIC_PAGE,
                        site=PUBLIC_WEB_SITE,
                        effect=BrowserEffect.READ_ONLY,
                    )
            except Exception:
                logger.exception("could not record the browser dispatch")
                return await self._actions.finish_attempt(
                    action_id, outcome=AttemptOutcome.FAILED, error_code="dispatch_not_recorded"
                )
            # --- committed. Nothing below holds a transaction ----------------

            outcome = await self._dispatch(
                client,
                request=DispatchRequest(
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    action_id=action_id,
                    attempt_id=attempt.id,
                    operation=INSPECT_PUBLIC_PAGE,
                    site=PUBLIC_WEB_SITE,
                    input={"url": proposal.url},
                ),
            )
        finally:
            await client.aclose()

        observation: PageObservation | None = None
        if outcome.outcome is AttemptOutcome.SUCCEEDED:
            try:
                observation = PageObservation.model_validate(outcome.observation)
                if (
                    observation.requested_url != proposal.url
                    or observation.observation_id != outcome.observation_id
                ):
                    raise ValueError("observation does not answer this dispatch")
            except (ValidationError, ValueError):
                # The page was read, but what came back cannot be trusted as
                # evidence. Known: there is no usable observation.
                observation = None
                outcome = Outcome(
                    outcome=AttemptOutcome.FAILED,
                    dispatch_status=DispatchStatus.FAILED_BEFORE_EFFECT,
                    submitted=False,
                    error_code="observation_invalid",
                    observation_id=None,
                    result={**outcome.result, "status": DispatchStatus.FAILED_BEFORE_EFFECT.value},
                )

        await self._close_dispatch(dispatch_id, outcome)
        logger.info(
            "page inspection finished",
            extra={
                "action_id": str(action_id),
                "attempt_id": str(attempt.id),
                "runtime_generation": str(self._runtime_generation),
                "worker_generation": str(worker_generation),
                "dispatch_id": str(dispatch_id),
                "operation": INSPECT_PUBLIC_PAGE,
                "observation_id": str(outcome.observation_id) if outcome.observation_id else None,
                "outcome": outcome.outcome.value,
                "error_code": outcome.error_code,
            },
        )
        result = _inspection_summary(outcome, observation)
        if observation is None:
            return await self._actions.finish_attempt(
                action_id, outcome=outcome.outcome, result=result, error_code=outcome.error_code
            )
        stored = observation

        async def store(connection: AsyncConnection, finished: AttemptRecord) -> None:
            await ObservationRepository(connection).insert(
                observation=stored,
                task_id=action.task_id,
                action_id=action_id,
                attempt_id=finished.id,
                dispatch_id=dispatch_id,
                worker_generation=worker_generation,
            )

        return await self._actions.finish_attempt(
            action_id, outcome=AttemptOutcome.SUCCEEDED, result=result, record=store
        )

    # ---- controlled downloads (Milestone 10 S2) ------------------------------

    async def open_download_worker(self) -> tuple[BrowserWorkerClient, uuid.UUID]:
        """Handshake and persist the worker generation BEFORE the transfer's step authorization is spent."""
        client = await self._client()
        try:
            return client, await self._bind_worker(client)
        except BaseException:
            await client.aclose()
            raise

    async def run_download(
        self,
        client: BrowserWorkerClient,
        worker_generation: uuid.UUID,
        *,
        action_id: uuid.UUID,
        attempt_id: uuid.UUID,
        url: str,
        transfer_id: uuid.UUID,
        max_bytes: int,
    ) -> Outcome:
        """Record the dispatch (committed), fetch into the quarantine, close the dispatch. No transaction is
        open while the worker fetches. The classification is the shared, honest one: any doubt after the
        request may have left is OUTCOME_UNKNOWN."""
        dispatch_id = uuid.uuid4()
        try:
            async with self._engine.begin() as connection:
                await BrowserRepository(connection).insert_dispatch(
                    dispatch_id=dispatch_id,
                    action_id=action_id,
                    attempt_id=attempt_id,
                    worker_generation=worker_generation,
                    operation=DOWNLOAD_TO_QUARANTINE,
                    site=PUBLIC_WEB_SITE,
                    effect=BrowserEffect.DOWNLOAD,
                )
        except Exception:
            logger.exception("could not record the download dispatch")
            return Outcome(
                outcome=AttemptOutcome.FAILED,
                dispatch_status=DispatchStatus.FAILED_BEFORE_EFFECT,
                submitted=False,
                error_code="dispatch_not_recorded",
                observation_id=None,
                result={},
            )
        outcome = await self._dispatch(
            client,
            request=DispatchRequest(
                dispatch_id=dispatch_id,
                runtime_generation=self._runtime_generation,
                expected_worker_generation=worker_generation,
                action_id=action_id,
                attempt_id=attempt_id,
                operation=DOWNLOAD_TO_QUARANTINE,
                site=PUBLIC_WEB_SITE,
                input={"url": url, "transfer_id": str(transfer_id), "max_bytes": max_bytes},
            ),
        )
        await self._close_dispatch(dispatch_id, outcome)
        return outcome

    # ---- reconciliation -----------------------------------------------------

    async def reconcile_booking(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Establish what happened, by reading. It never books anything.

        `lookup_booking` is read-only: it navigates to a page and reads it.
        There is no submission on this path at all, which is what makes it safe
        to run against an action whose outcome is unknown -- running it cannot
        create the thing it is looking for.
        """
        view = await self._actions.get_action(action_id)
        action = view.action
        if action.tool_name != COMMIT_BOOKING:
            raise BrowserExecutionNotSupportedError(action_id, action.tool_name)
        proposal = parse_booking_proposal(action_id, action.proposal)
        reference = booking_reference(action_id)
        if action.status not in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING):
            # Only an unknown outcome has anything to reconcile. Refused before
            # any browser is contacted.
            raise InvalidActionTransitionError(action_id, action.status, ActionStatus.RECONCILING)
        if expected_revision is not None and action.revision != expected_revision:
            raise StaleActionRevisionError(action_id, expected_revision, action.revision)
        # Milestone 10 S5: bounded. Counted from the durable lookup dispatches, so a restart does not reset
        # it. Refused before anything is written: the action stays exactly as uncertain as it was.
        await self._require_lookup_allowed(action_id)

        if action.status is ActionStatus.OUTCOME_UNKNOWN:
            view = await self._actions.begin_reconciliation(
                action_id, expected_revision=action.revision
            )
        # The verdict is recorded only against the revision this call started
        # from, so two concurrent checks cannot both write a verdict.
        reconciling_revision = view.action.revision

        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)
            dispatch_id = uuid.uuid4()
            async with self._engine.begin() as connection:
                # M10 S5 review finding 3: the bound is counted and the lookup recorded in ONE transaction under
                # the task row lock, so concurrent reconcile calls cannot all pass the count before any inserts.
                await TaskRepository(connection).lock_task(action.task_id)
                records = await BrowserRepository(connection).list_dispatches(action_id)
                self._check_lookup_allowance(records)
                # M10 S5 review finding 1: absence can only be authoritative once the commit can no longer happen.
                absence_fenced = _commit_is_settled(records, current_worker=worker_generation)
                await BrowserRepository(connection).insert_dispatch(
                    dispatch_id=dispatch_id,
                    action_id=action_id,
                    # No attempt: a lookup is not an execution attempt, and
                    # recording it as one would corrupt the attempt count that
                    # everything else is measured against.
                    attempt_id=None,
                    worker_generation=worker_generation,
                    operation=LOOKUP_BOOKING,
                    site=proposal.site,
                    effect=BrowserEffect.READ_ONLY,
                )
            answer = await self._lookup(
                client,
                request=DispatchRequest(
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    action_id=action_id,
                    attempt_id=None,
                    operation=LOOKUP_BOOKING,
                    site=proposal.site,
                    input={"reference": reference},
                ),
            )
        finally:
            await client.aclose()

        await self._close_dispatch(dispatch_id, answer)
        result, evidence = _reconciliation_verdict(
            site=proposal.site, reference=reference, answer=answer, absence_fenced=absence_fenced
        )
        logger.info(
            "browser reconciliation finished",
            extra={
                "action_id": str(action_id),
                "runtime_generation": str(self._runtime_generation),
                "worker_generation": str(worker_generation),
                "dispatch_id": str(dispatch_id),
                "operation": LOOKUP_BOOKING,
                "observation_id": str(answer.observation_id) if answer.observation_id else None,
                "outcome": result.value,
                "outcome_is_known": result is not AttemptOutcome.OUTCOME_UNKNOWN,
                "error_code": answer.error_code,
            },
        )
        return await self._actions.finish_reconciliation(
            action_id, result=result, evidence=evidence, expected_revision=reconciling_revision
        )

    async def _withdraw_locked_approval(self, action_id: uuid.UUID) -> None:
        try:
            current = await self._actions.get_action(action_id)
            await self._actions.reject_action(action_id, expected_revision=current.action.revision, reason="effect_locked")
        except Exception:  # noqa: BLE001 - the refusal stands either way; nothing was dispatched.
            logger.warning("a booking approval refused by the effect lock could not be withdrawn")

    async def _require_lookup_allowed(self, action_id: uuid.UUID) -> None:
        async with self._engine.connect() as connection:
            records = await BrowserRepository(connection).list_dispatches(action_id)
        self._check_lookup_allowance(records)

    @staticmethod
    def _check_lookup_allowance(records: list[Any]) -> None:
        rule = RECONCILIATION_REGISTRY[EffectKind.EXTERNAL_MUTATION]
        action_id = records[0].action_id if records else "?"
        now = datetime.now(UTC)
        # A lookup still open is in flight -- unless it is older than any worker call can last (a runtime that
        # died mid-lookup leaves its row open forever; that must not block reconciliation for good).
        if any(
            record.operation == LOOKUP_BOOKING and record.finished_at is None
            and (now - record.started_at).total_seconds() < _LOOKUP_IN_FLIGHT_SECONDS
            for record in records
        ):
            raise ReconciliationLimitedError(str(action_id), reason="lookup_in_flight", retry_after_seconds=30)
        ages = [(now - record.started_at).total_seconds() for record in records if record.operation == LOOKUP_BOOKING]
        allowed, reason, retry_after = lookup_allowance(rule, previous_ages_seconds=ages)
        if not allowed:
            raise ReconciliationLimitedError(str(action_id), reason=reason, retry_after_seconds=retry_after)

    # ---- dispatching --------------------------------------------------------

    async def _dispatch(
        self, client: BrowserWorkerClient, *, request: DispatchRequest
    ) -> Outcome:
        return await dispatch_and_classify(client, request=request)

    async def _lookup(self, client: BrowserWorkerClient, *, request: DispatchRequest) -> Outcome:
        outcome = await self._dispatch(client, request=request)
        if outcome.outcome is AttemptOutcome.FAILED:
            # A lookup that failed did not establish absence. Failing to look is
            # never evidence that there is nothing to see.
            return Outcome(
                outcome=AttemptOutcome.OUTCOME_UNKNOWN,
                dispatch_status=outcome.dispatch_status,
                submitted=False,
                error_code=outcome.error_code,
                observation_id=outcome.observation_id,
                result=outcome.result,
            )
        return outcome

    async def _close_dispatch(self, dispatch_id: uuid.UUID, outcome: Outcome) -> None:
        async with self._engine.begin() as connection:
            await BrowserRepository(connection).finish_dispatch(
                dispatch_id=dispatch_id,
                status=outcome.dispatch_status,
                submitted=outcome.submitted,
                observation_id=outcome.observation_id,
                error_code=outcome.error_code,
                duration_ms=outcome.result.get("duration_ms"),
                result=outcome.result,
            )

    # ---- read model ---------------------------------------------------------

    async def describe_dispatches(self, action_id: uuid.UUID) -> list[dict[str, Any]]:
        async with self._engine.connect() as connection:
            if await ActionRepository(connection).get_action(action_id) is None:
                raise ActionNotFoundError(action_id)
            records = await BrowserRepository(connection).list_dispatches(action_id)
        return [
            {
                "id": str(record.id),
                "attempt_id": str(record.attempt_id) if record.attempt_id else None,
                "worker_generation": str(record.worker_generation),
                "operation": record.operation,
                "site": record.site,
                "effect": record.effect.value,
                "status": record.status.value,
                "submitted": record.submitted,
                "observation_id": (
                    str(record.observation_id) if record.observation_id else None
                ),
                "error_code": record.error_code,
                "duration_ms": record.duration_ms,
                "started_at": record.started_at.isoformat(),
                "finished_at": record.finished_at.isoformat() if record.finished_at else None,
            }
            for record in records
        ]


# ---- classification ---------------------------------------------------------


async def dispatch_and_classify(
    client: BrowserWorkerClient, *, request: DispatchRequest
) -> Outcome:
    """Call the worker and classify. This is where honesty is enforced.

    Shared by every executor -- booking, page inspection and research steps --
    so there is one place that decides what a lost answer means, and no
    executor can quietly decide a timeout was a failure.
    """
    try:
        response = await client.dispatch(request)
    except BrowserWorkerError as error:
        return _outcome_from_error(error)
    except Exception as error:  # noqa: BLE001 - fail safe, never fail confident.
        logger.exception("unexpected browser dispatch failure")
        return Outcome(
            outcome=AttemptOutcome.OUTCOME_UNKNOWN,
            dispatch_status=DispatchStatus.OUTCOME_UNKNOWN,
            submitted=False,
            error_code="dispatch_error",
            observation_id=None,
            result={"reason": type(error).__name__},
        )
    return _outcome_from_response(response)


def _outcome_from_response(response: DispatchResponse) -> Outcome:
    observation = response.observation
    return Outcome(
        outcome=attempt_outcome_for(response.status),
        dispatch_status=DispatchStatus.of(response.status),
        submitted=response.submitted,
        error_code=response.error_code,
        observation_id=_observation_id(observation),
        result=_summarise(response),
        observation=observation,
    )


#: Refusal details a failed inspection may keep: stable codes and numbers only.
_KEPT_INSPECTION_FAILURE_FIELDS = ("http_status", "redirect_refusal", "refusal")


def _inspection_summary(outcome: Outcome, observation: PageObservation | None) -> dict[str, Any]:
    """The attempt result for an inspection: identity and metadata, no page text."""
    summary: dict[str, Any] = {
        key: outcome.result[key]
        for key in ("operation", "status", "worker_generation", "dispatch_id", "duration_ms", "replayed")
        if key in outcome.result
    }
    summary["submitted"] = False
    if observation is not None:
        summary.update(
            observation_id=str(observation.observation_id),
            content_hash=observation.content_hash,
            final_url=observation.final_url,
            document_epoch=observation.document_epoch,
            truncated=observation.truncated,
            settled=observation.settled,
            block_count=len(observation.blocks),
            link_count=len(observation.links),
        )
    else:
        for key in _KEPT_INSPECTION_FAILURE_FIELDS:
            value = outcome.observation.get(key)
            if isinstance(value, (int, str)) and not isinstance(value, bool) and len(str(value)) <= 64:
                summary[key] = value
    return summary


def _outcome_from_error(error: BrowserWorkerError) -> Outcome:
    """Map an RPC failure onto what it actually proves.

    `outcome_is_known` is declared by the exception type, not inferred here, so
    adding an error class forces whoever adds it to answer the only question
    that matters: could the side effect have happened?
    """
    if isinstance(error, BrowserWorkerUnavailableError | BrowserWorkerRejectedError):
        outcome = AttemptOutcome.FAILED
        status = DispatchStatus.FAILED_BEFORE_EFFECT
    elif isinstance(error, BrowserWorkerLostResponseError | StaleWorkerResultError):
        outcome = AttemptOutcome.OUTCOME_UNKNOWN
        status = DispatchStatus.OUTCOME_UNKNOWN
    else:  # pragma: no cover - the base class is not raised directly.
        outcome = AttemptOutcome.OUTCOME_UNKNOWN
        status = DispatchStatus.OUTCOME_UNKNOWN
    assert (outcome is not AttemptOutcome.OUTCOME_UNKNOWN) == error.outcome_is_known
    return Outcome(
        outcome=outcome,
        dispatch_status=status,
        submitted=False,
        error_code=error.code,
        observation_id=None,
        result={"reason": getattr(error, "reason", error.code)},
    )


def _observation_id(observation: dict[str, Any]) -> uuid.UUID | None:
    raw = observation.get("observation_id")
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:  # pragma: no cover - the worker always sends a UUID.
        return None


#: Fields worth keeping from an observation. Everything else is dropped: the
#: ledger stores what happened, not a copy of a web page. No HTML is persisted,
#: and no page prose -- a proposal's timeline should not be able to quote a site
#: back at whoever reads it later.
_KEPT_OBSERVATION_FIELDS = (
    "page",
    "booking_id",
    "reference",
    "postcondition_verified",
    "changed_facts",
    "receipt",
    "result",
    "booking_count",
    "booking",
    "available",
)


def _summarise(response: DispatchResponse) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "operation": response.operation,
        "status": response.status.value,
        "worker_generation": str(response.worker_generation),
        "dispatch_id": str(response.dispatch_id),
        "duration_ms": response.duration_ms,
        "submitted": response.submitted,
        "replayed": response.replayed,
    }
    observation_id = _observation_id(response.observation)
    if observation_id is not None:
        summary["observation_id"] = str(observation_id)
    for field in _KEPT_OBSERVATION_FIELDS:
        if field in response.observation:
            summary[field] = response.observation[field]
    return summary


#: Longer than any worker call is allowed to take (the worker's own operation timeout is capped at 3600 s).
_LOOKUP_IN_FLIGHT_SECONDS = 3600

#: Codes the RUNTIME assigns when it lost a commit's answer. The worker itself may still be running that
#: commit; only a worker's own answer (or its death) says the operation is over.
_RUNTIME_SIDE_LOSS_CODES = frozenset(
    {"browser_worker_lost_response", "stale_worker_result", "dispatch_error", "runtime_restart"}
)


def _commit_is_settled(records: list[Any], *, current_worker: uuid.UUID) -> bool:
    """Can the booking's commit still take effect? (M10 S5 review finding 1.)

    Settled when every consequential dispatch either has the worker's own final answer (the operation ended
    inside the worker), or was addressed to a worker generation that is not the one answering now (that
    worker process is gone, and its browser with it). A dispatch still open, or one whose answer the runtime
    lost while that same worker is still alive, may still be submitting: absence proves nothing yet.
    """
    for record in records:
        if record.operation != COMMIT_BOOKING:
            continue
        if record.finished_at is None or record.status is DispatchStatus.DISPATCHED:
            return False
        if record.worker_generation == current_worker and record.error_code in _RUNTIME_SIDE_LOSS_CODES:
            return False
    return True


def _reconciliation_verdict(
    *, site: str, reference: str, answer: Outcome, absence_fenced: bool = True
) -> tuple[AttemptOutcome, dict[str, Any]]:
    """Turn a lookup into a verdict, and record why it was reached.

    `FOUND` resolves the action. `NOT_FOUND` resolves it only where the site's
    own trust declaration says absence is authoritative -- otherwise the action
    stays `OUTCOME_UNKNOWN`, which is the correct answer for essentially every
    real website. `UNKNOWN`, and any failure to look at all, never resolve
    anything.
    """
    # The registry says WHO may call absence authoritative for an external mutation: the reviewed site
    # declaration, and nobody else (never the lookup, the worker, the page or a model).
    if RECONCILIATION_REGISTRY[EffectKind.EXTERNAL_MUTATION].absence is not AbsenceAuthority.SITE_DECLARED:
        raise RuntimeError("the effect registry no longer delegates booking absence to the site declaration")
    trust = site_trust(site)
    evidence: dict[str, Any] = {
        "source": "browser_lookup",
        "site": site,
        "reference": reference,
        "operation": LOOKUP_BOOKING,
        "observation_id": str(answer.observation_id) if answer.observation_id else None,
    }
    if answer.outcome is AttemptOutcome.OUTCOME_UNKNOWN:
        evidence.update(
            lookup=LookupStatus.UNKNOWN.value,
            reason=answer.error_code or "the lookup did not return an answer",
            absence_is_authoritative=trust.lookup_absence_is_authoritative,
        )
        return AttemptOutcome.OUTCOME_UNKNOWN, evidence

    lookup = str(answer.result.get("result", LookupStatus.UNKNOWN.value))
    evidence["lookup"] = lookup
    if lookup == LookupStatus.FOUND.value:
        evidence.update(
            booking_id=answer.result.get("booking_id"),
            booking_count=answer.result.get("booking_count"),
            booking=answer.result.get("booking"),
        )
        return AttemptOutcome.SUCCEEDED, evidence
    if lookup == LookupStatus.NOT_FOUND.value and trust.lookup_absence_is_authoritative and not absence_fenced:
        evidence.update(
            absence_is_authoritative=False,
            reason="the original submission may still be in flight in a live browser worker",
        )
        return AttemptOutcome.OUTCOME_UNKNOWN, evidence
    if lookup == LookupStatus.NOT_FOUND.value and trust.lookup_absence_is_authoritative:
        evidence.update(
            absence_is_authoritative=True,
            rationale=trust.rationale,
        )
        return AttemptOutcome.FAILED, evidence
    evidence.update(
        absence_is_authoritative=trust.lookup_absence_is_authoritative,
        reason=(
            "this site does not guarantee that an absent booking cannot exist"
            if lookup == LookupStatus.NOT_FOUND.value
            else "the site could not answer"
        ),
    )
    return AttemptOutcome.OUTCOME_UNKNOWN, evidence
