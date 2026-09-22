import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import (
    ObserveDesktopSurfaceBody,
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
from app.api.authenticated_schemas import (
    AuthenticatedResponse,
    AuthenticatedStepResponse,
    ConfirmAuthenticatedGrantBody,
    ExecuteAuthenticatedStepBody,
    PrepareAuthenticatedBody,
    RecordAuthenticatedAnswerBody,
    RevokeAuthenticatedGrantBody,
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
from app.domain.authenticated import (
    AUTHENTICATED_READ_TASK_TYPE,
    AuthenticatedStepEnvelope,
    parse_authenticated_step,
)
from app.api.form_prepare_schemas import (
    ConfirmFormGrantBody,
    DraftDecisionBody,
    DisclosureDecisionBody,
    FormPlanResponse,
    PlanningContextResponse,
    PrepareFormScopeBody,
    ProposeFormBody,
    RevokeFormGrantBody,
    SaveProtectedValueBody,
    SavedDetailListResponse,
    SavedDetailResponse,
)
from app.domain.form_prepare import FORM_PREPARE_TOOL, FormPrepareRefusal
from app.domain.local_form_draft import HANDOVER_TOOL
from app.domain.protected_values import ProtectedValueRefusal, is_protected_kind
from app.services.authenticated_read import AuthenticatedReadService
from app.services.form_draft import FormDraftService
from app.services.form_prepare import FormPrepareService
from app.services.authenticated_read import validate_request as validate_authenticated_request
from app.domain.research import (
    PUBLIC_RESEARCH_TASK_TYPE,
    ResearchRefusal,
    ResearchStepEnvelope,
    parse_step,
)
from app.domain.browser_profile import BrowserContextKind
from app.domain.login_takeover import TakeoverRefusal
from app.desktop.protocol import DesktopObservation, SurfaceListResponse
from app.services.browser_profiles import BrowserProfileService
from app.services.desktop import DesktopService
from app.domain.desktop_actions import TOOL_PREFIX
from app.services.desktop_actions import DesktopActionError, DesktopActionService
from app.api.desktop_action_schemas import (
    DesktopActionDecisionBody,
    DesktopActionReconcileBody,
    DesktopActionResponse,
    LatestDesktopActionResponse,
    ProposeFocusBody,
    ProposeFromPlanBody,
    ProposeLaunchBody,
    ProposeScrollBody,
    RegisteredAppResponse,
    RegisteredAppsResponse,
    ScrollTargetResponse,
    ScrollTargetsBody,
    ScrollTargetsResponse,
)
from app.services.desktop_disclosure import DesktopDisclosureService
from app.api.desktop_disclosure_schemas import (
    CreateDesktopReadBody,
    DesktopGrantBody,
    DesktopReadResponse,
    DesktopRevokeBody,
    LatestDesktopReadResponse,
    ProviderContextResponse,
    RecordDesktopResultBody,
)
from app.services.desktop_planning import DesktopPlanningService
from app.api.desktop_planning_schemas import (
    CreateDesktopPlanBody,
    DesktopPlanGrantBody,
    DesktopPlanResponse,
    DesktopPlanRevokeBody,
    LatestDesktopPlanResponse,
    PlanProviderContextResponse,
    RecordDesktopPlanResultBody,
)
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
    if request.get("type") == AUTHENTICATED_READ_TASK_TYPE:
        # An authenticated task is never stored without an objective, a profile
        # and the `account_private` classification. The scope card is a
        # separate, explicit step after this.
        try:
            _, profile_id = validate_authenticated_request(request)
        except (ResearchRefusal, ValueError):
            raise RequestValidationError([]) from None
        # The profile is checked here, before any task exists, and nothing is
        # opened: a profile that cannot be read never leaves a task behind.
        authenticated: AuthenticatedReadService = http_request.app.state.authenticated_read_service
        await authenticated.check_profile(profile_id)
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


async def _refuse_disclosure_tool(service: ActionService, action_id: uuid.UUID) -> None:
    """The generic action routes can never move a form-disclosure approval.

    Its approval is spent only by `/actions/{id}/field-disclosure/approve`, which
    re-checks every fact it depends on. Approving, claiming or finishing it through
    a generic route would skip that.
    """
    tool = (await service.get_action(action_id)).action.tool_name
    if tool in (FORM_PREPARE_TOOL, HANDOVER_TOOL):
        raise FormPrepareRefusal("use_disclosure_route")
    if tool.upper().startswith(TOOL_PREFIX):
        # Milestone 9 S3. A desktop effect exists only inside `DesktopActionService.approve`: the input baseline,
        # the guard, the durable dispatch and the worker call are one ordered unit. A generic approve, attempt or
        # finish would mint or settle an effect that skipped every one of them.
        raise DesktopActionError("use_desktop_route")


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
    if body.tool_name.upper().startswith(TOOL_PREFIX):
        raise DesktopActionError("use_desktop_route")
    if body.tool_name in (FORM_PREPARE_TOOL, HANDOVER_TOOL):
        # Only the form-preparation service, from a controller-built manifest, may
        # create this action. A generic caller cannot mint an approval for one.
        raise FormPrepareRefusal("use_disclosure_route")
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
    await _refuse_disclosure_tool(service, action_id)
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
    await _refuse_disclosure_tool(service, action_id)
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
    await _refuse_disclosure_tool(service, action_id)
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
    await _refuse_disclosure_tool(service, action_id)
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
    await _refuse_disclosure_tool(service, action_id)
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
    await _refuse_disclosure_tool(service, action_id)
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


# --- Authenticated account reading (Milestone 8a S3) ---------------------------
#
# Six routes, shaped exactly like the public-research ones -- and, as there, one
# of them is different: `authenticated/grant` is the trusted click, and it is the
# *only* way a scope becomes usable. `authenticated/steps` takes one member of a
# closed operation union plus a request id; it has no field for a URL, a
# selector, a script, a method, a header, a cookie, a key, text to type or a
# provider. Nothing here returns a URL, a cookie or an account identity.


def get_authenticated_read_service(request: Request) -> AuthenticatedReadService:
    service: AuthenticatedReadService = request.app.state.authenticated_read_service
    return service


AuthenticatedServiceDep = Annotated[
    AuthenticatedReadService, Depends(get_authenticated_read_service)
]


@router.get(
    "/tasks/{task_id}/authenticated",
    response_model=AuthenticatedResponse,
    responses=_NOT_FOUND,
    summary="The authenticated task, its scope, its redacted observations and its answer",
)
async def get_authenticated(
    task_id: uuid.UUID, service: AuthenticatedServiceDep
) -> AuthenticatedResponse:
    return AuthenticatedResponse.from_view(await service.describe(task_id))


@router.post(
    "/tasks/{task_id}/authenticated/prepare",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthenticatedResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Build the bounded account-reading scope and open its card (grants nothing)",
)
async def prepare_authenticated(
    task_id: uuid.UUID, body: PrepareAuthenticatedBody, service: AuthenticatedServiceDep
) -> AuthenticatedResponse:
    """Checks the profile deterministically and opens no browser."""
    return AuthenticatedResponse.from_view(await service.prepare(task_id, recipient=body.recipient))


@router.post(
    "/tasks/{task_id}/authenticated/grant",
    response_model=AuthenticatedResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Confirm the account-reading scope shown on the trusted card",
)
async def grant_authenticated_scope(
    task_id: uuid.UUID, body: ConfirmAuthenticatedGrantBody, service: AuthenticatedServiceDep
) -> AuthenticatedResponse:
    """Grants the stored scope by reference; no scope payload is accepted."""
    view = await service.confirm(
        task_id, grant_id=body.grant_id, expected_revision=body.expected_revision
    )
    return AuthenticatedResponse.from_view(view)


@router.post(
    "/tasks/{task_id}/authenticated/revoke",
    response_model=AuthenticatedResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Withdraw the account-reading scope and release the browser",
)
async def revoke_authenticated_scope(
    task_id: uuid.UUID, body: RevokeAuthenticatedGrantBody, service: AuthenticatedServiceDep
) -> AuthenticatedResponse:
    view = await service.revoke(
        task_id,
        reason=body.reason,
        grant_id=body.grant_id,
        expected_revision=body.expected_revision,
    )
    return AuthenticatedResponse.from_view(view)


@router.post(
    "/tasks/{task_id}/authenticated/steps",
    status_code=status.HTTP_201_CREATED,
    response_model=AuthenticatedStepResponse,
    responses={**_NOT_FOUND, **_CONFLICT, **_UNAVAILABLE},
    summary="Execute exactly one authorised account-reading operation (internal)",
)
async def execute_authenticated_step(
    task_id: uuid.UUID, body: ExecuteAuthenticatedStepBody, service: AuthenticatedServiceDep
) -> AuthenticatedStepResponse:
    """One step, checked against the task's grant before anything happens.

    The step is parsed by the closed operation union first, so an operation or a
    key outside the reviewed vocabulary is refused here and never reaches the
    grant check, the ledger or the browser."""
    envelope = AuthenticatedStepEnvelope(
        request_id=body.request_id,
        step=parse_authenticated_step(body.step),
        planner_calls=body.planner_calls,
    )
    return AuthenticatedStepResponse.from_result(await service.execute_step(task_id, envelope))


@router.post(
    "/tasks/{task_id}/authenticated/answer",
    response_model=AuthenticatedResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Record the one grounded account answer and close the scope",
)
async def record_authenticated_answer(
    task_id: uuid.UUID, body: RecordAuthenticatedAnswerBody, service: AuthenticatedServiceDep
) -> AuthenticatedResponse:
    view = await service.record_answer(
        task_id,
        answer=body.answer,
        provider=body.provider,
        model=body.model,
        planner_calls=body.planner_calls,
    )
    return AuthenticatedResponse.from_view(view)


# --- Form planning and exact disclosure approval (Milestone 8b S5) -----------
#
# **These routes change no website.** They have no field for a value to type, an
# origin, a selector, a URL, a script or a provider to *choose*. What they do:
#
#   * `PUT /protected-values/{kind}` saves one detail the user typed directly. The
#     kind is closed, the body is one string, and the response never echoes it.
#     Only the trusted desktop layer calls it; no model, voice turn or page can.
#   * `.../form/prepare-scope` builds the PENDING planning grant (grants nothing),
#     `.../form/grant` is the trusted click, `.../form/revoke` withdraws it.
#   * `.../form/planning-context` returns what ONE provider may see, only under a
#     confirmed grant, and only masked previews of saved details.
#   * `.../form/propose` takes a planner's `prepare_form` proposal, validates it
#     against persisted state and opens the exact approval.
#   * `/actions/{id}/field-disclosure/approve|reject` takes an expected revision
#     and nothing else. The manifest, the values and the origin all come from
#     persisted state. Approval ends in `prepared_nothing`.


def get_form_prepare_service(request: Request) -> FormPrepareService:
    service: FormPrepareService = request.app.state.form_prepare_service
    return service


FormPrepareServiceDep = Annotated[FormPrepareService, Depends(get_form_prepare_service)]


def get_form_draft_service(request: Request) -> FormDraftService:
    service: FormDraftService = request.app.state.form_draft_service
    return service


FormDraftServiceDep = Annotated[FormDraftService, Depends(get_form_draft_service)]


async def _form_plan(
    task_id: uuid.UUID, prepare: FormPrepareService, drafts: FormDraftService
) -> FormPlanResponse:
    return FormPlanResponse.from_view(
        await prepare.describe(task_id), handover=await drafts.handover_view(task_id)
    )


@router.get(
    "/protected-values",
    response_model=SavedDetailListResponse,
    summary="The saved details, as kind and masked preview only",
)
async def list_protected_values(service: FormPrepareServiceDep) -> SavedDetailListResponse:
    return SavedDetailListResponse(
        details=[SavedDetailResponse.from_detail(item) for item in await service.list_details()]
    )


@router.put(
    "/protected-values/{kind}",
    response_model=SavedDetailResponse,
    responses={**_CONFLICT},
    summary="Save one detail the user typed directly (trusted desktop layer only)",
)
async def save_protected_value(
    kind: str, body: SaveProtectedValueBody, service: FormPrepareServiceDep
) -> SavedDetailResponse:
    """The kind is one of eight. The value is never echoed and never logged."""
    if not is_protected_kind(kind):
        raise ProtectedValueRefusal("unknown_kind")
    return SavedDetailResponse.from_detail(await service.save_detail(kind, body.value))


@router.get(
    "/tasks/{task_id}/authenticated/form",
    response_model=FormPlanResponse,
    responses=_NOT_FOUND,
    summary="The task's form-planning grant and disclosure card",
)
async def get_form_plan(
    task_id: uuid.UUID, service: FormPrepareServiceDep, drafts: FormDraftServiceDep
) -> FormPlanResponse:
    return await _form_plan(task_id, service, drafts)


@router.post(
    "/tasks/{task_id}/authenticated/form/prepare-scope",
    status_code=status.HTTP_201_CREATED,
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Build the form-planning scope and open its card (grants nothing)",
)
async def prepare_form_scope(
    task_id: uuid.UUID, body: PrepareFormScopeBody, service: FormPrepareServiceDep
) -> FormPlanResponse:
    return FormPlanResponse.from_view(
        await service.prepare_scope(task_id, allowed_data_refs=list(body.allowed_data_refs))
    )


@router.post(
    "/tasks/{task_id}/authenticated/form/grant",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Confirm the form-planning scope shown on the trusted card",
)
async def grant_form_scope(
    task_id: uuid.UUID, body: ConfirmFormGrantBody, service: FormPrepareServiceDep
) -> FormPlanResponse:
    return FormPlanResponse.from_view(
        await service.confirm(
            task_id, grant_id=body.grant_id, expected_revision=body.expected_revision
        )
    )


@router.post(
    "/tasks/{task_id}/authenticated/form/revoke",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Withdraw the form-planning scope",
)
async def revoke_form_scope(
    task_id: uuid.UUID, body: RevokeFormGrantBody, service: FormPrepareServiceDep
) -> FormPlanResponse:
    return FormPlanResponse.from_view(
        await service.revoke(
            task_id,
            reason=body.reason,
            grant_id=body.grant_id,
            expected_revision=body.expected_revision,
        )
    )


@router.post(
    "/tasks/{task_id}/authenticated/form/planning-context",
    response_model=PlanningContextResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="What the one named planner may see (internal)",
)
async def form_planning_context(
    task_id: uuid.UUID, service: FormPrepareServiceDep
) -> PlanningContextResponse:
    return PlanningContextResponse.from_context(await service.planning_context(task_id))


@router.post(
    "/tasks/{task_id}/authenticated/form/propose",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Validate a prepare_form proposal and open the exact approval (internal)",
)
async def propose_form(
    task_id: uuid.UUID, body: ProposeFormBody, service: FormPrepareServiceDep
) -> FormPlanResponse:
    """Refused before any approval, card or action exists if the proposal is not exact."""
    return FormPlanResponse.from_view(
        await service.propose(task_id, body.proposal, provider=body.provider)
    )


@router.post(
    "/actions/{action_id}/field-disclosure/approve",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Approve exactly this disclosure manifest (trusted click; changes no website)",
)
async def approve_field_disclosure(
    action_id: uuid.UUID, body: DisclosureDecisionBody, service: FormPrepareServiceDep,
    drafts: FormDraftServiceDep,
) -> FormPlanResponse:
    view = await service.approve(action_id, expected_revision=body.expected_revision)
    return await _form_plan(view.action.task_id, service, drafts)


@router.post(
    "/actions/{action_id}/field-disclosure/reject",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Decline this disclosure manifest",
)
async def reject_field_disclosure(
    action_id: uuid.UUID, body: DisclosureDecisionBody, service: FormPrepareServiceDep
) -> FormPlanResponse:
    view = await service.reject(action_id, expected_revision=body.expected_revision)
    return FormPlanResponse.from_view(await service.describe(view.action.task_id))


# --- The network-frozen local form draft (Milestone 8b S6) ---------------------------
#
# Every route is an id and, where a card was on screen, the revision it showed. There is no
# field for a value, a manifest, an origin, a selector, a URL, a provider or a freeze flag.
#
#   * `.../form/preparation-mode` reopens the profile headed, returns internally to the page
#     being read and observes it afresh. Nothing is written.
#   * `/actions/{id}/field-disclosure/approve` (above) is the trusted click that FILLS, with
#     the network frozen, inside that window.
#   * `/form-drafts/{id}/discard` destroys the dirty page WHILE FROZEN, then thaws.
#   * `/form-drafts/{id}/handover-request` opens the SECOND exact approval;
#     `/actions/{id}/form-handover/approve|reject` decides it. Only the approval restores the
#     network for the human. Lumi never submits.
#   * `.../form/stop` is discard plus close.


@router.post(
    "/tasks/{task_id}/authenticated/form/preparation-mode",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Open the headed preparation window and observe the form afresh (writes nothing)",
)
async def start_form_preparation(
    task_id: uuid.UUID, service: FormPrepareServiceDep, drafts: FormDraftServiceDep
) -> FormPlanResponse:
    await drafts.start_preparation(task_id)
    return await _form_plan(task_id, service, drafts)


@router.post(
    "/tasks/{task_id}/authenticated/form/stop",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Stop: discard any local draft while frozen, then close the window",
)
async def stop_form_preparation(
    task_id: uuid.UUID, service: FormPrepareServiceDep, drafts: FormDraftServiceDep
) -> FormPlanResponse:
    await drafts.stop(task_id)
    return await _form_plan(task_id, service, drafts)


@router.post(
    "/form-drafts/{draft_id}/discard",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Discard the local draft: destroy the dirty page while frozen, then thaw",
)
async def discard_form_draft(
    draft_id: uuid.UUID, body: DraftDecisionBody, service: FormPrepareServiceDep,
    drafts: FormDraftServiceDep,
) -> FormPlanResponse:
    record = await drafts.discard(draft_id, expected_revision=body.expected_revision)
    return await _form_plan(record.task_id, service, drafts)


@router.post(
    "/form-drafts/{draft_id}/handover-request",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Open the second exact approval for lifting the freeze (changes nothing yet)",
)
async def request_form_handover(
    draft_id: uuid.UUID, body: DraftDecisionBody, service: FormPrepareServiceDep,
    drafts: FormDraftServiceDep,
) -> FormPlanResponse:
    view = await drafts.request_handover(draft_id, expected_revision=body.expected_revision)
    return await _form_plan(view.action.task_id, service, drafts)


@router.post(
    "/actions/{action_id}/form-handover/approve",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Approve exactly this handover (trusted click; the page may then send)",
)
async def approve_form_handover(
    action_id: uuid.UUID, body: DisclosureDecisionBody, service: FormPrepareServiceDep,
    drafts: FormDraftServiceDep,
) -> FormPlanResponse:
    view = await drafts.approve_handover(action_id, expected_revision=body.expected_revision)
    return await _form_plan(view.action.task_id, service, drafts)


@router.post(
    "/actions/{action_id}/form-handover/reject",
    response_model=FormPlanResponse,
    responses={**_NOT_FOUND, **_CONFLICT},
    summary="Decline the handover; the network stays frozen",
)
async def reject_form_handover(
    action_id: uuid.UUID, body: DisclosureDecisionBody, service: FormPrepareServiceDep,
    drafts: FormDraftServiceDep,
) -> FormPlanResponse:
    view = await drafts.reject_handover(action_id, expected_revision=body.expected_revision)
    return await _form_plan(view.action.task_id, service, drafts)


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


def get_login_takeover_service(request: Request) -> LoginTakeoverService:
    service: LoginTakeoverService = request.app.state.login_takeover_service
    return service


LoginTakeoverServiceDep = Annotated[
    LoginTakeoverService, Depends(get_login_takeover_service)
]


@router.get(
    "/browser-profiles",
    response_model=BrowserProfileListResponse,
    summary="Lumi-managed browser profiles, by opaque id (internal)",
)
async def list_browser_profiles(
    service: BrowserProfileServiceDep, takeovers: LoginTakeoverServiceDep
) -> BrowserProfileListResponse:
    """Milestone 8a S2: each profile also carries its open takeover, if any.

    This is the durable answer the desktop hydrates its screen-capture guard
    from after an Electron-main restart -- an id, a status and an expiry, and
    nothing that describes the page the human is signing in to.
    """
    profiles = await service.list_profiles()
    active = {attempt.profile_id: attempt for attempt in await takeovers.list_active_attempts()}
    return BrowserProfileListResponse(
        profiles=[
            BrowserProfileResponse.from_profile(
                profile, active_takeover=active.get(profile.id)
            )
            for profile in profiles
        ]
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


# --- Windows desktop observation (Milestone 9, slice 1) -----------------------------
#
# Two routes and no verb. There is no route that focuses, invokes, types, selects,
# scrolls, clicks, launches or executes anything, and neither request has a field for a
# window handle, a process id, a selector, coordinates, a script or a property name.
# Observations stay local: no other route, model, memory or summary refers to them.


def get_desktop_service(request: Request) -> DesktopService:
    service: DesktopService = request.app.state.desktop_service
    return service


DesktopServiceDep = Annotated[DesktopService, Depends(get_desktop_service)]
_DESKTOP_REFUSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_504_GATEWAY_TIMEOUT: {"model": ErrorResponse},
}


@router.get(
    "/desktop/surfaces",
    response_model=SurfaceListResponse,
    responses=_DESKTOP_REFUSED,
    summary="The user-visible Windows surfaces (internal, local only)",
)
async def list_desktop_surfaces(service: DesktopServiceDep) -> SurfaceListResponse:
    return await service.list_surfaces()


@router.post(
    "/desktop/observations",
    response_model=DesktopObservation,
    responses=_DESKTOP_REFUSED,
    summary="One bounded, read-only semantic observation of a surface (internal, local only)",
)
async def observe_desktop_surface(
    body: ObserveDesktopSurfaceBody, service: DesktopServiceDep
) -> DesktopObservation:
    return await service.observe(body.worker_generation, body.surface_ref, body.surface_epoch)


# --- Windows desktop disclosure and read-only reasoning (Milestone 9, slice 2) ---------
#
# Still no verb that touches a desktop. These routes observe ONE surface locally (S1's read),
# open a trusted card, take the trusted click, release ONE redacted projection after the claim
# has committed, and record the one provider attempt's outcome. There is no route that takes a
# snapshot, a digest, a provider choice from the renderer, a selector, coordinates or a script,
# and none that focuses, invokes, types, selects, scrolls, clicks or launches.


def get_desktop_disclosure_service(request: Request) -> DesktopDisclosureService:
    service: DesktopDisclosureService = request.app.state.desktop_disclosure_service
    return service


DesktopDisclosureServiceDep = Annotated[DesktopDisclosureService, Depends(get_desktop_disclosure_service)]
_DESKTOP_DISCLOSURE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND,
    **_CONFLICT,
    status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_504_GATEWAY_TIMEOUT: {"model": ErrorResponse},
}


@router.post(
    "/desktop/read-tasks",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopReadResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Observe one surface locally and open the disclosure card (nothing is sent)",
)
async def create_desktop_read(
    body: CreateDesktopReadBody, service: DesktopDisclosureServiceDep
) -> DesktopReadResponse:
    return DesktopReadResponse.from_view(
        await service.create(
            objective=body.objective,
            recipient=body.recipient,
            model=body.model,
            worker_generation=body.worker_generation,
            surface_ref=body.surface_ref,
            surface_epoch=body.surface_epoch,
        )
    )


@router.get(
    "/desktop/read-tasks/latest",
    response_model=LatestDesktopReadResponse,
    summary="The newest desktop read, if any (local, private)",
)
async def latest_desktop_read(service: DesktopDisclosureServiceDep) -> LatestDesktopReadResponse:
    view = await service.latest()
    return LatestDesktopReadResponse(read=None if view is None else DesktopReadResponse.from_view(view))


@router.get(
    "/desktop/read-tasks/{task_id}",
    response_model=DesktopReadResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="One desktop read, as the trusted card shows it",
)
async def get_desktop_read(task_id: uuid.UUID, service: DesktopDisclosureServiceDep) -> DesktopReadResponse:
    return DesktopReadResponse.from_view(await service.describe(task_id))


@router.post(
    "/desktop/read-tasks/{task_id}/grant",
    response_model=DesktopReadResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Confirm the exact disclosure shown on the trusted card (single use)",
)
async def grant_desktop_disclosure(
    task_id: uuid.UUID, body: DesktopGrantBody, service: DesktopDisclosureServiceDep
) -> DesktopReadResponse:
    return DesktopReadResponse.from_view(
        await service.confirm(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision)
    )


@router.post(
    "/desktop/read-tasks/{task_id}/revoke",
    response_model=DesktopReadResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Decline or withdraw the disclosure (cannot un-send after a claim)",
)
async def revoke_desktop_disclosure(
    task_id: uuid.UUID, body: DesktopRevokeBody, service: DesktopDisclosureServiceDep
) -> DesktopReadResponse:
    return DesktopReadResponse.from_view(
        await service.revoke(
            task_id, grant_id=body.grant_id, expected_revision=body.expected_revision, reason=body.reason
        )
    )


@router.post(
    "/desktop/read-tasks/{task_id}/disclosure",
    response_model=ProviderContextResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Claim the single-use approval and release the redacted projection for ONE call (internal)",
)
async def claim_desktop_disclosure(
    task_id: uuid.UUID, service: DesktopDisclosureServiceDep
) -> ProviderContextResponse:
    return ProviderContextResponse.from_context(await service.claim(task_id))


@router.post(
    "/desktop/read-tasks/{task_id}/result",
    response_model=DesktopReadResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Record the one provider attempt's read-only result or failure (internal)",
)
async def record_desktop_result(
    task_id: uuid.UUID, body: RecordDesktopResultBody, service: DesktopDisclosureServiceDep
) -> DesktopReadResponse:
    return DesktopReadResponse.from_view(
        await service.record_result(
            task_id, disclosure_id=body.disclosure_id, result=body.result, failure=body.failure
        )
    )


# --- Windows desktop action planning (Milestone 9, slice 4) ------------------------------
#
# Still no verb that touches a desktop. These routes observe ONE surface locally (S1's read), open a
# trusted planning card (the redacted snapshot plus the person's own typed candidate values), take the
# trusted click, release ONE redacted projection plus value descriptors after the claim has committed,
# and record the one provider attempt's proposed action (or its failure). This is disclosure authority
# only: recording a proposed action here runs nothing. Turning it into something that can actually run
# is `POST /desktop/actions/from-plan`, on the ordinary action ledger, behind its own separate approval.


def get_desktop_planning_service(request: Request) -> DesktopPlanningService:
    service: DesktopPlanningService = request.app.state.desktop_planning_service
    return service


DesktopPlanningServiceDep = Annotated[DesktopPlanningService, Depends(get_desktop_planning_service)]


@router.post(
    "/desktop/action-plans",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopPlanResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Observe one surface locally and open the planning card (nothing is sent)",
)
async def create_desktop_plan(
    body: CreateDesktopPlanBody, service: DesktopPlanningServiceDep
) -> DesktopPlanResponse:
    return DesktopPlanResponse.from_view(
        await service.create(
            objective=body.objective,
            recipient=body.recipient,
            model=body.model,
            worker_generation=body.worker_generation,
            surface_ref=body.surface_ref,
            surface_epoch=body.surface_epoch,
            values=[(item.classification, item.value) for item in body.values],
        )
    )


@router.get(
    "/desktop/action-plans/latest",
    response_model=LatestDesktopPlanResponse,
    summary="The newest desktop action plan, if any (local, private)",
)
async def latest_desktop_plan(service: DesktopPlanningServiceDep) -> LatestDesktopPlanResponse:
    view = await service.latest()
    return LatestDesktopPlanResponse(plan=None if view is None else DesktopPlanResponse.from_view(view))


@router.get(
    "/desktop/action-plans/{task_id}",
    response_model=DesktopPlanResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="One desktop action plan, as the trusted card shows it",
)
async def get_desktop_plan(task_id: uuid.UUID, service: DesktopPlanningServiceDep) -> DesktopPlanResponse:
    return DesktopPlanResponse.from_view(await service.describe(task_id))


@router.post(
    "/desktop/action-plans/{task_id}/grant",
    response_model=DesktopPlanResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Confirm the exact planning disclosure shown on the trusted card (single use)",
)
async def grant_desktop_plan(
    task_id: uuid.UUID, body: DesktopPlanGrantBody, service: DesktopPlanningServiceDep
) -> DesktopPlanResponse:
    return DesktopPlanResponse.from_view(
        await service.confirm(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision)
    )


@router.post(
    "/desktop/action-plans/{task_id}/revoke",
    response_model=DesktopPlanResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Decline or withdraw the planning disclosure (cannot un-send after a claim)",
)
async def revoke_desktop_plan(
    task_id: uuid.UUID, body: DesktopPlanRevokeBody, service: DesktopPlanningServiceDep
) -> DesktopPlanResponse:
    return DesktopPlanResponse.from_view(
        await service.revoke(
            task_id, grant_id=body.grant_id, expected_revision=body.expected_revision, reason=body.reason
        )
    )


@router.post(
    "/desktop/action-plans/{task_id}/claim",
    response_model=PlanProviderContextResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Claim the single-use approval and release the redacted projection for ONE call (internal)",
)
async def claim_desktop_plan(task_id: uuid.UUID, service: DesktopPlanningServiceDep) -> PlanProviderContextResponse:
    return PlanProviderContextResponse.from_context(await service.claim(task_id))


@router.post(
    "/desktop/action-plans/{task_id}/result",
    response_model=DesktopPlanResponse,
    responses=_DESKTOP_DISCLOSURE_RESPONSES,
    summary="Record the one provider attempt's proposed action or failure (internal)",
)
async def record_desktop_plan_result(
    task_id: uuid.UUID, body: RecordDesktopPlanResultBody, service: DesktopPlanningServiceDep
) -> DesktopPlanResponse:
    return DesktopPlanResponse.from_view(
        await service.record_result(task_id, plan_id=body.plan_id, result=body.result, failure=body.failure)
    )


# --- Windows desktop actions (Milestone 9, slice 3) --------------------------------------
#
# Exactly three effects, each behind an exact trusted approval on the ordinary action ledger: focus one
# surface, scroll one control by a closed step, open one registered application by id. There is no route
# that takes a handle, a path, an argument, a coordinate, a key, a selector or a script.


def get_desktop_action_service(request: Request) -> DesktopActionService:
    service: DesktopActionService = request.app.state.desktop_action_service
    return service


DesktopActionServiceDep = Annotated[DesktopActionService, Depends(get_desktop_action_service)]
_DESKTOP_ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_NOT_FOUND,
    **_CONFLICT,
    status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    status.HTTP_504_GATEWAY_TIMEOUT: {"model": ErrorResponse},
}


