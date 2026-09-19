import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import (
    ActionListResponse,
    ActionResponse,
    BookingSearchResponse,
    BookingSlotResponse,
    BrowserProfileListResponse,
    BrowserProfileResponse,
    BrowserDispatchListResponse,
    BrowserDispatchResponse,
    CancelBookingTaskResponse,
    CancelLoginBody,
    CancelTaskBody,
    ConfirmSignedInBody,
    CreateBrowserProfileBody,
    DeleteBrowserProfileBody,
    LoginAttemptResponse,
    LoginTakeoverResponse,
    OpenLoginWindowBody,
    ClinicInfoResponse,
    ConfirmResearchGrantBody,
    DoctorProfileResponse,
    CreateTaskBody,
    ErrorResponse,
    ExecuteResearchStepBody,
    ExpectedRevisionBody,
    FinishAttemptBody,
    FinishReconciliationBody,
    HealthResponse,
    InspectionResponse,
    PrepareBookingBody,
    PrepareInspectionBody,
    PrepareResearchBody,
    RecordPageAnswerBody,
    RecordResearchAnswerBody,
    ResearchResponse,
    ResearchStepResponse,
    RevokeResearchGrantBody,
    ProposeActionBody,
    RejectActionBody,
    ReviseBookingCriteriaBody,
    ReviseBookingCriteriaResponse,
    TaskEventListResponse,
    TaskEventResponse,
    TaskResponse,
)
from app.db.engine import ping_database
from app.domain.booking_criteria import BookingCriteria, InvalidBookingCriteriaError
from app.services.actions import ActionService
from app.services.booking_preparation import BookingPreparationService
from app.services.booking_tasks import BOOKING_TASK_TYPE, BookingTaskService
from app.services.browser_execution import BrowserExecutionService
from app.services.clinic_info import (
    CLINIC_INFO_TASK_TYPE,
    ClinicInfoService,
    validate_request as validate_clinic_info_request,
)
from app.domain.errors import BrowserObservationError, DestinationNotAllowedError
from app.domain.page_observation import PAGE_INSPECTION_TASK_TYPE
from app.domain.public_url import UrlPolicyError
from app.domain.research import (
    PUBLIC_RESEARCH_TASK_TYPE,
    ResearchRefusal,
    ResearchStepEnvelope,
    parse_step,
)
from app.domain.browser_profile import BrowserContextKind
from app.domain.login_takeover import TakeoverRefusal
from app.services.browser_profiles import BrowserProfileService
from app.services.login_takeover import LoginTakeoverService
from app.services.page_inspection import PageInspectionService
from app.services.page_inspection import validate_request as validate_inspection_request
from app.services.research_tasks import ResearchService
from app.services.research_tasks import validate_request as validate_research_request
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
            content=HealthResponse(
                status="unavailable",
                database="unreachable",
                runtime_generation=request.app.state.runtime_generation.id,
            ).model_dump(mode="json"),
        )
    return HealthResponse(
        status="ok",
        database="ok",
        runtime_generation=request.app.state.runtime_generation.id,
    )


