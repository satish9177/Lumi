"""Milestone 7a: inspect one approved public page, and record a grounded answer.

The workflow reuses the durable controller unchanged and adds nothing it does
not need:

    task (page_inspection: url + question)                      -- created by main
      -> prepare: an immutable `inspect_public_page` proposal
         (canonical URL, host, question, policy version, limits,
         provider disclosure) + an exact, expiring approval request
      -> trusted click: approve (the existing single-use approval)
      -> execute: claim approval -> attempt -> dispatch record ->
         worker -> validated observation stored WITH the finished attempt
      -> main reads the observation, asks its own model router, and
         records the answer here, where grounding is checked again

Python never calls a model provider and never holds a provider credential. The
worker never sees the question. The answer can only be recorded for the latest
observation of the task, bound to its content hash, and only if every quote it
cites really is in the cited block.

Outcome semantics for this read-only operation, stated once:

* SUCCEEDED -- a validated observation is durably stored with the attempt.
* FAILED -- Lumi knows no usable observation was obtained (the destination was
  refused, the page failed to load, or its result was unusable).
* OUTCOME_UNKNOWN -- the dispatch may have completed but Lumi did not receive
  or record its result (lost response, worker crash, runtime restart). A public
  read has no consequential effect to reconcile, so the action simply stays
  unknown; it is never rewritten as failed, never retried automatically, and a
  repeat is a new action the user approves again.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, RiskTier
from app.domain.errors import (
    ActionNotFoundError,
    DestinationNotAllowedError,
    InspectionProposalError,
    ObservationNotAvailableError,
    PublicInspectionNotConfiguredError,
    StaleObservationError,
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.page_observation import (
    INSPECT_PUBLIC_PAGE,
    MAX_TEXT_CHARS,
    PAGE_INSPECTION_TASK_TYPE,
    DisclosureSpec,
    InspectionProposal,
    PageAnswer,
    parse_question,
    verify_grounding,
)
from app.domain.public_url import PublicUrlPolicy, UrlPolicyError
from app.domain.task_status import TaskEventType, accepts_actions
from app.repositories.actions import ActionRepository
from app.repositories.observations import ObservationRecord, ObservationRepository
from app.repositories.tasks import TaskRepository
from app.services.actions import ActionService, ActionView
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.page_inspection")

#: Everything a page-inspection task request may carry. Provenance fields are
#: written by the trusted desktop layer.
REQUEST_KEYS = frozenset({"type", "text", "url", "question", "source", "request_id", "voice_turn_id"})


def validate_request(request: dict[str, Any], policy: PublicUrlPolicy) -> None:
    """Refuse to store an inspection task that could never be approved.

    The URL must already be in its canonical spelling, so the task, the card
    and the proposal all show the same bytes.
    """
    if not set(request) <= REQUEST_KEYS:
        raise ValueError("unknown request field")
    url = request.get("url")
    if not isinstance(url, str):
        raise ValueError("url")
    checked = policy.check(url)
    if checked.url != url:
        raise UrlPolicyError("not_canonical")
    parse_question(request.get("question"))


def parse_inspection_proposal(action_id: uuid.UUID, proposal: dict[str, Any]) -> InspectionProposal:
    try:
        return InspectionProposal.model_validate(proposal)
    except ValidationError:
        raise InspectionProposalError(action_id) from None


@dataclass(frozen=True, slots=True)
class InspectionView:
    action: ActionView
    observation: ObservationRecord | None


class PageInspectionService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        tasks: TaskService,
        actions: ActionService,
        policy: PublicUrlPolicy,
    ) -> None:
        self._engine = engine
        self._tasks = tasks
        self._actions = actions
        self._policy = policy

    @property
    def policy(self) -> PublicUrlPolicy:
        return self._policy

    def check_destination(self, url: str) -> str:
        if not self._policy.configured:
            raise PublicInspectionNotConfiguredError()
        try:
            checked = self._policy.check(url)
        except UrlPolicyError as error:
            raise DestinationNotAllowedError(error.code) from None
        if checked.url != url:
            raise DestinationNotAllowedError("not_canonical")
        return checked.host

    async def prepare(self, task_id: uuid.UUID, disclosure: DisclosureSpec) -> ActionView:
        """Build the exact proposal and open its approval request."""
        task = await self._tasks.get_task(task_id)
        if task.request.get("type") != PAGE_INSPECTION_TASK_TYPE:
            raise TaskKindMismatchError(task_id, PAGE_INSPECTION_TASK_TYPE)
        if not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        url = str(task.request.get("url", ""))
        host = self.check_destination(url)
        proposal = InspectionProposal(
            url=url,
            host=host,
            question=parse_question(task.request.get("question")),
            policy_version=self._policy.version,
            disclosure=DisclosureSpec(
                recipients=disclosure.recipients,
                max_text_chars=min(disclosure.max_text_chars, MAX_TEXT_CHARS),
            ),
        )
        view = await self._actions.propose_or_reuse_open_action(
            task_id,
            tool_name=INSPECT_PUBLIC_PAGE,
            risk_tier=RiskTier.R1,
            proposal=proposal.model_dump(mode="json"),
        )
        if view.action.status is not ActionStatus.PROPOSED:
            return view
        # Opening the request grants nothing; it makes this exact proposal reviewable.
        return await self._actions.request_approval(
            view.action.id, expected_revision=view.action.revision
        )

    async def describe(self, action_id: uuid.UUID) -> InspectionView:
        view = await self._actions.get_action(action_id)
        if view.action.tool_name != INSPECT_PUBLIC_PAGE:
            raise ActionNotFoundError(action_id)
        async with self._engine.connect() as connection:
            observation = await ObservationRepository(connection).for_action(action_id)
        return InspectionView(action=view, observation=observation)

    async def record_answer(
        self,
        action_id: uuid.UUID,
        *,
        observation_id: uuid.UUID,
        content_hash: str,
        answer: PageAnswer,
        provider: str,
        model: str,
    ) -> InspectionView:
        """Store the first grounded answer for the task's current observation.

        Idempotent: an observation that already has an answer keeps it, and the
        stored answer is returned. Grounding is verified here even though main
        verified it, because this is the authority that stores it.
        """
        async with self._engine.begin() as connection:
            actions = ActionRepository(connection)
            action = await actions.get_action(action_id)
            if action is None or action.tool_name != INSPECT_PUBLIC_PAGE:
                raise ActionNotFoundError(action_id)
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(action.task_id)
            if task is None:  # pragma: no cover - tasks are never deleted.
                raise TaskNotFoundError(action.task_id)
            repository = ObservationRepository(connection)
            record = await repository.get(observation_id)
            if record is None or record.action_id != action_id:
                raise ObservationNotAvailableError(action_id)
            if record.observation.content_hash != content_hash:
                raise StaleObservationError(observation_id)
            latest = await repository.latest_for_task(action.task_id)
            if latest is None or latest.id != record.id:
                raise StaleObservationError(observation_id)
            if record.answer is not None:
                return InspectionView(action=await self._actions.get_action(action_id), observation=record)
            verify_grounding(record.observation, answer)
            updated = await repository.record_answer(
                observation_id=observation_id, answer=answer, provider=provider, model=model
            )
            if updated is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(action.task_id)
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task.id)
            await tasks.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_PAGE_ANSWER_RECORDED,
                payload={
                    "action_id": str(action_id),
                    "observation_id": str(observation_id),
                    "content_hash": content_hash,
                    "answer_status": answer.status,
                    "evidence_count": len(answer.evidence),
                    "provider": provider,
                },
            )
        logger.info(
            "page answer recorded",
            extra={
                "action_id": str(action_id),
                "observation_id": str(observation_id),
                "answer_status": answer.status,
                "provider": provider,
            },
        )
        return InspectionView(action=await self._actions.get_action(action_id), observation=updated)
