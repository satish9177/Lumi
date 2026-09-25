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
    #: Milestone 12 S1: opaque refs the planner cites from this orchestration's own registry. Shape is
    #: bounded here; ownership, kind, freshness and per-capability compatibility are re-checked by the
    #: service, never trusted from this wire shape alone.
    resources: list[str] | None = Field(default=None, max_length=4)
    #: Milestone 12 S2: the new resource's backing id, for a capability whose output needs one (e.g.
    #: document_read's new document_id) -- main already performed the real action and knows this value.
    #: Required or forbidden per capability; the service decides which, never trusted from this shape alone.
    result_backing_id: uuid.UUID | None = None


class RegisterResourceBody(_Body):
    expected_revision: int = Field(ge=1)
    #: Closed to `REGISTERABLE_RESOURCE_KINDS`; re-checked by the service, not trusted from this wire shape.
    kind: str = Field(min_length=1, max_length=32)
    safe_label: str = Field(min_length=1, max_length=200)
    #: `document_ref` only: the file id within `document_task_id`.
    backing_id: uuid.UUID | None = None
    #: `public_url_ref` only: the canonical, already policy-checked URL.
    backing_text: str | None = Field(default=None, min_length=1, max_length=2048)
    #: `document_ref` only: the document task this file belongs to. Set once per orchestration.
    document_task_id: uuid.UUID | None = None


class OrchestrationStepResponse(BaseModel):
    sequence: int
    capability_id: str
    status: str
    child_task_id: uuid.UUID | None
    result_handle: str | None
    result_summary: str | None
    #: Milestone 12 S3: a controller-authored, bounded note while still PENDING/AWAITING_APPROVAL (e.g.
    #: `manual_handoff_required`'s own safe instruction). Never a step's result -- see `result_summary`.
    pending_note: str | None


class OrchestrationResourceResponse(BaseModel):
    ref: str
    kind: str
    privacy_class: str
    safe_label: str
    single_use: bool
    #: Milestone 12 S2: model-invisible backing identity, projected here ONLY because this whole response is
    #: main-process-internal (never forwarded verbatim to the planner -- `orchestration-coordinator.ts`
    #: builds the planner's own facts from `ref`/`kind`/`safe_label` alone). Lets main resolve which file,
    #: document or URL a cited resource actually is, to call that capability's own existing service method.
    backing_id: uuid.UUID | None = None
    backing_text: str | None = None


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
    #: Milestone 12 S2: the one document task this orchestration's document resources refer into, if any.
    #: Main-only; never shown to the planner.
    document_task_id: uuid.UUID | None
    #: What this runtime can execute right now -- the planner's only allowed step choices.
    available_capabilities: list[str]
    #: Milestone 12 S1: resources this orchestration currently owns (not consumed, not expired). Seeing one
    #: here is never authority to use it with any particular capability.
    resources: list[OrchestrationResourceResponse]
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
            document_task_id=record.document_task_id,
            available_capabilities=list(view.available_capabilities),
            resources=[
                OrchestrationResourceResponse(
                    ref=resource.ref,
                    kind=resource.kind,
                    privacy_class=resource.privacy_class,
                    safe_label=resource.safe_label,
                    single_use=resource.single_use,
                    backing_id=resource.backing_id,
                    backing_text=resource.backing_text,
                )
                for resource in view.resources
            ],
            steps=[
                OrchestrationStepResponse(
                    sequence=step.sequence,
                    capability_id=step.capability_id,
                    status=step.status,
                    child_task_id=step.child_task_id,
                    result_handle=step.result_handle,
                    result_summary=step.result_summary,
                    pending_note=step.pending_note,
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
    "/orchestrations/{orchestration_id}/resources", status_code=status.HTTP_201_CREATED,
    response_model=OrchestrationResponse, responses=_RESPONSES,
    summary="Milestone 12 S2: make a trusted input resource available -- an approved document or a "
    "policy-checked URL Lumi already validated. Never reachable from the planner or the model",
)
async def register_orchestration_resource(
    orchestration_id: uuid.UUID, body: RegisterResourceBody, service: OrchestrationServiceDep
) -> OrchestrationResponse:
    return OrchestrationResponse.from_view(
        await service.register_resource(
            orchestration_id,
            expected_revision=body.expected_revision,
            kind=body.kind,
            safe_label_text=body.safe_label,
            backing_id=body.backing_id,
            backing_text=body.backing_text,
            document_task_id=body.document_task_id,
        )
    )


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
            resources=body.resources,
            result_backing_id=body.result_backing_id,
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