@router.post("/lifecycle/shutdown", status_code=status.HTTP_202_ACCEPTED)
async def shutdown(request: Request) -> Response:
    """Ask this authenticated runtime generation to stop gracefully."""
    callback: Any = request.app.state.request_shutdown
    callback()
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/tasks", status_code=status.HTTP_201_CREATED, response_model=TaskResponse)
async def create_task(
    body: CreateTaskBody, service: TaskServiceDep, http_request: Request
) -> TaskResponse:
    request = body.request.model_dump(mode="json", exclude_none=True)
    if request.get("type") == BOOKING_TASK_TYPE:
        # A booking task is never stored with constraints search would refuse.
        try:
            BookingCriteria.from_request(request)
        except InvalidBookingCriteriaError:
            raise RequestValidationError([]) from None
    if request.get("type") == CLINIC_INFO_TASK_TYPE:
        try:
            validate_clinic_info_request(request)
        except BrowserObservationError:
            raise RequestValidationError([]) from None
    if request.get("type") == PUBLIC_RESEARCH_TASK_TYPE:
        # A research task is never stored without an objective that could be
        # worked on. The scope card is a separate, explicit step after this.
        try:
            validate_research_request(request)
        except (ResearchRefusal, ValueError):
            raise RequestValidationError([]) from None
    if request.get("type") == PAGE_INSPECTION_TASK_TYPE:
        inspection: PageInspectionService = http_request.app.state.page_inspection_service
        try:
            validate_inspection_request(request, inspection.policy)
        except UrlPolicyError as error:
            raise DestinationNotAllowedError(error.code) from None
        except ValueError:
            raise RequestValidationError([]) from None
    task = await service.create_task(request)
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
    action_id: uuid.UUID,
    service: BrowserServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    """Claims the approval, dispatches to the worker, records what came back.

    The body is empty on purpose. Everything that governs the side effect --
    which slot, which doctor, which price, which site, or which public page --
    comes from the immutable proposal the approval was bound to. The executor
    is chosen by the persisted tool name. The only optional input is the action
    revision the user reviewed, which the approval claim enforces.
    """
    return ActionResponse.from_view(
        await service.execute(action_id, expected_revision=_expected_revision(body))
    )


@router.post(
    "/actions/{action_id}/browser-reconciliation",
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Establish what actually happened, by reading the site (internal)",
)
async def reconcile_action_in_browser(
    action_id: uuid.UUID,
    service: BrowserServiceDep,
    body: Annotated[ExpectedRevisionBody | None, Body()] = None,
) -> ActionResponse:
    """Runs the read-only lookup. It cannot book anything, and never retries."""
    return ActionResponse.from_view(
        await service.reconcile_booking(action_id, expected_revision=_expected_revision(body))
    )


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


# --- Booking preparation ----------------------------------------------------
#
# Read-only discovery for the trusted desktop broker. Search takes nothing but
# the task id (criteria come from the persisted task request); prepare takes a
# slot id and nothing else, and builds the proposal from what the worker reads.


def get_booking_preparation_service(request: Request) -> BookingPreparationService:
    service: BookingPreparationService = request.app.state.booking_preparation_service
    return service


BookingServiceDep = Annotated[
    BookingPreparationService, Depends(get_booking_preparation_service)
]


@router.post(
    "/tasks/{task_id}/booking/search",
    response_model=BookingSearchResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Search the reviewed appointment site (read-only)",
)
async def search_booking_slots(
    task_id: uuid.UUID, service: BookingServiceDep
) -> BookingSearchResponse:
    slots = await service.search(task_id)
    return BookingSearchResponse(
        task_id=task_id,
        slots=[BookingSlotResponse(**slot.model_dump()) for slot in slots],
    )


@router.post(
    "/tasks/{task_id}/booking/prepare",
    status_code=status.HTTP_201_CREATED,
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Observe a slot and propose booking it, awaiting approval",
)
async def prepare_booking_action(
    task_id: uuid.UUID, body: PrepareBookingBody, service: BookingServiceDep
) -> ActionResponse:
    return ActionResponse.from_view(await service.prepare(task_id, body.slot_id))


# --- Booking task changes ---------------------------------------------------
#
# Task-level changes a conversation can ask for. Both run under the task lock
# and refuse while a booking may already exist at the site; neither can approve
# or execute anything.


def get_booking_task_service(request: Request) -> BookingTaskService:
    service: BookingTaskService = request.app.state.booking_task_service
    return service


BookingTaskServiceDep = Annotated[BookingTaskService, Depends(get_booking_task_service)]


@router.post(
    "/tasks/{task_id}/booking/criteria",
    response_model=ReviseBookingCriteriaResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Revise the booking constraints; invalidate bookings they now exclude",
)
async def revise_booking_criteria(
    task_id: uuid.UUID, body: ReviseBookingCriteriaBody, service: BookingTaskServiceDep
) -> ReviseBookingCriteriaResponse:
    task, invalidated = await service.revise_criteria(
        task_id, expected_revision=body.expected_revision, criteria=body.criteria
    )
    return ReviseBookingCriteriaResponse(
        task=TaskResponse.from_record(task), invalidated_action_ids=invalidated
    )


