import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, status
from fastapi.responses import JSONResponse

from app.api.schemas import (
    CancelTaskBody,
    CreateTaskBody,
    ErrorResponse,
    HealthResponse,
    TaskEventListResponse,
    TaskEventResponse,
    TaskResponse,
)
from app.db.engine import ping_database
from app.services.tasks import TaskService

logger = logging.getLogger(__name__)

router = APIRouter()


def get_task_service(request: Request) -> TaskService:
    service: TaskService = request.app.state.task_service
    return service


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}}
_CONFLICT: dict[int | str, dict[str, Any]] = {status.HTTP_409_CONFLICT: {"model": ErrorResponse}}


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
