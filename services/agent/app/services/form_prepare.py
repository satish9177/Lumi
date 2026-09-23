"""Milestone 8b S5: form planning and the exact disclosure approval.

**This module changes no website and touches no browser.** It never opens one, never
calls the worker and never creates a dispatch. Its whole job is to turn "which saved
detail would go where" into an exact, reviewable, digest-bound approval. (Since Milestone
8b S6 the approval it builds funds one network-frozen local draft; carrying that out is
`FormDraftService`'s job, and `approve` below only hands the exact approval to it.)

```text
observed form (S4)
   -> trusted click: FORM PLANNING grant (a task grant, S3-style)
   -> planning context: bounded structure + MASKED previews, for ONE provider
   -> planner proposes `prepare_form`  (a proposal, not a worker operation)
   -> controller validates it deterministically, builds the DisclosureManifest
   -> trusted card shows the manifest; the user approves exactly that digest
   -> (S6) the approval funds ONE local draft, filled with the network frozen
   -> (S5, historical) `prepared_nothing`: the approval was spent and nothing happened
```

**Where the authority sits.** The planner proposes; this module authorises. The
provider is the grant's, chosen from the account-reading grant it grew out of; the
origin is derived from the profile and the observed page; the saved values never
leave `protected_values` (only digests and masked previews are read). The renderer
hands over an id and a revision and nothing else.

**What "still true at approval time" means.** Approval re-checks, in the approving
transaction and under the task lock: the grant is active and on the same account
and revoke epoch, the saved values still have the bound digests, and the observation
is still the newest for its tab (no later document or form epoch, same worker).
Persisted state cannot see an unobserved DOM change; S6's worker-live check, made
immediately before any write, is what covers that.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.action_status import ActionStatus, RiskTier
from app.domain.authenticated import AUTHENTICATED_READ_TASK_TYPE
from app.domain.authenticated_forms import ElementProjection
from app.domain.browser_profile import BrowserProfile, ProfileStatus
from app.domain.errors import (
    ActionNotFoundError,
    AuthenticatedGrantNotFoundError,
    AuthenticatedGrantNotUsableError,
    AuthenticatedProfileUnavailableError,
    TaskConcurrencyError,
    TaskKindMismatchError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.form_prepare import (
    FORM_PREPARE_TOOL,
    ProtectedSnapshot,
    MANIFEST_POLICY_VERSION,
    MAX_FORM_FIELDS,
    PREPARED_NOTHING,
    DisclosureManifest,
    FormPrepareRefusal,
    FormPrepareScope,
    ObservedForm,
    TextEntry,
    account_binding,
    parse_manifest,
    parse_prepare_form,
    resolve_fields,
    verify_protected_values_current,
)
from app.domain.protected_values import (
    PROTECTED_KINDS,
    canonicalize,
    is_protected_kind,
    preview,
    value_digest,
)
from app.domain.research import GrantStatus
from app.domain.task_status import TaskEventType, accepts_actions
from app.repositories.actions import ActionRecord, ActionRepository
from app.repositories.form_drafts import DraftRecord, FormDraftRepository
from app.repositories.authenticated import (
    AuthenticatedGrantRecord,
    AuthenticatedObservationRecord,
    AuthenticatedRepository,
)
from app.repositories.form_prepare import (
    FormPrepareGrantRecord,
    FormPrepareRepository,
    ProtectedValueRepository,
    SavedDetail,
)
from app.repositories.profiles import BrowserProfileRepository
from app.repositories.tasks import TaskRecord, TaskRepository
from app.repositories.workflows import WorkflowRepository
from app.services.actions import ActionService, ActionView
from app.services.form_state import FormStateRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only; form_draft imports this module.
    from app.services.form_draft import FormDraftService

logger = logging.getLogger("lumi.form_prepare")


@dataclass(frozen=True, slots=True)
class DisclosureView:
    """One exact disclosure approval, as the trusted card needs it."""

    action: ActionRecord
    manifest: DisclosureManifest
    approval_status: str | None
    approval_expires_at: Any
    #: What the spent approval ended in (`local_draft_prepared`, `local_draft_partial`,
    #: `local_draft_not_written`, or the historical S5 `prepared_nothing`); otherwise None.
    result_code: str | None
    #: `form-prepare-v2` (S6: approving fills, locally, frozen) or `form-prepare-v1` (S5,
    #: historical: approving did nothing, and can never be executed).
    executable: bool = False


@dataclass(frozen=True, slots=True)
class FormPlanView:
    task: TaskRecord
    objective: str
    site: str | None
    #: Every saved detail, kind and masked preview only.
    saved_details: tuple[SavedDetail, ...]
    grant: FormPrepareGrantRecord | None
    disclosure: DisclosureView | None
    #: Forms and elements in the newest observation (counts, for diagnostics).
    form_count: int
    candidate_element_count: int
    #: Milestone 8b S6. The newest local draft (any status), and whether the profile is
    #: currently in preparation mode (a headed window, nothing written).
    draft: DraftRecord | None = None
    preparing: bool = False
    #: Milestone 10 S4. `workflow` for the `form` step of a cross-app workflow: `saved_details` are then
    #: that workflow's adopted values (kind and masked preview only), never the global saved details.
    value_source: str = "saved_details"


@dataclass(frozen=True, slots=True)
class PlanningContext:
    """What ONE provider may see. Built only under a usable, confirmed grant."""

    grant_id: uuid.UUID
    recipient: str
    objective: str
    site_display: str
    observation_ref: str
    forms: tuple[dict[str, Any], ...]
    saved_data: tuple[dict[str, str], ...]


def _element_view(element: ElementProjection) -> dict[str, Any]:
    """The structural facts a planner needs. No value state, no locator, no name/id."""
    return {
        "element_ref": element.element_ref,
        "role": element.role,
        "control_type": element.control_type,
        "accessible_name": element.accessible_name,
        "required": element.required,
        "enabled": element.enabled,
        "visible": element.visible,
        "read_only": element.read_only,
        "max_length": element.max_length,
        "submit_like": element.submit_like,
        "option_refs": [{"ref": option.ref, "label": option.label} for option in element.option_refs],
    }


def _origin_for(profile: BrowserProfile, host: str | None) -> str:
    """The exact origin the observed page belongs to, from the profile's own list."""
    for origin in profile.allowed_origins:
        if host is not None and urlsplit(origin).hostname == host.lower():
            return origin.lower()
    raise FormPrepareRefusal("origin_changed")


