from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorDetail, ErrorResponse
from app.browser.errors import (
    BrowserExecutionNotSupportedError,
    BrowserWorkerError,
    BrowserWorkerNotConfiguredError,
)
from app.domain.booking import BookingProposalError
from app.domain.page_observation import AnswerNotGroundedError
from app.domain.research import AnswerNotGroundedError as ResearchAnswerNotGroundedError
from app.domain.browser_profile import ProfileRefusal
from app.domain.login_takeover import TakeoverRefusal
from app.domain.research import ResearchRefusal
from app.services.research_search import SearchFailedError
from app.domain.errors import (
    BookingCriteriaError,
    BookingCriteriaMismatchError,
    TaskAlreadyBookedError,
    TaskHasUnresolvedActionError,
    ActionAlreadyOpenError,
    ActionConcurrencyError,
    BookingSlotUnavailableError,
    BrowserObservationError,
    ActionNotFoundError,
    ActionProposalConflictError,
    ApprovalNotUsableError,
    AttemptNotFoundError,
    InvalidActionTransitionError,
    StaleActionRevisionError,
    StaleTaskRevisionError,
    TaskConcurrencyError,
    TaskNotAcceptingActionsError,
    TaskNotCancellableError,
    TaskKindMismatchError,
    TaskNotFoundError,
    DestinationNotAllowedError,
    InspectionProposalError,
    ObservationNotAvailableError,
    PublicInspectionNotConfiguredError,
    ResearchAnswerAlreadyRecordedError,
    ResearchBudgetExhaustedError,
    ResearchGrantNotFoundError,
    ResearchGrantNotUsableError,
    ResearchNotConfiguredError,
    ResearchSessionUnavailableError,
    ResearchStepInFlightError,
    ResearchStepRefusedError,
    StaleObservationError,
)


def _error(status_code: int, detail: ErrorDetail) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=detail).model_dump(mode="json", exclude_none=True),
    )


Handler = Callable[[Request, Exception], Awaitable[JSONResponse]]


def _simple(status_code: int, code: str) -> Handler:
    """One handler for errors whose message is the whole story."""

    async def handler(_: Request, exc: Exception) -> JSONResponse:
        return _error(status_code, ErrorDetail(code=code, message=str(exc)))

    return handler


async def _stale_task_revision(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StaleTaskRevisionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="stale_revision", message=str(exc), current_revision=exc.current_revision),
    )


async def _stale_action_revision(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StaleActionRevisionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(
            code="stale_action_revision",
            message=str(exc),
            current_revision=exc.current_revision,
        ),
    )


async def _approval_not_usable(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApprovalNotUsableError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="approval_not_usable", message=str(exc), reason=exc.reason),
    )


async def _destination_not_allowed(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DestinationNotAllowedError)
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorDetail(
            code="destination_not_allowed",
            message="That page is not an allowed inspection destination.",
            reason=exc.code,
        ),
    )


async def _answer_not_grounded(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AnswerNotGroundedError)
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorDetail(
            code="answer_not_grounded",
            message="The answer is not supported by the stored observation.",
            reason=exc.code,
        ),
    )


def _reasoned(status_code: int, code: str, message: str, attribute: str) -> Handler:
    """A stable code plus one machine-readable reason. Never internal text."""

    async def handler(_: Request, exc: Exception) -> JSONResponse:
        reason = getattr(exc, attribute, None)
        return _error(
            status_code,
            ErrorDetail(
                code=code,
                message=message,
                reason=str(reason) if isinstance(reason, str) else None,
            ),
        )

    return handler


def _fixed(status_code: int, code: str, message: str) -> Handler:
    """For errors whose text may carry internal detail: a fixed safe message."""

    async def handler(_: Request, __: Exception) -> JSONResponse:
        return _error(status_code, ErrorDetail(code=code, message=message))

    return handler


