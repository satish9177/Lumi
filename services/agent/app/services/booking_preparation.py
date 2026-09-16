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
import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.browser.client import BrowserWorkerClient
from app.browser.config import DEFAULT_SITE
from app.browser.protocol import DispatchRequest, OperationStatus
from app.domain.action_status import RiskTier
from app.domain.booking import BookingProposal
from app.domain.errors import (
    BookingSlotUnavailableError,
    BrowserObservationError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
)
from app.domain.task_status import accepts_actions
from app.services.actions import ActionService, ActionView
from app.services.browser_execution import COMMIT_BOOKING, WorkerSource, open_worker_client
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.booking.preparation")

BOOKING_TASK_TYPE = "appointment_booking"
SEARCH_OPERATION = "search_appointments"
READ_SLOT_OPERATION = "read_available_slots"
MAX_SLOTS = 50
SLOT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_SPECIALTY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,59}$")
_DAYS = frozenset(
    {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
)


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


def search_criteria(request: dict[str, Any]) -> dict[str, str]:
    """The persisted task request, reduced to the two search fields it may hold."""
    specialty = request.get("specialty", "")
    day = request.get("day", "")
    if not isinstance(specialty, str) or (specialty and not _SPECIALTY_PATTERN.match(specialty)):
        specialty = ""
    if not isinstance(day, str) or (day and day not in _DAYS):
        day = ""
    return {"specialty": specialty, "day": day}


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
        request = await self._booking_task(task_id)
        status, observation = await self._observe(
            SEARCH_OPERATION, search_criteria(request), task_id
        )
        if status is not OperationStatus.OK:
            raise BrowserObservationError("search_failed")
        raw = observation.get("slots")
        if not isinstance(raw, list) or len(raw) > MAX_SLOTS:
            raise BrowserObservationError("unreadable_search_results")
        try:
            return [ObservedSlotSummary.model_validate(item) for item in raw]
        except ValidationError:
            raise BrowserObservationError("unreadable_search_results") from None

    async def prepare(self, task_id: uuid.UUID, slot_id: str) -> ActionView:
        await self._booking_task(task_id)
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