@router.post(
    "/tasks/{task_id}/booking/cancel",
    response_model=CancelBookingTaskResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Cancel a booking task unless a booking may already exist",
)
async def cancel_booking_task(
    task_id: uuid.UUID,
    service: BookingTaskServiceDep,
    body: Annotated[CancelTaskBody | None, Body()] = None,
) -> CancelBookingTaskResponse:
    task, rejected = await service.cancel(
        task_id, expected_revision=body.expected_revision if body is not None else None
    )
    return CancelBookingTaskResponse(
        task=TaskResponse.from_record(task), rejected_action_ids=rejected
    )


# --- Clinic information (read-only workflow) --------------------------------
#
# The second workflow on the same controller. It takes a task id and nothing
# else; the query comes from the persisted task request. There is no action,
# approval or attempt because nothing it does changes the outside world.


def get_clinic_info_service(request: Request) -> ClinicInfoService:
    service: ClinicInfoService = request.app.state.clinic_info_service
    return service


ClinicInfoServiceDep = Annotated[ClinicInfoService, Depends(get_clinic_info_service)]


@router.post(
    "/tasks/{task_id}/info/lookup",
    response_model=ClinicInfoResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Read public doctor profiles for a clinic-info task (read-only)",
)
async def lookup_clinic_info(
    task_id: uuid.UUID, service: ClinicInfoServiceDep
) -> ClinicInfoResponse:
    task, profiles = await service.lookup(task_id)
    return ClinicInfoResponse(
        task=TaskResponse.from_record(task),
        profiles=[DoctorProfileResponse(**profile.model_dump()) for profile in profiles],
    )


# --- Public page inspection (Milestone 7a) -----------------------------------
#
# Prepare takes a task id and the provider disclosure main will honour; the URL
# and question come from the persisted task. Execution is the shared
# `/actions/{id}/browser-execution` route. The answer route accepts a grounded
# answer bound to an observation id and content hash, and re-verifies it.


def get_page_inspection_service(request: Request) -> PageInspectionService:
    service: PageInspectionService = request.app.state.page_inspection_service
    return service


InspectionServiceDep = Annotated[PageInspectionService, Depends(get_page_inspection_service)]


@router.post(
    "/tasks/{task_id}/inspection/prepare",
    status_code=status.HTTP_201_CREATED,
    response_model=ActionResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Propose inspecting the task's page and ask for exact approval",
)
async def prepare_page_inspection(
    task_id: uuid.UUID, body: PrepareInspectionBody, service: InspectionServiceDep
) -> ActionResponse:
    return ActionResponse.from_view(await service.prepare(task_id, body.disclosure))


@router.get(
    "/actions/{action_id}/inspection",
    response_model=InspectionResponse,
    responses=_NOT_FOUND,
    summary="The inspection action, its stored observation and recorded answer",
)
async def get_page_inspection(
    action_id: uuid.UUID, service: InspectionServiceDep
) -> InspectionResponse:
    view = await service.describe(action_id)
    return InspectionResponse.build(view.action, view.observation)


# --- Public web research (Milestone 7b) ---------------------------------------
#
# Six routes. Notice which one is different: `research/grant` is the trusted
# click, and it is the *only* way a scope becomes usable. Every other route
# either reads, or executes one step inside a scope that is already active.
#
# `research/steps` takes one member of a closed operation union plus a request
# id. It has no field for a selector, a script, a URL, an HTTP method, a
# header, a cookie or a browser flag, and there is no route that accepts a
# sequence of operations: one step, one authorization, one observation.


def get_research_service(request: Request) -> ResearchService:
    service: ResearchService = request.app.state.research_service
    return service


ResearchServiceDep = Annotated[ResearchService, Depends(get_research_service)]