@router.get("/desktop/actions/apps", response_model=RegisteredAppsResponse, summary="The registered applications")
async def list_registered_apps(service: DesktopActionServiceDep) -> RegisteredAppsResponse:
    return RegisteredAppsResponse(apps=[RegisteredAppResponse(app_id=a, label=b) for a, b in service.registered_apps()])


@router.post(
    "/desktop/actions/focus",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Open the exact approval card for bringing one surface to the front (nothing happens yet)",
)
async def propose_desktop_focus(body: ProposeFocusBody, service: DesktopActionServiceDep) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(
        await service.propose_focus(
            worker_generation=body.worker_generation, surface_ref=body.surface_ref, surface_epoch=body.surface_epoch
        )
    )


@router.post(
    "/desktop/actions/scroll",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Open the exact approval card for one semantic scroll (nothing happens yet)",
)
async def propose_desktop_scroll(body: ProposeScrollBody, service: DesktopActionServiceDep) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(
        await service.propose_scroll(
            worker_generation=body.worker_generation,
            observation_id=body.observation_id,
            control_ref=body.control_ref,
            step=body.step,
        )
    )


@router.post(
    "/desktop/actions/scroll-targets",
    response_model=ScrollTargetsResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Observe one surface locally and list only its scrollable controls (nothing is sent anywhere)",
)
async def desktop_scroll_targets(body: ScrollTargetsBody, service: DesktopActionServiceDep) -> ScrollTargetsResponse:
    observation_id, targets = await service.scroll_targets(
        worker_generation=body.worker_generation, surface_ref=body.surface_ref, surface_epoch=body.surface_epoch
    )
    return ScrollTargetsResponse(
        observation_id=observation_id,
        targets=[ScrollTargetResponse(control_ref=ref, role=role, name=name) for ref, role, name in targets],
    )


