"""Milestone 10 S1: the runtime's document service -- M10 file roots, task-owned file refs, extraction,
local comparison and ONE exact provider disclosure.

Authority is checked here, in this order, on every operation that touches a file:

1. the task is a `document_task` and the ref/document/grant named belongs to *that* task (task B can never
   read task A's file, document or approval);
2. for a root file: the root is ACTIVE and has `can_read`, and is still the same directory (identity);
3. the name is re-resolved from the root with an `lstat` walk (no reparse point anywhere);
4. the bytes are read from a handle proven to be the resolved file, inside the root, with the identity and
   SHA-256 recorded when the ref was created -- a replaced file under the same name is `file_changed`.

No method returns, logs or records an absolute path. Extracted text goes to exactly three places: the
`documents` row (retained a day), the trusted view (a bounded preview), and -- only after a trusted click,
once -- the redacted projection named on the disclosure card.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import file_refs as file_refs_table
from app.db.tables import tasks as tasks_table
from app.documents.errors import ExtractionRefusal
from app.documents.limits import MAX_FILE_BYTES
from app.documents.runner import Extracted, run_helper
from app.documents.sniff import check_claim
from app.domain.authenticated import Recipient
from app.domain.documents import (
    DOCUMENT_TASK_TYPE,
    ERROR_DOCUMENT_GONE,
    ERROR_INVALID_OUTPUT,
    ERROR_NOT_GROUNDED,
    ERROR_RUNTIME_RESTART,
    MAX_FILES_PER_TASK,
    PROVIDER_FAILURE_CODES,
    RETENTION_SECONDS,
    STALE_CLAIM_SECONDS,
    CannotCompare,
    Comparison,
    CompareResult,
    DisclosedDocument,
    DocumentDiscloseScope,
    DocumentRefusal,
    LocalComparison,
    NotGroundedError,
    Projection,
    build_projection,
    compare_locally,
    parse_compare_result,
    preview,
    text_sha256,
    validate_label,
    validate_model,
    validate_objective,
    validate_purpose,
    verify_grounding,
)
from app.domain.errors import TaskConcurrencyError, TaskKindMismatchError, TaskNotAcceptingActionsError, TaskNotFoundError
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.files.broker import (
    FileBrokerRefusal,
    ProtectedFolders,
    ListedFile,
    VerifiedRead,
    document_format_for,
    inspect_dropped,
    listable,
    inspect_root,
    list_documents,
    read_verified,
    resolve,
    verify_root,
)
from app.files.handles import FileIdentity, normcase
from app.files.names import FileNameRefusal, display_relative, validate_file_name, validate_relative_path
from app.repositories.documents import (
    DocumentAnswerRecord,
    DocumentDisclosureRecord,
    DocumentGrantRecord,
    DocumentRecord,
    DocumentRepository,
)
from app.repositories.files import FileRefRecord, FileRepository, FileRootRecord
from app.repositories.tasks import TaskRecord, TaskRepository

logger = logging.getLogger("lumi.documents")

Extractor = Callable[[bytes, str], Extracted]


# ---- views -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RootView:
    root_id: uuid.UUID
    label: str
    can_read: bool
    can_create: bool
    can_modify: bool
    revision: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RootListing:
    root_id: uuid.UUID
    files: tuple[ListedFile, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileView:
    file_id: uuid.UUID
    source: str
    display_name: str
    relative_path: str | None
    root_id: uuid.UUID | None
    format: str
    size_bytes: int
    added_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentView:
    document_id: uuid.UUID
    file_id: uuid.UUID
    format: str
    page_count: int | None
    text_chars: int
    truncated: bool
    flags: dict[str, int]
    preview: str | None
    expires_at: datetime
    purged: bool


@dataclass(frozen=True, slots=True)
class CardDocument:
    doc_ref: str
    document_id: uuid.UUID
    label: str
    excerpt: str | None


@dataclass(frozen=True, slots=True)
class DisclosureCardView:
    grant_id: uuid.UUID
    grant_revision: int
    grant_status: str
    expires_at: datetime | None
    recipient: str
    model: str
    purpose: str
    documents: tuple[CardDocument, ...]
    max_excerpt_bytes: int
    text_bytes: int | None
    redaction_count: int | None
    truncated: bool | None
    redaction_policy: str


@dataclass(frozen=True, slots=True)
class DocumentTaskView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    objective: str
    phase: str
    files: tuple[FileView, ...]
    documents: tuple[DocumentView, ...]
    card: DisclosureCardView | None
    disclosure: DocumentDisclosureRecord | None
    answer: DocumentAnswerRecord | None


@dataclass(frozen=True, slots=True)
class ProviderContext:
    disclosure_id: uuid.UUID
    task_id: uuid.UUID
    purpose: str
    recipient: str
    model: str
    projection: dict[str, Any]
    projection_digest: str


def _refused(error: FileBrokerRefusal | FileNameRefusal | ExtractionRefusal) -> DocumentRefusal:
    return DocumentRefusal(error.code)


class DocumentService:
    """Short transactions only. File I/O and extraction run in worker threads, outside any transaction."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        grant_ttl_seconds: int,
        forbidden_roots: tuple[str, ...] = (),
        extractor: Extractor = run_helper,
        protected_folders: ProtectedFolders | None = None,
    ) -> None:
        self._engine = engine
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        self._forbidden = forbidden_roots
        self._protected = protected_folders
        self._extractor = extractor

    # ---- roots ---------------------------------------------------------------------------------

    async def register_root(self, *, path: str, label: object, can_read: bool, can_create: bool, can_modify: bool) -> RootView:
        """A folder the person chose in a native dialog in Electron main, with the permissions they ticked."""
        if can_modify:
            raise DocumentRefusal("modify_not_supported")
        if not (can_read or can_create):
            raise DocumentRefusal("permission_missing")
        name = validate_label(label)
        try:
            facts = await asyncio.to_thread(inspect_root, path, forbidden=self._forbidden, protected=self._protected)
        except FileBrokerRefusal as refusal:
            raise _refused(refusal) from None
        key = normcase(facts.canonical_path)
        async with self._engine.begin() as connection:
            repository = FileRepository(connection)
            if await repository.active_root_for_key(key) is not None:
                raise DocumentRefusal("root_already_approved")
            try:
                async with connection.begin_nested():
                    root = await repository.insert_root(
                        root_id=uuid.uuid4(),
                        label=name,
                        canonical_path=facts.canonical_path,
                        path_key=key,
                        volume_serial=facts.volume,
                        dir_index=facts.index,
                        can_read=can_read,
                        can_create=can_create,
                    )
            except IntegrityError:
                raise DocumentRefusal("root_already_approved") from None
        return _root_view(root)

    async def list_roots(self) -> list[RootView]:
        async with self._engine.connect() as connection:
            return [_root_view(root) for root in await FileRepository(connection).list_roots()]

    async def revoke_root(self, root_id: uuid.UUID, *, expected_revision: int | None) -> RootView:
        async with self._engine.begin() as connection:
            repository = FileRepository(connection)
            root = await repository.get_root(root_id)
            if root is None:
                raise DocumentRefusal("root_not_found")
            revoked = await repository.revoke_root(root_id, expected_revision=expected_revision)
            if revoked is None:
                raise DocumentRefusal("root_revoked" if not root.active else "root_changed")
        return _root_view(revoked)

    async def list_root_files(self, root_id: uuid.UUID) -> RootListing:
        root = await self._readable_root(root_id)
        try:
            canonical = await asyncio.to_thread(verify_root, root.canonical_path, volume=root.volume_serial, index=root.dir_index)
            files, truncated = await asyncio.to_thread(list_documents, canonical)
        except FileBrokerRefusal as refusal:
            raise _refused(refusal) from None
        return RootListing(root_id=root.id, files=tuple(files), truncated=truncated)

    async def _readable_root(self, root_id: uuid.UUID) -> FileRootRecord:
        async with self._engine.connect() as connection:
            root = await FileRepository(connection).get_root(root_id)
        if root is None:
            raise DocumentRefusal("root_not_found")
        if not root.active:
            raise DocumentRefusal("root_revoked")
        if not root.can_read:
            raise DocumentRefusal("permission_missing")
        return root

    # ---- tasks and files -----------------------------------------------------------------------

    async def create_task(self, *, objective: object = "") -> DocumentTaskView:
        goal = validate_objective(objective)
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(), status=TaskStatus.READY, request={"type": DOCUMENT_TASK_TYPE, "objective": goal}
            )
            await tasks.append_event(task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value})
            return await self._view(connection, task.id)

    async def add_root_file(self, task_id: uuid.UUID, *, root_id: uuid.UUID, relative_path: object) -> DocumentTaskView:
        try:
            components = validate_relative_path(relative_path)
        except FileNameRefusal as refusal:
            raise _refused(refusal) from None
        name = components[-1]
        kind = document_format_for(name)
        if kind is None:
            raise DocumentRefusal("unsupported_format")
        if not listable(components):
            # Only what the listing itself would show (S1 review finding 2): no dot-folders, AppData,
            # `.git`, `node_modules`, Office lock files or anything deeper than the listing goes.
            raise DocumentRefusal("not_listable")
        await self._require_room(task_id)
        root = await self._readable_root(root_id)
        try:
            read = await asyncio.to_thread(self._read_root_file, root, components, None, None)
            await asyncio.to_thread(check_claim, read.data, os.path.splitext(name)[1])
        except (FileBrokerRefusal, ExtractionRefusal) as refusal:
            raise _refused(refusal) from None
        async with self._engine.begin() as connection:
            task = await self._lock(connection, task_id)
            # Re-checked under the task lock, with the root row share-locked: a revocation that committed
            # while the file was being read wins.
            current = await FileRepository(connection).get_root(root_id, lock=True)
            if current is None or not current.active or not current.can_read:
                raise DocumentRefusal("root_revoked")
            await self._check_room(connection, task_id)
            ref = await FileRepository(connection).insert_ref(
                ref_id=uuid.uuid4(),
                task_id=task.id,
                source="ROOT_FILE",
                root_id=root.id,
                relative_path=display_relative(components),
                local_path=None,
                display_name=name,
                format=kind,
                volume_serial=read.identity.volume,
                file_index=read.identity.index,
                size_bytes=read.identity.size,
                mtime_ns=read.identity.mtime_ns,
                sha256=read.sha256,
            )
            await self._event(connection, task.id, TaskEventType.TASK_DOCUMENT_FILE_ADDED, {"file_id": str(ref.id), "source": ref.source, "format": ref.format})
            return await self._view(connection, task.id)

    async def add_dropped_file(self, task_id: uuid.UUID, *, path: str, display_name: object) -> DocumentTaskView:
        """Exactly the one file main holds as dropped. It never grants its folder or any sibling."""
        try:
            name = validate_file_name(display_name)
        except FileNameRefusal as refusal:
            raise _refused(refusal) from None
        kind = document_format_for(name)
        if kind is None:
            raise DocumentRefusal("unsupported_format")
        await self._require_room(task_id)
        try:
            canonical = await asyncio.to_thread(inspect_dropped, path)
            if os.path.basename(canonical).casefold() != name.casefold():
                raise DocumentRefusal("path_mismatch")
            read = await asyncio.to_thread(
                read_verified, canonical, expected_final=canonical, max_bytes=MAX_FILE_BYTES, root_canonical=None
            )
            await asyncio.to_thread(check_claim, read.data, os.path.splitext(name)[1])
        except (FileBrokerRefusal, ExtractionRefusal) as refusal:
            raise _refused(refusal) from None
        async with self._engine.begin() as connection:
            task = await self._lock(connection, task_id)
            await self._check_room(connection, task_id)
            ref = await FileRepository(connection).insert_ref(
                ref_id=uuid.uuid4(),
                task_id=task.id,
                source="DROPPED_FILE",
                root_id=None,
                relative_path=None,
                local_path=canonical,
                display_name=name,
                format=kind,
                volume_serial=read.identity.volume,
                file_index=read.identity.index,
                size_bytes=read.identity.size,
                mtime_ns=read.identity.mtime_ns,
                sha256=read.sha256,
            )
            await self._event(connection, task.id, TaskEventType.TASK_DOCUMENT_FILE_ADDED, {"file_id": str(ref.id), "source": ref.source, "format": ref.format})
            return await self._view(connection, task.id)

    async def extract(self, task_id: uuid.UUID, *, file_id: uuid.UUID) -> DocumentTaskView:
        """Re-verify the exact file version, then extract it in the contained helper. Idempotent per ref."""
        async with self._engine.connect() as connection:
            task = await self._require_task(connection, task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            ref = await FileRepository(connection).get_ref(file_id)
            if ref is None or ref.task_id != task_id:
                raise DocumentRefusal("file_not_found")
            existing = await DocumentRepository(connection).document_for_ref(ref.id)
            if existing is not None:
                return await self._view(connection, task_id)
        read = await self._reverify(ref)
        try:
            extracted = await asyncio.to_thread(self._extractor, read.data, ref.format)
        except ExtractionRefusal as refusal:
            async with self._engine.begin() as connection:
                await self._lock(connection, task_id)
                await self._event(
                    connection,
                    task_id,
                    TaskEventType.TASK_DOCUMENT_EXTRACTION_REFUSED,
                    {"file_id": str(ref.id), "error_code": refusal.code},
                )
            raise _refused(refusal) from None
        if extracted.format != ref.format:
            raise DocumentRefusal("type_mismatch")
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id)
            # A revocation that committed while the helper ran wins (S1 review finding 3a).
            await self._require_live_source(connection, ref, lock=True)
            repository = DocumentRepository(connection)
            if await repository.document_for_ref(ref.id) is None:
                document = await repository.insert_document(
                    document_id=uuid.uuid4(),
                    task_id=task_id,
                    file_ref_id=ref.id,
                    format=extracted.format,
                    page_count=extracted.pages,
                    text_value=extracted.text,
                    text_sha256=text_sha256(extracted.text),
                    truncated=extracted.truncated,
                    flags=extracted.flags,
                    retention=timedelta(seconds=RETENTION_SECONDS),
                )
                await self._event(
                    connection,
                    task_id,
                    TaskEventType.TASK_DOCUMENT_EXTRACTED,
                    {
                        "file_id": str(ref.id),
                        "document_id": str(document.id),
                        "format": document.format,
                        "page_count": document.page_count,
                        "text_chars": document.text_chars,
                        "truncated": document.truncated,
                    },
                )
            return await self._view(connection, task_id)

    async def _reverify(self, ref: FileRefRecord) -> VerifiedRead:
        expected = FileIdentity(volume=ref.volume_serial, index=ref.file_index, size=ref.size_bytes, mtime_ns=ref.mtime_ns)
        if ref.source == "ROOT_FILE":
            assert ref.root_id is not None and ref.relative_path is not None
            root = await self._readable_root(ref.root_id)
            try:
                components = validate_relative_path(ref.relative_path)
                return await asyncio.to_thread(self._read_root_file, root, components, expected, ref.sha256)
            except (FileBrokerRefusal, FileNameRefusal) as refusal:
                raise _refused(refusal) from None
        assert ref.local_path is not None
        try:
            canonical = await asyncio.to_thread(inspect_dropped, ref.local_path)
            return await asyncio.to_thread(
                read_verified,
                canonical,
                expected_final=ref.local_path,
                max_bytes=MAX_FILE_BYTES,
                root_canonical=None,
                expected=expected,
                expected_sha256=ref.sha256,
            )
        except FileBrokerRefusal as refusal:
            raise _refused(refusal) from None

    @staticmethod
    def _read_root_file(
        root: FileRootRecord, components: tuple[str, ...], expected: FileIdentity | None, expected_sha256: str | None
    ) -> VerifiedRead:
        canonical = verify_root(root.canonical_path, volume=root.volume_serial, index=root.dir_index)
        path = resolve(canonical, components)
        return read_verified(
            path,
            expected_final=path,
            max_bytes=MAX_FILE_BYTES,
            root_canonical=canonical,
            expected=expected,
            expected_sha256=expected_sha256,
        )

    async def _require_room(self, task_id: uuid.UUID) -> None:
        async with self._engine.connect() as connection:
            task = await self._require_task(connection, task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            await self._check_room(connection, task_id)

    @staticmethod
    async def _check_room(connection: AsyncConnection, task_id: uuid.UUID) -> None:
        if len(await FileRepository(connection).refs_for_task(task_id)) >= MAX_FILES_PER_TASK:
            raise DocumentRefusal("too_many_files")

    # ---- reading -------------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> DocumentTaskView:
        await self._expire_stale_claims()
        await self.sweep_expired()
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            return await self._view(connection, task_id)

    async def latest(self) -> DocumentTaskView | None:
        await self._expire_stale_claims()
        await self.sweep_expired()
        async with self._engine.connect() as connection:
            row = (await connection.execute(_LATEST_DOCUMENT_TASK)).scalar_one_or_none()
            return None if row is None else await self._view(connection, row)

    async def compare_local(self, task_id: uuid.UUID, *, first_id: uuid.UUID, second_id: uuid.UUID) -> LocalComparison:
        """Deterministic, on this machine. Nothing is sent anywhere and nothing is recorded."""
        if first_id == second_id:
            raise DocumentRefusal("same_document")
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            first = await self._text(connection, task_id, first_id)
            second = await self._text(connection, task_id, second_id)
        return compare_locally(first, second)

    @staticmethod
    async def _require_live_source(connection: AsyncConnection, ref: FileRefRecord, *, lock: bool = False) -> None:
        """A root file's authority is its root's: revoked (or read permission gone) means the document is
        no longer usable -- not for extraction, comparison, a card, a confirmation or a claim."""
        if ref.source != "ROOT_FILE" or ref.root_id is None:
            return
        root = await FileRepository(connection).get_root(ref.root_id, lock=lock)
        if root is None or not root.active or not root.can_read:
            raise DocumentRefusal("root_revoked")

    @classmethod
    async def _text(cls, connection: AsyncConnection, task_id: uuid.UUID, document_id: uuid.UUID) -> str:
        repository = DocumentRepository(connection)
        record = await repository.get_document(document_id)
        if record is None or record.task_id != task_id:
            raise DocumentRefusal("document_not_found")
        ref = await FileRepository(connection).get_ref(record.file_ref_id)
        if ref is None or ref.task_id != task_id:  # pragma: no cover - a document always has its task's ref.
            raise DocumentRefusal("document_not_found")
        await cls._require_live_source(connection, ref)
        text = await repository.text_for(document_id, task_id=task_id)
        if text is None:
            raise DocumentRefusal("document_expired")
        if text_sha256(text) != record.text_sha256:  # pragma: no cover - a row that disagrees with itself.
            raise DocumentRefusal("document_changed")
        return text

    # ---- the disclosure card and the trusted click ---------------------------------------------

    async def create_disclosure(
        self,
        task_id: uuid.UUID,
        *,
        document_ids: list[uuid.UUID],
        recipient: Recipient,
        model: object,
        purpose: object,
    ) -> DocumentTaskView:
        """Open the card. Nothing is sent. The provider and model are main's choice, never the renderer's."""
        model_name = validate_model(model)
        stated = validate_purpose(purpose)
        if not 1 <= len(document_ids) <= 2 or len(set(document_ids)) != len(document_ids):
            raise DocumentRefusal("documents_invalid")
        async with self._engine.begin() as connection:
            task = await self._lock(connection, task_id)
            repository = DocumentRepository(connection)
            if await repository.disclosure_for_task(task_id) is not None:
                raise DocumentRefusal("disclosure_already_made")
            listed: list[DisclosedDocument] = []
            texts: dict[str, str] = {}
            for position, document_id in enumerate(document_ids):
                text = await self._text(connection, task_id, document_id)
                record = await repository.get_document(document_id)
                assert record is not None
                ref = await FileRepository(connection).get_ref(record.file_ref_id)
                assert ref is not None
                doc_ref = ("d1", "d2")[position]
                listed.append(
                    DisclosedDocument(
                        doc_ref=doc_ref, document_id=document_id, text_sha256=record.text_sha256, label=ref.display_name
                    )
                )
                texts[doc_ref] = text
            scope = DocumentDiscloseScope(
                task_id=task_id, documents=tuple(listed), recipient=recipient, model=model_name, purpose=stated
            )
            build_projection(scope, texts)  # refuse now if it cannot be built
            open_grant = await repository.open_grant_for_task(task_id)
            if open_grant is not None:
                await repository.revoke_grant(grant_id=open_grant.id, expected_revision=None)
            grant = await repository.insert_grant(grant_id=uuid.uuid4(), scope=scope)
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_DOCUMENT_DISCLOSURE_REQUESTED,
                {"grant_id": str(grant.id), "grant_revision": grant.revision, "scope_digest": grant.scope_digest, "recipient": scope.recipient, "document_count": len(listed)},
            )
            await self._move_task(connection, task_id, TaskStatus.WAITING_APPROVAL)
            return await self._view(connection, task_id)

    async def confirm(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int) -> DocumentTaskView:
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id)
            repository = DocumentRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DocumentRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise DocumentRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise DocumentRefusal("grant_changed")
            await self._scope_texts(connection, grant)  # the documents must still be exactly these
            confirmed = await repository.confirm_grant(
                grant_id=grant.id, expected_revision=expected_revision, scope_digest=grant.scope_digest, ttl=self._grant_ttl
            )
            if confirmed is None:
                raise DocumentRefusal("grant_changed")
            await self._event(
                connection,
                task_id,
                TaskEventType.TASK_DOCUMENT_DISCLOSURE_GRANTED,
                {"grant_id": str(confirmed.id), "grant_revision": confirmed.revision, "scope_digest": confirmed.scope_digest},
            )
            await self._move_task(connection, task_id, TaskStatus.READY)
            return await self._view(connection, task_id)

    async def revoke(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None, reason: str) -> DocumentTaskView:
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id, require_accepting=False)
            repository = DocumentRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise DocumentRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.revoke_grant(grant_id=grant.id, expected_revision=expected_revision)
                if closed is None:
                    raise DocumentRefusal("grant_changed")
                await self._event(
                    connection,
                    task_id,
                    TaskEventType.TASK_DOCUMENT_DISCLOSURE_REVOKED,
                    {"grant_id": str(closed.id), "grant_revision": closed.revision, "reason": reason},
                )
                await self._move_task(connection, task_id, TaskStatus.READY)
            return await self._view(connection, task_id)

    async def _scope_texts(self, connection: AsyncConnection, grant: DocumentGrantRecord) -> dict[str, str]:
        texts: dict[str, str] = {}
        for item in grant.scope.documents:
            try:
                text = await self._text(connection, grant.task_id, item.document_id)
            except DocumentRefusal as refusal:
                raise DocumentRefusal("root_revoked" if refusal.code == "root_revoked" else "document_changed") from None
            if text_sha256(text) != item.text_sha256:
                raise DocumentRefusal("document_changed")
            texts[item.doc_ref] = text
        return texts

    # ---- the claim -------------------------------------------------------------------------------

    async def claim(self, task_id: uuid.UUID) -> ProviderContext:
        """Consume the grant and open the disclosure in ONE committed transaction, then release the projection.

        Whatever happens after this returns -- a crash, a timeout, garbage -- the approval is spent, and a
        result that never arrives becomes OUTCOME_UNKNOWN; nothing is ever sent a second time.
        """
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id)
            repository = DocumentRepository(connection)
            grant = await repository.latest_grant_for_task(task_id)
            if grant is None:
                raise DocumentRefusal("grant_not_found")
            if grant.status is not GrantStatus.ACTIVE:
                raise DocumentRefusal("grant_not_active")
            if await repository.grant_is_expired(grant.id):
                raise DocumentRefusal("grant_expired")
            projection = build_projection(grant.scope, await self._scope_texts(connection, grant))
            claimed = await repository.claim_grant(grant_id=grant.id, expected_revision=grant.revision)
            if claimed is None:
                raise DocumentRefusal("grant_not_active")
            disclosure = await repository.insert_disclosure(
                disclosure_id=uuid.uuid4(),
                task_id=task_id,
                grant_id=grant.id,
                recipient=grant.scope.recipient,
                model=grant.scope.model,
                projection_digest=projection.digest,
                document_count=len(grant.scope.documents),
                text_bytes=projection.text_bytes,
                redaction_count=projection.redaction_count,
                truncated=projection.truncated,
            )
            await self._event(
                connection,
                task_id,
                TaskEventType.TASK_DOCUMENT_DISCLOSURE_STARTED,
                {
                    "grant_id": str(grant.id),
                    "disclosure_id": str(disclosure.id),
                    "recipient": disclosure.recipient,
                    "projection_digest": disclosure.projection_digest,
                    "text_bytes": disclosure.text_bytes,
                    "redaction_count": disclosure.redaction_count,
                },
            )
            await self._move_task(connection, task_id, TaskStatus.EXECUTING)
        return ProviderContext(
            disclosure_id=disclosure.id,
            task_id=task_id,
            purpose=grant.scope.purpose,
            recipient=disclosure.recipient,
            model=disclosure.model,
            projection=projection.payload,
            projection_digest=projection.digest,
        )

    async def record_result(
        self, task_id: uuid.UUID, *, disclosure_id: uuid.UUID, result: Any | None, failure: str | None
    ) -> DocumentTaskView:
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id, require_accepting=False)
            repository = DocumentRepository(connection)
            disclosure = await repository.get_disclosure(disclosure_id)
            if disclosure is None or disclosure.task_id != task_id:
                raise DocumentRefusal("disclosure_not_started")
            if disclosure.status != "STARTED":
                raise DocumentRefusal("disclosure_already_recorded")
            if (failure is None) == (result is None):
                raise DocumentRefusal("result_malformed")
            if failure is not None:
                if failure not in PROVIDER_FAILURE_CODES:
                    raise DocumentRefusal("failure_invalid")
                await self._fail(connection, task_id, disclosure, failure)
                return await self._view(connection, task_id)
            grounded = await self._ground(connection, disclosure, result)
            if isinstance(grounded, str):
                await self._fail(connection, task_id, disclosure, grounded)
                return await self._view(connection, task_id)
            parsed, evidence = grounded
            findings = (
                [
                    {"kind": finding.kind, "text": finding.text, "evidence": stored}
                    for finding, stored in zip(parsed.findings, evidence, strict=True)
                ]
                if isinstance(parsed, Comparison)
                else []
            )
            try:
                async with connection.begin_nested():
                    answer = await repository.insert_answer(
                        answer_id=uuid.uuid4(),
                        task_id=task_id,
                        disclosure_id=disclosure.id,
                        recipient=disclosure.recipient,
                        model=disclosure.model,
                        kind=parsed.kind,
                        summary=parsed.summary if isinstance(parsed, Comparison) else None,
                        reason=parsed.reason if isinstance(parsed, CannotCompare) else None,
                        findings=findings,
                    )
            except SQLAlchemyError:
                logger.error("a document comparison could not be stored")
                await self._fail(connection, task_id, disclosure, ERROR_INVALID_OUTPUT)
                return await self._view(connection, task_id)
            await repository.finish_disclosure(disclosure_id=disclosure.id, status="SUCCEEDED", error_code=None)
            await self._event(
                connection,
                task_id,
                TaskEventType.TASK_DOCUMENT_COMPARISON_RECORDED,
                {"disclosure_id": str(disclosure.id), "answer_id": str(answer.id), "kind": answer.kind, "finding_count": len(findings)},
            )
            await self._move_task(connection, task_id, TaskStatus.READY)
            return await self._view(connection, task_id)

    async def _ground(
        self, connection: AsyncConnection, disclosure: DocumentDisclosureRecord, payload: Any
    ) -> "str | tuple[CompareResult, list[list[dict[str, str]]]]":
        try:
            parsed = parse_compare_result(payload)
        except DocumentRefusal:
            return ERROR_INVALID_OUTPUT
        grant = await DocumentRepository(connection).get_grant(disclosure.grant_id)
        if grant is None:
            return ERROR_DOCUMENT_GONE
        try:
            projection: Projection = build_projection(grant.scope, await self._scope_texts(connection, grant))
        except DocumentRefusal:
            return ERROR_DOCUMENT_GONE
        if projection.digest != disclosure.projection_digest:
            return ERROR_NOT_GROUNDED
        try:
            evidence = verify_grounding(projection, parsed)
        except NotGroundedError:
            return ERROR_NOT_GROUNDED
        return parsed, evidence

    async def _fail(self, connection: AsyncConnection, task_id: uuid.UUID, disclosure: DocumentDisclosureRecord, code: str) -> None:
        await DocumentRepository(connection).finish_disclosure(disclosure_id=disclosure.id, status="FAILED", error_code=code)
        await self._event(
            connection, task_id, TaskEventType.TASK_DOCUMENT_DISCLOSURE_FAILED, {"disclosure_id": str(disclosure.id), "error_code": code}
        )
        await self._move_task(connection, task_id, TaskStatus.READY)

    # ---- recovery and retention ------------------------------------------------------------------

    async def recover_started(self) -> int:
        """Startup: a disclosure still STARTED belongs to a dead runtime. Its outcome is unknown, forever."""
        return await self._mark_unknown(older_than_seconds=None)

    async def _expire_stale_claims(self) -> None:
        await self._mark_unknown(older_than_seconds=STALE_CLAIM_SECONDS)

    async def _mark_unknown(self, *, older_than_seconds: int | None) -> int:
        async with self._engine.connect() as connection:
            started = await DocumentRepository(connection).list_started(older_than_seconds=older_than_seconds)
        marked = 0
        for disclosure in started:
            async with self._engine.begin() as connection:
                task = await TaskRepository(connection).lock_task(disclosure.task_id)
                finished = await DocumentRepository(connection).finish_disclosure(
                    disclosure_id=disclosure.id, status="OUTCOME_UNKNOWN", error_code=ERROR_RUNTIME_RESTART
                )
                if finished is None or task is None:
                    continue
                marked += 1
                await self._event(
                    connection,
                    task.id,
                    TaskEventType.TASK_DOCUMENT_DISCLOSURE_OUTCOME_UNKNOWN,
                    {"disclosure_id": str(disclosure.id), "error_code": ERROR_RUNTIME_RESTART},
                )
                await self._move_task(connection, task.id, TaskStatus.READY)
        if marked:
            logger.warning("%d document disclosure(s) were in flight when the runtime stopped; outcome unknown, not repeated.", marked)
        return marked

    async def sweep_expired(self) -> int:
        async with self._engine.begin() as connection:
            return await DocumentRepository(connection).purge_expired(retention=timedelta(seconds=RETENTION_SECONDS))

    # ---- helpers -------------------------------------------------------------------------------

    @staticmethod
    async def _require_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DOCUMENT_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DOCUMENT_TASK_TYPE)
        return task

    @staticmethod
    async def _lock(connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != DOCUMENT_TASK_TYPE:
            raise TaskKindMismatchError(task_id, DOCUMENT_TASK_TYPE)
        if require_accepting and not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task_id, task.status)
        return task

    @staticmethod
    async def _event(connection: AsyncConnection, task_id: uuid.UUID, event_type: TaskEventType, payload: dict[str, Any]) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        assert current is not None
        advanced = await tasks.advance_task(task_id=current.id, expected_revision=current.revision)
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)
        await tasks.append_event(task=advanced, event_type=event_type, payload=payload)

    @staticmethod
    async def _move_task(connection: AsyncConnection, task_id: uuid.UUID, status: TaskStatus) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        if current is None or not accepts_actions(current.status) or current.status is status:
            return
        moved = await tasks.advance_task(task_id=current.id, expected_revision=current.revision, status=status)
        if moved is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> DocumentTaskView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        files = await FileRepository(connection).refs_for_task(task_id)
        repository = DocumentRepository(connection)
        records = await repository.documents_for_task(task_id)
        documents: list[DocumentView] = []
        for record in records:
            text = None if record.purged else await repository.text_for(record.id, task_id=task_id)
            documents.append(
                DocumentView(
                    document_id=record.id,
                    file_id=record.file_ref_id,
                    format=record.format,
                    page_count=record.page_count,
                    text_chars=record.text_chars,
                    truncated=record.truncated,
                    flags=record.flags,
                    preview=None if text is None else preview(text),
                    expires_at=record.expires_at,
                    purged=text is None,
                )
            )
        grant = await repository.latest_grant_for_task(task_id)
        disclosure = await repository.disclosure_for_task(task_id)
        answer = await repository.answer_for_task(task_id)
        card = None if grant is None else await self._card(connection, grant)
        expired_active = grant is not None and grant.status is GrantStatus.ACTIVE and await repository.grant_is_expired(grant.id)
        return DocumentTaskView(
            task_id=task.id,
            task_status=task.status.value,
            task_revision=task.revision,
            objective=str(task.request.get("objective", "")),
            phase=_phase(grant, disclosure, expired_active=bool(expired_active)),
            files=tuple(
                FileView(
                    file_id=ref.id,
                    source=ref.source,
                    display_name=ref.display_name,
                    relative_path=ref.relative_path,
                    root_id=ref.root_id,
                    format=ref.format,
                    size_bytes=ref.size_bytes,
                    added_at=ref.created_at,
                )
                for ref in files
            ),
            documents=tuple(documents),
            card=card,
            disclosure=disclosure,
            answer=answer,
        )

    async def _card(self, connection: AsyncConnection, grant: DocumentGrantRecord) -> DisclosureCardView:
        scope = grant.scope
        projection: Projection | None = None
        if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
            try:
                projection = build_projection(scope, await self._scope_texts(connection, grant))
            except DocumentRefusal:
                projection = None
        return DisclosureCardView(
            grant_id=grant.id,
            grant_revision=grant.revision,
            grant_status=grant.status.value,
            expires_at=grant.expires_at,
            recipient=scope.recipient,
            model=scope.model,
            purpose=scope.purpose,
            documents=tuple(
                CardDocument(
                    doc_ref=item.doc_ref,
                    document_id=item.document_id,
                    label=item.label,
                    excerpt=None if projection is None else projection.excerpt(item.doc_ref),
                )
                for item in scope.documents
            ),
            max_excerpt_bytes=scope.max_excerpt_bytes,
            text_bytes=None if projection is None else projection.text_bytes,
            redaction_count=None if projection is None else projection.redaction_count,
            truncated=None if projection is None else projection.truncated,
            redaction_policy=scope.redaction_policy,
        )


def _root_view(root: FileRootRecord) -> RootView:
    return RootView(
        root_id=root.id,
        label=root.label,
        can_read=root.can_read,
        can_create=root.can_create,
        can_modify=root.can_modify,
        revision=root.revision,
        created_at=root.created_at,
    )


def _phase(grant: DocumentGrantRecord | None, disclosure: DocumentDisclosureRecord | None, *, expired_active: bool) -> str:
    if disclosure is not None:
        return {"STARTED": "comparing", "SUCCEEDED": "compared", "FAILED": "failed", "OUTCOME_UNKNOWN": "outcome_unknown"}[
            disclosure.status
        ]
    if grant is None:
        return "local"
    if grant.status is GrantStatus.PENDING:
        return "awaiting_approval"
    if grant.status is GrantStatus.ACTIVE:
        return "expired" if expired_active else "approved"
    return "local"


_LATEST_DOCUMENT_TASK = (
    select(tasks_table.c.id)
    .where(
        tasks_table.c.request["type"].astext == DOCUMENT_TASK_TYPE,
        exists().where(file_refs_table.c.task_id == tasks_table.c.id),
    )
    .order_by(tasks_table.c.created_at.desc(), tasks_table.c.id)
    .limit(1)
)