def _is_current(
    record: AuthenticatedObservationRecord, records: list[AuthenticatedObservationRecord]
) -> str | None:
    """Why `record` is no longer the observation to plan against, or None if it is."""
    observation = record.observation
    reasons: set[str] = set()
    for other in records:
        later = other.observation
        if later.tab != observation.tab or later.kind != "page":
            continue
        if other.worker_generation != record.worker_generation and later.sequence > observation.sequence:
            reasons.add("stale_observation")
        elif later.document_epoch > observation.document_epoch:
            reasons.add("stale_document_epoch")
        elif later.document_epoch == observation.document_epoch and later.form_epoch > observation.form_epoch:
            reasons.add("stale_form_epoch")
    # The most fundamental reason first: a different browser, then a different
    # document, then a re-rendered form.
    for reason in ("stale_observation", "stale_document_epoch", "stale_form_epoch"):
        if reason in reasons:
            return reason
    return None


async def workflow_for_task(connection: AsyncConnection, task_id: uuid.UUID) -> uuid.UUID | None:
    """Milestone 10 S4. The workflow whose `form` step this task is, or None for an ordinary task.

    Decided by `workflow_steps`, which only the workflow controller writes, in the same transaction that
    creates the task. A workflow child task in any other role can never plan a form.
    """
    step = await WorkflowRepository(connection).step_for_task(task_id)
    if step is None:
        return None
    if step.role != "form":
        raise FormPrepareRefusal("not_a_form_step")
    return step.workflow_id


async def value_snapshots(
    connection: AsyncConnection, workflow_id: uuid.UUID | None, kinds: Any, *, lock: bool = False
) -> dict[str, ProtectedSnapshot]:
    """Digests and previews from exactly one source: the global saved details, or ONE live workflow's values."""
    if workflow_id is None:
        return await ProtectedValueRepository(connection).snapshots(kinds, lock=lock)
    return await WorkflowRepository(connection).snapshots(workflow_id, kinds, lock=lock)


async def require_workflow_live(connection: AsyncConnection, workflow_id: uuid.UUID | None) -> None:
    if workflow_id is not None and not await WorkflowRepository(connection).is_live(workflow_id):
        raise FormPrepareRefusal("workflow_not_active")