@router.post(
    "/desktop/actions/launch",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Open the exact approval card for opening one registered application (nothing happens yet)",
)
async def propose_desktop_launch(body: ProposeLaunchBody, service: DesktopActionServiceDep) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(await service.propose_launch(app_id=body.app_id))


@router.get("/desktop/actions/latest", response_model=LatestDesktopActionResponse, summary="The newest desktop action")
async def latest_desktop_action(service: DesktopActionServiceDep) -> LatestDesktopActionResponse:
    view = await service.latest()
    return LatestDesktopActionResponse(action=None if view is None else DesktopActionResponse.from_view(view))


@router.get(
    "/desktop/actions/{action_id}",
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="One desktop action, as the trusted card shows it",
)
async def get_desktop_action(action_id: uuid.UUID, service: DesktopActionServiceDep) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(await service.get(action_id))


@router.post(
    "/desktop/actions/{action_id}/approve",
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="The exact approval click: claim it once and perform ONE effect",
)
async def approve_desktop_action(
    action_id: uuid.UUID, body: DesktopActionDecisionBody, service: DesktopActionServiceDep
) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(await service.approve(action_id, expected_revision=body.expected_revision))


@router.post(
    "/desktop/actions/{action_id}/decline",
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Decline the card. A declined action can never run",
)
async def decline_desktop_action(
    action_id: uuid.UUID, body: DesktopActionDecisionBody, service: DesktopActionServiceDep
) -> DesktopActionResponse:
    return DesktopActionResponse.from_view(await service.decline(action_id, expected_revision=body.expected_revision))


