import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.schemas import (
    ActionListResponse,
    ActionResponse,
    BrowserDispatchListResponse,
    BrowserDispatchResponse,
    CancelTaskBody,
    CreateTaskBody,
    ErrorResponse,
    ExpectedRevisionBody,
    FinishAttemptBody,
    FinishReconciliationBody,
    HealthResponse,
    ProposeActionBody,
    RejectActionBody,
    TaskEventListResponse,
    TaskEventResponse,
    TaskResponse,
)
from app.db.engine import ping_database
from app.services.actions import ActionService
from app.services.browser_execution import BrowserExecutionService
from app.services.tasks import TaskService

logger = logging.getLogger(__name__)

router = APIRouter()


def get_task_service(request: Request) -> TaskService:
    service: TaskService = request.app.state.task_service
    return service


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT: dict[int | str, dict[str, Any]] = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}


def _expected_revision(body: ExpectedRevisionBody | None) -> int | None:
    return body.expected_revision if body is not None else None


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
)
async def health(request: Request) -> HealthResponse | JSONResponse:
    try:
        await ping_database(request.app.state.engine)
    except Exception:
        logger.warning("Health check could not reach the database", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=HealthResponse(status="unavailable", database="unreachable").model_dump(),
        )
    return HealthResponse(status="ok", database="ok")


@router.post("/tasks", status_code=status.HTTP_201_CREATED, response_model=TaskResponse)
async def create_task(body: CreateTaskBody, service: TaskServiceDep) -> TaskResponse:
    task = await service.create_task(body.request.model_dump(mode="json", exclude_none=True))
    return TaskResponse.from_record(task)


@router.get("/tasks/{task_id}", response_model=TaskResponse, responses=_NOT_FOUND)
async def get_task(task_id: uuid.UUID, service: TaskServiceDep) -> TaskResponse:
    return TaskResponse.from_record(await service.get_task(task_id))


@router.get("/tasks/{task_id}/events", response_model=TaskEventListResponse, responses=_NOT_FOUND)
async def list_task_events(
    task_id: uuid.UUID,
    service: TaskServiceDep,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TaskEventListResponse:
    events = await service.list_events(task_id, after_sequence=after_sequence, limit=limit)
    return TaskEventListResponse(
        task_id=task_id, events=[TaskEventResponse.from_record(event) for event in events]
    )


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
)
async def cancel_task(
    task_id: uuid.UUID,
    service: TaskServiceDep,
    body: Annotated[CancelTaskBody | None, Body()] = None,
) -> TaskResponse:
    expected_revision = body.expected_revision if body is not None else None
    task = await service.cancel_task(task_id, expected_revision=expected_revision)
    return TaskResponse.from_record(task)


# --- Action ledger ---------------------------------------------------------
#
# The execution and reconciliation routes below are scaffolding. There is no
# external executor in this milestone: `POST /actions/{id}/attempts` persists the
# intent to act and stops, and the finish/reconcile routes record a result that
# tests supply synthetically. They exist so the durable lifecycle can be proven
# now, and so the trusted Electron broker and the isolated browser worker have a
# stable surface to call later. They are not a public API.


def get_action_service(request: Request) -> ActionService:
    service: ActionService = request.app.state.action_service
    return service


ActionServiceDep = Annotated[ActionService, Depends(get_action_service)]


