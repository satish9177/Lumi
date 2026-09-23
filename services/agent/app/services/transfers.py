"""Milestone 10 S2: the runtime's controlled-download and file-placement service.

Order is the safety property, as for every other effect in Lumi:

```text
create   validate URL (public policy), root (ACTIVE + CREATE), name (document types only), destination absent
         -> task + PENDING `file_transfer` grant + transfer manifest row (the card). Nothing is fetched.
confirm  the trusted click -> grant ACTIVE for ten minutes
download bind the worker -> ONE transaction: action `transfer_download` + effect keys + effect-lock check +
         step authorization consumed + attempt -> COMMIT -> dispatch row COMMIT -> the worker fetches into
         the quarantine -> the runtime re-verifies the manifest (length + SHA-256) itself -> finish
place    ONE transaction: action `transfer_place` + effect key + lock check + step authorization + attempt
         -> COMMIT -> re-verify + sniff the quarantined bytes -> atomic no-overwrite rename relative to a
         held directory handle -> verify by handle -> finish
reconcile  read-only, from local evidence only (quarantine markers, file indexes). Never refetches.
```

A lost answer is `OUTCOME_UNKNOWN`. Nothing is ever retried: a download is never repeated because a
destination file is missing, and a placement is never repeated because it "looks like" it did not happen.
"""

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db.tables import file_transfers as file_transfers_table
from app.db.tables import tasks as tasks_table
from app.documents.sniff import dangerous_kind, sniff
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.effects import EffectLockedError, download_keys, placement_key
from app.domain.errors import TaskConcurrencyError, TaskKindMismatchError, TaskNotAcceptingActionsError, TaskNotFoundError
from app.domain.public_url import PublicUrlPolicy, UrlPolicyError
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.domain.transfers import (
    DEFAULT_MAX_BYTES,
    TOOL_DOWNLOAD,
    TOOL_PLACE,
    TRANSFER_TASK_TYPE,
    TransferRefusal,
    TransferScope,
    origin_of,
    source_digest,
    validate_destination_name,
    validate_intent,
)
from app.files import quarantine
from app.files.broker import FileBrokerRefusal, verify_root
from app.files.handles import FileIdentity, identity_of, is_reparse_point, is_within
from app.files.place import PlacementRefusal, place
from app.repositories.actions import ActionRecord, ActionRepository
from app.repositories.files import FileRepository, FileRootRecord
from app.repositories.tasks import TaskRecord, TaskRepository
from app.repositories.transfers import TransferGrantRecord, TransferRecord, TransferRepository
from app.services.actions import ActionService, ActionView
from app.services.browser_execution import BrowserExecutionService

logger = logging.getLogger("lumi.transfers")

#: Each step is one action per transfer task, found by these keys (never twice).
_DOWNLOAD_KEY = "transfer-download"
_PLACE_KEY = "transfer-place"

STEP_TTL = timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class TransferView:
    task_id: uuid.UUID
    task_status: str
    task_revision: int
    phase: str
    transfer: TransferRecord
    grant: TransferGrantRecord | None
    download_status: str | None
    place_status: str | None
    root_label: str


class _TransferAuthorizer:
    """Mints and consumes one step authorization from a `file_transfer` grant, for `start_scoped_attempt`."""

    def __init__(self, grant: TransferGrantRecord, runtime_generation: uuid.UUID) -> None:
        self._grant = grant
        self._runtime_generation = runtime_generation

    def authorization_payload(self) -> dict[str, Any]:
        return {
            "grant_id": str(self._grant.id),
            "grant_revision": self._grant.revision,
            "scope_digest": self._grant.scope_digest,
            "policy_version": self._grant.policy_version,
            "authorization": "task_grant",
        }

    async def mint(self, connection: AsyncConnection, *, action: ActionRecord) -> uuid.UUID:
        return await TransferRepository(connection).insert_step_authorization(
            grant=self._grant,
            action_id=action.id,
            action_revision=action.revision,
            proposal_digest=action.proposal_digest,
            runtime_generation=self._runtime_generation,
            ttl=STEP_TTL,
        )

    async def consume(
        self, connection: AsyncConnection, *, authorization_id: uuid.UUID, action_revision: int, proposal_digest: str
    ) -> bool:
        return await TransferRepository(connection).consume_step_authorization(
            authorization_id=authorization_id,
            action_revision=action_revision,
            proposal_digest=proposal_digest,
            runtime_generation=self._runtime_generation,
        )