@router.post(
    "/desktop/actions/from-plan",
    status_code=status.HTTP_201_CREATED,
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="Open the exact execution approval card from one SUCCEEDED plan (nothing happens yet)",
)
async def propose_desktop_action_from_plan(
    body: ProposeFromPlanBody, service: DesktopActionServiceDep
) -> DesktopActionResponse:
    """S4. The plan's disclosure approval is not execution authority: this independently re-verifies
    everything (the plan is really SUCCEEDED and unconsumed, the observation and every ref it names
    still exist) before opening a second, separate, exact approval card."""
    return DesktopActionResponse.from_view(await service.propose_from_plan(body.plan_id))


@router.post(
    "/desktop/actions/{action_id}/reconcile",
    response_model=DesktopActionResponse,
    responses=_DESKTOP_ACTION_RESPONSES,
    summary="The only way out of an unresolved S4 mutation: record what the person actually observed",
)
async def reconcile_desktop_action(
    action_id: uuid.UUID, body: DesktopActionReconcileBody, service: DesktopActionServiceDep
) -> DesktopActionResponse:
    """Valid only from `OUTCOME_UNKNOWN` on a `set_control_value`/`select_control`/`invoke_control`
    action. Never retries or re-derives the effect: the person looked at the live application and
    reports what they saw. `still_unknown` leaves the block on every other desktop action in place."""
    return DesktopActionResponse.from_view(
        await service.reconcile(action_id, expected_revision=body.expected_revision, outcome=body.outcome)
    )
