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
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.browser.errors import (
    BrowserExecutionNotSupportedError,
    BrowserWorkerError,
    BrowserWorkerLostResponseError,
    BrowserWorkerNotConfiguredError,
    BrowserWorkerRejectedError,
    BrowserWorkerUnavailableError,
    StaleWorkerResultError,
)
from app.browser.protocol import DispatchRequest, DispatchResponse
from app.domain.action_status import ActionStatus, AttemptOutcome
from app.domain.booking import booking_reference, parse_booking_proposal
from app.domain.browser_dispatch import (
    BrowserEffect,
    DispatchStatus,
    LookupStatus,
    attempt_outcome_for,
)
from app.domain.errors import ActionNotFoundError
from app.domain.sites import site_trust
from app.repositories.actions import ActionRepository
from app.repositories.browser import BrowserRepository
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


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a dispatch established, before it is written anywhere."""

    outcome: AttemptOutcome
    dispatch_status: DispatchStatus
    submitted: bool
    error_code: str | None
    observation_id: uuid.UUID | None
    result: dict[str, Any]


class BrowserExecutionService:
    """Drives `commit_booking` and its reconciliation. Owns no browser itself."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: BrowserWorkerConfig | None,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._worker = worker

    @property
    def is_configured(self) -> bool:
        return self._worker is not None

    # ---- worker identity ----------------------------------------------------

    def _client(self) -> BrowserWorkerClient:
        if self._worker is None:
            raise BrowserWorkerNotConfiguredError()
        return BrowserWorkerClient(
            base_url=self._worker.base_url,
            token=self._worker.token,
            runtime_generation=self._runtime_generation,
            timeout_seconds=self._worker.timeout_seconds,
        )

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

    # ---- execution ----------------------------------------------------------

    async def execute_booking(self, action_id: uuid.UUID) -> ActionView:
        """Approve -> attempt -> browser -> outcome, for one booking action."""
        view = await self._actions.get_action(action_id)
        action = view.action
        if action.tool_name != COMMIT_BOOKING:
            raise BrowserExecutionNotSupportedError(action_id, action.tool_name)
        # Parsed from the *persisted* proposal, so the values that execute are
        # the values the approval was bound to, byte for byte. Nothing the
        # worker or the page says later can edit them.
        proposal = parse_booking_proposal(action_id, action.proposal)
        reference = booking_reference(action_id)

        client = self._client()
        try:
            # Everything that can go wrong harmlessly goes wrong here, before
            # the approval is claimed: no attempt started, nothing to reconcile.
            worker_generation = await self._bind_worker(client)

            # --- the last database work before the outside world -------------
            view = await self._actions.start_attempt(action_id)
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

    # ---- reconciliation -----------------------------------------------------

    async def reconcile_booking(self, action_id: uuid.UUID) -> ActionView:
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

        if action.status is ActionStatus.OUTCOME_UNKNOWN:
            view = await self._actions.begin_reconciliation(action_id)

        client = self._client()
        try:
            worker_generation = await self._bind_worker(client)
            dispatch_id = uuid.uuid4()
            async with self._engine.begin() as connection:
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
            site=proposal.site, reference=reference, answer=answer
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
            action_id, result=result, evidence=evidence
        )

    # ---- dispatching --------------------------------------------------------

    async def _dispatch(
        self, client: BrowserWorkerClient, *, request: DispatchRequest
    ) -> Outcome:
        """Call the worker and classify. This is where honesty is enforced."""
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


def _outcome_from_response(response: DispatchResponse) -> Outcome:
    observation = response.observation
    return Outcome(
        outcome=attempt_outcome_for(response.status),
        dispatch_status=DispatchStatus.of(response.status),
        submitted=response.submitted,
        error_code=response.error_code,
        observation_id=_observation_id(observation),
        result=_summarise(response),
    )


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


def _reconciliation_verdict(
    *, site: str, reference: str, answer: Outcome
) -> tuple[AttemptOutcome, dict[str, Any]]:
    """Turn a lookup into a verdict, and record why it was reached.

    `FOUND` resolves the action. `NOT_FOUND` resolves it only where the site's
    own trust declaration says absence is authoritative -- otherwise the action
    stays `OUTCOME_UNKNOWN`, which is the correct answer for essentially every
    real website. `UNKNOWN`, and any failure to look at all, never resolve
    anything.
    """
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
