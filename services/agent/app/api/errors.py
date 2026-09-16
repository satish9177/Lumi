from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorDetail, ErrorResponse
from app.domain.errors import (
    StaleTaskRevisionError,
    TaskConcurrencyError,
    TaskNotCancellableError,
    TaskNotFoundError,
)


def _error(status_code: int, detail: ErrorDetail) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=detail).model_dump(mode="json", exclude_none=True),
    )


async def _not_found(_: Request, exc: Exception) -> JSONResponse:
    return _error(status.HTTP_404_NOT_FOUND, ErrorDetail(code="task_not_found", message=str(exc)))


async def _not_cancellable(_: Request, exc: Exception) -> JSONResponse:
    return _error(
        status.HTTP_409_CONFLICT, ErrorDetail(code="task_not_cancellable", message=str(exc))
    )


async def _stale_revision(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StaleTaskRevisionError)
    return _error(
        status.HTTP_409_CONFLICT,
        ErrorDetail(code="stale_revision", message=str(exc), current_revision=exc.current_revision),
    )


async def _concurrency(_: Request, exc: Exception) -> JSONResponse:
    return _error(
        status.HTTP_409_CONFLICT, ErrorDetail(code="concurrent_modification", message=str(exc))
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(TaskNotFoundError, _not_found)
    app.add_exception_handler(TaskNotCancellableError, _not_cancellable)
    app.add_exception_handler(StaleTaskRevisionError, _stale_revision)
    app.add_exception_handler(TaskConcurrencyError, _concurrency)