@router.get(
    "/tasks/{task_id}/research",
    response_model=ResearchResponse,
    responses=_NOT_FOUND,
    summary="The research task, its scope, its observations and its answer",
)
async def get_research(task_id: uuid.UUID, service: ResearchServiceDep) -> ResearchResponse:
    return ResearchResponse.from_view(await service.describe(task_id))


@router.post(
    "/tasks/{task_id}/research/prepare",
    status_code=status.HTTP_201_CREATED,
    response_model=ResearchResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Build the bounded research scope and open its card (grants nothing)",
)
async def prepare_research(
    task_id: uuid.UUID, body: PrepareResearchBody, service: ResearchServiceDep
) -> ResearchResponse:
    view = await service.prepare(task_id, disclosure=body.disclosure, budgets=body.budgets)
    return ResearchResponse.from_view(view)


@router.post(
    "/tasks/{task_id}/research/grant",
    response_model=ResearchResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Confirm the research scope shown on the trusted card",
)
async def grant_research_scope(
    task_id: uuid.UUID, body: ConfirmResearchGrantBody, service: ResearchServiceDep
) -> ResearchResponse:
    """Grants the stored scope by reference; no scope payload is accepted."""
    view = await service.confirm(
        task_id, grant_id=body.grant_id, expected_revision=body.expected_revision
    )
    return ResearchResponse.from_view(view)


@router.post(
    "/tasks/{task_id}/research/revoke",
    response_model=ResearchResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Withdraw the research scope and drop the browser session",
)
async def revoke_research_scope(
    task_id: uuid.UUID, body: RevokeResearchGrantBody, service: ResearchServiceDep
) -> ResearchResponse:
    view = await service.revoke(
        task_id,
        reason=body.reason,
        grant_id=body.grant_id,
        expected_revision=body.expected_revision,
    )
    return ResearchResponse.from_view(view)


@router.post(
    "/tasks/{task_id}/research/steps",
    status_code=status.HTTP_201_CREATED,
    response_model=ResearchStepResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Execute exactly one authorised research operation (internal)",
)
async def execute_research_step(
    task_id: uuid.UUID, body: ExecuteResearchStepBody, service: ResearchServiceDep
) -> ResearchStepResponse:
    """One step, checked against the task's grant before anything happens.

    The step is parsed by the closed operation union first, so an operation
    that is not in the reviewed vocabulary is refused here and never reaches
    the grant check, the ledger or the browser. Replaying the same
    `request_id` returns the stored result of that step and executes nothing,
    which is what makes a duplicated planner request safe.
    """
    envelope = ResearchStepEnvelope(
        request_id=body.request_id,
        step=parse_step(body.step),
        planner_calls=body.planner_calls,
    )
    return ResearchStepResponse.from_result(await service.execute_step(task_id, envelope))


@router.post(
    "/tasks/{task_id}/research/answer",
    response_model=ResearchResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Record the one grounded research answer and close the scope",
)
async def record_research_answer(
    task_id: uuid.UUID, body: RecordResearchAnswerBody, service: ResearchServiceDep
) -> ResearchResponse:
    view = await service.record_answer(
        task_id,
        answer=body.answer,
        provider=body.provider,
        model=body.model,
        planner_calls=body.planner_calls,
    )
    return ResearchResponse.from_view(view)


@router.post(
    "/actions/{action_id}/inspection/answer",
    response_model=InspectionResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Record a grounded answer for the current observation (once)",
)
async def record_page_answer(
    action_id: uuid.UUID, body: RecordPageAnswerBody, service: InspectionServiceDep
) -> InspectionResponse:
    view = await service.record_answer(
        action_id,
        observation_id=body.observation_id,
        content_hash=body.content_hash,
        answer=body.answer,
        provider=body.provider,
        model=body.model,
    )
    return InspectionResponse.build(view.action, view.observation)


