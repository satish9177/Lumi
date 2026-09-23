"""Milestone 10 S4 routes: the cross-app preparation workflow controller.

Runtime-internal (main holds the only bearer credential and pins each path). These routes create child tasks
in fixed roles and manage candidates and their exact adoption; every other step of a child task goes through
the route that already owns it (transfers, documents, authenticated reading, form preparation). There is no
route here that submits, uploads, clicks, presses a key, types a value, or names a provider, an origin, a
provenance or a value: a candidate's kind, value and provenance are derived by the runtime from its source.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import ActionResponse, ErrorResponse
from app.domain.transfers import MAX_INTENT_CHARS
from app.domain.workflows import MAX_OBJECTIVE_CHARS
from app.services.workflows import WorkflowService, WorkflowView

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateWorkflowBody(_Body):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)


class WorkflowDownloadBody(_Body):
    url: str = Field(min_length=8, max_length=2048)
    root_id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=255)
    intent: str = Field(min_length=1, max_length=MAX_INTENT_CHARS)


class WorkflowExtractBody(_Body):
    document_id: uuid.UUID


class WorkflowFormBody(_Body):
    profile_id: uuid.UUID
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE_CHARS)


class WorkflowStopBody(_Body):
    reason: str = Field(default="user_stopped", min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")


class AdoptionDecisionBody(_Body):
    expected_revision: int = Field(ge=1)


class WorkflowStepResponse(BaseModel):
    role: str
    task_id: uuid.UUID
    task_status: str


class WorkflowCandidateResponse(BaseModel):
    """A candidate field. `value` is the person's own document text, for the trusted view only."""

    candidate_id: uuid.UUID
    kind: str
    provenance: str
    value: str | None
    preview: str
    status: str
    document_label: str
    quote: str | None
    doc_ref: str | None


class WorkflowValueResponse(BaseModel):
    """An adopted workflow value: kind, provenance and masked preview. Never the value."""

    kind: str
    provenance: str
    preview: str
    purged: bool
    adopted_at: datetime


class WorkflowAdoptionResponse(BaseModel):
    action_id: uuid.UUID
    revision: int
    action_status: str
    approval_status: str | None
    candidate_id: uuid.UUID
    kind: str
    provenance: str
    preview: str
    #: Shown on the trusted card while the approval is pending, so the person approves what they read.
    value: str | None
    document_label: str


class WorkflowResponse(BaseModel):
    workflow_id: uuid.UUID
    status: str
    live: bool
    revision: int
    objective: str
    expires_at: datetime
    stop_reason: str | None
    steps: list[WorkflowStepResponse]
    transfer_status: str | None
    placed_name: str | None
    document_count: int
    disclosure_status: str | None
    candidates: list[WorkflowCandidateResponse]
    values: list[WorkflowValueResponse]
    adoptions: list[WorkflowAdoptionResponse]

    @classmethod
    def from_view(cls, view: WorkflowView) -> "WorkflowResponse":
        workflow = view.workflow
        return cls(
            workflow_id=workflow.id,
            status=workflow.status,
            live=view.live,
            revision=workflow.revision,
            objective=workflow.objective,
            expires_at=workflow.expires_at,
            stop_reason=workflow.stop_reason,
            steps=[WorkflowStepResponse(role=step.role, task_id=step.task_id, task_status=step.task_status) for step in view.steps],
            transfer_status=view.transfer_status,
            placed_name=view.placed_name,
            document_count=view.document_count,
            disclosure_status=view.disclosure_status,
            candidates=[
                WorkflowCandidateResponse(
                    candidate_id=item.candidate_id,
                    kind=item.kind,
                    provenance=item.provenance,
                    value=item.value,
                    preview=item.preview,
                    status=item.status,
                    document_label=item.document_label,
                    quote=item.quote,
                    doc_ref=item.doc_ref,
                )
                for item in view.candidates
            ],
            values=[
                WorkflowValueResponse(
                    kind=item.kind, provenance=item.provenance, preview=item.preview, purged=item.purged, adopted_at=item.created_at
                )
                for item in view.values
            ],
            adoptions=[
                WorkflowAdoptionResponse(
                    action_id=item.action_id,
                    revision=item.revision,
                    action_status=item.action_status,
                    approval_status=item.approval_status,
                    candidate_id=item.candidate_id,
                    kind=item.kind,
                    provenance=item.provenance,
                    preview=item.preview,
                    value=item.value,
                    document_label=item.document_label,
                )
                for item in view.adoptions
            ],
        )


