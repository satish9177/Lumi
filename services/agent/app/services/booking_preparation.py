"""Search for appointments and prepare a booking proposal, read-only.

This is how a booking action comes into existence from the desktop. The caller
supplies only a task id and, to prepare, a slot id it picked from a search. The
consequential values -- doctor, time, price, currency -- are never accepted from
the caller: the runtime asks the isolated worker to read the slot page *now*,
and the proposal is built from that observation. The worker cannot approve
anything, and page text never reaches the proposal except as the typed slot
fields the reviewed adapter reads.

Neither operation changes the site. Search and slot observation are READ_ONLY
registry operations, and they run before any action exists, so they are not
recorded in the per-action dispatch ledger; they are logged instead.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.browser.client import BrowserWorkerClient
from app.browser.config import DEFAULT_SITE
from app.browser.protocol import DispatchRequest, OperationStatus
from app.domain.action_status import RiskTier
from app.domain.booking import BookingProposal
from app.domain.booking_criteria import BookingCriteria, InvalidBookingCriteriaError
from app.domain.errors import (
    BookingCriteriaError,
    BookingCriteriaMismatchError,
    BookingSlotUnavailableError,
    BrowserObservationError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
)
from app.domain.task_status import accepts_actions
from app.services.actions import ActionService, ActionView
from app.services.booking_tasks import BOOKING_TASK_TYPE, BookingTaskService
from app.services.browser_execution import COMMIT_BOOKING, WorkerSource, open_worker_client
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.booking.preparation")

SEARCH_OPERATION = "search_appointments"
READ_SLOT_OPERATION = "read_available_slots"
MAX_SLOTS = 50
SLOT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class ObservedSlotSummary(BaseModel):
    """One slot as the reviewed adapter read it. Strict: anything else is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_id: str = Field(pattern=SLOT_ID_PATTERN)
    doctor: str = Field(min_length=1, max_length=120)
    specialty: str = Field(max_length=120)
    time: datetime
    price: int = Field(ge=0, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator("time")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("time must carry a UTC offset")
        return value


def task_criteria(task_id: uuid.UUID, request: dict[str, Any]) -> BookingCriteria:
    """The persisted task constraints, or a refusal if they cannot be applied."""
    try:
        return BookingCriteria.from_request(request)
    except InvalidBookingCriteriaError:
        raise BookingCriteriaError(task_id, "stored criteria are unreadable") from None


class BookingPreparationService:
    def __init__(
        self,
        *,
        tasks: TaskService,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
    ) -> None:
        self._tasks = tasks
        self._actions = actions
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._booking_tasks = BookingTaskService(actions)

    async def _booking_task(self, task_id: uuid.UUID) -> dict[str, Any]:
        task = await self._tasks.get_task(task_id)
        if task.request.get("type") != BOOKING_TASK_TYPE:
            raise TaskKindMismatchError(task_id, BOOKING_TASK_TYPE)
        if not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        return task.request

    async def _observe(
        self, operation: str, payload: dict[str, Any], task_id: uuid.UUID
    ) -> tuple[OperationStatus, dict[str, Any]]:
        client: BrowserWorkerClient = await open_worker_client(
            self._worker, self._runtime_generation
        )
        try:
            identity = await client.identify()
            request = DispatchRequest(
                dispatch_id=uuid.uuid4(),
                runtime_generation=self._runtime_generation,
                expected_worker_generation=identity.worker_generation,
                action_id=None,
                attempt_id=None,
                operation=operation,
                site=DEFAULT_SITE,
                input=payload,
            )
            response = await client.dispatch(request)
        finally:
            await client.aclose()
        logger.info(
            "read-only browser observation finished",
            extra={
                "task_id": str(task_id),
                "operation": operation,
                "dispatch_id": str(request.dispatch_id),
                "worker_generation": str(identity.worker_generation),
                "status": response.status.value,
                "submitted": response.submitted,
            },
        )
        if response.submitted:  # pragma: no cover - read-only operations never submit.
            raise BrowserObservationError("unexpected_submission")
        return response.status, response.observation

    async def search(self, task_id: uuid.UUID) -> list[ObservedSlotSummary]:
        """Read the site, keep the slots the task's constraints admit, record them.

        The time window and price ceiling are applied to what the worker
        observed. The admitted slots are appended to the task timeline, which is
        the only list a later selection (by voice or otherwise) may refer to.
        """
        request = await self._booking_task(task_id)
        criteria = task_criteria(task_id, request)
        status, observation = await self._observe(
            SEARCH_OPERATION, criteria.site_search(), task_id
        )
        if status is not OperationStatus.OK:
            raise BrowserObservationError("search_failed")
        raw = observation.get("slots")
        if not isinstance(raw, list) or len(raw) > MAX_SLOTS:
            raise BrowserObservationError("unreadable_search_results")
        try:
            observed = [ObservedSlotSummary.model_validate(item) for item in raw]
        except ValidationError:
            raise BrowserObservationError("unreadable_search_results") from None
        admitted = [
            slot
            for slot in observed
            if criteria.admits(time=slot.time, price=slot.price, currency=slot.currency)
        ]
        await self._booking_tasks.record_search(
            task_id,
            criteria=criteria,
            slots=[slot.model_dump(mode="json") for slot in admitted],
            observed_count=len(observed),
        )
        return admitted

    async def prepare(self, task_id: uuid.UUID, slot_id: str) -> ActionView:
        request = await self._booking_task(task_id)
        criteria = task_criteria(task_id, request)
        status, observation = await self._observe(
            READ_SLOT_OPERATION, {"slot_id": slot_id}, task_id
        )
        if status is OperationStatus.RESOURCE_UNAVAILABLE:
            raise BookingSlotUnavailableError(slot_id)
        if status is not OperationStatus.OK or observation.get("available") is not True:
            raise BrowserObservationError("slot_not_readable")
        try:
            slot = ObservedSlotSummary.model_validate(observation.get("slot"))
        except ValidationError:
            raise BrowserObservationError("slot_not_readable") from None
        if slot.slot_id != slot_id:
            raise BrowserObservationError("slot_mismatch")
        proposal = BookingProposal(
            site=DEFAULT_SITE,
            slot_id=slot.slot_id,
            doctor=slot.doctor,
            time=slot.time,
            price=slot.price,
            currency=slot.currency,
        )
        # The slot as it is *now* must still satisfy what the user asked for: a
        # price that rose past their ceiling is not silently offered for review.
        if not criteria.admits_proposal(proposal):
            raise BookingCriteriaMismatchError(task_id, slot_id)
        view = await self._actions.propose_exclusive_action(
            task_id,
            tool_name=COMMIT_BOOKING,
            risk_tier=RiskTier.R2,
            proposal=proposal.model_dump(mode="json"),
        )
        # Opening the approval request grants nothing; it only makes the exact
        # stored proposal reviewable. Bound to the revision just created.
        return await self._actions.request_approval(
            view.action.id, expected_revision=view.action.revision
        )
