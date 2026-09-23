"""Milestone 10 S1 routes: M10 file roots and approved documents.

Every route is runtime-internal: Electron main holds the only bearer credential and pins each path in its
own allowlist. There is no route that reads an arbitrary path, writes, copies, moves, renames or deletes a
file, opens a document in another application, or takes a provider choice from the renderer.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.api.document_schemas import (
    AddDroppedFileBody,
    AddRootFileBody,
    CompareBody,
    CreateDisclosureBody,
    CreateDocumentTaskBody,
    DocumentGrantBody,
    DocumentProviderContextResponse,
    DocumentRevokeBody,
    DocumentTaskResponse,
    ExtractBody,
    FileRootListResponse,
    FileRootResponse,
    LatestDocumentTaskResponse,
    LocalComparisonResponse,
    RecordComparisonBody,
    RegisterRootBody,
    RevokeRootBody,
    RootListingResponse,
)
from app.api.schemas import ErrorResponse
from app.services.documents import DocumentService

router = APIRouter()


def get_document_service(request: Request) -> DocumentService:
    service: DocumentService = request.app.state.document_service
    return service


DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
    status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
}


# ---- roots -----------------------------------------------------------------------------------------


@router.get("/file-roots", response_model=FileRootListResponse, summary="M10 file roots (labels and permissions only)")
async def list_file_roots(service: DocumentServiceDep) -> FileRootListResponse:
    return FileRootListResponse(roots=[FileRootResponse.from_view(view) for view in await service.list_roots()])


@router.post(
    "/file-roots",
    status_code=status.HTTP_201_CREATED,
    response_model=FileRootResponse,
    responses=_RESPONSES,
    summary="Register a folder main's native dialog returned, with explicit permissions (main only)",
)
async def register_file_root(body: RegisterRootBody, service: DocumentServiceDep) -> FileRootResponse:
    return FileRootResponse.from_view(
        await service.register_root(
            path=body.path, label=body.label, can_read=body.can_read, can_create=body.can_create, can_modify=body.can_modify
        )
    )


@router.post("/file-roots/{root_id}/revoke", response_model=FileRootResponse, responses=_RESPONSES, summary="Revoke a root")
async def revoke_file_root(root_id: uuid.UUID, body: RevokeRootBody, service: DocumentServiceDep) -> FileRootResponse:
    return FileRootResponse.from_view(await service.revoke_root(root_id, expected_revision=body.expected_revision))


@router.get(
    "/file-roots/{root_id}/files",
    response_model=RootListingResponse,
    responses=_RESPONSES,
    summary="A bounded listing of supported documents under a READ root (root-relative names only)",
)
async def list_file_root_files(root_id: uuid.UUID, service: DocumentServiceDep) -> RootListingResponse:
    return RootListingResponse.from_listing(await service.list_root_files(root_id))


# ---- document tasks ----------------------------------------------------------------------------------


@router.post(
    "/document-tasks", status_code=status.HTTP_201_CREATED, response_model=DocumentTaskResponse, summary="A new document task"
)
async def create_document_task(body: CreateDocumentTaskBody, service: DocumentServiceDep) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(await service.create_task(objective=body.objective))


@router.get("/document-tasks/latest", response_model=LatestDocumentTaskResponse, summary="The newest document task, if any")
async def latest_document_task(service: DocumentServiceDep) -> LatestDocumentTaskResponse:
    view = await service.latest()
    return LatestDocumentTaskResponse(task=None if view is None else DocumentTaskResponse.from_view(view))


@router.get("/document-tasks/{task_id}", response_model=DocumentTaskResponse, responses=_RESPONSES, summary="One document task")
async def get_document_task(task_id: uuid.UUID, service: DocumentServiceDep) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(await service.describe(task_id))


@router.post(
    "/document-tasks/{task_id}/files",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Add one file from a READ root by its root-relative name (bound to its identity and hash)",
)
async def add_root_file(task_id: uuid.UUID, body: AddRootFileBody, service: DocumentServiceDep) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.add_root_file(task_id, root_id=body.root_id, relative_path=body.relative_path)
    )


@router.post(
    "/document-tasks/{task_id}/dropped-files",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Add exactly the one file main holds as dropped (main only; never its folder)",
)
async def add_dropped_file(task_id: uuid.UUID, body: AddDroppedFileBody, service: DocumentServiceDep) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.add_dropped_file(task_id, path=body.path, display_name=body.display_name)
    )


@router.post(
    "/document-tasks/{task_id}/extract",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Re-verify one file of this task and extract bounded text in the contained helper",
)
async def extract_document(task_id: uuid.UUID, body: ExtractBody, service: DocumentServiceDep) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(await service.extract(task_id, file_id=body.file_id))


@router.post(
    "/document-tasks/{task_id}/compare",
    response_model=LocalComparisonResponse,
    responses=_RESPONSES,
    summary="Compare two of this task's documents LOCALLY (nothing is sent anywhere)",
)
async def compare_documents(task_id: uuid.UUID, body: CompareBody, service: DocumentServiceDep) -> LocalComparisonResponse:
    return LocalComparisonResponse.from_comparison(
        await service.compare_local(task_id, first_id=body.first_document_id, second_id=body.second_document_id)
    )


@router.post(
    "/document-tasks/{task_id}/disclosure",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Open the exact disclosure card (documents, excerpts, one provider, one model, purpose). Nothing is sent",
)
async def create_document_disclosure(
    task_id: uuid.UUID, body: CreateDisclosureBody, service: DocumentServiceDep
) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.create_disclosure(
            task_id, document_ids=body.document_ids, recipient=body.recipient, model=body.model, purpose=body.purpose
        )
    )


@router.post(
    "/document-tasks/{task_id}/disclosure/grant",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="The trusted click: confirm exactly the card shown (single use)",
)
async def grant_document_disclosure(
    task_id: uuid.UUID, body: DocumentGrantBody, service: DocumentServiceDep
) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.confirm(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision)
    )


@router.post(
    "/document-tasks/{task_id}/disclosure/revoke",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Decline or withdraw the disclosure (cannot un-send after a claim)",
)
async def revoke_document_disclosure(
    task_id: uuid.UUID, body: DocumentRevokeBody, service: DocumentServiceDep
) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.revoke(task_id, grant_id=body.grant_id, expected_revision=body.expected_revision, reason=body.reason)
    )


@router.post(
    "/document-tasks/{task_id}/disclosure/claim",
    response_model=DocumentProviderContextResponse,
    responses=_RESPONSES,
    summary="Spend the approval and release the redacted excerpts for ONE provider call (internal)",
)
async def claim_document_disclosure(task_id: uuid.UUID, service: DocumentServiceDep) -> DocumentProviderContextResponse:
    return DocumentProviderContextResponse.from_context(await service.claim(task_id))


@router.post(
    "/document-tasks/{task_id}/disclosure/result",
    response_model=DocumentTaskResponse,
    responses=_RESPONSES,
    summary="Record the one provider attempt's grounded comparison or failure (internal)",
)
async def record_document_comparison(
    task_id: uuid.UUID, body: RecordComparisonBody, service: DocumentServiceDep
) -> DocumentTaskResponse:
    return DocumentTaskResponse.from_view(
        await service.record_result(task_id, disclosure_id=body.disclosure_id, result=body.result, failure=body.failure)
    )