# --- Persistent browser profiles (Milestone 8a S1) ---------------------------
#
# Three fixed routes, each with its own contract, in the same style as every
# other capability here. Deliberately **not** one `manageProfile(action, json)`
# endpoint: a single route taking a verb and a JSON blob is how a narrow
# capability quietly becomes a wide one.
#
# What no route here accepts, from any caller, at any time:
#
#     profilePath   userDataDir   cookieFile   storageState   browserExecutablePath
#
# The profile directory is derived from the profile id inside the runtime and
# the worker, and it appears in no request and no response. S1 exposes the
# backend contract only; there is no renderer IPC, no card and no login flow,
# because those are S2.


def get_browser_profile_service(request: Request) -> BrowserProfileService:
    service: BrowserProfileService = request.app.state.browser_profile_service
    return service


BrowserProfileServiceDep = Annotated[
    BrowserProfileService, Depends(get_browser_profile_service)
]


@router.get(
    "/browser-profiles",
    response_model=BrowserProfileListResponse,
    summary="Lumi-managed browser profiles, by opaque id (internal)",
)
async def list_browser_profiles(
    service: BrowserProfileServiceDep,
) -> BrowserProfileListResponse:
    profiles = await service.list_profiles()
    return BrowserProfileListResponse(
        profiles=[BrowserProfileResponse.from_profile(profile) for profile in profiles]
    )


@router.post(
    "/browser-profiles",
    status_code=status.HTTP_201_CREATED,
    response_model=BrowserProfileResponse,
    responses=_CONFLICT,
    summary="Create one profile bound to one registrable domain (internal)",
)
async def create_browser_profile(
    body: CreateBrowserProfileBody, service: BrowserProfileServiceDep
) -> BrowserProfileResponse:
    """The site is canonicalised through the pinned Public Suffix List, and the
    binding is immutable from here on -- in the service and in the database."""
    profile = await service.create_profile(site=body.site, label=body.label)
    return BrowserProfileResponse.from_profile(profile)


@router.get(
    "/browser-profiles/{profile_id}",
    response_model=BrowserProfileResponse,
    responses=_CONFLICT,
    summary="One profile's identity and lifecycle (internal)",
)
async def get_browser_profile(
    profile_id: uuid.UUID, service: BrowserProfileServiceDep
) -> BrowserProfileResponse:
    return BrowserProfileResponse.from_profile(await service.get_profile(profile_id))


@router.post(
    "/browser-profiles/{profile_id}/open",
    response_model=BrowserProfileResponse,
    responses={**_CONFLICT, **_UNAVAILABLE},
    summary="Lease the profile and open its persistent context (internal)",
)
async def open_browser_profile(
    profile_id: uuid.UUID, service: BrowserProfileServiceDep
) -> BrowserProfileResponse:
    """Opening is not signing in. The profile gets a browser and a lease; S1
    navigates nowhere, observes nothing and never reports anyone as signed in."""
    opened = await service.open_profile(
        profile_id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
    )
    return BrowserProfileResponse.from_profile(opened.profile)


@router.post(
    "/browser-profiles/{profile_id}/close",
    response_model=BrowserProfileResponse,
    responses={**_CONFLICT, **_UNAVAILABLE},
    summary="Close the persistent context and drop the lease (internal)",
)
async def close_browser_profile(
    profile_id: uuid.UUID, service: BrowserProfileServiceDep
) -> BrowserProfileResponse:
    await service.close_profile(profile_id)
    return BrowserProfileResponse.from_profile(await service.get_profile(profile_id))


@router.post(
    "/browser-profiles/{profile_id}/delete",
    response_model=BrowserProfileResponse,
    responses=_CONFLICT,
    summary="Remove the local profile directory (never a website logout)",
)
async def delete_browser_profile(
    profile_id: uuid.UUID,
    service: BrowserProfileServiceDep,
    body: Annotated[DeleteBrowserProfileBody | None, Body()] = None,
) -> BrowserProfileResponse:
    """Removes the sign-in data Lumi stored on this computer. It does **not**
    sign the user out on the website: no request of any kind is made to it."""
    deleted = await service.delete_profile(
        profile_id,
        expected_revision=body.expected_revision if body is not None else None,
    )
    return BrowserProfileResponse.from_profile(deleted)


