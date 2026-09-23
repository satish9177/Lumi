"""Milestone 10 S4: the cross-app preparation workflow controller.

**It is not a planner.** It holds no tool, calls no model and never decides what happens next. It creates
child tasks in fixed roles, hands each one to the service that already owns that authority (transfers,
documents, authenticated reading and form preparation), and records lineage so that an artifact produced in
one role is consumed only by the same workflow, in the role the plan names:

```text
role download   TransferService      card -> trusted click -> download -> placement           (S2, unchanged)
role documents  DocumentService      the placed file (same identity, same bytes) -> extract -> compare ->
                                     optional ONE disclosure                                 (S1, unchanged)
                WorkflowService      candidates: document_extracted | provider_derived (grounded quote)
                                     -> exact adoption approval -> workflow-scoped value
role form       AuthenticatedRead +  observation -> planning grant -> exact manifest -> frozen local draft
                FormPrepare/Draft    placing ONLY this workflow's values                   (M8, unchanged)
                                     -> STOP BEFORE SUBMIT (there is no submit, Enter or click anywhere)
```

Every separate authority keeps its own approval. In particular, the provider disclosure (a
`document_disclose` grant naming a provider and model) and the form's receiving origin (the account-reading
grant, the planning grant and the exact manifest) are different approvals on different tasks: approving one
never creates, confirms or implies the other.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.action_status import ActionStatus, RiskTier
from app.domain.authenticated import AUTHENTICATED_READ_TASK_TYPE, ACCOUNT_PRIVATE
from app.domain.errors import TaskConcurrencyError, TaskNotFoundError
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, TaskStatus
from app.domain.workflows import (
    ADOPT_TOOL,
    MAX_CANDIDATES_PER_WORKFLOW,
    WORKFLOW_TTL_SECONDS,
    AdoptionProposal,
    WorkflowRefusal,
    parse_adoption,
    validate_objective,
)
from app.repositories.actions import ActionRecord, ActionRepository
from app.repositories.authenticated import AuthenticatedRepository
from app.repositories.files import FileRepository
from app.repositories.tasks import TaskRepository
from app.repositories.transfers import TransferRecord, TransferRepository
from app.repositories.workflows import CandidateRecord, StepRecord, ValueRecord, WorkflowRecord, WorkflowRepository
from app.services.actions import ActionService, ActionView
# The ONE documents import: extracted text stays inside `DocumentService`, which returns only found fields.
from app.services.documents import DocumentRefusal, DocumentService
from app.services.transfers import TransferService

logger = logging.getLogger("lumi.workflows")

ProfileCheck = Callable[[uuid.UUID], Awaitable[None]]

_ADOPTED: dict[str, Any] = {"code": "workflow_value_adopted"}


@dataclass(frozen=True, slots=True)
class StepView:
    role: str
    task_id: uuid.UUID
    task_status: str


@dataclass(frozen=True, slots=True)
class CandidateView:
    candidate_id: uuid.UUID
    kind: str
    provenance: str
    #: The candidate's own text: the person's document text, for the trusted view only. None once purged.
    value: str | None
    preview: str
    status: str
    document_id: uuid.UUID
    document_label: str
    #: For a provider-derived candidate: the grounded quote it was lifted from, and the document reference.
    quote: str | None
    doc_ref: str | None


@dataclass(frozen=True, slots=True)
class AdoptionView:
    action_id: uuid.UUID
    revision: int
    action_status: str
    approval_status: str | None
    candidate_id: uuid.UUID
    kind: str
    provenance: str
    preview: str
    value: str | None
    document_label: str


@dataclass(frozen=True, slots=True)
class WorkflowView:
    workflow: WorkflowRecord
    live: bool
    steps: tuple[StepView, ...]
    transfer_status: str | None
    placed_name: str | None
    document_count: int
    disclosure_status: str | None
    candidates: tuple[CandidateView, ...]
    values: tuple[ValueRecord, ...]
    adoptions: tuple[AdoptionView, ...]


class WorkflowService:
    """Short transactions. Every child-task authority stays with the service that owns it."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        documents: DocumentService,
        transfers: TransferService | None,
        check_profile: ProfileCheck | None,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._documents = documents
        self._transfers = transfers
        self._check_profile = check_profile

    # ---- the workflow --------------------------------------------------------------------------

    async def create(self, *, objective: object) -> WorkflowView:
        goal = validate_objective(objective)
        async with self._engine.begin() as connection:
            workflow = await WorkflowRepository(connection).insert(
                workflow_id=uuid.uuid4(), objective=goal, ttl=timedelta(seconds=WORKFLOW_TTL_SECONDS)
            )
            return await self._view(connection, workflow.id)

    async def describe(self, workflow_id: uuid.UUID) -> WorkflowView:
        await self.sweep()
        async with self._engine.connect() as connection:
            if await WorkflowRepository(connection).get(workflow_id) is None:
                raise WorkflowRefusal("workflow_not_found")
            return await self._view(connection, workflow_id)

    async def latest(self) -> WorkflowView | None:
        await self.sweep()
        async with self._engine.connect() as connection:
            workflow = await WorkflowRepository(connection).latest()
            return None if workflow is None else await self._view(connection, workflow.id)

    async def stop(self, workflow_id: uuid.UUID, *, reason: str = "user_stopped") -> WorkflowView:
        """Stop: no new step, candidate, adoption or form use; purge the derived values; revoke open grants.

        It never undoes an effect that already happened (a download, a placement, a disclosure already sent,
        a frozen local draft) and never marks anything in flight failed.
        """
        async with self._engine.begin() as connection:
            repository = WorkflowRepository(connection)
            if await repository.get(workflow_id) is None:
                raise WorkflowRefusal("workflow_not_found")
            # Lock order everywhere: child task rows, then the workflow row (S4 review finding 3).
            steps = await repository.steps(workflow_id)
            for task_id in sorted(step.task_id for step in steps):
                await TaskRepository(connection).lock_task(task_id)
            workflow = await repository.get(workflow_id, lock=True)
            assert workflow is not None
            if workflow.status == "ACTIVE":
                await repository.stop(workflow_id, reason=reason)
                await repository.purge(workflow_id=workflow_id)
                for step in steps:
                    await self._event(connection, step.task_id, TaskEventType.TASK_WORKFLOW_STOPPED, {"workflow_id": str(workflow_id), "reason": reason})
                    if step.role == "form":
                        # S4 review finding 6: the form step's account reading ends with the workflow. The
                        # planning grant and any manifest already refuse a stopped workflow; the browser and
                        # any frozen local draft are left for the person, never submitted or discarded here.
                        accounts = AuthenticatedRepository(connection)
                        grant = await accounts.open_grant_for_task(step.task_id)
                        if grant is not None:
                            await accounts.close_grant(grant_id=grant.id, status=GrantStatus.REVOKED)
        # Pending approvals and open grants are closed through their own services, outside that transaction.
        for step in steps:
            await self._close_open_authority(step)
        async with self._engine.connect() as connection:
            return await self._view(connection, workflow_id)

    async def _close_open_authority(self, step: StepRecord) -> None:
        async with self._engine.connect() as connection:
            actions = [
                action
                for action in await ActionRepository(connection).list_actions(step.task_id, limit=500)
                if action.tool_name == ADOPT_TOOL and action.status in (ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL)
            ]
            transfer_grant = None
            document_grant = None
            if step.role == "download":
                transfer = await TransferRepository(connection).for_task(step.task_id)
                if transfer is not None:
                    found = await TransferRepository(connection).get_grant(transfer.grant_id)
                    if found is not None and found.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                        transfer_grant = found
            if step.role == "documents":
                document_grant = (await self._documents.task_summary(connection, step.task_id)).open_grant_id
        for action in actions:
            try:
                await self._actions.reject_action(action.id, reason="workflow_stopped")
            except Exception:  # noqa: BLE001 - best effort; the guard refuses a stopped workflow anyway.
                logger.info("an adoption approval could not be closed on stop")
        if transfer_grant is not None and self._transfers is not None:
            try:
                await self._transfers.revoke(step.task_id, grant_id=transfer_grant.id, expected_revision=None)
            except Exception:  # noqa: BLE001
                logger.info("a transfer grant could not be revoked on stop")
        if document_grant is not None:
            try:
                await self._documents.revoke(step.task_id, grant_id=document_grant, expected_revision=None, reason="workflow_stopped")
            except Exception:  # noqa: BLE001
                logger.info("a disclosure grant could not be revoked on stop")

    async def sweep(self) -> int:
        async with self._engine.begin() as connection:
            return await WorkflowRepository(connection).purge()

    # ---- lineage helpers -------------------------------------------------------------------------

    @staticmethod
    async def _live(connection: AsyncConnection, workflow_id: uuid.UUID, *, lock: bool = True) -> WorkflowRecord:
        repository = WorkflowRepository(connection)
        workflow = await repository.get(workflow_id, lock=lock)
        if workflow is None:
            raise WorkflowRefusal("workflow_not_found")
        if workflow.status != "ACTIVE":
            raise WorkflowRefusal("workflow_not_active")
        if not await repository.is_live(workflow_id):
            raise WorkflowRefusal("workflow_expired")
        return workflow

    def _still_live(self, workflow_id: uuid.UUID) -> Callable[[AsyncConnection, uuid.UUID], Awaitable[None]]:
        async def check(connection: AsyncConnection, _task_id: uuid.UUID) -> None:
            await self._live(connection, workflow_id)

        return check

    def _linker(self, workflow_id: uuid.UUID, role: str) -> Callable[[AsyncConnection, uuid.UUID], Awaitable[None]]:
        """Records the step in the SAME transaction that creates the child task, under the workflow lock."""

        async def link(connection: AsyncConnection, task_id: uuid.UUID) -> None:
            await self._live(connection, workflow_id)
            repository = WorkflowRepository(connection)
            if await repository.step(workflow_id, role) is not None:
                raise WorkflowRefusal("step_exists")
            await repository.insert_step(workflow_id=workflow_id, task_id=task_id, role=role)
            await repository.bump(workflow_id)
            await self._event(connection, task_id, TaskEventType.TASK_WORKFLOW_STEP_LINKED, {"workflow_id": str(workflow_id), "role": role})

        return link

    async def _precheck(self, workflow_id: uuid.UUID, role: str) -> None:
        async with self._engine.connect() as connection:
            await self._live(connection, workflow_id, lock=False)
            if await WorkflowRepository(connection).step(workflow_id, role) is not None:
                raise WorkflowRefusal("step_exists")

    async def _locked_step(self, connection: AsyncConnection, workflow_id: uuid.UUID, role: str) -> StepRecord:
        """The step, with its task row locked FIRST and then the live workflow row (the one lock order)."""
        step = await self._step(connection, workflow_id, role)
        await TaskRepository(connection).lock_task(step.task_id)
        await self._live(connection, workflow_id)
        return step

    @staticmethod
    async def _step(connection: AsyncConnection, workflow_id: uuid.UUID, role: str) -> StepRecord:
        step = await WorkflowRepository(connection).step(workflow_id, role)
        if step is None:
            raise WorkflowRefusal("step_missing")
        return step

    # ---- role download ---------------------------------------------------------------------------

    async def start_download(
        self, workflow_id: uuid.UUID, *, url: object, root_id: uuid.UUID, file_name: object, intent: object
    ) -> WorkflowView:
        """Open the S2 download card as this workflow's `download` step. Nothing is fetched."""
        if self._transfers is None:
            raise WorkflowRefusal("downloads_not_configured")
        await self._precheck(workflow_id, "download")
        await self._transfers.create(
            url=url, root_id=root_id, file_name=file_name, intent=intent, link=self._linker(workflow_id, "download")
        )
        return await self.describe(workflow_id)

    # ---- role documents --------------------------------------------------------------------------

    async def start_documents(self, workflow_id: uuid.UUID) -> WorkflowView:
        """Create the `documents` step and bring in exactly the file this workflow's transfer placed.

        Idempotent: if the step exists without that file (for example the folder was not readable a moment
        ago), calling again only retries the import.
        """
        async with self._engine.connect() as connection:
            await self._live(connection, workflow_id, lock=False)
            download = await self._step(connection, workflow_id, "download")
            transfer = await TransferRepository(connection).for_task(download.task_id)
            existing = await WorkflowRepository(connection).step(workflow_id, "documents")
        placed = self._require_placed(transfer)
        if existing is None:
            view = await self._documents.create_task(objective="", link=self._linker(workflow_id, "documents"))
            task_id = view.task_id
        else:
            task_id = existing.task_id
        async with self._engine.connect() as connection:
            refs = await FileRepository(connection).refs_for_task(task_id)
        already = any(
            ref.source == "ROOT_FILE"
            and ref.root_id == placed.dest_root_id
            and ref.relative_path == placed.dest_name
            and ref.sha256 == placed.sha256
            for ref in refs
        )
        if not already:
            assert placed.sha256 is not None and placed.placed_volume is not None and placed.placed_index is not None
            try:
                await self._documents.add_placed_file(
                    task_id,
                    root_id=placed.dest_root_id,
                    name=placed.dest_name,
                    expected_sha256=placed.sha256,
                    expected_volume=placed.placed_volume,
                    expected_index=placed.placed_index,
                    check=self._still_live(workflow_id),
                )
            except DocumentRefusal as refusal:
                raise WorkflowRefusal(refusal.code) from None
        return await self.describe(workflow_id)

    @staticmethod
    def _require_placed(transfer: TransferRecord | None) -> TransferRecord:
        if (
            transfer is None
            or transfer.status != "PLACED"
            or transfer.sha256 is None
            or transfer.placed_volume is None
            or transfer.placed_index is None
        ):
            raise WorkflowRefusal("not_placed")
        return transfer

    # ---- candidates ------------------------------------------------------------------------------

    async def extract_candidates(self, workflow_id: uuid.UUID, *, document_id: uuid.UUID) -> WorkflowView:
        """`document_extracted` candidates: labelled lines of ONE document of this workflow's documents step."""
        async with self._engine.begin() as connection:
            step = await self._locked_step(connection, workflow_id, "documents")
            try:
                record_sha, found = await self._documents.candidate_fields(connection, step.task_id, document_id)
            except DocumentRefusal as refusal:
                raise WorkflowRefusal(_document_code(refusal.code)) from None
            repository = WorkflowRepository(connection)
            created = 0
            for item in found:
                if await repository.count_candidates(workflow_id) >= MAX_CANDIDATES_PER_WORKFLOW:
                    break
                inserted = await repository.insert_candidate(
                    workflow_id=workflow_id,
                    source_task_id=step.task_id,
                    document_id=document_id,
                    document_text_sha256=record_sha,
                    kind=item.kind,
                    provenance="document_extracted",
                    value=item.canonical,
                    value_digest=item.digest,
                    preview=item.preview,
                    span_start=item.start,
                    span_end=item.end,
                )
                created += inserted is not None
            await self._event(
                connection, step.task_id, TaskEventType.TASK_WORKFLOW_CANDIDATES_FOUND,
                {"workflow_id": str(workflow_id), "provenance": "document_extracted", "candidate_count": created},
            )
            await repository.bump(workflow_id)
            return await self._view(connection, workflow_id)

    async def derive_candidates(self, workflow_id: uuid.UUID) -> WorkflowView:
        """`provider_derived` candidates: labelled values inside quotes the ONE approved provider returned
        and the runtime grounded in the disclosed projection. The provider named neither a kind nor a value."""
        async with self._engine.begin() as connection:
            step = await self._locked_step(connection, workflow_id, "documents")
            try:
                disclosure_id, projection_digest, grounded = await self._documents.grounded_fields(connection, step.task_id)
            except DocumentRefusal as refusal:
                raise WorkflowRefusal(_document_code(refusal.code)) from None
            repository = WorkflowRepository(connection)
            created = 0
            for item in grounded:
                if await repository.count_candidates(workflow_id) >= MAX_CANDIDATES_PER_WORKFLOW:
                    break
                inserted = await repository.insert_candidate(
                    workflow_id=workflow_id,
                    source_task_id=step.task_id,
                    document_id=item.document_id,
                    document_text_sha256=item.text_sha256,
                    kind=item.found.kind,
                    provenance="provider_derived",
                    value=item.found.canonical,
                    value_digest=item.found.digest,
                    preview=item.found.preview,
                    span_start=item.found.start,
                    span_end=item.found.end,
                    disclosure_id=disclosure_id,
                    projection_digest=projection_digest,
                    doc_ref=item.doc_ref,
                    quote=item.quote,
                )
                created += inserted is not None
            await self._event(
                connection, step.task_id, TaskEventType.TASK_WORKFLOW_CANDIDATES_FOUND,
                {"workflow_id": str(workflow_id), "provenance": "provider_derived", "candidate_count": created},
            )
            await repository.bump(workflow_id)
            return await self._view(connection, workflow_id)

    # ---- the exact adoption approval ----------------------------------------------------------------

    async def propose_adoption(self, workflow_id: uuid.UUID, *, candidate_id: uuid.UUID) -> WorkflowView:
        """Open the adoption card for one candidate. Nothing is adopted until the trusted click."""
        async with self._engine.connect() as connection:
            await self._live(connection, workflow_id, lock=False)
            step = await self._step(connection, workflow_id, "documents")
            repository = WorkflowRepository(connection)
            candidate = await repository.get_candidate(candidate_id)
            if candidate is None or candidate.workflow_id != workflow_id:
                raise WorkflowRefusal("candidate_not_found")
            if candidate.status != "PROPOSED" or candidate.value is None:
                raise WorkflowRefusal("candidate_not_proposed")
            if await repository.value_for_kind(workflow_id, candidate.kind) is not None:
                raise WorkflowRefusal("value_already_adopted")
            previous = [
                action
                for action in await ActionRepository(connection).list_actions(step.task_id, limit=500)
                if action.tool_name == ADOPT_TOOL
            ]
        # Validated, not trusted: the kind and provenance are closed by the table's CHECKs and the model's.
        proposal = AdoptionProposal.model_validate(
            {
                "workflow_id": workflow_id,
                "candidate_id": candidate.id,
                "data_kind": candidate.kind,
                "provenance": candidate.provenance,
                "value_digest": candidate.value_digest,
                "preview": candidate.preview,
                "document_id": candidate.document_id,
                "document_text_sha256": candidate.document_text_sha256,
                "disclosure_id": candidate.disclosure_id,
                "projection_digest": candidate.projection_digest,
            }
        )
        for action in previous:
            if action.status in (ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL):
                await self._actions.reject_action(action.id, reason="superseded")
        view, _ = await self._actions.propose_action(
            step.task_id,
            idempotency_key=f"{ADOPT_TOOL}-{len(previous) + 1}",
            tool_name=ADOPT_TOOL,
            risk_tier=RiskTier.R2,
            proposal=proposal.proposal(),
        )
        await self._actions.request_approval(view.action.id, expected_revision=view.action.revision)
        return await self.describe(workflow_id)

    async def _adoption_action(self, action_id: uuid.UUID) -> tuple[ActionRecord, AdoptionProposal]:
        async with self._engine.connect() as connection:
            action = await ActionRepository(connection).get_action(action_id)
        if action is None or action.tool_name != ADOPT_TOOL:
            raise WorkflowRefusal("adoption_not_found")
        return action, parse_adoption(action.proposal)

    async def approve_adoption(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """The trusted click: exact, single-use. Every fact is re-derived inside the approving transaction."""
        _, proposal = await self._adoption_action(action_id)

        async def guard(connection: AsyncConnection, action: ActionRecord) -> None:
            await self._live(connection, proposal.workflow_id)
            step = await self._step(connection, proposal.workflow_id, "documents")
            if action.task_id != step.task_id:
                raise WorkflowRefusal("adoption_not_found")
            repository = WorkflowRepository(connection)
            candidate = await repository.get_candidate(proposal.candidate_id, lock=True)
            if candidate is None or candidate.workflow_id != proposal.workflow_id:
                raise WorkflowRefusal("candidate_not_found")
            if candidate.status != "PROPOSED" or candidate.value is None:
                raise WorkflowRefusal("candidate_not_proposed")
            if not _matches(candidate, proposal):
                raise WorkflowRefusal("candidate_changed")
            if await repository.value_for_kind(proposal.workflow_id, candidate.kind) is not None:
                raise WorkflowRefusal("value_already_adopted")
            canonical = await self._rederive(connection, step, candidate)
            await repository.insert_value(candidate=candidate, canonical=canonical, adopt_action_id=action.id)
            await repository.mark_candidate(candidate.id, status="ADOPTED")
            await repository.bump(proposal.workflow_id)
            # No task event here: the guard runs before the ledger's own transition on this task's row.

        settled = await self._actions.settle_exact_approval(
            action_id,
            expected_revision=expected_revision,
            guard=guard,
            result={**_ADOPTED, "kind": proposal.data_kind, "provenance": proposal.provenance},
        )
        async with self._engine.begin() as connection:
            await self._event(
                connection, settled.action.task_id, TaskEventType.TASK_WORKFLOW_VALUE_ADOPTED,
                {"workflow_id": str(proposal.workflow_id), "kind": proposal.data_kind, "provenance": proposal.provenance},
            )
        return settled

    async def _rederive(self, connection: AsyncConnection, step: StepRecord, candidate: CandidateRecord) -> str:
        """The value as it stands NOW in its source, canonicalised; refused if anything moved."""
        try:
            canonical = await self._documents.rederive_field(
                connection,
                step.task_id,
                document_id=candidate.document_id,
                text_sha256_expected=candidate.document_text_sha256,
                kind=candidate.kind,
                digest=candidate.value_digest,
                span_start=candidate.span_start,
                span_end=candidate.span_end,
                disclosure_id=candidate.disclosure_id,
                projection_digest=candidate.projection_digest,
                doc_ref=candidate.doc_ref,
                quote=candidate.quote,
            )
        except DocumentRefusal as refusal:
            raise WorkflowRefusal(_document_code(refusal.code)) from None
        if canonical != candidate.value:
            raise WorkflowRefusal("candidate_changed")
        return canonical

    async def reject_adoption(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        await self._adoption_action(action_id)
        return await self._actions.reject_action(action_id, expected_revision=expected_revision, reason="user_declined")

    # ---- role form ---------------------------------------------------------------------------------

    async def start_form(self, workflow_id: uuid.UUID, *, profile_id: uuid.UUID, objective: object) -> WorkflowView:
        """Create the `form` step: an ordinary authenticated task (M8), whose form planning may place only this
        workflow's adopted values. Its account-reading card, planning grant and manifest are its own approvals."""
        goal = validate_objective(objective)
        if self._check_profile is None:
            raise WorkflowRefusal("form_not_configured")
        await self._precheck(workflow_id, "form")
        await self._check_profile(profile_id)
        link = self._linker(workflow_id, "form")
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.insert_task(
                task_id=uuid.uuid4(),
                status=TaskStatus.CREATED,
                request={
                    "type": AUTHENTICATED_READ_TASK_TYPE,
                    "classification": ACCOUNT_PRIVATE,
                    "text": goal,
                    "objective": goal,
                    "source": "text",
                    "profile_id": str(profile_id),
                },
            )
            await tasks.append_event(task=task, event_type=TaskEventType.TASK_CREATED, payload={"status": task.status.value})
            await link(connection, task.id)
        return await self.describe(workflow_id)

    # ---- helpers -------------------------------------------------------------------------------------

    @staticmethod
    async def _event(connection: AsyncConnection, task_id: uuid.UUID, event_type: TaskEventType, payload: dict[str, Any]) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.lock_task(task_id)
        if current is None:
            raise TaskNotFoundError(task_id)
        advanced = await tasks.advance_task(task_id=current.id, expected_revision=current.revision)
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task_id)
        await tasks.append_event(task=advanced, event_type=event_type, payload=payload)

    async def _view(self, connection: AsyncConnection, workflow_id: uuid.UUID) -> WorkflowView:
        repository = WorkflowRepository(connection)
        workflow = await repository.get(workflow_id)
        assert workflow is not None
        tasks = TaskRepository(connection)
        steps: list[StepView] = []
        transfer: TransferRecord | None = None
        document_count = 0
        disclosure_status: str | None = None
        labels: dict[uuid.UUID, str] = {}
        adoptions: list[AdoptionView] = []
        candidates = await repository.candidates(workflow_id)
        by_id = {candidate.id: candidate for candidate in candidates}
        for step in await repository.steps(workflow_id):
            task = await tasks.get_task(step.task_id)
            steps.append(StepView(role=step.role, task_id=step.task_id, task_status=task.status.value if task else "UNKNOWN"))
            if step.role == "download":
                transfer = await TransferRepository(connection).for_task(step.task_id)
            if step.role == "documents":
                summary = await self._documents.task_summary(connection, step.task_id)
                labels = dict(summary.labels)
                document_count = summary.document_count
                disclosure_status = summary.disclosure_status
                adoptions = await self._adoptions(connection, step.task_id, by_id, labels)
        live = await repository.is_live(workflow_id)
        return WorkflowView(
            workflow=workflow,
            live=live,
            steps=tuple(steps),
            transfer_status=transfer.status if transfer is not None else None,
            placed_name=transfer.dest_name if transfer is not None and transfer.status == "PLACED" else None,
            document_count=document_count,
            disclosure_status=disclosure_status,
            candidates=tuple(
                CandidateView(
                    candidate_id=candidate.id,
                    kind=candidate.kind,
                    provenance=candidate.provenance,
                    value=candidate.value,
                    preview=candidate.preview,
                    status=candidate.status,
                    document_id=candidate.document_id,
                    document_label=labels.get(candidate.document_id, ""),
                    quote=candidate.quote,
                    doc_ref=candidate.doc_ref,
                )
                for candidate in candidates
            ),
            values=tuple(await repository.values(workflow_id)),
            adoptions=tuple(adoptions),
        )

    @staticmethod
    async def _adoptions(
        connection: AsyncConnection,
        task_id: uuid.UUID,
        candidates: dict[uuid.UUID, CandidateRecord],
        labels: dict[uuid.UUID, str],
    ) -> list[AdoptionView]:
        actions = ActionRepository(connection)
        latest: dict[uuid.UUID, tuple[ActionRecord, AdoptionProposal]] = {}
        for action in await actions.list_actions(task_id, limit=500):
            if action.tool_name != ADOPT_TOOL:
                continue
            try:
                proposal = parse_adoption(action.proposal)
            except WorkflowRefusal:  # pragma: no cover - only the controller writes these proposals.
                continue
            # Only the newest card per candidate is shown (S4 review finding 5: the view stays bounded by the
            # candidate cap however many times a card is superseded or declined).
            latest[proposal.candidate_id] = (action, proposal)
        views: list[AdoptionView] = []
        for action, proposal in latest.values():
            approvals = await actions.list_approvals(action.id)
            candidate = candidates.get(proposal.candidate_id)
            views.append(
                AdoptionView(
                    action_id=action.id,
                    revision=action.revision,
                    action_status=action.status.value,
                    approval_status=approvals[-1].status.value if approvals else None,
                    candidate_id=proposal.candidate_id,
                    kind=proposal.data_kind,
                    provenance=proposal.provenance,
                    preview=proposal.preview,
                    value=candidate.value if candidate is not None and action.status is ActionStatus.WAITING_APPROVAL else None,
                    document_label=labels.get(proposal.document_id, ""),
                )
            )
        return views


def _matches(candidate: CandidateRecord, proposal: AdoptionProposal) -> bool:
    return (
        candidate.kind == proposal.data_kind
        and candidate.provenance == proposal.provenance
        and candidate.value_digest == proposal.value_digest
        and candidate.preview == proposal.preview
        and candidate.document_id == proposal.document_id
        and candidate.document_text_sha256 == proposal.document_text_sha256
        and candidate.disclosure_id == proposal.disclosure_id
        and candidate.projection_digest == proposal.projection_digest
    )


__all__ = ["AdoptionView", "CandidateView", "StepView", "WorkflowService", "WorkflowView"]


def _document_code(code: str) -> str:
    """A document refusal, in the workflow's own vocabulary where one exists."""
    return {
        "document_not_found": "document_not_in_workflow",
        "disclosure_not_started": "disclosure_not_in_workflow",
    }.get(code, code)
