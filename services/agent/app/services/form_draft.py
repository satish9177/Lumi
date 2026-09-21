"""Milestone 8b S6: the network-frozen local form draft, from approval to hand-over.

**Lumi fills the form in its own browser with the network frozen, verifies the values
are in the fields, and hands you the browser. Nothing was sent while Lumi was filling.
If the form needs the network to accept a value, Lumi stops and tells you. Lumi never
submits.**

This module is the durable side of that promise. The browser side is
`app/browser/local_form_draft.py`; the two enforce the same rules independently.

```text
start_preparation   close the headless read context, reopen the SAME profile headed, return
                    internally to the page that was being read, take a completely fresh
                    observation. Every old ref, epoch, proposal and approval is dead.
fill_approved       ONE transaction: exact approval granted, claimed, attempt EXECUTING
                    -> dispatch row durable (frozen_at NULL)
                    -> worker ENTERS the two-layer freeze and proves it
                    -> frozen_at recorded  (only now may anything be written)
                    -> account re-checked -> ONE worker dispatch writes every field
                    -> a draft row, a finished attempt, a paused task: one transaction
discard             the worker destroys the dirty page WHILE FROZEN, then thaws
request_handover    a SECOND exact approval, bound to the draft digest
approve_handover    the worker re-verifies the live page, then restores the network
```

**Order is the property.** Durable intent precedes every browser effect, no transaction is
open while a browser is driven, and `frozen_at` is written *after* the freeze is proved and
*before* the first write -- never back-filled.

**The raw protected value.** It flows only `protected_values` -> this process's memory
(verified against the digest the user approved, under a share lock, in the approving
transaction) -> the loopback worker request -> the frozen page. It is never persisted,
logged, returned, put in an event, a ledger row, a draft row or a diagnostic. In S5 it never
left its row; in S6 it may go this far and no further.

**No provider is called** anywhere in this module, before, during or after a write.

**Nothing is retried.** A lost answer is `OUTCOME_UNKNOWN`. There is no reconciliation:
there is no authoritative verifier for "did the site save my draft". The freeze is what
makes the question about the *fill* moot, and `frozen_at` is what proves it.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.browser.errors import (
    BrowserWorkerError,
    BrowserWorkerLostResponseError,
    BrowserWorkerNotConfiguredError,
    StaleWorkerResultError,
)
from app.browser.protocol import (
    DispatchRequest,
    FormDiscardRequest,
    FormFreezeRequest,
    FormHandoverRequest,
    PrepareCaptureRequest,
    PrepareRestoreRequest,
)
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.authenticated import (
    AUTHENTICATED_READ_SITE,
    AUTHENTICATED_READ_TASK_TYPE,
    AuthenticatedStepEnvelope,
    PauseReason,
    parse_authenticated_step,
)
from app.domain.browser_dispatch import BrowserEffect, DispatchStatus
from app.domain.browser_profile import BrowserContextKind, BrowserProfile, ProfileStatus
from app.domain.errors import AuthenticatedProfileUnavailableError, TaskNotFoundError
from app.domain.form_prepare import (
    FORM_PREPARE_TOOL,
    DisclosureManifest,
    FormPrepareRefusal,
    ManifestField,
    parse_manifest,
)
from app.domain.local_form_draft import (
    EXECUTABLE_MANIFEST_POLICY,
    FILL_OPERATION,
    HANDOVER_OPERATION,
    HANDOVER_TOOL,
    LIVE_DRAFT_STATUSES,
    DraftFieldRecord,
    DraftStatus,
    FillFieldInput,
    FillFormInput,
    FillResult,
    HandoverProposal,
    VerifiedField,
    check_verified_hash,
    choice_verified_hash,
    draft_digest,
)
from app.domain.protected_values import value_digest
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions
from app.repositories.actions import ActionRecord, ActionRepository, AttemptRecord
from app.repositories.browser import BrowserRepository
from app.repositories.form_drafts import DraftRecord, FormDraftRepository
from app.repositories.form_prepare import FormPrepareRepository, ProtectedValueRepository
from app.repositories.profiles import BrowserProfileRepository
from app.repositories.tasks import TaskRepository
from app.services.actions import ActionService, ActionView
from app.services.authenticated_read import AuthenticatedReadService
from app.services.browser_execution import (
    Outcome,
    WorkerSource,
    dispatch_and_classify,
    open_worker_client,
)
from app.services.browser_profiles import BrowserProfileService
from app.services.form_prepare import FormPrepareService
from app.services.form_state import FormPhase, FormStateRegistry, ProfileFormState

logger = logging.getLogger("lumi.form_draft")

CODE_PREPARED = "local_draft_prepared"
CODE_PARTIAL = "local_draft_partial"
CODE_NOT_WRITTEN = "local_draft_not_written"
CODE_HANDED_OVER = "handed_over"

#: Refusal codes a worker freeze can answer with that mean "nothing was written".
_RESTORE_CODES = {
    "NO_DESTINATION": "preparation_destination_missing",
    "NAVIGATION_FAILED": "preparation_navigation_failed",
    "LEFT_SITE_SCOPE": "left_site_scope",
    "NOT_HEADED": "preparation_mode_required",
    "PROFILE_NOT_OPEN": "preparation_mode_required",
    "REFUSED": "preparation_mode_required",
}


@dataclass(frozen=True, slots=True)
class HandoverView:
    """One exact handover approval, as the trusted card needs it."""

    action: ActionRecord
    proposal: HandoverProposal
    approval_status: str | None
    approval_expires_at: Any
    result_code: str | None


def _field_record(
    field: ManifestField, verified_hash: str, written_at: datetime
) -> DraftFieldRecord:
    return DraftFieldRecord(
        element_ref=field.element_ref,
        element_identity_hash=field.element_identity_hash,
        control_type=field.control_type,
        data_ref=field.data_ref,
        value_digest=field.value_digest,
        option_ref=field.option_ref,
        option_identity_hash=field.option_identity_hash,
        checked=field.checked,
        verified_local_value_hash=verified_hash,
        written_at=written_at,
    )


def _expected_hash(field: ManifestField) -> str:
    """What a correctly-written field's verified hash must be, derived from the approval."""
    if field.data_ref is not None:
        assert field.value_digest is not None
        return field.value_digest
    if field.option_identity_hash is not None:
        return choice_verified_hash(field.option_identity_hash)
    assert field.checked is not None
    return check_verified_hash(field.element_identity_hash, field.checked)


