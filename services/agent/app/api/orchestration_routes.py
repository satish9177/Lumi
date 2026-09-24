"""Milestone 11 S2 routes: durable read-only task orchestration.

Runtime-internal (main holds the only bearer credential). There is no route here that accepts a URL, a
path, a selector, a script or an arbitrary tool/capability payload: `advance` takes only a closed
`capability_id` (validated against the Milestone 11 S1 catalog) plus, for a task-backed capability, the id
of a task Electron main already created through that capability's own existing boundary -- never anything
this route could use to invent one itself.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import ErrorResponse
from app.domain.orchestration import MAX_OBJECTIVE_CHARS
from app.services.orchestration import OrchestrationService, OrchestrationView

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateOrchestrationBody(_Body):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)


class RevisionBody(_Body):
    expected_revision: int = Field(ge=1)


class AdvanceOrchestrationBody(_Body):
    expected_revision: int = Field(ge=1)
    #: Closed-catalog membership is re-checked by the service, not trusted from the wire shape here.
    capability_id: str = Field(min_length=1, max_length=32)
    #: Task-backed capabilities only: the id of a task the caller already created through that
    #: capability's own boundary. Mutually exclusive with `resolved_summary`.
    task_id: uuid.UUID | None = None
    #: Synchronous capabilities only: a bounded result the caller already computed through that
    #: capability's own existing read method. Mutually exclusive with `task_id`.
    resolved_summary: str | None = Field(default=None, min_length=1, max_length=2000)


class OrchestrationStepResponse(BaseModel):
    sequence: int
    capability_id: str
    status: str
    child_task_id: uuid.UUID | None
    result_handle: str | None
    result_summary: str | None


class OrchestrationResponse(BaseModel):
    orchestration_id: uuid.UUID
    status: str
    pause_reason: str | None
    live: bool
    revision: int
    objective: str
    step_count: int
    child_task_count: int
    planner_calls: int
    created_at: datetime
    expires_at: datetime
    stopped_at: datetime | None
    #: What this runtime can execute right now -- the planner's only allowed step choices.
    available_capabilities: list[str]
    steps: list[OrchestrationStepResponse]

    @classmethod
    def from_view(cls, view: OrchestrationView) -> "OrchestrationResponse":
        record = view.orchestration
        return cls(
            orchestration_id=record.id,
            status=record.status,
            pause_reason=record.pause_reason,
            live=view.live,
            revision=record.revision,
            objective=record.objective,
            step_count=record.step_count,
            child_task_count=record.child_task_count,
            planner_calls=record.planner_calls,
            created_at=record.created_at,
            expires_at=record.expires_at,
            stopped_at=record.stopped_at,
            available_capabilities=list(view.available_capabilities),
            steps=[
                OrchestrationStepResponse(
                    sequence=step.sequence,
                    capability_id=step.capability_id,
                    status=step.status,
                    child_task_id=step.child_task_id,
                    result_handle=step.result_handle,
                    result_summary=step.result_summary,
                )
                for step in view.steps
            ],
        )


class LatestOrchestrationResponse(BaseModel):
    orchestration: OrchestrationResponse | None


def get_orchestration_service(request: Request) -> OrchestrationService:
    service: OrchestrationService = request.app.state.orchestration_service
    return service


OrchestrationServiceDep = Annotated[OrchestrationService, Depends(get_orchestration_service)]
_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


@router.post(
    "/orchestrations", status_code=status.HTTP_201_CREATED, response_model=OrchestrationResponse,
    summary="Start a durable orchestration graph. It holds no authority of its own; nothing is done",
)
async def create_orchestration(body: CreateOrchestrationBody, service: OrchestrationServiceDep) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(await service.create(objective=body.objective))


@router.get("/orchestrations/latest", response_model=LatestOrchestrationResponse, summary="The newest orchestration, if any")
async def latest_orchestration(service: OrchestrationServiceDep) -> LatestOrchestrationResponse:
    view = await service.latest()
    return LatestOrchestrationResponse(orchestration=None if view is None else OrchestrationResponse.from_view(view))


@router.get("/orchestrations/{orchestration_id}", response_model=OrchestrationResponse, responses=_RESPONSES, summary="One orchestration")
async def get_orchestration(orchestration_id: uuid.UUID, service: OrchestrationServiceDep) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(await service.describe(orchestration_id))


@router.post(
    "/orchestrations/{orchestration_id}/planner-call", response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="Count one planner call against the budget before Electron main asks a model anything",
)
async def orchestration_planner_call(
    orchestration_id: uuid.UUID, body: RevisionBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(
        await service.record_planner_call(orchestration_id, expected_revision=body.expected_revision)
    )


@router.post(
    "/orchestrations/{orchestration_id}/advance", response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="Record one capability choice. A task-backed step links a task the caller already created "
    "through its own boundary; a synchronous step records a result the caller already read",
)
async def orchestration_advance(
    orchestration_id: uuid.UUID, body: AdvanceOrchestrationBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(
        await service.advance(
            orchestration_id,
            expected_revision=body.expected_revision,
            capability_id=body.capability_id,
            task_id=body.task_id,
            resolved_summary=body.resolved_summary,
        )
    )


@router.post(
    "/orchestrations/{orchestration_id}/resume", response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="Re-check a paused step's live state. Never assumes a child task resolved just because time passed",
)
async def orchestration_resume(
    orchestration_id: uuid.UUID, body: RevisionBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(await service.resume(orchestration_id, expected_revision=body.expected_revision))


@router.post(
    "/orchestrations/{orchestration_id}/finish", response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="The planner decided the objective is satisfied. Requires at least one succeeded step",
)
async def orchestration_finish(
    orchestration_id: uuid.UUID, body: RevisionBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(await service.finish(orchestration_id, expected_revision=body.expected_revision))


@router.post(
    "/orchestrations/{orchestration_id}/stop", response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="Stop future scheduling. Never marks an in-flight step failed and never touches a child task",
)
async def orchestration_stop(
    orchestration_id: uuid.UUID, body: RevisionBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(await service.stop(orchestration_id, expected_revision=body.expected_revision))