# --- Manual login and human takeover (Milestone 8a S2) -----------------------
#
# Three fixed routes plus one read. No route accepts a URL, a credential, a
# cookie, page content, a browser argument or an executable path. `site` never
# appears in a request: the profile's own bound, immutable site is what the
# runtime hands to the worker, never anything a caller names here.


def get_login_takeover_service(request: Request) -> LoginTakeoverService:
    service: LoginTakeoverService = request.app.state.login_takeover_service
    return service


LoginTakeoverServiceDep = Annotated[
    LoginTakeoverService, Depends(get_login_takeover_service)
]


def _takeover_response(outcome: Any) -> LoginTakeoverResponse:
    return LoginTakeoverResponse(
        attempt=LoginAttemptResponse.from_attempt(outcome.attempt),
        profile=BrowserProfileResponse.from_profile(outcome.profile),
        refusal_reason=outcome.refusal_reason,
    )


@router.post(
    "/browser-profiles/{profile_id}/takeover",
    response_model=LoginTakeoverResponse,
    responses={**_CONFLICT, **_UNAVAILABLE},
    summary="Open a headed window for the human to sign in themselves (internal)",
)
async def open_login_window(
    profile_id: uuid.UUID, body: OpenLoginWindowBody, service: LoginTakeoverServiceDep
) -> LoginTakeoverResponse:
    """The trusted "Sign in manually" click. Agent automation is suspended for
    the life of the takeover this starts: no planner call, no observation, no
    provider call is reachable while it is open."""
    outcome = await service.start_takeover(profile_id, expected_revision=body.expected_revision)
    return _takeover_response(outcome)


@router.get(
    "/browser-profiles/{profile_id}/takeover/{attempt_id}",
    response_model=LoginAttemptResponse,
    responses=_CONFLICT,
    summary="One takeover's bounded interval and how it ended (internal)",
)
async def get_login_takeover(
    profile_id: uuid.UUID, attempt_id: uuid.UUID, service: LoginTakeoverServiceDep
) -> LoginAttemptResponse:
    attempt = await service.get_attempt(attempt_id)
    if attempt.profile_id != profile_id:
        raise TakeoverRefusal("login_attempt_not_found")
    return LoginAttemptResponse.from_attempt(attempt)


@router.post(
    "/browser-profiles/{profile_id}/takeover/{attempt_id}/confirm",
    response_model=LoginTakeoverResponse,
    responses={**_CONFLICT, **_UNAVAILABLE},
    summary="End the takeover and run the one deterministic post-login check (internal)",
)
async def confirm_signed_in(
    profile_id: uuid.UUID,
    attempt_id: uuid.UUID,
    body: ConfirmSignedInBody,
    service: LoginTakeoverServiceDep,
) -> LoginTakeoverResponse:
    """The trusted "I'm signed in" click. This is not, by itself, treated as
    proof that a sign-in succeeded -- only the check it starts is."""
    outcome = await service.confirm_takeover(
        profile_id, attempt_id, expected_revision=body.expected_revision
    )
    return _takeover_response(outcome)


@router.post(
    "/browser-profiles/{profile_id}/takeover/{attempt_id}/cancel",
    response_model=LoginTakeoverResponse,
    responses={**_CONFLICT, **_UNAVAILABLE},
    summary="End the takeover without any authentication claim (internal)",
)
async def cancel_login(
    profile_id: uuid.UUID,
    attempt_id: uuid.UUID,
    body: CancelLoginBody,
    service: LoginTakeoverServiceDep,
) -> LoginTakeoverResponse:
    """The trusted "Cancel" click. Never a logout: no request reaches the
    website, and no cookie is cleared."""
    outcome = await service.cancel_takeover(
        profile_id, attempt_id, expected_revision=body.expected_revision
    )
    return _takeover_response(outcome)