class FormDraftService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        reads: AuthenticatedReadService,
        profiles: BrowserProfileService,
        form: FormPrepareService,
        runtime_generation: uuid.UUID,
        worker: WorkerSource | None,
        state: FormStateRegistry,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._reads = reads
        self._profiles = profiles
        self._form = form
        self._runtime_generation = runtime_generation
        self._worker = worker
        self._state = state

    # ---- plumbing ------------------------------------------------------------------------

    async def _client(self) -> BrowserWorkerClient:
        if self._worker is None:
            raise BrowserWorkerNotConfiguredError()
        return await open_worker_client(self._worker, self._runtime_generation)

    async def _bind_worker(self, client: BrowserWorkerClient) -> uuid.UUID:
        identity = await client.identify()
        async with self._engine.begin() as connection:
            repository = BrowserRepository(connection)
            if await repository.get_worker_generation(identity.worker_generation) is None:
                await repository.register_worker_generation(
                    worker_generation=identity.worker_generation,
                    runtime_generation=self._runtime_generation,
                    worker_started_at=datetime.fromisoformat(identity.started_at),
                )
        return identity.worker_generation

    async def _profile_of_task(self, task_id: uuid.UUID) -> BrowserProfile:
        async with self._engine.connect() as connection:
            task = await TaskRepository(connection).get_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
                raise FormPrepareRefusal("not_an_authenticated_task")
            try:
                profile_id = uuid.UUID(str(task.request.get("profile_id")))
            except ValueError:
                raise AuthenticatedProfileUnavailableError("profile_not_found") from None
            profile = await BrowserProfileRepository(connection).get(profile_id)
        if profile is None or profile.is_deleted:
            raise AuthenticatedProfileUnavailableError("profile_not_found")
        return profile

    async def _pause(self, task_id: uuid.UUID, reason: PauseReason, **payload: Any) -> None:
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None or not accepts_actions(task.status):
                return
            advanced = await tasks.advance_task(
                task_id=task.id, expected_revision=task.revision, status=TaskStatus.PAUSED
            )
            if advanced is not None:
                await tasks.append_event(
                    task=advanced,
                    event_type=TaskEventType.TASK_AUTHENTICATED_PAUSED,
                    payload={"reason": reason.value, **payload},
                )

    async def _event(self, task_id: uuid.UUID, event: TaskEventType, **payload: Any) -> None:
        """One timeline event: ids, statuses and counts only -- never a value or a label."""
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                return
            advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
            if advanced is not None:
                await tasks.append_event(task=advanced, event_type=event, payload=payload)

    async def _resume(self, task_id: uuid.UUID, event: TaskEventType, **payload: Any) -> None:
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None or not accepts_actions(task.status):
                return
            status = TaskStatus.READY if task.status is TaskStatus.PAUSED else None
            advanced = await tasks.advance_task(
                task_id=task.id, expected_revision=task.revision, status=status
            )
            if advanced is not None:
                await tasks.append_event(task=advanced, event_type=event, payload=payload)

    async def _supersede_open_disclosures(self, task_id: uuid.UUID) -> None:
        """Whatever was proposed or approved-in-waiting against the old document dies."""
        async with self._engine.connect() as connection:
            actions = await ActionRepository(connection).list_actions(task_id, limit=500)
        for action in actions:
            if action.tool_name == FORM_PREPARE_TOOL and action.status in (
                ActionStatus.WAITING_APPROVAL,
                ActionStatus.PROPOSED,
            ):
                await self._actions.reject_action(action.id, reason="preparation_mode")

    # ---- preparation mode ------------------------------------------------------------------

    async def start_preparation(self, task_id: uuid.UUID) -> None:
        """Reopen the profile headed, return to the page being read, observe it afresh.

        The destination never leaves the worker. If the worker cannot return to the same
        page safely, this refuses (`preparation_destination_missing`) and the user's only
        path is to open the form in the window themselves -- no URL is ever accepted from
        anywhere else. Nothing from the headless document survives: every approval that
        was waiting is rejected, every ref dies with the old context.
        """
        profile = await self._profile_of_task(task_id)
        self._state.assert_clean(profile.id)
        view = await self._reads.describe(task_id)
        if view.grant is None or view.grant.status.value != "ACTIVE":
            raise FormPrepareRefusal("grant_not_usable")
        if view.unresolved_step:
            raise FormPrepareRefusal("step_in_flight")
        if profile.status is not ProfileStatus.AUTHENTICATED:
            raise AuthenticatedProfileUnavailableError("login_required")
        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)
            captured = await client.prepare_capture(
                PrepareCaptureRequest(
                    profile_id=profile.id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    site=profile.site,
                )
            )
            if captured.status != "CAPTURED":
                raise FormPrepareRefusal(captured.error_code or "preparation_destination_missing")
            await self._supersede_open_disclosures(task_id)
            # The headless read context ends here, and with it every old ref and epoch.
            await self._profiles.close_profile(profile.id)
            opened = await self._profiles.open_profile(
                profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE, headed=True
            )
            if opened.worker_generation != worker_generation:
                await self._profiles.close_profile(profile.id)
                raise FormPrepareRefusal("preparation_destination_missing")
            self._state.begin_preparation(
                profile.id, task_id=task_id, worker_generation=worker_generation
            )
            restored = await client.prepare_restore(
                PrepareRestoreRequest(
                    profile_id=profile.id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                    site=profile.site,
                )
            )
        except BaseException:
            await self._abandon_preparation(profile.id)
            raise
        finally:
            await client.aclose()
        if restored.status != "RESTORED":
            await self._abandon_preparation(profile.id)
            raise FormPrepareRefusal(_RESTORE_CODES.get(restored.status, "preparation_navigation_failed"))
        # A completely fresh S4 observation, through the normal account-read gates
        # (credential surface, account identity, in-site) in the headed window.
        step = await self._reads.execute_step(
            task_id,
            AuthenticatedStepEnvelope(
                request_id=f"prep-{uuid.uuid4().hex[:20]}",
                step=parse_authenticated_step({"operation": "observe", "tab": "t1"}),
                planner_calls=0,
            ),
        )
        if step.observation is None or step.observation.observation.inventory.forms == []:
            # Paused (login required, account changed...) or no form on the page.
            await self._abandon_preparation(profile.id)
            if step.observation is not None:
                raise FormPrepareRefusal("no_form_observed")
            return
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is not None:
                advanced = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
                if advanced is not None:
                    await tasks.append_event(
                        task=advanced,
                        event_type=TaskEventType.TASK_FORM_PREPARATION_STARTED,
                        payload={
                            "profile_id": str(profile.id),
                            "form_count": step.observation.observation.inventory.form_count,
                            "element_count": step.observation.observation.inventory.element_count,
                        },
                    )

    async def _abandon_preparation(self, profile_id: uuid.UUID) -> None:
        """Leave preparation mode: nothing was written, so closing the window is safe."""
        state = self._state.get(profile_id)
        if state is None or state.phase is not FormPhase.PREPARATION:
            return
        try:
            await self._profiles.close_profile(profile_id)
        except (BrowserWorkerError, FormPrepareRefusal):
            logger.info("could not close the preparation window", extra={"profile_id": str(profile_id)})
        self._state.clear(profile_id)

    # ---- the fill -----------------------------------------------------------------------------

    async def fill_approved(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """The exact approval, carried out. See the module docstring for the order."""
        view = await self._actions.get_action(action_id)
        action = view.action
        manifest = parse_manifest(action.proposal)
        if manifest.policy_version != EXECUTABLE_MANIFEST_POLICY:
            # A historical S5 approval. It is terminal or superseded and never executable.
            raise FormPrepareRefusal("legacy_manifest_not_executable")
        state = self._state.get(manifest.profile_id)
        if (
            state is None
            or state.phase is not FormPhase.PREPARATION
            or state.task_id != manifest.task_id
            or manifest.observation_id not in state.observation_ids
        ):
            raise FormPrepareRefusal("preparation_mode_required")

        captured: dict[str, str] = {}

        async def guard(connection: AsyncConnection, record: ActionRecord) -> None:
            await self._form._guard(connection, record, manifest)
            # The exact bytes the user approved, verified against the approved digest under
            # a share lock, in the approving transaction, and held in memory only.
            raw = await ProtectedValueRepository(connection).values_for_execution(manifest.data_refs)
            for field in manifest.fields:
                if field.data_ref is None:
                    continue
                value = raw.get(field.data_ref)
                if value is None or value_digest(value) != field.value_digest:
                    raise FormPrepareRefusal("protected_value_changed")
                captured[field.data_ref] = value

        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)
            if worker_generation != state.worker_generation:
                raise FormPrepareRefusal("preparation_mode_required")
            started = await self._actions.begin_exact_execution(
                action_id, expected_revision=expected_revision, guard=guard
            )
            attempt = next(item for item in started.attempts if item.finished_at is None)
            state.phase = FormPhase.FILLING
            state.action_id = action_id
            dispatch_id = uuid.uuid4()
            state.dispatch_id = dispatch_id
            try:
                async with self._engine.begin() as connection:
                    await BrowserRepository(connection).insert_dispatch(
                        dispatch_id=dispatch_id,
                        action_id=action_id,
                        attempt_id=attempt.id,
                        worker_generation=worker_generation,
                        operation=FILL_OPERATION,
                        site=AUTHENTICATED_READ_SITE,
                        effect=BrowserEffect.LOCAL_DRAFT,
                    )
            except Exception:
                logger.exception("could not record the local-draft dispatch")
                self._back_to_preparation(state)
                return await self._actions.finish_attempt(
                    action_id, outcome=AttemptOutcome.FAILED, error_code="dispatch_not_recorded",
                    result={"code": CODE_NOT_WRITTEN, "dirty": False},
                )
            # --- committed. Nothing below holds a transaction. -------------------------------
            return await self._carry_out_fill(
                client=client, manifest=manifest, captured=captured, action_id=action_id,
                attempt=attempt, dispatch_id=dispatch_id, worker_generation=worker_generation,
                state=state,
            )
        finally:
            captured.clear()
            await client.aclose()

    @staticmethod
    def _back_to_preparation(state: ProfileFormState) -> None:
        state.phase = FormPhase.PREPARATION
        state.action_id = None
        state.dispatch_id = None

    async def _carry_out_fill(
        self,
        *,
        client: BrowserWorkerClient,
        manifest: DisclosureManifest,
        captured: dict[str, str],
        action_id: uuid.UUID,
        attempt: AttemptRecord,
        dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
        state: ProfileFormState,
    ) -> ActionView:
        profile_id = manifest.profile_id
        # ENTER THE FREEZE, and have the worker prove it.
        freeze_failure = await self._freeze(client, profile_id, dispatch_id, worker_generation)
        if freeze_failure is not None:
            return await self._finish_without_writes(
                action_id, dispatch_id, state, profile_id, worker_generation, freeze_failure,
                frozen=False,
            )
        # frozen_at: written only after the proof and before the first write; never after.
        async with self._engine.begin() as connection:
            marked = await BrowserRepository(connection).mark_frozen(
                dispatch_id=dispatch_id, worker_generation=worker_generation
            )
        if marked is None:
            await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            return await self._finish_without_writes(
                action_id, dispatch_id, state, profile_id, worker_generation,
                "freeze_not_recorded", frozen=False,
            )
        # The account is checked again, from the database, immediately before the write.
        account_problem = await self._account_problem(manifest)
        if account_problem is not None:
            await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            return await self._finish_without_writes(
                action_id, dispatch_id, state, profile_id, worker_generation,
                account_problem, frozen=True,
            )
        request = DispatchRequest(
            dispatch_id=dispatch_id,
            runtime_generation=self._runtime_generation,
            expected_worker_generation=worker_generation,
            action_id=action_id,
            attempt_id=attempt.id,
            operation=FILL_OPERATION,
            site=AUTHENTICATED_READ_SITE,
            profile_id=profile_id,
            input=(await self._fill_input(manifest, captured)).model_dump(mode="json"),
        )
        outcome = await dispatch_and_classify(client, request=request)
        return await self._settle_fill(
            client, manifest=manifest, outcome=outcome, action_id=action_id,
            attempt=attempt, dispatch_id=dispatch_id, worker_generation=worker_generation,
            state=state,
        )

    async def _fill_input(
        self, manifest: DisclosureManifest, captured: dict[str, str]
    ) -> FillFormInput:
        """The strict worker input, from the persisted manifest and the approved values."""
        async with self._engine.connect() as connection:
            grant = await FormPrepareRepository(connection).get_grant(manifest.form_prepare_grant_id)
            profile = await BrowserProfileRepository(connection).get(manifest.profile_id)
        if grant is None or profile is None:
            raise FormPrepareRefusal("account_changed")
        fields: list[FillFieldInput] = []
        for item in manifest.fields:
            if item.data_ref is not None:
                fields.append(
                    FillFieldInput(
                        element_ref=item.element_ref,
                        element_identity_hash=item.element_identity_hash,
                        control_type=item.control_type,
                        text_value=captured[item.data_ref],
                        value_digest=item.value_digest,
                    )
                )
            elif item.option_ref is not None:
                fields.append(
                    FillFieldInput(
                        element_ref=item.element_ref,
                        element_identity_hash=item.element_identity_hash,
                        control_type=item.control_type,
                        option_ref=item.option_ref,
                        option_identity_hash=item.option_identity_hash,
                    )
                )
            else:
                fields.append(
                    FillFieldInput(
                        element_ref=item.element_ref,
                        element_identity_hash=item.element_identity_hash,
                        control_type="checkbox",
                        checked=item.checked,
                    )
                )
        return FillFormInput(
            site=profile.site,
            expected_account_fingerprint=grant.scope.account_fingerprint,
            observation_id=manifest.observation_id,
            tab=manifest.tab,
            document_epoch=manifest.document_epoch,
            form_epoch=manifest.form_epoch,
            form_ref=manifest.form_ref,
            manifest_digest=manifest.manifest_digest,
            fields=fields,
        )

    async def _account_problem(self, manifest: DisclosureManifest) -> str | None:
        async with self._engine.connect() as connection:
            grant = await FormPrepareRepository(connection).get_grant(manifest.form_prepare_grant_id)
            profile = await BrowserProfileRepository(connection).get(manifest.profile_id)
        if profile is None or profile.is_deleted:
            return "account_changed"
        if profile.status is not ProfileStatus.AUTHENTICATED:
            return "login_required"
        if grant is None or (
            profile.revoke_epoch != grant.profile_revoke_epoch
            or profile.account_fingerprint != grant.scope.account_fingerprint
        ):
            return "account_changed"
        return None

    async def _freeze(
        self, client: BrowserWorkerClient, profile_id: uuid.UUID, dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
    ) -> str | None:
        """Ask the worker to enter the freeze. None on a proven freeze, else a stable code.

        A lost answer is *not* a failure Lumi can stand behind: the worker may have frozen
        (and may even hold the freeze). It is reported as `freeze_unknown`, and the caller
        discards to release whatever the worker holds.
        """
        try:
            proof = await client.form_freeze(
                FormFreezeRequest(
                    profile_id=profile_id,
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                )
            )
        except (BrowserWorkerLostResponseError, StaleWorkerResultError):
            return "freeze_unknown"
        except BrowserWorkerError as error:
            return getattr(error, "code", "freeze_failed")
        if proof.status != "FROZEN":
            return proof.error_code or "freeze_failed"
        if proof.guard_in_flight != 0 or proof.broker_active_connections != 0:
            return "freeze_failed"
        return None

    async def _release_freeze(
        self, client: BrowserWorkerClient, profile_id: uuid.UUID, dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
    ) -> bool:
        """Best-effort: destroy the page while frozen and thaw. Never retried blindly."""
        try:
            answer = await client.form_discard(
                FormDiscardRequest(
                    profile_id=profile_id,
                    dispatch_id=dispatch_id,
                    runtime_generation=self._runtime_generation,
                    expected_worker_generation=worker_generation,
                )
            )
        except BrowserWorkerError:
            return False
        return answer.status in ("DISCARDED", "NOTHING_TO_DISCARD", "PROFILE_NOT_OPEN")

    async def _finish_without_writes(
        self, action_id: uuid.UUID, dispatch_id: uuid.UUID, state: ProfileFormState,
        profile_id: uuid.UUID, worker_generation: uuid.UUID, code: str, *, frozen: bool,
    ) -> ActionView:
        """A failure before any field write. The page was not touched.

        Everything but a lost freeze answer is a failure Lumi can stand behind. A lost
        answer means the worker may hold the freeze, so the page is discarded and the
        outcome is unknown -- with no claim about the remote effect, because
        `frozen_at` is NULL.
        """
        unknown = code == "freeze_unknown"
        if unknown:
            client = await self._client()
            try:
                await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            finally:
                await client.aclose()
        await self._close_dispatch(
            dispatch_id,
            DispatchStatus.OUTCOME_UNKNOWN if unknown else DispatchStatus.FAILED_BEFORE_EFFECT,
            code,
            {"dirty": False, "frozen_at_present": frozen},
        )
        self._back_to_preparation(state)
        return await self._actions.finish_attempt(
            action_id,
            outcome=AttemptOutcome.OUTCOME_UNKNOWN if unknown else AttemptOutcome.FAILED,
            error_code=code,
            result={"code": CODE_NOT_WRITTEN, "dirty": False, "frozen_at_present": frozen},
        )

    async def _close_dispatch(
        self, dispatch_id: uuid.UUID, status: DispatchStatus, error_code: str | None,
        result: dict[str, Any],
    ) -> None:
        async with self._engine.begin() as connection:
            await BrowserRepository(connection).finish_dispatch(
                dispatch_id=dispatch_id, status=status, submitted=False, observation_id=None,
                error_code=error_code, duration_ms=None, result=result,
            )

    async def _settle_fill(
        self,
        client: BrowserWorkerClient,
        *,
        manifest: DisclosureManifest,
        outcome: Outcome,
        action_id: uuid.UUID,
        attempt: AttemptRecord,
        dispatch_id: uuid.UUID,
        worker_generation: uuid.UUID,
        state: ProfileFormState,
    ) -> ActionView:
        profile_id = manifest.profile_id
        fill = _parse_fill(outcome)
        if fill is None and outcome.outcome is AttemptOutcome.FAILED:
            # The worker refused before running anything (stale generation, freeze owned,
            # not headed...): a failure Lumi can stand behind. The freeze it may still hold
            # is released by destroying the page while frozen.
            await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            return await self._finish_without_writes(
                action_id, dispatch_id, state, profile_id, worker_generation,
                outcome.error_code or "worker_refused", frozen=True,
            )
        if fill is None:
            # No usable answer: the worker may have written, and the page is frozen. The
            # freeze is released by destroying it, and the outcome is UNKNOWN -- but with
            # `frozen_at` set, no request can have left.
            await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            await self._close_dispatch(
                dispatch_id, DispatchStatus.OUTCOME_UNKNOWN, outcome.error_code or "no_answer",
                {"dirty": True, "frozen_at_present": True, "local_state": "lost"},
            )
            self._back_to_preparation(state)
            finished = await self._actions.finish_attempt(
                action_id, outcome=AttemptOutcome.OUTCOME_UNKNOWN,
                error_code=outcome.error_code or "no_answer",
                result={"code": CODE_PARTIAL, "dirty": True, "frozen_at_present": True,
                        "remote_effect": "impossible_under_verified_freeze", "local_state": "lost"},
            )
            await self._pause(manifest.task_id, PauseReason.BROWSER_LOST)
            return finished

        # Every verified field must be exactly what was approved; the runtime does not
        # take the worker's word for it.
        approved = {item.element_ref: item for item in manifest.fields}
        verified: dict[str, str] = {}
        for item in fill.verified_fields:
            field = approved.get(item.element_ref)
            if field is not None and item.verified_local_value_hash == _expected_hash(field):
                verified[item.element_ref] = item.verified_local_value_hash
        complete = (
            fill.draft_complete and len(verified) == len(manifest.fields) and not fill.error_code
        )
        dirty = fill.dirty and bool(verified)
        summary: dict[str, Any] = {
            "draft_complete": complete,
            "fields_attempted": fill.fields_attempted,
            "fields_verified": len(verified),
            "blocked_request_count": fill.blocked_request_count,
            "resolution_delta": fill.resolution_delta,
            "dial_delta": fill.dial_delta,
            "dirty": dirty,
            "page_discarded": bool(outcome.observation.get("page_discarded")),
            "frozen_at_present": True,
        }
        await self._close_dispatch(
            dispatch_id,
            DispatchStatus.OK if complete else DispatchStatus.FAILED_BEFORE_EFFECT,
            fill.error_code,
            summary,
        )
        if not dirty:
            # Nothing written and verified. The worker already gave the network back (a clean
            # page) or destroyed the page while frozen and did the same.
            self._back_to_preparation(state)
            if fill.dirty and not bool(outcome.observation.get("page_discarded")):
                await self._release_freeze(client, profile_id, dispatch_id, worker_generation)
            return await self._actions.finish_attempt(
                action_id, outcome=AttemptOutcome.FAILED, error_code=fill.error_code or "not_written",
                result={"code": CODE_NOT_WRITTEN, **summary},
            )

        status = DraftStatus.PREPARED if complete else DraftStatus.STALE
        now = datetime.now(UTC)
        records = [
            _field_record(approved[ref], hash_, now)
            for ref, hash_ in sorted(verified.items(), key=lambda pair: int(pair[0][1:]))
        ]
        draft_id = uuid.uuid4()
        digest = draft_digest(
            task_id=manifest.task_id, profile_id=profile_id, action_id=action_id,
            manifest_digest=manifest.manifest_digest, dispatch_id=dispatch_id,
            observation_id=manifest.observation_id, tab=manifest.tab,
            document_epoch=manifest.document_epoch, form_epoch=manifest.form_epoch,
            form_ref=manifest.form_ref, status=status, fields=records,
        )

        async def record(connection: AsyncConnection, finished: AttemptRecord) -> None:
            await FormDraftRepository(connection).insert(
                draft_id=draft_id, task_id=manifest.task_id, profile_id=profile_id,
                action_id=action_id, attempt_id=finished.id, dispatch_id=dispatch_id,
                manifest_digest=manifest.manifest_digest, draft_digest=digest,
                observation_id=manifest.observation_id, tab=manifest.tab,
                document_epoch=manifest.document_epoch, form_epoch=manifest.form_epoch,
                form_ref=manifest.form_ref, status=status, fields=records,
            )

        state.phase = FormPhase.DIRTY
        state.draft_id = draft_id
        finished = await self._actions.finish_attempt(
            action_id,
            outcome=AttemptOutcome.SUCCEEDED if complete else AttemptOutcome.FAILED,
            error_code=None if complete else (fill.error_code or "unsupported_under_freeze"),
            result={"code": CODE_PREPARED if complete else CODE_PARTIAL, "draft_id": str(draft_id), **summary},
            record=record,
        )
        await self._event(
            manifest.task_id, TaskEventType.TASK_FORM_DRAFT_RECORDED,
            draft_id=str(draft_id), status=status.value, field_count=len(records),
            verified_count=len(records), frozen_at_present=True,
        )
        # The task waits for a human: discard, or take the browser over.
        await self._pause(manifest.task_id, PauseReason.FORM_DRAFT, draft_id=str(draft_id))
        logger.info(
            "local form draft recorded",
            extra={
                "task_id": str(manifest.task_id), "draft_id": str(draft_id), "status": status.value,
                "field_count": len(records), "dispatch_id": str(dispatch_id),
                "blocked_request_count": fill.blocked_request_count,
            },
        )
        return finished

    # ---- discard / stop --------------------------------------------------------------------------

    async def _live_draft(
        self, draft_id: uuid.UUID, expected_revision: int | None
    ) -> DraftRecord:
        async with self._engine.connect() as connection:
            draft = await FormDraftRepository(connection).get(draft_id)
        if draft is None:
            raise FormPrepareRefusal("draft_not_found")
        if draft.status not in LIVE_DRAFT_STATUSES:
            raise FormPrepareRefusal("draft_not_live")
        if expected_revision is not None and draft.revision != expected_revision:
            raise FormPrepareRefusal("draft_changed")
        return draft

    async def discard(self, draft_id: uuid.UUID, *, expected_revision: int) -> DraftRecord:
        """The trusted Discard: destroy the dirty page WHILE FROZEN, then thaw.

        Never "thaw then reload": a dirty page thawed while alive can autosave. The
        worker closes the tab, verifies it is gone and only then opens the network.
        """
        draft = await self._live_draft(draft_id, expected_revision)
        state = self._state.get(draft.profile_id)
        if state is not None and state.phase in (FormPhase.HANDOVER, FormPhase.UNKNOWN):
            # A handover is running, or its answer was lost: the network may already be back,
            # so there is no frozen page to destroy and nothing safe to claim.
            raise FormPrepareRefusal("form_is_dirty")
        if state is not None and state.worker_generation is not None:
            client = await self._client()
            try:
                worker_generation = await self._bind_worker(client)
                if worker_generation == state.worker_generation:
                    answer = await client.form_discard(
                        FormDiscardRequest(
                            profile_id=draft.profile_id, dispatch_id=draft.dispatch_id,
                            runtime_generation=self._runtime_generation,
                            expected_worker_generation=worker_generation,
                        )
                    )
                    if answer.status == "REFUSED":
                        raise FormPrepareRefusal(answer.error_code or "freeze_owned")
                # A different worker generation means the browser holding the draft is gone.
            finally:
                await client.aclose()
        async with self._engine.begin() as connection:
            moved = await FormDraftRepository(connection).transition(
                draft_id=draft.id, expected_revision=draft.revision,
                allowed_from=LIVE_DRAFT_STATUSES, to=DraftStatus.DISCARDED,
            )
        if moved is None:
            raise FormPrepareRefusal("draft_changed")
        if state is not None:
            state.phase = FormPhase.PREPARATION
            state.draft_id = None
            state.action_id = None
            state.dispatch_id = None
            state.observation_ids.clear()
        await self._resume(draft.task_id, TaskEventType.TASK_FORM_DRAFT_DISCARDED, draft_id=str(draft.id))
        return moved

    async def stop(self, task_id: uuid.UUID) -> None:
        """Stop: discard any local draft while frozen, THEN close the window."""
        profile = await self._profile_of_task(task_id)
        async with self._engine.connect() as connection:
            draft = await FormDraftRepository(connection).live_for_profile(profile.id)
        if draft is not None:
            await self.discard(draft.id, expected_revision=draft.revision)
        state = self._state.get(profile.id)
        if state is not None and state.phase is FormPhase.PREPARATION:
            await self._profiles.close_profile(profile.id)
            self._state.clear(profile.id)

    # ---- handover: the second exact approval -----------------------------------------------------

    async def request_handover(self, draft_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """Open the second exact approval for lifting the freeze. Safe references only."""
        draft = await self._live_draft(draft_id, expected_revision)
        state = self._state.get(draft.profile_id)
        if state is None or state.phase is not FormPhase.DIRTY or state.draft_id != draft.id:
            raise FormPrepareRefusal("draft_changed")
        profile = await self._profile_of_task(draft.task_id)
        proposal = HandoverProposal(
            task_id=draft.task_id, profile_id=draft.profile_id, draft_id=draft.id,
            draft_digest=draft.draft_digest, site_display=profile.site,
            field_count=len(draft.fields), partial=draft.status is DraftStatus.STALE,
        )
        async with self._engine.connect() as connection:
            previous = [
                item for item in await ActionRepository(connection).list_actions(draft.task_id, limit=500)
                if item.tool_name == HANDOVER_TOOL
            ]
        for item in previous:
            if item.status in (ActionStatus.WAITING_APPROVAL, ActionStatus.PROPOSED):
                await self._actions.reject_action(item.id, reason="superseded")
        view, _ = await self._actions.propose_action(
            draft.task_id,
            idempotency_key=f"{HANDOVER_TOOL}-{len(previous) + 1}",
            tool_name=HANDOVER_TOOL,
            risk_tier=RiskTier.R2,
            proposal=proposal.model_dump(mode="json"),
        )
        return await self._actions.request_approval(view.action.id, expected_revision=view.action.revision)

    async def reject_handover(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        view = await self._actions.get_action(action_id)
        if view.action.tool_name != HANDOVER_TOOL:
            raise FormPrepareRefusal("not_a_handover_approval")
        return await self._actions.reject_action(
            action_id, expected_revision=expected_revision, reason="user_declined"
        )

    async def approve_handover(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """The trusted click that lets the page send. Exact, single-use, re-verified live."""
        view = await self._actions.get_action(action_id)
        action = view.action
        if action.tool_name != HANDOVER_TOOL:
            raise FormPrepareRefusal("not_a_handover_approval")
        proposal = HandoverProposal.model_validate(action.proposal)
        loaded: dict[str, DraftRecord] = {}

        async def guard(connection: AsyncConnection, record: ActionRecord) -> None:
            draft = await FormDraftRepository(connection).get(proposal.draft_id)
            if draft is None or draft.task_id != record.task_id or draft.profile_id != proposal.profile_id:
                raise FormPrepareRefusal("draft_changed")
            if draft.status not in LIVE_DRAFT_STATUSES:
                raise FormPrepareRefusal("draft_not_live")
            if draft.draft_digest != proposal.draft_digest:
                raise FormPrepareRefusal("draft_changed")
            state = self._state.get(draft.profile_id)
            if state is None or state.phase is not FormPhase.DIRTY or state.draft_id != draft.id:
                raise FormPrepareRefusal("draft_changed")
            profile = await BrowserProfileRepository(connection).get(draft.profile_id)
            if profile is None or profile.is_deleted:
                raise FormPrepareRefusal("account_changed")
            if profile.status is not ProfileStatus.AUTHENTICATED:
                raise FormPrepareRefusal("login_required")
            source = await ActionRepository(connection).get_action(draft.action_id)
            if source is None or parse_manifest(source.proposal).profile_revoke_epoch != profile.revoke_epoch:
                raise FormPrepareRefusal("account_changed")
            loaded["draft"] = draft

        client = await self._client()
        try:
            worker_generation = await self._bind_worker(client)
            started = await self._actions.begin_exact_execution(
                action_id, expected_revision=expected_revision, guard=guard
            )
            draft = loaded["draft"]
            state = self._state.get(draft.profile_id)
            if state is None or worker_generation != state.worker_generation:
                raise FormPrepareRefusal("draft_changed")
            attempt = next(item for item in started.attempts if item.finished_at is None)
            state.phase = FormPhase.HANDOVER
            handover_dispatch = uuid.uuid4()
            async with self._engine.begin() as connection:
                await BrowserRepository(connection).insert_dispatch(
                    dispatch_id=handover_dispatch, action_id=action_id, attempt_id=attempt.id,
                    worker_generation=worker_generation, operation=HANDOVER_OPERATION,
                    site=AUTHENTICATED_READ_SITE, effect=BrowserEffect.LOCAL_DRAFT,
                )
            # --- committed. The network may be restored from here on. ---------------------------
            try:
                answer = await client.form_handover(
                    FormHandoverRequest(
                        profile_id=draft.profile_id,
                        dispatch_id=draft.dispatch_id,
                        runtime_generation=self._runtime_generation,
                        expected_worker_generation=worker_generation,
                        fields=[
                            VerifiedField(
                                element_ref=item.element_ref,
                                verified_local_value_hash=item.verified_local_value_hash,
                            )
                            for item in draft.fields
                        ],
                    )
                )
            except (BrowserWorkerLostResponseError, StaleWorkerResultError):
                # The network may already be back. Unknown; never retried, never reconciled.
                state.phase = FormPhase.UNKNOWN
                await self._close_dispatch(handover_dispatch, DispatchStatus.OUTCOME_UNKNOWN, "lost_response", {})
                return await self._actions.finish_attempt(
                    action_id, outcome=AttemptOutcome.OUTCOME_UNKNOWN, error_code="lost_response",
                    result={"code": "handover_unknown"},
                )
            except BrowserWorkerError as error:
                state.phase = FormPhase.DIRTY
                await self._close_dispatch(
                    handover_dispatch, DispatchStatus.FAILED_BEFORE_EFFECT, error.code, {}
                )
                return await self._actions.finish_attempt(
                    action_id, outcome=AttemptOutcome.FAILED, error_code=error.code,
                    result={"code": "handover_refused"},
                )
            if answer.status != "HANDED_OVER":
                # Refused with the network still frozen (the page is not the approved draft).
                state.phase = FormPhase.DIRTY
                await self._close_dispatch(
                    handover_dispatch, DispatchStatus.FAILED_BEFORE_EFFECT, answer.error_code,
                    {"verified_count": answer.verified_count},
                )
                return await self._actions.finish_attempt(
                    action_id, outcome=AttemptOutcome.FAILED,
                    error_code=answer.error_code or "draft_changed",
                    result={"code": "handover_refused", "verified_count": answer.verified_count},
                )
            await self._close_dispatch(
                handover_dispatch, DispatchStatus.OK, None, {"verified_count": answer.verified_count}
            )

            async def record(connection: AsyncConnection, _: AttemptRecord) -> None:
                await FormDraftRepository(connection).transition(
                    draft_id=draft.id, expected_revision=None,
                    allowed_from=LIVE_DRAFT_STATUSES, to=DraftStatus.HANDED_OVER,
                )

            finished = await self._actions.finish_attempt(
                action_id, outcome=AttemptOutcome.SUCCEEDED,
                result={"code": CODE_HANDED_OVER, "verified_count": answer.verified_count},
                record=record,
            )
            self._state.clear(draft.profile_id)
            await self._pause(draft.task_id, PauseReason.USER_TAKEOVER, draft_id=str(draft.id))
            return finished
        finally:
            await client.aclose()

    # ---- read model --------------------------------------------------------------------------------

    async def latest_draft(self, task_id: uuid.UUID) -> DraftRecord | None:
        async with self._engine.connect() as connection:
            return await FormDraftRepository(connection).latest_for_task(task_id)

    async def handover_view(self, task_id: uuid.UUID) -> HandoverView | None:
        async with self._engine.connect() as connection:
            repository = ActionRepository(connection)
            candidates = [
                item for item in await repository.list_actions(task_id, limit=500)
                if item.tool_name == HANDOVER_TOOL
            ]
            if not candidates:
                return None
            action = candidates[-1]
            approvals = await repository.list_approvals(action.id)
            attempts = await repository.list_attempts(action.id)
        result = attempts[-1].result if attempts and attempts[-1].result else {}
        approval = approvals[-1] if approvals else None
        return HandoverView(
            action=action,
            proposal=HandoverProposal.model_validate(action.proposal),
            approval_status=approval.status.value if approval else None,
            approval_expires_at=approval.expires_at if approval else None,
            result_code=result.get("code") if isinstance(result, dict) else None,
        )

    def is_preparing(self, profile_id: uuid.UUID) -> bool:
        return self._state.is_preparing(profile_id)


def _parse_fill(outcome: Outcome) -> FillResult | None:
    """The worker's structured answer, or None if there is not a usable one."""
    raw = outcome.observation.get("result") if isinstance(outcome.observation, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return FillResult.model_validate(raw)
    except ValueError:
        return None


__all__ = ["FormDraftService", "HandoverView"]
