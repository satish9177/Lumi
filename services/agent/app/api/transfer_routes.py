"""Milestone 10 S2 routes: controlled downloads and file placement.

Runtime-internal (main holds the only bearer credential and pins each path). There is no route that takes
a destination path, a quarantine path, an overwrite flag, a header or a cookie, and none that opens,
executes or deletes a user file. `download` and `place` are the two steps of one confirmed grant, and each
spends one single-use step authorization at most once.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import ErrorResponse
from app.domain.transfers import MAX_INTENT_CHARS
from app.services.transfers import TransferService, TransferView

router = APIRouter()


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTransferBody(_Body):
    url: str = Field(min_length=8, max_length=2048)
    root_id: uuid.UUID
    file_name: str = Field(min_length=1, max_length=255)
    intent: str = Field(min_length=1, max_length=MAX_INTENT_CHARS)


class TransferGrantBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int = Field(ge=1)


class TransferRevokeBody(_Body):
    grant_id: uuid.UUID
    expected_revision: int | None = Field(default=None, ge=1)


class TransferCardResponse(BaseModel):
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    source_url: str
    source_origin: str
    intent: str
    dest_root_id: uuid.UUID
    dest_root_label: str
    dest_name: str
    expected_kind: Literal["pdf", "docx", "txt"]
    max_bytes: int
    overwrite: bool


class TransferResponse(BaseModel):
    """Safe metadata only: no quarantine path, no absolute destination path, no file index."""

    task_id: uuid.UUID
    task_status: str
    task_revision: int
    transfer_id: uuid.UUID
    phase: Literal[
        "awaiting_approval", "approved", "declined", "downloading", "quarantined", "placing", "placed", "failed",
        "download_unknown", "placement_unknown",
    ]
    status: str
    error_code: str | None
    download_status: str | None
    place_status: str | None
    length: int | None
    sha256: str | None
    kind: str | None
    dest_root_label: str
    dest_name: str
    card: TransferCardResponse | None

    @classmethod
    def from_view(cls, view: TransferView) -> "TransferResponse":
        grant = view.grant
        transfer = view.transfer
        return cls(
            task_id=view.task_id,
            task_status=view.task_status,
            task_revision=view.task_revision,
            transfer_id=transfer.id,
            phase=view.phase,
            status=transfer.status,
            error_code=transfer.error_code,
            download_status=view.download_status,
            place_status=view.place_status,
            length=transfer.length,
            sha256=transfer.sha256,
            kind=transfer.kind,
            dest_root_label=view.root_label,
            dest_name=transfer.dest_name,
            card=None
            if grant is None
            else TransferCardResponse(
                grant_id=grant.id,
                grant_revision=grant.revision,
                grant_status=grant.status.value,
                expires_at=grant.expires_at,
                source_url=grant.scope.source_url,
                source_origin=grant.scope.source_origin,
                intent=grant.scope.intent,
                dest_root_id=grant.scope.dest_root_id,
                dest_root_label=grant.scope.dest_root_label,
                dest_name=grant.scope.dest_name,
                expected_kind=grant.scope.expected_kind,
                max_bytes=grant.scope.max_bytes,
                overwrite=grant.scope.overwrite,
            ),
        )


class LatestTransferResponse(BaseModel):
    transfer: TransferResponse | None


def get_transfer_service(request: Request) -> TransferService:
    service: TransferService = request.app.state.transfer_service
    return service


TransferServiceDep = Annotated[TransferService, Depends(get_transfer_service)]
_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
    status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
}


@router.post("/transfers", status_code=status.HTTP_201_CREATED, response_model=TransferResponse, responses=_RESPONSES,
             summary="Open the download card (source, destination folder, name, limits, no overwrite). Nothing is fetched")
async def create_transfer(body: CreateTransferBody, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(
        await service.create(url=body.url, root_id=body.root_id, file_name=body.file_name, intent=body.intent)
    )


@router.get("/transfers/latest", response_model=LatestTransferResponse, summary="The newest transfer, if any")
async def latest_transfer(service: TransferServiceDep) -> LatestTransferResponse:
    view = await service.latest()
    return LatestTransferResponse(transfer=None if view is None else TransferResponse.from_view(view))


@router.get("/transfers/{task_id}", response_model=TransferResponse, responses=_RESPONSES, summary="One transfer")
async def get_transfer(task_id: uuid.UUID, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.describe(task_id))


@router.post("/transfers/{task_id}/grant", response_model=TransferResponse, responses=_RESPONSES, summary="The trusted click")
async def grant_transfer(task_id: uuid.UUID, body: TransferGrantBody, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.confirm(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision))


@router.post("/transfers/{task_id}/revoke", response_model=TransferResponse, responses=_RESPONSES, summary="Decline or stop")
async def revoke_transfer(task_id: uuid.UUID, body: TransferRevokeBody, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.revoke(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision))


@router.post("/transfers/{task_id}/download", response_model=TransferResponse, responses=_RESPONSES,
             summary="Step 1: fetch the approved URL into the quarantine (once)")
async def download_transfer(task_id: uuid.UUID, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.download(task_id))


@router.post("/transfers/{task_id}/place", response_model=TransferResponse, responses=_RESPONSES,
             summary="Step 2: atomically place the verified download into the approved folder (no overwrite)")
async def place_transfer(task_id: uuid.UUID, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.place(task_id))


@router.post("/transfers/{task_id}/reconcile", response_model=TransferResponse, responses=_RESPONSES,
             summary="Read-only: establish an unknown step's outcome from local evidence. Never refetches")
async def reconcile_transfer(task_id: uuid.UUID, service: TransferServiceDep) -> TransferResponse:
    return TransferResponse.from_view(await service.reconcile(task_id))
