from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorDetail, ErrorResponse
from app.domain.errors import (
    ActionConcurrencyError,
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


def register_error_handlers(app: FastAPI) -> None:
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
    app.add_exception_handler(StaleTaskRevisionError, _stale_task_revision)
    app.add_exception_handler(StaleActionRevisionError, _stale_action_revision)
    app.add_exception_handler(ApprovalNotUsableError, _approval_not_usable)