@router.post(
    "/tasks/{task_id}/actions",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Propose an action",
)
async def propose_action(
    task_id: uuid.UUID,
    body: ProposeActionBody,
    service: ActionServiceDep,
    response: Response,
) -> ActionResponse:
    """Record a proposal. Replaying the same idempotency key returns `200`."""
    view, created = await service.propose_action(
        task_id,
        idempotency_key=body.idempotency_key,
        tool_name=body.tool_name,
        risk_tier=body.risk_tier,
        proposal=body.proposal,
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ActionResponse.from_view(view)


@router.get("/tasks/{task_id}/actions", response_model=ActionListResponse, responses=_NOT_FOUND)
async def list_task_actions(
    task_id: uuid.UUID,
    service: ActionServiceDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ActionListResponse:
    views = await service.list_actions(task_id, limit=limit)
    return ActionListResponse(
        task_id=task_id, actions=[ActionResponse.from_view(view) for view in views]
    )


@router.get("/actions/{action_id}", response_model=ActionResponse, responses=_NOT_FOUND)
async def get_action(action_id: uuid.UUID, service: ActionServiceDep) -> ActionResponse:
    return ActionResponse.from_view(await service.get_action(action_id))


@router.post(
    "/actions/{action_id}/approval-request",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Ask for approval of this exact proposal",
)
async def request_action_approval(
    action_id: uuid.UUID,
    service: ActionServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    view = await service.request_approval(
        action_id, expected_revision=_expected_revision(body)
    )
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/approve",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Approve the pending request",
)
async def approve_action(
    action_id: uuid.UUID,
    service: ActionServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    """Approves the stored proposal by reference; no proposal payload is accepted."""
    view = await service.approve_action(action_id, expected_revision=_expected_revision(body))
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/reject",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Reject the action so it can never execute",
)
async def reject_action(
    action_id: uuid.UUID,
    service: ActionServiceDep,
    body: Annotated[RejectActionBody | None, Body()] = None,
) -> ActionResponse:
    view = await service.reject_action(
        action_id,
        expected_revision=_expected_revision(body),
        reason=body.reason if body is not None else None,
    )
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/attempts",
    status_code=status.HTTP_201_CREATED,
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Claim the approval and persist the intent to execute (internal)",
)
async def start_action_attempt(
    action_id: uuid.UUID,
    service: ActionServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    view = await service.start_attempt(action_id, expected_revision=_expected_revision(body))
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/attempts/finish",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Report what the executor observed (internal)",
)
async def finish_action_attempt(
    action_id: uuid.UUID, body: FinishAttemptBody, service: ActionServiceDep
) -> ActionResponse:
    view = await service.finish_attempt(
        action_id,
        outcome=body.outcome,
        result=body.result,
        error_code=body.error_code,
        expected_revision=body.expected_revision,
    )
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/reconciliation",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Begin establishing what actually happened (internal)",
)
async def begin_action_reconciliation(
    action_id: uuid.UUID,
    service: ActionServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    """Valid only from `OUTCOME_UNKNOWN`. Never starts a second attempt."""
    view = await service.begin_reconciliation(
        action_id, expected_revision=_expected_revision(body)
    )
    return ActionResponse.from_view(view)


@router.post(
    "/actions/{action_id}/reconciliation/finish",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Record the authoritative reconciliation result (internal)",
)
async def finish_action_reconciliation(
    action_id: uuid.UUID, body: FinishReconciliationBody, service: ActionServiceDep
) -> ActionResponse:
    view = await service.finish_reconciliation(
        action_id,
        result=body.result,
        evidence=body.evidence,
        expected_revision=body.expected_revision,
    )
    return ActionResponse.from_view(view)


# --- Browser execution (internal) ------------------------------------------
#
# These routes are how the trusted runtime asks the isolated worker to act. They
# take an action id and nothing else: what happens in the browser is decided by
# the persisted proposal and the reviewed registry, never by the caller. There is
# deliberately no route that accepts a URL, a selector, a script or an operation
# name, because such a route would be a generic browser-automation API, and a
# generic browser-automation API behind an agent is a remote code execution
# primitive wearing a cardigan.
#
# Like the attempt and reconciliation routes, they are loopback-only internal
# scaffolding for tests and for the future trusted Electron broker.


def get_browser_execution_service(request: Request) -> BrowserExecutionService:
    service: BrowserExecutionService = request.app.state.browser_execution_service
    return service


BrowserServiceDep = Annotated[BrowserExecutionService, Depends(get_browser_execution_service)]

_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}
}


@router.post(
    "/actions/{action_id}/browser-execution",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Execute an approved booking in the browser (internal)",
)
async def execute_action_in_browser(
    action_id: uuid.UUID, service: BrowserServiceDep
) -> ActionResponse:
    """Claims the approval, dispatches to the worker, records what came back.

    The body is empty on purpose. Everything that governs the side effect --
    which slot, which doctor, which price, which site -- comes from the
    immutable proposal the approval was bound to.
    """
    return ActionResponse.from_view(await service.execute_booking(action_id))


@router.post(
    "/actions/{action_id}/browser-reconciliation",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Establish what actually happened, by reading the site (internal)",
)
async def reconcile_action_in_browser(
    action_id: uuid.UUID, service: BrowserServiceDep
) -> ActionResponse:
    """Runs the read-only lookup. It cannot book anything, and never retries."""
    return ActionResponse.from_view(await service.reconcile_booking(action_id))


@router.get(
    "/actions/{action_id}/browser-dispatches",
    response_model=BrowserDispatchListResponse,
    responses=_NOT_FOUND,
    summary="The browser work recorded for this action (internal)",
)
async def list_browser_dispatches(
    action_id: uuid.UUID, service: BrowserServiceDep
) -> BrowserDispatchListResponse:
    dispatches = await service.describe_dispatches(action_id)
    return BrowserDispatchListResponse(
        action_id=action_id,
        dispatches=[BrowserDispatchResponse.model_validate(row) for row in dispatches],
    )