class FormPrepareService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        grant_ttl_seconds: int,
        forms: FormStateRegistry | None = None,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._grant_ttl = timedelta(seconds=grant_ttl_seconds)
        #: Milestone 8b S6: which profiles are in preparation mode or hold a local draft.
        self._forms = forms
        self._drafts: "FormDraftService | None" = None

    def attach_drafts(self, drafts: "FormDraftService") -> None:
        """Wire the service that carries an exact approval out (Milestone 8b S6)."""
        self._drafts = drafts

    # ---- saved details (the trusted, direct-user path) ------------------------

    async def save_detail(self, kind: str, raw_value: object) -> SavedDetail:
        """Save one detail the user typed directly. Returns kind and preview only.

        No model, voice turn or page reaches this: the route exists for the
        trusted desktop layer, takes a closed kind and one string, and the
        response never echoes the value.
        """
        canonical = canonicalize(kind, raw_value)
        async with self._engine.begin() as connection:
            return await ProtectedValueRepository(connection).upsert(
                kind=kind,
                canonical=canonical,
                digest=value_digest(canonical),
                preview=preview(kind, canonical),
            )

    async def list_details(self) -> list[SavedDetail]:
        async with self._engine.connect() as connection:
            details = await ProtectedValueRepository(connection).list_details()
        return sorted(details, key=lambda item: PROTECTED_KINDS.index(item.kind))

    # ---- reading ----------------------------------------------------------------

    async def describe(self, task_id: uuid.UUID) -> FormPlanView:
        async with self._engine.connect() as connection:
            return await self._view(connection, task_id)

    async def _task(self, connection: AsyncConnection, task_id: uuid.UUID) -> TaskRecord:
        task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
            raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
        return task

    async def _view(self, connection: AsyncConnection, task_id: uuid.UUID) -> FormPlanView:
        task = await self._task(connection, task_id)
        repository = FormPrepareRepository(connection)
        grant = await repository.open_grant_for_task(task_id) or await repository.latest_grant_for_task(
            task_id
        )
        workflow_id = await workflow_for_task(connection, task_id)
        if workflow_id is None:
            details = tuple(await ProtectedValueRepository(connection).list_details())
        else:
            live = await WorkflowRepository(connection).is_live(workflow_id)
            details = tuple(
                SavedDetail(kind=value.kind, preview=value.preview, updated_at=value.created_at)
                for value in await WorkflowRepository(connection).values(workflow_id)
                if live and not value.purged
            )
        records = await AuthenticatedRepository(connection).list_observations(task_id)
        pages = [record for record in records if record.observation.kind == "page"]
        newest = pages[-1].observation if pages else None
        profile = await self._profile(connection, task)
        return FormPlanView(
            draft=await FormDraftRepository(connection).latest_for_task(task_id),
            preparing=bool(
                profile is not None and self._forms is not None and self._forms.is_preparing(profile.id)
            ),
            task=task,
            objective=str(task.request.get("objective", "")),
            site=profile.site if profile is not None else None,
            saved_details=tuple(sorted(details, key=lambda item: PROTECTED_KINDS.index(item.kind))),
            grant=grant,
            disclosure=await self._latest_disclosure(connection, task_id),
            form_count=newest.inventory.form_count if newest else 0,
            candidate_element_count=newest.inventory.element_count if newest else 0,
            value_source="saved_details" if workflow_id is None else "workflow",
        )

    @staticmethod
    async def _profile(connection: AsyncConnection, task: TaskRecord) -> BrowserProfile | None:
        try:
            profile_id = uuid.UUID(str(task.request.get("profile_id")))
        except ValueError:
            return None
        return await BrowserProfileRepository(connection).get(profile_id)

    @staticmethod
    async def _latest_disclosure(
        connection: AsyncConnection, task_id: uuid.UUID
    ) -> DisclosureView | None:
        repository = ActionRepository(connection)
        candidates = [
            action
            for action in await repository.list_actions(task_id, limit=500)
            if action.tool_name == FORM_PREPARE_TOOL
        ]
        if not candidates:
            return None
        action = candidates[-1]
        approvals = await repository.list_approvals(action.id)
        approval = approvals[-1] if approvals else None
        attempts = await repository.list_attempts(action.id)
        result = attempts[-1].result if attempts and attempts[-1].result else {}
        manifest = parse_manifest(action.proposal)
        return DisclosureView(
            action=action,
            manifest=manifest,
            approval_status=approval.status.value if approval else None,
            approval_expires_at=approval.expires_at if approval else None,
            result_code=result.get("code") if isinstance(result, dict) else None,
            executable=manifest.policy_version == MANIFEST_POLICY_VERSION,
        )

    # ---- the planning scope -----------------------------------------------------

    async def _source_grant(
        self, connection: AsyncConnection, task_id: uuid.UUID
    ) -> AuthenticatedGrantRecord:
        repository = AuthenticatedRepository(connection)
        grant = await repository.open_grant_for_task(task_id)
        if grant is None or grant.status is not GrantStatus.ACTIVE:
            raise AuthenticatedGrantNotUsableError("account reading has not been allowed for this task")
        if await repository.grant_is_expired(grant.id):
            raise AuthenticatedGrantNotUsableError("the account-reading scope has expired")
        return grant

    async def prepare_scope(
        self, task_id: uuid.UUID, *, allowed_data_refs: list[str]
    ) -> FormPlanView:
        """Open the PENDING planning grant. Nothing is disclosed and nothing is opened.

        The provider is the account-reading grant's; the origin is derived from
        the profile and the newest observed page; the allowed refs are the user's
        selection, limited to details that are actually saved.
        """
        if not allowed_data_refs or not all(is_protected_kind(ref) for ref in allowed_data_refs):
            raise FormPrepareRefusal("data_ref_not_allowed")
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
                raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            repository = FormPrepareRepository(connection)
            profile = await self._profile(connection, task)
            if profile is not None and self._forms is not None:
                self._forms.assert_clean(profile.id)  # no new planning while a draft exists
            if await repository.open_grant_for_task(task_id) is not None:
                return await self._view(connection, task_id)
            source = await self._source_grant(connection, task_id)
            if profile is None or profile.is_deleted:
                raise AuthenticatedProfileUnavailableError("profile_not_found")
            if profile.status is not ProfileStatus.AUTHENTICATED or profile.account_fingerprint is None:
                raise AuthenticatedProfileUnavailableError("profile_not_authenticated")
            if (
                profile.revoke_epoch != source.profile_revoke_epoch
                or profile.account_fingerprint != source.scope.account_fingerprint
            ):
                raise FormPrepareRefusal("account_changed")
            records = await AuthenticatedRepository(connection).list_observations(task_id)
            pages = [
                record
                for record in records
                if record.observation.kind == "page" and record.observation.inventory.forms
            ]
            if not pages:
                raise FormPrepareRefusal("no_form_observed")
            workflow_id = await workflow_for_task(connection, task_id)
            await require_workflow_live(connection, workflow_id)
            saved = await value_snapshots(connection, workflow_id, allowed_data_refs)
            if set(allowed_data_refs) - set(saved):
                raise FormPrepareRefusal("data_ref_unavailable")
            scope = FormPrepareScope(
                profile_id=profile.id,
                site=profile.site,
                account_fingerprint=profile.account_fingerprint,
                profile_revoke_epoch=profile.revoke_epoch,
                source_authenticated_grant_id=source.id,
                planning_recipient=source.scope.disclosure.recipient,
                recipient_origin=_origin_for(profile, pages[-1].observation.host),
                allowed_data_refs=allowed_data_refs,
                workflow_id=workflow_id,
            )
            grant = await repository.insert_grant(grant_id=uuid.uuid4(), task_id=task_id, scope=scope)
            await self._event(
                connection,
                task,
                TaskEventType.TASK_FORM_PREPARE_SCOPE_REQUESTED,
                {
                    "grant_id": str(grant.id),
                    "grant_revision": grant.revision,
                    "grant_status": grant.status.value,
                    "scope_digest": grant.scope_digest,
                    "policy_version": grant.policy_version,
                    "allowed_data_ref_count": len(scope.allowed_data_refs),
                },
            )
            return await self._view(connection, task_id)

    async def confirm(
        self, task_id: uuid.UUID, *, grant_id: uuid.UUID, expected_revision: int
    ) -> FormPlanView:
        """The trusted click: the only way a planning grant becomes usable."""
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            repository = FormPrepareRepository(connection)
            grant = await repository.get_grant(grant_id)
            if grant is None or grant.task_id != task_id:
                raise AuthenticatedGrantNotFoundError(task_id)
            if grant.status is not GrantStatus.PENDING:
                raise AuthenticatedGrantNotUsableError(f"it is {grant.status.value.lower()}")
            if grant.revision != expected_revision:
                raise AuthenticatedGrantNotUsableError("it changed since you reviewed it")
            confirmed = await repository.confirm_grant(
                grant_id=grant.id,
                expected_revision=expected_revision,
                scope_digest=grant.scope_digest,
                ttl=self._grant_ttl,
            )
            if confirmed is None:
                raise AuthenticatedGrantNotUsableError(
                    "the account or its sign-in changed since you reviewed it"
                )
            await self._event(
                connection,
                task,
                TaskEventType.TASK_FORM_PREPARE_SCOPE_GRANTED,
                {
                    "grant_id": str(confirmed.id),
                    "grant_revision": confirmed.revision,
                    "grant_status": confirmed.status.value,
                    "scope_digest": confirmed.scope_digest,
                },
            )
            return await self._view(connection, task_id)

    async def revoke(
        self,
        task_id: uuid.UUID,
        *,
        reason: str,
        grant_id: uuid.UUID | None = None,
        expected_revision: int | None = None,
    ) -> FormPlanView:
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            repository = FormPrepareRepository(connection)
            grant = (
                await repository.get_grant(grant_id)
                if grant_id is not None
                else await repository.open_grant_for_task(task_id)
            )
            if grant is None or grant.task_id != task_id:
                raise AuthenticatedGrantNotFoundError(task_id)
            closed = grant
            if grant.status in (GrantStatus.PENDING, GrantStatus.ACTIVE):
                updated = await repository.close_grant(
                    grant_id=grant.id, status=GrantStatus.REVOKED, expected_revision=expected_revision
                )
                if updated is None:
                    raise AuthenticatedGrantNotUsableError("it changed since you reviewed it")
                closed = updated
            await self._event(
                connection,
                task,
                TaskEventType.TASK_FORM_PREPARE_SCOPE_REVOKED,
                {
                    "grant_id": str(closed.id),
                    "grant_revision": closed.revision,
                    "grant_status": closed.status.value,
                    "reason": reason,
                },
            )
            return await self._view(connection, task_id)

    @staticmethod
    async def _event(
        connection: AsyncConnection,
        task: TaskRecord,
        event_type: TaskEventType,
        payload: dict[str, Any],
    ) -> None:
        tasks = TaskRepository(connection)
        current = await tasks.get_task(task.id)
        assert current is not None
        advanced = await tasks.advance_task(task_id=current.id, expected_revision=current.revision)
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise TaskConcurrencyError(task.id)
        await tasks.append_event(task=advanced, event_type=event_type, payload=payload)

    # ---- the grant, as it stands right now ------------------------------------------

    async def _usable_grant(
        self, connection: AsyncConnection, task_id: uuid.UUID
    ) -> FormPrepareGrantRecord:
        repository = FormPrepareRepository(connection)
        grant = await repository.open_grant_for_task(task_id)
        if grant is None:
            raise AuthenticatedGrantNotFoundError(task_id)
        if grant.status is GrantStatus.PENDING:
            raise AuthenticatedGrantNotUsableError("it has not been granted")
        await self._require_grant_usable(connection, grant)
        return grant

    async def _require_grant_usable(
        self, connection: AsyncConnection, grant: FormPrepareGrantRecord
    ) -> None:
        """Raise the precise reason a grant cannot be used, decided by the database."""
        repository = FormPrepareRepository(connection)
        # Milestone 10 S4: a workflow grant is usable only while its workflow is, and only by its own
        # `form` step (the scope's workflow must still be the task's).
        if await workflow_for_task(connection, grant.task_id) != grant.scope.workflow_id:
            raise FormPrepareRefusal("not_a_form_step")
        await require_workflow_live(connection, grant.scope.workflow_id)
        if await repository.usable_now(grant.id):
            return
        profile = await BrowserProfileRepository(connection).get(grant.profile_id)
        if profile is None or profile.is_deleted:
            raise AuthenticatedProfileUnavailableError("profile_deleted")
        if (
            profile.revoke_epoch != grant.profile_revoke_epoch
            or profile.account_fingerprint != grant.scope.account_fingerprint
        ):
            raise FormPrepareRefusal("account_changed")
        if profile.status is not ProfileStatus.AUTHENTICATED:
            raise AuthenticatedProfileUnavailableError("login_required")
        if grant.status is not GrantStatus.ACTIVE:
            raise AuthenticatedGrantNotUsableError(f"it is {grant.status.value.lower()}")
        if await repository.grant_is_expired(grant.id):
            raise AuthenticatedGrantNotUsableError("it has expired")
        raise FormPrepareRefusal("grant_not_usable")

    # ---- the planning context ----------------------------------------------------------

    async def planning_context(self, task_id: uuid.UUID) -> PlanningContext:
        """Build what ONE provider may see. Refused unless the grant is usable now.

        Bounded structure of the newest observed forms, and the masked previews of
        the allowed saved details. The raw value is not read here at all.
        """
        async with self._engine.begin() as connection:
            tasks = TaskRepository(connection)
            task = await tasks.lock_task(task_id)
            if task is None:
                raise TaskNotFoundError(task_id)
            if task.request.get("type") != AUTHENTICATED_READ_TASK_TYPE:
                raise TaskKindMismatchError(task_id, AUTHENTICATED_READ_TASK_TYPE)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            grant = await self._usable_grant(connection, task_id)
            records = await AuthenticatedRepository(connection).list_observations(task_id)
            pages = [
                record
                for record in records
                if record.observation.kind == "page" and record.observation.inventory.forms
            ]
            if not pages:
                raise FormPrepareRefusal("no_form_observed")
            record = pages[-1]
            stale = _is_current(record, records)
            if stale is not None:
                raise FormPrepareRefusal(stale)
            observation = record.observation
            profile = await self._profile(connection, task)
            assert profile is not None
            if self._forms is not None:
                # No provider may be given a page's structure or a preview once anything is
                # written, and planning waits for the fresh, headed observation.
                self._forms.assert_clean(profile.id)
            if _origin_for(profile, observation.host) != grant.scope.recipient_origin:
                raise FormPrepareRefusal("origin_changed")
            saved = await value_snapshots(connection, grant.scope.workflow_id, grant.scope.allowed_data_refs)
            forms = tuple(
                {
                    "form_ref": form.ref,
                    "label": form.label,
                    "elements": [
                        _element_view(element)
                        for element in observation.inventory.elements
                        if element.form_ref == form.ref
                    ],
                }
                for form in observation.inventory.forms
            )
            await self._event(
                connection,
                task,
                TaskEventType.TASK_FORM_PLANNING_CONTEXT_BUILT,
                {
                    "grant_id": str(grant.id),
                    "protected_value_count": len(saved),
                    "allowed_data_ref_count": len(grant.scope.allowed_data_refs),
                    "form_count": len(forms),
                    "candidate_element_count": observation.inventory.element_count,
                },
            )
            return PlanningContext(
                grant_id=grant.id,
                recipient=grant.scope.planning_recipient,
                objective=str(task.request.get("objective", "")),
                site_display=profile.site,
                observation_ref=observation.ref,
                forms=forms,
                saved_data=tuple(
                    {"data_ref": kind, "kind": kind, "preview": saved[kind].preview}
                    for kind in grant.scope.allowed_data_refs
                    if kind in saved
                ),
            )

    # ---- the proposal -------------------------------------------------------------------

    async def propose(self, task_id: uuid.UUID, payload: Any, *, provider: str) -> FormPlanView:
        """Validate a `prepare_form` proposal, build the manifest, open the approval.

        Everything is refused **before** an action, an approval or a card exists.
        `provider` is the recipient the caller says produced the proposal; a
        proposal attributed to any other provider is refused.
        """
        proposal = parse_prepare_form(payload)
        async with self._engine.connect() as connection:
            task = await self._task(connection, task_id)
            if not accepts_actions(task.status):
                raise TaskNotAcceptingActionsError(task_id, task.status)
            grant = await self._usable_grant(connection, task_id)
            if provider != grant.scope.planning_recipient:
                raise FormPrepareRefusal("recipient_mismatch")
            records = await AuthenticatedRepository(connection).list_observations(task_id)
            profile = await self._profile(connection, task)
            assert profile is not None
            saved = await value_snapshots(connection, grant.scope.workflow_id, grant.scope.allowed_data_refs)
            provenance = (
                {}
                if grant.scope.workflow_id is None
                else {
                    value.kind: value.provenance
                    for value in await WorkflowRepository(connection).values(grant.scope.workflow_id)
                    if not value.purged
                }
            )
        record = next(
            (item for item in records if item.observation.ref == proposal.observation), None
        )
        if record is None or record.observation.kind != "page" or record.observation.tab is None:
            raise FormPrepareRefusal("unknown_observation")
        stale = _is_current(record, records)
        if stale is not None:
            raise FormPrepareRefusal(stale)
        observation = record.observation
        if self._forms is not None:
            self._forms.assert_clean(profile.id)
            state = self._forms.get(profile.id)
            if state is None or observation.observation_id not in state.observation_ids:
                # An approval built from a headless document could never be executed: the
                # draft lives in the headed preparation window and only an observation taken
                # there counts.
                raise FormPrepareRefusal("preparation_mode_required")
        if observation.schema_version != 2 or not any(
            form.ref == proposal.form_ref for form in observation.inventory.forms
        ):
            raise FormPrepareRefusal("wrong_form")
        origin = _origin_for(profile, observation.host)
        if origin != grant.scope.recipient_origin:
            raise FormPrepareRefusal("origin_changed")
        form = next(item for item in observation.inventory.forms if item.ref == proposal.form_ref)
        assert observation.tab is not None  # Checked with the record above.
        observed = ObservedForm(
            observation_id=observation.observation_id,
            observation_ref=observation.ref,
            tab=observation.tab,
            document_epoch=observation.document_epoch,
            form_epoch=observation.form_epoch,
            form_ref=form.ref,
            form_label=form.label,
            elements=tuple(observation.inventory.elements),
        )
        for entry in proposal.entries:
            if isinstance(entry, TextEntry) and entry.data_ref not in grant.scope.allowed_data_refs:
                raise FormPrepareRefusal("data_ref_not_allowed")
        fields = resolve_fields(
            proposal=proposal, observed=observed, scope=grant.scope, saved=saved
        )
        if grant.scope.workflow_id is not None:
            # Every placed value says where it came from; a value whose provenance is unknown is not placed.
            if any(field.data_ref is not None and field.data_ref not in provenance for field in fields):
                raise FormPrepareRefusal("data_ref_unavailable")
            fields = [
                field.model_copy(update={"provenance": provenance[field.data_ref]}) if field.data_ref else field
                for field in fields
            ]
        if len(fields) > MAX_FORM_FIELDS:  # pragma: no cover - resolve_fields bounds it.
            raise FormPrepareRefusal("too_many_entries")
        manifest = DisclosureManifest.build(
            task_id=task_id,
            profile_id=profile.id,
            form_prepare_grant_id=grant.id,
            planning_recipient=grant.scope.planning_recipient,
            site_display=profile.site,
            recipient_origin=origin,
            account_binding=account_binding(grant.scope.account_fingerprint),
            profile_revoke_epoch=grant.scope.profile_revoke_epoch,
            observation_id=observation.observation_id,
            observation_ref=observation.ref,
            tab=observation.tab,
            document_epoch=observation.document_epoch,
            form_epoch=observation.form_epoch,
            form_ref=form.ref,
            form_label=form.label,
            fields=fields,
            workflow_id=grant.scope.workflow_id,
        )
        await self._open_approval(task_id, manifest)
        return await self.describe(task_id)

    async def _open_approval(self, task_id: uuid.UUID, manifest: DisclosureManifest) -> None:
        """Supersede any open disclosure, record the exact proposal, ask for approval."""
        async with self._engine.connect() as connection:
            previous = [
                action
                for action in await ActionRepository(connection).list_actions(task_id, limit=500)
                if action.tool_name == FORM_PREPARE_TOOL
            ]
        for action in previous:
            if action.status in (ActionStatus.WAITING_APPROVAL, ActionStatus.PROPOSED):
                await self._actions.reject_action(action.id, reason="superseded")
        view, _ = await self._actions.propose_action(
            task_id,
            idempotency_key=f"{FORM_PREPARE_TOOL}-{len(previous) + 1}",
            tool_name=FORM_PREPARE_TOOL,
            risk_tier=RiskTier.R2,
            proposal=manifest.proposal(),
        )
        await self._actions.request_approval(view.action.id, expected_revision=view.action.revision)

    # ---- the exact approval ----------------------------------------------------------------

    async def _disclosure_action(
        self, connection: AsyncConnection, action_id: uuid.UUID
    ) -> tuple[ActionRecord, DisclosureManifest]:
        action = await ActionRepository(connection).get_action(action_id)
        if action is None:
            raise ActionNotFoundError(action_id)
        if action.tool_name != FORM_PREPARE_TOOL:
            raise FormPrepareRefusal("not_a_disclosure_approval")
        manifest = parse_manifest(action.proposal)
        return action, manifest

    async def approve(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """The trusted click. Exact, single-use, and (since S6) it fills -- locally, frozen.

        The renderer supplies an id and an expected revision; the manifest, the origin, the
        values and the fields all come from persisted state. Every fact the approval depends
        on is re-checked inside the approving transaction by `_guard`. What the click *does*
        is `FormDraftService.fill_approved`: freeze the network at two layers, write the
        approved values into the headed preparation window, verify them, and stay frozen.

        A `form-prepare-v1` manifest is a historical S5 approval, whose approval could only
        ever end in `prepared_nothing`. It is never executable, whatever its state.
        """
        async with self._engine.connect() as connection:
            _, manifest = await self._disclosure_action(connection, action_id)
        if manifest.policy_version != MANIFEST_POLICY_VERSION:
            raise FormPrepareRefusal("legacy_manifest_not_executable")
        if self._drafts is None:
            raise FormPrepareRefusal("preparation_mode_required")
        return await self._drafts.fill_approved(action_id, expected_revision=expected_revision)

    async def _guard(
        self, connection: AsyncConnection, action: ActionRecord, manifest: DisclosureManifest
    ) -> None:
        if manifest.compute_digest() != manifest.manifest_digest:  # pragma: no cover - re-derived.
            raise FormPrepareRefusal("manifest_invalid")
        if action.task_id != manifest.task_id:
            raise FormPrepareRefusal("manifest_invalid")
        repository = FormPrepareRepository(connection)
        grant = await repository.get_grant(manifest.form_prepare_grant_id)
        if grant is None or grant.task_id != action.task_id or grant.profile_id != manifest.profile_id:
            raise AuthenticatedGrantNotFoundError(action.task_id)
        await self._require_grant_usable(connection, grant)
        if (
            grant.scope.profile_revoke_epoch != manifest.profile_revoke_epoch
            or account_binding(grant.scope.account_fingerprint) != manifest.account_binding
        ):
            raise FormPrepareRefusal("account_changed")
        if (
            grant.scope.recipient_origin != manifest.recipient_origin
            or grant.scope.planning_recipient != manifest.planning_recipient
        ):
            raise FormPrepareRefusal("origin_changed")
        if grant.scope.workflow_id != manifest.workflow_id:
            raise FormPrepareRefusal("manifest_invalid")
        profile = await BrowserProfileRepository(connection).get(grant.profile_id)
        assert profile is not None
        if _origin_for(profile, urlsplit(manifest.recipient_origin).hostname) != manifest.recipient_origin:
            raise FormPrepareRefusal("origin_changed")
        # Saved values: share-locked so an update waits for this decision.
        saved = await value_snapshots(connection, manifest.workflow_id, manifest.data_refs, lock=True)
        verify_protected_values_current(
            manifest, {kind: snapshot.value_digest for kind, snapshot in saved.items()}
        )
        if manifest.workflow_id is not None:
            provenance = {
                value.kind: value.provenance
                for value in await WorkflowRepository(connection).values(manifest.workflow_id)
                if not value.purged
            }
            for field in manifest.fields:
                if field.data_ref is not None and provenance.get(field.data_ref) != field.provenance:
                    raise FormPrepareRefusal("protected_value_changed")
        records = await AuthenticatedRepository(connection).list_observations(action.task_id)
        record = next(
            (item for item in records if item.observation.observation_id == manifest.observation_id),
            None,
        )
        if record is None:
            raise FormPrepareRefusal("stale_observation")
        stale = _is_current(record, records)
        if stale is not None:
            raise FormPrepareRefusal(stale)
        if (
            record.observation.document_epoch != manifest.document_epoch
            or record.observation.form_epoch != manifest.form_epoch
        ):
            raise FormPrepareRefusal("stale_form_epoch")

    async def reject(self, action_id: uuid.UUID, *, expected_revision: int) -> ActionView:
        """The user declines. A rejected disclosure authorises nothing, ever."""
        async with self._engine.connect() as connection:
            await self._disclosure_action(connection, action_id)
        return await self._actions.reject_action(
            action_id, expected_revision=expected_revision, reason="user_declined"
        )


__all__ = [
    "DisclosureView",
    "FormPlanView",
    "FormPrepareService",
    "PlanningContext",
]