def register_error_handlers(app: FastAPI) -> None:
    async def invalid_request(_: Request, __: Exception) -> JSONResponse:
        return _error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            ErrorDetail(code="invalid_request", message="Request validation failed."),
        )

    # FastAPI's default includes rejected input values. Runtime callers need a
    # stable code, never an echo of proposal text or malformed credentials.
    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(
        TaskNotFoundError, _simple(status.HTTP_404_NOT_FOUND, "task_not_found")
    )
    app.add_exception_handler(
        ActionNotFoundError, _simple(status.HTTP_404_NOT_FOUND, "action_not_found")
    )
    app.add_exception_handler(
        TaskNotCancellableError, _simple(status.HTTP_409_CONFLICT, "task_not_cancellable")
    )
    app.add_exception_handler(
        TaskNotAcceptingActionsError,
        _simple(status.HTTP_409_CONFLICT, "task_not_accepting_actions"),
    )
    app.add_exception_handler(
        ActionProposalConflictError,
        _simple(status.HTTP_409_CONFLICT, "action_proposal_conflict"),
    )
    app.add_exception_handler(
        InvalidActionTransitionError,
        _simple(status.HTTP_409_CONFLICT, "invalid_action_transition"),
    )
    app.add_exception_handler(
        AttemptNotFoundError, _simple(status.HTTP_409_CONFLICT, "no_unfinished_attempt")
    )
    app.add_exception_handler(
        TaskConcurrencyError, _simple(status.HTTP_409_CONFLICT, "concurrent_modification")
    )
    app.add_exception_handler(
        ActionConcurrencyError, _simple(status.HTTP_409_CONFLICT, "concurrent_modification")
    )
    # A runtime with no browser worker configured simply has no browser
    # capability. 503 rather than 500: nothing is broken, the capability is
    # absent, and no side effect was attempted.
    app.add_exception_handler(
        BrowserWorkerNotConfiguredError,
        _simple(status.HTTP_503_SERVICE_UNAVAILABLE, "browser_worker_not_configured"),
    )
    app.add_exception_handler(
        BrowserExecutionNotSupportedError,
        _simple(status.HTTP_409_CONFLICT, "browser_execution_not_supported"),
    )
    # A stored proposal the booking tool cannot parse is never executed on a
    # best-effort reading of the parts that did parse.
    app.add_exception_handler(
        BookingProposalError, _simple(status.HTTP_409_CONFLICT, "invalid_booking_proposal")
    )
    app.add_exception_handler(
        ActionAlreadyOpenError, _simple(status.HTTP_409_CONFLICT, "action_already_open")
    )
    app.add_exception_handler(
        TaskKindMismatchError, _simple(status.HTTP_409_CONFLICT, "task_kind_mismatch")
    )
    app.add_exception_handler(
        BookingSlotUnavailableError,
        _simple(status.HTTP_409_CONFLICT, "booking_slot_unavailable"),
    )
    app.add_exception_handler(
        BrowserObservationError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "browser_observation_failed",
            "The appointment site could not be read.",
        ),
    )
    # Worker reachability failures outside a dispatch (handshake, discovery).
    # The not-configured subclass keeps its own handler: Starlette resolves
    # handlers by the exception's MRO, most specific first.
    app.add_exception_handler(
        BrowserWorkerError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "browser_worker_unavailable",
            "The browser worker is unavailable.",
        ),
    )
    app.add_exception_handler(
        TaskHasUnresolvedActionError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "task_has_unresolved_action",
            "A booking for this task may already exist; resolve it first.",
        ),
    )
    app.add_exception_handler(
        TaskAlreadyBookedError,
        _fixed(status.HTTP_409_CONFLICT, "task_already_booked", "The booking for this task is confirmed."),
    )
    app.add_exception_handler(
        BookingCriteriaError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "invalid_booking_criteria",
            "The booking constraints could not be applied.",
        ),
    )
    app.add_exception_handler(
        BookingCriteriaMismatchError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "booking_criteria_mismatch",
            "That appointment no longer matches the requested constraints.",
        ),
    )
    app.add_exception_handler(DestinationNotAllowedError, _destination_not_allowed)
    app.add_exception_handler(AnswerNotGroundedError, _answer_not_grounded)
    app.add_exception_handler(
        InspectionProposalError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "invalid_inspection_proposal",
            "The stored inspection proposal is not valid.",
        ),
    )
    app.add_exception_handler(
        PublicInspectionNotConfiguredError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "public_inspection_not_configured",
            "Public page inspection is not configured.",
        ),
    )
    app.add_exception_handler(
        ObservationNotAvailableError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "observation_not_available",
            "There is no stored page observation for that action.",
        ),
    )
    app.add_exception_handler(
        StaleObservationError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "stale_observation",
            "That observation is no longer the current one.",
        ),
    )
    # --- Milestone 7b public research ------------------------------------
    app.add_exception_handler(
        ResearchNotConfiguredError,
        _fixed(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_not_configured",
            "Public web research is not configured.",
        ),
    )
    app.add_exception_handler(
        ResearchGrantNotFoundError,
        _fixed(
            status.HTTP_404_NOT_FOUND,
            "research_grant_not_found",
            "There is no research scope for that task.",
        ),
    )
    app.add_exception_handler(
        ResearchGrantNotUsableError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "research_grant_not_usable",
            "That research scope does not authorise anything right now.",
            "reason",
        ),
    )
    app.add_exception_handler(
        ResearchStepRefusedError,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_step_refused",
            "That research step was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ProfileRefusal,
        # One stable code, one machine-readable reason, and never a path: a
        # profile refusal must not be the thing that tells a caller where the
        # profile directory is, or whether one exists.
        _reasoned(
            status.HTTP_409_CONFLICT,
            "browser_profile_refused",
            "That browser profile operation was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        TakeoverRefusal,
        # Milestone 8a S2. Never page text, never a title, never a URL: a
        # takeover refusal carries a stable code and nothing page-derived.
        _reasoned(
            status.HTTP_409_CONFLICT,
            "login_attempt_refused",
            "That login takeover operation was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchRefusal,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_step_refused",
            "That research input was refused.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchBudgetExhaustedError,
        _reasoned(
            status.HTTP_409_CONFLICT,
            "research_budget_exhausted",
            "This research task has reached one of its limits.",
            "limit",
        ),
    )
    app.add_exception_handler(
        ResearchStepInFlightError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "research_step_in_flight",
            "A research step for this task has not finished yet.",
        ),
    )
    app.add_exception_handler(
        ResearchSessionUnavailableError,
        _reasoned(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_session_unavailable",
            "The research browser session is not available.",
            "code",
        ),
    )
    app.add_exception_handler(
        ResearchAnswerAlreadyRecordedError,
        _fixed(
            status.HTTP_409_CONFLICT,
            "research_answer_already_recorded",
            "This research task already has a recorded answer.",
        ),
    )
    app.add_exception_handler(
        ResearchAnswerNotGroundedError,
        _reasoned(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "research_answer_not_grounded",
            "The answer is not supported by what this task observed.",
            "code",
        ),
    )
    app.add_exception_handler(
        SearchFailedError,
        _reasoned(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "research_search_failed",
            "The public search could not be completed.",
            "code",
        ),
    )
    app.add_exception_handler(StaleTaskRevisionError, _stale_task_revision)
    app.add_exception_handler(StaleActionRevisionError, _stale_action_revision)
    app.add_exception_handler(ApprovalNotUsableError, _approval_not_usable)
