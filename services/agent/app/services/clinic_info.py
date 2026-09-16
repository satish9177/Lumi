"""The second workflow: a read-only clinic-information lookup.

It exists to show that the durable task controller is not an appointment
booking controller. A `clinic_info` task reuses everything the booking task
uses -- the task row and its lock, the ordered timeline, the reviewed operation
registry, the isolated worker, the runtime generation -- and nothing it does not
need: there is no proposal, no approval and no execution attempt, because
reading a doctor's public profile changes nothing anywhere.

That is the risk model applied, not skipped. `read_doctor_profiles` is
READ_ONLY and SAFE_TO_RETRY, so a lookup interrupted by a crash is simply asked
again: there is nothing to reconcile and nothing that could be duplicated.

Page text stays data. The adapter returns typed fields; this service keeps only
the fields the user asked about and bounds every string before it reaches the
timeline.
"""

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.browser.protocol import OperationStatus
from app.domain.errors import (
    BrowserObservationError,
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.task_status import TaskEventType, accepts_actions
from app.repositories.tasks import TaskRecord, TaskRepository
from app.services.actions import ActionService
from app.services.browser_execution import WorkerSource
from app.services.observation import observe_read_only
from app.services.tasks import TaskService

CLINIC_INFO_TASK_TYPE = "clinic_info"
READ_PROFILES_OPERATION = "read_doctor_profiles"
MAX_PROFILES = 10
_NAME = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,59}$")

Topic = Literal["overview", "hours", "fee", "languages", "address", "walk_ins"]
TOPICS: tuple[str, ...] = ("overview", "hours", "fee", "languages", "address", "walk_ins")


class ClinicInfoQuery(BaseModel):
    """What the user asked about. Stored on the task request, never a URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    specialty: str = Field(default="", max_length=60)
    doctor: str = Field(default="", max_length=60)
    topic: Topic = "overview"

    @classmethod
    def from_request(cls, task_id: uuid.UUID, request: dict[str, Any]) -> "ClinicInfoQuery":
        fields = {key: request[key] for key in ("specialty", "doctor", "topic") if key in request}
        try:
            query = cls.model_validate(fields)
        except ValidationError:
            raise BrowserObservationError("unreadable_query") from None
        for value in (query.specialty, query.doctor):
            if value and not _NAME.match(value):
                raise BrowserObservationError("unreadable_query")
        if not (query.specialty or query.doctor):
            raise BrowserObservationError("unreadable_query")
        return query


class ProfileFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doctor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    doctor: str = Field(min_length=1, max_length=120)
    specialty: str = Field(max_length=120)
    clinic: str = Field(max_length=120)
    address: str = Field(max_length=200)
    hours: str = Field(max_length=80)
    consultation_fee: int = Field(ge=0, le=10_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    languages: list[str] = Field(max_length=12)
    walk_ins: bool


#: Everything a clinic-info request may carry. Provenance fields are written by
#: the trusted desktop layer; there is no place for a URL or a selector.
REQUEST_KEYS = frozenset({"type", "text", "specialty", "doctor", "topic", "source", "voice_turn_id", "request_id"})


def validate_request(request: dict[str, Any]) -> None:
    """Refuse to store a clinic-info task the lookup could never run."""
    if not set(request) <= REQUEST_KEYS:
        raise BrowserObservationError("unreadable_query")
    ClinicInfoQuery.from_request(uuid.UUID(int=0), request)


class ClinicInfoService:
    def __init__(
        self,
        *,
        tasks: TaskService,
        actions: ActionService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
    ) -> None:
        self._tasks = tasks
        self._engine = actions.engine
        self._runtime_generation = runtime_generation
        self._worker = worker

    async def lookup(self, task_id: uuid.UUID) -> tuple[TaskRecord, list[ProfileFact]]:
        task = await self._tasks.get_task(task_id)
        if task.request.get("type") != CLINIC_INFO_TASK_TYPE:
            raise TaskKindMismatchError(task_id, CLINIC_INFO_TASK_TYPE)
        if not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        query = ClinicInfoQuery.from_request(task_id, task.request)

        # No transaction is open while the browser reads the page.
        status, observation = await observe_read_only(
            self._worker,
            self._runtime_generation,
            operation=READ_PROFILES_OPERATION,
            payload={"specialty": query.specialty, "doctor": query.doctor},
            task_id=task_id,
        )
        if status is not OperationStatus.OK:
            raise BrowserObservationError("lookup_failed")
        raw = observation.get("profiles")
        if not isinstance(raw, list) or len(raw) > MAX_PROFILES * 2:
            raise BrowserObservationError("unreadable_profiles")
        try:
            profiles = [ProfileFact.model_validate(item) for item in raw][:MAX_PROFILES]
        except ValidationError:
            raise BrowserObservationError("unreadable_profiles") from None

        async with self._engine.begin() as connection:
            repository = TaskRepository(connection)
            locked = await repository.lock_task(task_id)
            if locked is None:
                raise TaskNotFoundError(task_id)
            if not accepts_actions(locked.status):
                raise TaskNotAcceptingActionsError(task_id, locked.status)
            advanced = await repository.advance_task(
                task_id=task_id, expected_revision=locked.revision
            )
            if advanced is None:  # pragma: no cover - the task row lock is held.
                raise TaskConcurrencyError(task_id)
            await repository.append_event(
                task=advanced,
                event_type=TaskEventType.TASK_INFO_LOOKUP_COMPLETED,
                payload={
                    "query": query.model_dump(mode="json"),
                    "profiles": [profile.model_dump(mode="json") for profile in profiles],
                    "observed_count": len(raw),
                },
            )
        return advanced, profiles