class LatestWorkflowResponse(BaseModel):
    workflow: WorkflowResponse | None


def get_workflow_service(request: Request) -> WorkflowService:
    service: WorkflowService = request.app.state.workflow_service
    return service


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]
_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
}


@router.post("/workflows", status_code=status.HTTP_201_CREATED, response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Start a cross-app preparation workflow. It owns lineage only; nothing is done")
async def create_workflow(body: CreateWorkflowBody, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.create(objective=body.objective))


@router.get("/workflows/latest", response_model=LatestWorkflowResponse, summary="The newest workflow, if any")
async def latest_workflow(service: WorkflowServiceDep) -> LatestWorkflowResponse:
    view = await service.latest()
    return LatestWorkflowResponse(workflow=None if view is None else WorkflowResponse.from_view(view))


@router.get("/workflows/{workflow_id}", response_model=WorkflowResponse, responses=_RESPONSES, summary="One workflow")
async def get_workflow(workflow_id: uuid.UUID, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.describe(workflow_id))


@router.post("/workflows/{workflow_id}/download", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Role download: open the S2 download card as this workflow's step. Nothing is fetched")
async def workflow_download(workflow_id: uuid.UUID, body: WorkflowDownloadBody, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(
        await service.start_download(workflow_id, url=body.url, root_id=body.root_id, file_name=body.file_name, intent=body.intent)
    )


@router.post("/workflows/{workflow_id}/documents", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Role documents: bring in exactly the file this workflow placed (same identity, same bytes)")
async def workflow_documents(workflow_id: uuid.UUID, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.start_documents(workflow_id))


@router.post("/workflows/{workflow_id}/candidates/extract", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Local, deterministic candidate fields from one document of this workflow")
async def workflow_extract(workflow_id: uuid.UUID, body: WorkflowExtractBody, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.extract_candidates(workflow_id, document_id=body.document_id))


@router.post("/workflows/{workflow_id}/candidates/derive", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Candidate fields inside the grounded quotes of this workflow's ONE disclosure")
async def workflow_derive(workflow_id: uuid.UUID, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.derive_candidates(workflow_id))


@router.post("/workflows/{workflow_id}/candidates/{candidate_id}/adopt", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Open the exact adoption card for one candidate. Nothing is adopted yet")
async def workflow_propose_adoption(workflow_id: uuid.UUID, candidate_id: uuid.UUID, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.propose_adoption(workflow_id, candidate_id=candidate_id))


@router.post("/workflows/adoptions/{action_id}/approve", response_model=ActionResponse, responses=_RESPONSES,
             summary="The trusted click: adopt exactly this candidate as a workflow-scoped value (single use)")
async def workflow_approve_adoption(action_id: uuid.UUID, body: AdoptionDecisionBody, service: WorkflowServiceDep) -> ActionResponse:
    return ActionResponse.from_view(await service.approve_adoption(action_id, expected_revision=body.expected_revision))


@router.post("/workflows/adoptions/{action_id}/reject", response_model=ActionResponse, responses=_RESPONSES,
             summary="Decline the adoption. A rejected adoption authorises nothing, ever")
async def workflow_reject_adoption(action_id: uuid.UUID, body: AdoptionDecisionBody, service: WorkflowServiceDep) -> ActionResponse:
    return ActionResponse.from_view(await service.reject_adoption(action_id, expected_revision=body.expected_revision))


@router.post("/workflows/{workflow_id}/form", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Role form: an authenticated task that may place ONLY this workflow's adopted values")
async def workflow_form(workflow_id: uuid.UUID, body: WorkflowFormBody, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.start_form(workflow_id, profile_id=body.profile_id, objective=body.objective))


@router.post("/workflows/{workflow_id}/stop", response_model=WorkflowResponse, responses=_RESPONSES,
             summary="Stop: no further step; derived values purged; open grants revoked. Nothing is undone")
async def workflow_stop(workflow_id: uuid.UUID, body: WorkflowStopBody, service: WorkflowServiceDep) -> WorkflowResponse:
    return WorkflowResponse.from_view(await service.stop(workflow_id, reason=body.reason))
