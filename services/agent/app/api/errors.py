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
from app.domain.errors import (
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
    app.add_exception_handler(StaleTaskRevisionError, _stale_task_revision)
    app.add_exception_handler(StaleActionRevisionError, _stale_action_revision)
    app.add_exception_handler(ApprovalNotUsableError, _approval_not_usable)