class TransferService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        browser: BrowserExecutionService,
        policy: PublicUrlPolicy,
        quarantine_root: str,
        runtime_generation: uuid.UUID,
        grant_ttl_seconds: int,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._browser = browser
        self._policy = policy
        self._quarantine_root = quarantine_root
        self._runtime_generation = runtime_generation
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        self._running: set[uuid.UUID] = set()

    @property
    def quarantine_root(self) -> str:
        return self._quarantine_root

    # ---- the card --------------------------------------------------------------------------------

    async def create(
        self,
        *,
        url: object,
        root_id: uuid.UUID,
        file_name: object,
        intent: object,
        max_bytes: int = DEFAULT_MAX_BYTES,
        link: Callable[[AsyncConnection, uuid.UUID], Awaitable[None]] | None = None,
    ) -> TransferView:
        """Open the card. `link` (Milestone 10 S4) records workflow lineage in the SAME transaction."""
        if not self._policy.configured:
            raise TransferRefusal("downloads_not_configured")
        if not isinstance(url, str):
            raise TransferRefusal("url_invalid")
        try:
            checked = self._policy.check(url)
        except UrlPolicyError as error:
            raise TransferRefusal(f"url_{error.code}"[:40]) from None
        if checked.url != url:
            raise TransferRefusal("url_not_canonical")
        name, kind = validate_destination_name(file_name)
        purpose = validate_intent(intent)
        if not 1 <= max_bytes <= DEFAULT_MAX_BYTES:
            raise TransferRefusal("max_bytes_invalid")
        root = await self._creatable_root(root_id)
        await asyncio.to_thread(self._require_destination_absent, root, name)
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(), status=TaskStatus.WAITING_APPROVAL, request={"type": TRANSFER_TASK_TYPE}
            )
            await tasks.append_event(task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value})
            scope = TransferScope(
                task_id=task.id,
                transfer_id=uuid.uuid4(),
                source_url=checked.url,
                source_origin=origin_of(checked.url),
                intent=purpose,
                dest_root_id=root.id,
                dest_root_label=root.label,
                dest_name=name,
                expected_kind=kind,
                max_bytes=max_bytes,
            )
            repository = TransferRepository(connection)
            grant = await repository.insert_grant(grant_id=uuid.uuid4(), scope=scope)
            await repository.insert_transfer(
                scope=scope, grant_id=grant.id, source_digest=source_digest(scope.source_url), source_host=checked.host
            )
            await self._event(
                connection,
                task.id,
                TaskEventType.TASK_TRANSFER_REQUESTED,
                {"transfer_id": str(scope.transfer_id), "grant_id": str(grant.id), "scope_digest": grant.scope_digest},
            )
            if link is not None:
                await link(connection, task.id)
            return await self._view(connection, task.id)

    async def _creatable_root(self, root_id: uuid.UUID) -> FileRootRecord:
        async with self._engine.connect() as connection:
            root = await FileRepository(connection).get_root(root_id)
        if root is None:
            raise TransferRefusal("root_not_found")
        if not root.active:
            raise TransferRefusal("root_revoked")
        if not root.can_create:
            raise TransferRefusal("permission_missing")
        return root

    @staticmethod
    def _require_destination_absent(root: FileRootRecord, name: str) -> str:
        try:
            canonical = verify_root(root.canonical_path, volume=root.volume_serial, index=root.dir_index)
        except FileBrokerRefusal as refusal:
            raise TransferRefusal(refusal.code) from None
        target = os.path.join(canonical, name)
        try:
            os.lstat(target)
        except FileNotFoundError:
            return canonical
        except OSError:
            raise TransferRefusal("destination_unavailable") from None
        raise TransferRefusal("destination_exists")

    # ---- the trusted click -------------------------------------------------------------------------

    async def confirm(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int) -> TransferView:
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id)
            repository = TransferRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise TransferRefusal("grant_not_found")
            if grant.status is not GrantStatus.PENDING:
                raise TransferRefusal("grant_not_pending")
            if grant.revision != expected_revision:
                raise TransferRefusal("grant_changed")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id, expected_revision=expected_revision, scope_digest=grant.scope_digest, ttl=self._grant_ttl
            )
            if confirmed is None:
                raise TransferRefusal("grant_changed")
            await self._event(
                connection, task_id, TaskEventType.TASK_TRANSFER_GRANTED, {"grant_id": str(grant.id), "grant_revision": confirmed.revision}
            )
            await self._move(connection, task_id, TaskStatus.READY)
            return await self._view(connection, task_id)

    async def revoke(self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int | None) -> TransferView:
        """Decline or stop. Revokes future steps; never undoes a fetch or a placement that already happened."""
        async with self._engine.begin() as connection:
            await self._lock(connection, task_id, require_accepting=False)
            repository = TransferRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise TransferRefusal("grant_not_found")
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                closed = await repository.close_grant(grant_id=grant.id, status=GrantStatus.REVOKED, expected_revision=expected_revision)
                if closed is None:
                    raise TransferRefusal("grant_changed")
                transfer = await repository.for_task(task_id)
                if transfer is not None and transfer.status in ("PENDING", "QUARANTINED"):
                    await repository.update(transfer.id, status="CANCELLED")
                await self._event(connection, task_id, TaskEventType.TASK_TRANSFER_REVOKED, {"grant_id": str(grant.id)})
            return await self._view(connection, task_id)

    @staticmethod
    async def _step_actions(connection: AsyncConnection, transfer: TransferRecord) -> tuple[ActionRecord | None, ActionRecord | None]:
        """The download and placement actions, found by (task, idempotency key) -- not only through the
        transfer row. The attempt commits before the row records its id, so a crash between the two commits
        must still find the action, or it could never be reconciled and its effect keys would stay locked
        (S2 review finding 1)."""
        actions = ActionRepository(connection)
        download = await actions.get_action_by_idempotency_key(task_id=transfer.task_id, idempotency_key=_DOWNLOAD_KEY)
        placement = await actions.get_action_by_idempotency_key(task_id=transfer.task_id, idempotency_key=_PLACE_KEY)
        return download, placement

    # ---- step 1: the download -----------------------------------------------------------------------

    async def download(self, task_id: uuid.UUID) -> TransferView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            repository = TransferRepository(connection)
            transfer = await repository.for_task(task_id)
            if transfer is None:
                raise TransferRefusal("transfer_not_found")
            if transfer.download_action_id is not None or (await self._step_actions(connection, transfer))[0] is not None:
                return await self._view(connection, task_id)  # a repeated request never fetches twice
            grant = await repository.get_grant(transfer.grant_id)
        if grant is None or grant.status is not GrantStatus.ACTIVE:
            raise TransferRefusal("grant_not_active")
        scope = grant.scope
        try:
            if self._policy.check(scope.source_url).url != scope.source_url:
                raise TransferRefusal("url_not_canonical")
        except UrlPolicyError:
            raise TransferRefusal("url_no_longer_allowed") from None
        root = await self._creatable_root(scope.dest_root_id)
        await asyncio.to_thread(self._require_destination_absent, root, scope.dest_name)
        if task_id in self._running:
            raise TransferRefusal("wrong_phase")
        self._running.add(task_id)
        try:
            client, worker_generation = await self._browser.open_download_worker()
            try:
                proposal = {
                    "transfer_id": str(scope.transfer_id),
                    "source_url": scope.source_url,
                    "source_digest": source_digest(scope.source_url),
                    "dest_root_id": str(scope.dest_root_id),
                    "dest_name": scope.dest_name,
                    "max_bytes": scope.max_bytes,
                }
                try:
                    view, created = await self._actions.start_scoped_attempt(
                        task_id,
                        tool_name=TOOL_DOWNLOAD,
                        idempotency_key=_DOWNLOAD_KEY,
                        risk_tier=RiskTier.R2,
                        proposal=proposal,
                        authorizer=_TransferAuthorizer(grant, self._runtime_generation),
                        effect_keys=download_keys(
                            source_url=scope.source_url, root_id=str(scope.dest_root_id), file_name=scope.dest_name
                        ),
                    )
                except EffectLockedError as locked:
                    raise TransferRefusal("effect_locked") from locked
                if not created:
                    async with self._engine.connect() as connection:
                        return await self._view(connection, task_id)
                attempt = next(item for item in view.attempts if item.finished_at is None)
                async with self._engine.begin() as connection:
                    await TransferRepository(connection).update(transfer.id, status="DOWNLOADING", download_action_id=view.action.id)
                # ---- committed. The request may now leave the machine. ----
                outcome = await self._browser.run_download(
                    client,
                    worker_generation,
                    action_id=view.action.id,
                    attempt_id=attempt.id,
                    url=scope.source_url,
                    transfer_id=scope.transfer_id,
                    max_bytes=scope.max_bytes,
                )
            finally:
                await client.aclose()
            await self._finish_download(task_id, transfer, scope, view.action.id, outcome.outcome, outcome.error_code, outcome.observation)
        finally:
            self._running.discard(task_id)
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _finish_download(
        self,
        task_id: uuid.UUID,
        transfer: TransferRecord,
        scope: TransferScope,
        action_id: uuid.UUID,
        outcome: AttemptOutcome,
        error_code: str | None,
        observation: dict[str, Any],
    ) -> None:
        manifest = None
        wrong_kind = False
        if outcome is AttemptOutcome.SUCCEEDED:
            # The worker said it finished. The runtime believes the quarantine, not the answer: the manifest
            # must verify against the payload bytes, match what the worker reported, and be a document.
            manifest = await asyncio.to_thread(quarantine.read_complete, self._quarantine_root, scope.transfer_id)
            if manifest is None or manifest.sha256 != observation.get("sha256") or manifest.length != observation.get("length"):
                # The request happened, but what it produced cannot be verified: unknown, never retried.
                outcome, error_code, manifest = AttemptOutcome.OUTCOME_UNKNOWN, "download_not_verified", None
            elif manifest.kind != scope.expected_kind:
                # A KNOWN, completed download of the wrong kind (a DOCX served for `resume.pdf`). The download
                # effect is resolved; the transfer is failed and can never be placed under the approved name.
                wrong_kind = True
        payload_identity = None
        if manifest is not None:
            payload_identity = await asyncio.to_thread(self._payload_identity, scope.transfer_id)
            if payload_identity is None:
                outcome, error_code, manifest = AttemptOutcome.OUTCOME_UNKNOWN, "download_not_verified", None

        async def record(connection: AsyncConnection, _: Any) -> None:
            repository = TransferRepository(connection)
            if outcome is AttemptOutcome.SUCCEEDED and manifest is not None and payload_identity is not None:
                await repository.update(
                    transfer.id,
                    status="FAILED" if wrong_kind else "QUARANTINED",
                    length=manifest.length,
                    sha256=manifest.sha256,
                    kind=manifest.kind,
                    content_type=manifest.content_type[:100],
                    quarantine_volume=payload_identity.volume,
                    quarantine_index=payload_identity.index,
                    quarantined_at=datetime.now().astimezone(),
                    error_code="type_mismatch" if wrong_kind else None,
                )
            else:
                await repository.update(
                    transfer.id,
                    status="FAILED" if outcome is AttemptOutcome.FAILED else "OUTCOME_UNKNOWN",
                    error_code=(error_code or "download_failed")[:40],
                )

        await self._actions.finish_attempt(
            action_id,
            outcome=outcome,
            result={"transfer_id": str(transfer.id), "length": manifest.length if manifest else None},
            error_code=error_code,
            record=record,
        )

    def _payload_identity(self, transfer_id: uuid.UUID) -> FileIdentity | None:
        path = quarantine.transfer_directory(self._quarantine_root, transfer_id) / quarantine.PAYLOAD
        try:
            facts = os.lstat(path)
        except OSError:
            return None
        if is_reparse_point(facts) or facts.st_nlink != 1:
            return None
        return identity_of(facts)

    # ---- step 2: the placement ------------------------------------------------------------------------

    async def place(self, task_id: uuid.UUID) -> TransferView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            repository = TransferRepository(connection)
            transfer = await repository.for_task(task_id)
            if transfer is None:
                raise TransferRefusal("transfer_not_found")
            if transfer.place_action_id is not None or (await self._step_actions(connection, transfer))[1] is not None:
                return await self._view(connection, task_id)
            if transfer.status != "QUARANTINED" or transfer.quarantine_index is None or transfer.sha256 is None:
                raise TransferRefusal("wrong_phase")
            grant = await repository.get_grant(transfer.grant_id)
        if grant is None or grant.status is not GrantStatus.ACTIVE:
            raise TransferRefusal("grant_not_active")
        scope = grant.scope
        root = await self._creatable_root(scope.dest_root_id)
        # Everything that can refuse harmlessly is checked before the step authorization is spent.
        payload_path = str(quarantine.transfer_directory(self._quarantine_root, scope.transfer_id) / quarantine.PAYLOAD)
        await asyncio.to_thread(self._verify_quarantined, transfer)
        canonical = await asyncio.to_thread(self._require_destination_absent, root, scope.dest_name)
        try:
            view, created = await self._actions.start_scoped_attempt(
                task_id,
                tool_name=TOOL_PLACE,
                idempotency_key=_PLACE_KEY,
                risk_tier=RiskTier.R2,
                proposal={
                    "transfer_id": str(scope.transfer_id),
                    "sha256": transfer.sha256,
                    "dest_root_id": str(scope.dest_root_id),
                    "dest_name": scope.dest_name,
                },
                authorizer=_TransferAuthorizer(grant, self._runtime_generation),
                effect_keys=(placement_key(root_id=str(scope.dest_root_id), file_name=scope.dest_name),),
            )
        except EffectLockedError as locked:
            raise TransferRefusal("effect_locked") from locked
        if not created:
            async with self._engine.connect() as connection:
                return await self._view(connection, task_id)
        async with self._engine.begin() as connection:
            await TransferRepository(connection).update(transfer.id, status="PLACING", place_action_id=view.action.id)
        # ---- committed. From the rename call on, the file may have moved. ----
        outcome = AttemptOutcome.OUTCOME_UNKNOWN
        error_code: str | None = "placement_unverified"
        placed_identity: FileIdentity | None = None
        try:
            await asyncio.to_thread(self._verify_quarantined, transfer)
            placed = await asyncio.to_thread(
                place,
                payload_path=payload_path,
                quarantine_directory=str(quarantine.transfer_directory(self._quarantine_root, scope.transfer_id)),
                payload_identity=FileIdentity(
                    volume=transfer.quarantine_volume or 0, index=transfer.quarantine_index, size=transfer.length or 0, mtime_ns=0
                ),
                root_canonical=canonical,
                root_volume=root.volume_serial,
                root_index=root.dir_index,
                file_name=scope.dest_name,
                verify=lambda data: self._verify_bytes(transfer, data),
            )
            placed_identity = placed.identity
            outcome, error_code = AttemptOutcome.SUCCEEDED, None
        except TransferRefusal as refusal:
            outcome, error_code = AttemptOutcome.FAILED, refusal.code
        except PlacementRefusal as refusal:
            outcome = AttemptOutcome.OUTCOME_UNKNOWN if refusal.effect_possible else AttemptOutcome.FAILED
            error_code = refusal.code
        except Exception:  # noqa: BLE001 - a surprise after the commit is never a claimed failure.
            logger.exception("placement raised unexpectedly")
            outcome, error_code = AttemptOutcome.OUTCOME_UNKNOWN, "placement_error"

        async def record(connection: AsyncConnection, _: Any) -> None:
            repository = TransferRepository(connection)
            if outcome is AttemptOutcome.SUCCEEDED and placed_identity is not None:
                await repository.update(
                    transfer.id,
                    status="PLACED",
                    placed_volume=placed_identity.volume,
                    placed_index=placed_identity.index,
                    placed_at=datetime.now().astimezone(),
                    error_code=None,
                )
                await repository.close_grant(grant_id=grant.id, status=GrantStatus.COMPLETED)
            else:
                # A definite failure is final for this transfer: its one placement step is spent. It is FAILED
                # (so its quarantine is swept), never a QUARANTINED row nothing can move on (review finding 6).
                await repository.update(
                    transfer.id,
                    status="FAILED" if outcome is AttemptOutcome.FAILED else "OUTCOME_UNKNOWN",
                    error_code=(error_code or "placement_failed")[:40],
                )

        await self._actions.finish_attempt(view.action.id, outcome=outcome, result={"transfer_id": str(transfer.id)}, error_code=error_code, record=record)
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    @staticmethod
    def _verify_bytes(transfer: TransferRecord, data: bytes) -> None:
        """The bytes read through the placement's own share-read-only handle: exactly the verified download,
        and still an allowed document by signature (S2 review finding 4)."""
        if len(data) != transfer.length or hashlib.sha256(data).hexdigest() != transfer.sha256:
            raise TransferRefusal("quarantine_changed")
        kind = sniff(data)
        if dangerous_kind(kind) or kind != transfer.kind:
            raise TransferRefusal("download_type_refused")

    def _verify_quarantined(self, transfer: TransferRecord) -> None:
        """The payload is still exactly the verified download, and its bytes are still an allowed document."""
        manifest = quarantine.read_complete(self._quarantine_root, transfer.id)
        if manifest is None or manifest.sha256 != transfer.sha256 or manifest.length != transfer.length:
            raise TransferRefusal("quarantine_changed")
        identity = self._payload_identity(transfer.id)
        if identity is None or identity.index != transfer.quarantine_index:
            raise TransferRefusal("quarantine_changed")
        path = quarantine.transfer_directory(self._quarantine_root, transfer.id) / quarantine.PAYLOAD
        with open(path, "rb") as handle:
            data = handle.read(manifest.length + 1)
        kind = sniff(data)
        if dangerous_kind(kind) or kind != transfer.kind:
            raise TransferRefusal("download_type_refused")
        if not quarantine.has_zone_identifier(str(path)):
            raise TransferRefusal("provenance_missing")

    # ---- reconciliation (read-only, local evidence) ------------------------------------------------------

    async def reconcile(self, task_id: uuid.UUID) -> TransferView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            transfer = await TransferRepository(connection).for_task(task_id)
            if transfer is None:
                raise TransferRefusal("transfer_not_found")
            pending = [
                action
                for action in await self._step_actions(connection, transfer)
                if action is not None and action.status in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING)
            ]
        if not pending:
            raise TransferRefusal("nothing_to_reconcile")
        action = pending[0]
        view: ActionView = await self._actions.get_action(action.id)
        if view.action.status is ActionStatus.OUTCOME_UNKNOWN:
            view = await self._actions.begin_reconciliation(action.id, expected_revision=view.action.revision)
        revision = view.action.revision
        if action.tool_name == TOOL_DOWNLOAD:
            result, evidence, manifest = await asyncio.to_thread(self._download_evidence, transfer)
        else:
            async with self._engine.connect() as connection:
                root = await FileRepository(connection).get_root(transfer.dest_root_id)
            result, evidence = await asyncio.to_thread(self._placement_evidence, transfer, root)
            manifest = None
        await self._actions.finish_reconciliation(action.id, result=result, evidence=evidence, expected_revision=revision)
        async with self._engine.begin() as connection:
            repository = TransferRepository(connection)
            # Backfill the row's action id if a crash came between the attempt's commit and the row's update.
            if action.tool_name == TOOL_DOWNLOAD and transfer.download_action_id is None:
                await repository.update(transfer.id, download_action_id=action.id)
            if action.tool_name == TOOL_PLACE and transfer.place_action_id is None:
                await repository.update(transfer.id, place_action_id=action.id)
            if action.tool_name == TOOL_DOWNLOAD:
                if result is AttemptOutcome.SUCCEEDED and manifest is not None:
                    identity = self._payload_identity(transfer.id)
                    grant = await repository.get_grant(transfer.grant_id)
                    expected = grant.scope.expected_kind if grant is not None else None
                    await repository.update(
                        transfer.id,
                        # A verified download of the wrong kind is known and resolved, and never placeable.
                        status="FAILED" if manifest.kind != expected else ("QUARANTINED" if identity is not None else "OUTCOME_UNKNOWN"),
                        length=manifest.length,
                        sha256=manifest.sha256,
                        kind=manifest.kind,
                        content_type=manifest.content_type[:100],
                        quarantine_volume=identity.volume if identity else None,
                        quarantine_index=identity.index if identity else None,
                        quarantined_at=datetime.now().astimezone(),
                        error_code="type_mismatch" if manifest.kind != expected else None,
                    )
                elif result is AttemptOutcome.FAILED:
                    await repository.update(transfer.id, status="FAILED", error_code="no_request_was_made")
            else:
                if result is AttemptOutcome.SUCCEEDED:
                    await repository.update(
                        transfer.id,
                        status="PLACED",
                        placed_volume=transfer.quarantine_volume,
                        placed_index=transfer.quarantine_index,  # a same-volume rename keeps the index
                        placed_at=datetime.now().astimezone(),
                        error_code=None,
                    )
                    await repository.close_grant(grant_id=transfer.grant_id, status=GrantStatus.COMPLETED)
                elif result is AttemptOutcome.FAILED:
                    await repository.update(transfer.id, status="FAILED", error_code="not_placed")
            return await self._view(connection, task_id)

    def _download_evidence(self, transfer: TransferRecord) -> tuple[AttemptOutcome, dict[str, Any], quarantine.CompleteManifest | None]:
        if not quarantine.started(self._quarantine_root, transfer.id):
            # No marker is NOT yet proof: a dispatch the runtime gave up on may still reach the worker. The
            # tombstone makes the worker's own exclusive `begin` fail for ever; only then is absence a fact.
            claimed = quarantine.claim_absence(self._quarantine_root, transfer.id)
            if claimed is True:
                return AttemptOutcome.FAILED, {"source": "quarantine", "started": False, "tombstone": True}, None
            if claimed is None:
                return AttemptOutcome.OUTCOME_UNKNOWN, {"source": "quarantine", "started": False, "known": False}, None
            # The worker got there first: fall through to the markers it wrote.
        manifest = quarantine.read_complete(self._quarantine_root, transfer.id)
        if manifest is not None and manifest.kind in ("pdf", "docx", "txt"):
            return AttemptOutcome.SUCCEEDED, {"source": "quarantine", "started": True, "complete": True}, manifest
        return AttemptOutcome.OUTCOME_UNKNOWN, {"source": "quarantine", "started": True, "complete": False}, None

    def _placement_evidence(self, transfer: TransferRecord, root: FileRootRecord | None) -> tuple[AttemptOutcome, dict[str, Any]]:
        """A same-volume rename is atomic and keeps the file index, so the file is in exactly one place.
        At the destination with the quarantined index: placed. Still in quarantine with that index and the
        destination absent: not placed. Anything else (a different file at the name, neither, both): unknown."""
        if transfer.quarantine_index is None or root is None:
            return AttemptOutcome.OUTCOME_UNKNOWN, {"source": "file_index", "known": False}

        def index_at(path: str) -> int | None:
            try:
                facts = os.lstat(path)
            except FileNotFoundError:
                return -1
            except OSError:
                return None
            return None if is_reparse_point(facts) else int(facts.st_ino)

        destination = index_at(os.path.join(root.canonical_path, transfer.dest_name))
        in_quarantine = index_at(str(quarantine.transfer_directory(self._quarantine_root, transfer.id) / quarantine.PAYLOAD))
        if destination == transfer.quarantine_index and in_quarantine == -1:
            return AttemptOutcome.SUCCEEDED, {"source": "file_index", "at_destination": True}
        if in_quarantine == transfer.quarantine_index and destination == -1:
            return AttemptOutcome.FAILED, {"source": "file_index", "at_destination": False}
        return AttemptOutcome.OUTCOME_UNKNOWN, {"source": "file_index", "known": False}

    # ---- quarantine lifetime -----------------------------------------------------------------------------

    async def sweep_quarantine(self) -> int:
        """Remove the quarantine directory of placed, failed or cancelled transfers after its lifetime.
        OUTCOME_UNKNOWN transfers keep theirs: it is evidence. Only Lumi's own fixed file names are
        removed, then the (by then empty) directory; nothing else is ever deleted."""
        async with self._engine.connect() as connection:
            old = await TransferRepository(connection).cleanable(older_than=timedelta(seconds=quarantine.QUARANTINE_LIFETIME_SECONDS))
        cleaned = 0
        for transfer in old:
            await asyncio.to_thread(self._remove_quarantine, transfer.id)
            async with self._engine.begin() as connection:
                await TransferRepository(connection).update(transfer.id, cleaned_at=datetime.now().astimezone())
            cleaned += 1
        return cleaned

    def _remove_quarantine(self, transfer_id: uuid.UUID) -> None:
        directory = quarantine.transfer_directory(self._quarantine_root, transfer_id)
        try:
            facts = os.lstat(directory)
        except OSError:
            return
        # Never through a junction or symlink, and never outside Lumi's own quarantine (review finding 5).
        if is_reparse_point(facts) or not is_within(os.path.realpath(directory), os.path.realpath(self._quarantine_root)):
            logger.warning("quarantine sweep skipped a directory that is not a plain quarantine directory")
            return
        if (directory / quarantine.ABSENT).is_file():
            # A reconciliation tombstone stays: it is what keeps a late dispatch from ever fetching.
            return
        for name in (quarantine.COMPLETE, quarantine.PAYLOAD, quarantine.PARTIAL, quarantine.STARTED):
            try:
                os.unlink(directory / name)
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass

    # ---- reading -------------------------------------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> TransferView:
        async with self._engine.connect() as connection:
            await self._require_task(connection, task_id)
            return await self._view(connection, task_id)

    async def latest(self) -> TransferView | None:
        async with self._engine.connect() as connection:
            row = (await connection.execute(_LATEST_TRANSFER_TASK)).scalar_one_or_none()
            return None if row is None else await self._view(connection, row)

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> TransferView:
        task = await TaskRepository(connection).get_task(task_id)
        assert task is not None
        repository = TransferRepository(connection)
        transfer = await repository.for_task(task_id)
        if transfer is None:
            raise TransferRefusal("transfer_not_found")
        grant = await repository.get_grant(transfer.grant_id)
        download, placement = await self._step_actions(connection, transfer)
        root = await FileRepository(connection).get_root(transfer.dest_root_id)
        return TransferView(
            task_id=task.id,
            task_status=task.status.value,
            task_revision=task.revision,
            phase=_phase(transfer, grant, download, placement),
            transfer=transfer,
            grant=grant,
            download_status=download.status.value if download else None,
            place_status=placement.status.value if placement else None,
            root_label=root.label if root is not None else "",
        )

    @staticmethod
    async def _require_task(connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != TRANSFER_TASK_TYPE:
            raise TaskKindMismatchError(task_id, TRANSFER_TASK_TYPE)
        return task

    @staticmethod
    async def _lock(connection: AsyncConnection, task_id: uuid.UUID, *, require_accepting: bool = True) -> TaskRecord:
        task = await TaskRepository(connection).lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != TRANSFER_TASK_TYPE:
            raise TaskKindMismatchError(task_id, TRANSFER_TASK_TYPE)
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
    async def _move(connection: AsyncConnection, task_id: uuid.UUID, status: TaskStatus) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task_id)
        if current is None or not accepts_actions(current.status) or current.status is status:
            return
        await tasks.advance_task(task_id=current.id, expected_revision=current.revision, status=status)


def _phase(
    transfer: TransferRecord, grant: TransferGrantRecord | None, download: ActionRecord | None, placement: ActionRecord | None
) -> str:
    if placement is not None and placement.status in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING):
        return "placement_unknown"
    if download is not None and download.status in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING):
        return "download_unknown"
    return {
        "PENDING": "awaiting_approval" if grant is not None and grant.status is GrantStatus.PENDING else (
            "approved" if grant is not None and grant.status is GrantStatus.ACTIVE else "declined"
        ),
        "DOWNLOADING": "downloading",
        "QUARANTINED": "quarantined",
        "PLACING": "placing",
        "PLACED": "placed",
        "FAILED": "failed",
        "OUTCOME_UNKNOWN": "download_unknown" if placement is None else "placement_unknown",
        "CANCELLED": "declined",
    }[transfer.status]


_LATEST_TRANSFER_TASK = (
    select(tasks_table.c.id)
    .where(
        tasks_table.c.request["type"].astext == TRANSFER_TASK_TYPE,
        exists().where(file_transfers_table.c.task_id == tasks_table.c.id),
    )
    .order_by(tasks_table.c.created_at.desc(), tasks_table.c.id)
    .limit(1)
)
