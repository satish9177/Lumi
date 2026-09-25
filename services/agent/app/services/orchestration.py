"""Milestone 11 S2: the durable orchestration controller.

**It is not a planner.** It holds no tool of its own and never decides what happens next -- that is
Electron main's `OrchestrationPlanner`, the one place in this codebase a model is asked to choose a next
step, exactly like `ResearchPlanner`/`DesktopPlanner`/`FormPlanner` before it. This service only:

* creates and reads the durable graph (`orchestrations`, `orchestration_steps`);
* validates a chosen capability id against the closed catalog and this runtime's honestly-scoped composed
  subset (`app/domain/orchestration.py`);
* for a **task-backed** capability, links a child task the CALLER already created through that capability's
  own existing boundary (the same call a direct request would make) and reads that task's OWN service for
  its current resolution -- it never creates, grants or advances that child task itself;
* for a **synchronous** capability, records a bounded result summary the caller already computed through
  that capability's own existing read method;
* enforces budgets (steps, child tasks, planner calls) and a simple repeat-capability loop guard, pausing
  rather than silently widening a limit or guessing a step.

Every capability's own approval/grant/disclosure boundary is unchanged and unskippable: choosing
`public_research` here does exactly what typing a research request today does -- it shows the same trusted
scope card, and nothing is searched or read until the same trusted click.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncEngine

from app.desktop.protocol import APP_ID_PATTERN
from app.domain.action_status import ActionStatus
from app.domain.authenticated import PauseReason as AuthenticatedPauseReason
from app.domain.desktop_actions import ScrollProposal
from app.domain.errors import TaskNotFoundError
from app.domain.orchestration import (
    COMPOSED_CAPABILITY_IDS,
    DESKTOP_LAUNCH_OPERATIONS,
    DESKTOP_SAFE_ACTION_OPERATIONS,
    EXPECTED_TASK_TYPE,
    MAX_CHILD_TASKS,
    MAX_PLANNER_CALLS,
    MAX_STEPS,
    ORCHESTRATION_TTL_SECONDS,
    REPEATABLE_CAPABILITY_IDS,
    SYNCHRONOUS_CAPABILITY_IDS,
    TASK_BACKED_CAPABILITY_IDS,
    OrchestrationRefusal,
    OrchestrationStatus,
    StepStatus,
    bounded_summary,
    result_handle,
    validate_capability_id,
    validate_objective,
)
from app.domain.orchestration_resources import (
    CAPABILITY_OUTPUT_RESOURCE,
    CAPABILITY_RESOURCE_REQUIREMENTS,
    REGISTERABLE_RESOURCE_KINDS,
    next_ref,
    parse_desktop_target_backing,
    safe_label,
    trusted_input_label,
    validate_resource_refs,
)
from app.repositories.orchestration import OrchestrationRecord, OrchestrationRepository, StepRecord
from app.repositories.orchestration_resources import OrchestrationResourceRepository, ResourceRecord
from app.repositories.tasks import TaskRepository
from app.services.authenticated_read import AuthenticatedReadService
from app.services.desktop import DesktopService
from app.services.desktop_actions import DesktopActionError, DesktopActionService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.projects import ProjectService
from app.services.research_tasks import ResearchService

#: capability_id -> the label a result handle carries (`research_result:3`). Closed, spelled here.
_OUTPUT_CLASS: dict[str, str] = {
    "public_research": "research_result", "project_status": "project_status", "project_start": "project_run_result",
    "document_read": "document_result", "document_compare": "document_comparison", "account_read": "account_result",
    "desktop_observe": "desktop_observation", "desktop_reason": "desktop_result",
    "desktop_safe_action": "desktop_action_result", "launch_registered_app": "desktop_launch_result",
    "project_stop": "project_run_result",
}

#: Milestone 12 S3. `AuthenticatedReadService`'s own, already-reviewed pause reasons that mean "a human must
#: act outside Lumi" -- mapped onto the orchestration's one coarse `manual_handoff_required` reason. Every
#: value here is something `AuthenticatedReadService` already detects deterministically today; nothing new
#: is invented to widen coverage (there is no separate CAPTCHA signal -- see `app/domain/orchestration.py`).
_MANUAL_HANDOFF_REASONS: Final[frozenset[AuthenticatedPauseReason]] = frozenset(
    {
        AuthenticatedPauseReason.LOGIN_REQUIRED,
        AuthenticatedPauseReason.ACCOUNT_CHANGED,
        AuthenticatedPauseReason.ACCOUNT_IDENTITY_UNKNOWN,
        AuthenticatedPauseReason.LEFT_SITE_SCOPE,
    }
)

#: Controller-authored, template-only safe instructions -- never account page text, a title or a URL. Shown
#: verbatim in the orchestration step's own bounded `result_summary`, so the cockpit can display a real
#: instruction without opening the linked account-reading task's own panel.
_MANUAL_HANDOFF_INSTRUCTIONS: Final[dict[AuthenticatedPauseReason, str]] = {
    AuthenticatedPauseReason.LOGIN_REQUIRED: (
        "Manual action required: sign in to the account in the Lumi browser, completing any verification "
        "the site asks for (including a CAPTCHA). When finished, return here and choose Continue."
    ),
    AuthenticatedPauseReason.ACCOUNT_CHANGED: (
        "Manual action required: Lumi found a different signed-in account than the one this task started "
        "with. Check the account in the Lumi browser, then return here and choose Continue."
    ),
    AuthenticatedPauseReason.ACCOUNT_IDENTITY_UNKNOWN: (
        "Manual action required: Lumi could not tell which account is signed in. Check the account in the "
        "Lumi browser, then return here and choose Continue."
    ),
    AuthenticatedPauseReason.LEFT_SITE_SCOPE: (
        "Manual action required: the page moved outside the site this task approved. If you need to browse "
        "elsewhere yourself, do that in the Lumi browser, then return here and choose Continue."
    ),
}

#: `RunView.phase` (`app/services/projects.py`'s own closed vocabulary) -> this step's resolution. A run
#: that is alive in any form (starting/running/ready) already counts as the effect having happened; whether
#: it is *ready* is what the separate, synchronous `project_status` capability reports.
_PROJECT_RUN_ALIVE_PHASES: Final = frozenset({"starting", "running", "ready", "succeeded"})
_PROJECT_RUN_FAILED_PHASES: Final = frozenset({"declined", "expired", "failed", "stopped", "ended_with_runtime"})

_MAX_TEXT = 2000


@dataclass(frozen=True, slots=True)
class OrchestrationView:
    orchestration: OrchestrationRecord
    live: bool
    steps: tuple[StepRecord, ...]
    #: What this runtime can execute right now -- shown to the planner as its allowed step choices.
    available_capabilities: tuple[str, ...]
    #: Milestone 12 S1: resources this orchestration currently owns and may cite (not consumed, not
    #: expired). Seeing one here is never authority to use it with any particular capability --
    #: `CAPABILITY_RESOURCE_REQUIREMENTS` decides that, independently, every time `advance()` is called.
    resources: tuple[ResourceRecord, ...] = ()


class OrchestrationService:
    def __init__(
        self, engine: AsyncEngine, *, research: ResearchService, project: ProjectService,
        authenticated: AuthenticatedReadService, desktop: DesktopService,
        desktop_disclosure: DesktopDisclosureService, desktop_action: DesktopActionService,
    ) -> None:
        self._engine = engine
        self._research = research
        self._project = project
        self._authenticated = authenticated
        self._desktop = desktop
        self._desktop_disclosure = desktop_disclosure
        self._desktop_action = desktop_action

    # ---- reads -------------------------------------------------------------------------------------

    async def create(self, *, objective: object) -> OrchestrationView:
        goal = validate_objective(objective)
        async with self._engine.begin() as connection:
            record = await OrchestrationRepository(connection).insert(
                orchestration_id=uuid.uuid4(), objective=goal, ttl=timedelta(seconds=ORCHESTRATION_TTL_SECONDS)
            )
            return await self._view(connection, record.id)

    async def describe(self, orchestration_id: uuid.UUID) -> OrchestrationView:
        async with self._engine.connect() as connection:
            record = await OrchestrationRepository(connection).get(orchestration_id)
            if record is None:
                raise OrchestrationRefusal("orchestration_not_found")
            return await self._view(connection, orchestration_id)

    async def latest(self) -> OrchestrationView | None:
        async with self._engine.connect() as connection:
            record = await OrchestrationRepository(connection).latest()
            return None if record is None else await self._view(connection, record.id)

    async def _view(self, connection: Any, orchestration_id: uuid.UUID) -> OrchestrationView:
        repository = OrchestrationRepository(connection)
        record = await repository.get(orchestration_id)
        assert record is not None
        steps = await repository.steps(orchestration_id)
        live = await repository.is_live(orchestration_id)
        available: tuple[str, ...] = ()
        if live and record.status == OrchestrationStatus.RUNNING:
            already_succeeded = {
                step.capability_id for step in steps
                if step.status == StepStatus.SUCCEEDED and step.capability_id not in REPEATABLE_CAPABILITY_IDS
            }
            available = tuple(sorted(COMPOSED_CAPABILITY_IDS - already_succeeded))
        resources = await OrchestrationResourceRepository(connection).available(orchestration_id)
        return OrchestrationView(
            orchestration=record, live=live, steps=tuple(steps), available_capabilities=available,
            resources=tuple(resources),
        )

    # ---- registering a trusted input resource -----------------------------------------------------------

    async def register_resource(
        self,
        orchestration_id: uuid.UUID,
        *,
        expected_revision: int,
        kind: object,
        safe_label_text: object,
        backing_id: object = None,
        backing_text: object = None,
        document_task_id: object = None,
    ) -> OrchestrationView:
        """Milestone 12 S2: makes a resource available that a trusted action outside the planner loop
        provided -- an already-approved document the user picked, or a URL Lumi already policy-checked --
        never a capability's own result. The model never reaches this method; only Electron main calls it,
        after it has already performed the trusted action (adding a file to a document task, checking a URL)
        through that capability's own existing boundary."""
        if not isinstance(kind, str) or kind not in REGISTERABLE_RESOURCE_KINDS:
            raise OrchestrationRefusal("resources_invalid")
        label = trusted_input_label(_require_text(safe_label_text, code="resources_invalid"))

        if kind == "document_ref":
            if backing_text is not None:
                raise OrchestrationRefusal("resources_invalid")
            file_id = _require_uuid(backing_id, code="resources_invalid")
            task_id = _require_uuid(document_task_id, code="resources_invalid")
            async with self._engine.begin() as connection:
                repository = OrchestrationRepository(connection)
                # `_live` takes the same row lock `_live_running` does, so nothing else can set
                # `document_task_id` between this read and the write below -- there is no race to fence.
                record = await self._live(repository, orchestration_id, expected_revision)
                if record.document_task_id is None:
                    await repository.set_document_task_id(orchestration_id, document_task_id=task_id)
                elif record.document_task_id != task_id:
                    raise OrchestrationRefusal("document_task_mismatch")
                resource_repository = OrchestrationResourceRepository(connection)
                ref = next_ref(await resource_repository.count(orchestration_id))
                await resource_repository.mint(
                    orchestration_id=orchestration_id, ref=ref, kind="document_ref", producing_step_id=None,
                    privacy_class="private", safe_label=label, backing_id=file_id,
                )
                return await self._view(connection, orchestration_id)

        if kind == "account_context_ref":
            # Milestone 12 S3. `backing_id` is a `browser_profiles` id, not a document/file id -- reusing the
            # same model-invisible column `document_ref` already uses, for the same reason (an opaque backing
            # identity a capability's own dispatch resolves, never shown to the planner). Never a
            # `document_task_id`: that column is document-only.
            if backing_text is not None or document_task_id is not None:
                raise OrchestrationRefusal("resources_invalid")
            profile_id = _require_uuid(backing_id, code="resources_invalid")
            # The same deterministic checks a direct `authenticated_read` task creation already passes
            # through (signed in, not deleted, not mid-takeover, not leased by another runtime generation).
            # Raises one of `AuthenticatedProfileUnavailableError`/`AuthenticatedReadNotConfiguredError` on
            # its own, already-reviewed terms; never a new one invented here.
            await self._authenticated.check_profile(profile_id)
            async with self._engine.begin() as connection:
                repository = OrchestrationRepository(connection)
                await self._live(repository, orchestration_id, expected_revision)
                resource_repository = OrchestrationResourceRepository(connection)
                ref = next_ref(await resource_repository.count(orchestration_id))
                await resource_repository.mint(
                    orchestration_id=orchestration_id, ref=ref, kind="account_context_ref", producing_step_id=None,
                    privacy_class="private", safe_label=label, backing_id=profile_id,
                )
                return await self._view(connection, orchestration_id)

        if kind == "desktop_target_ref":
            # Milestone 12 S4. `backing_text` carries the whole identity (`worker_generation|surface_ref
            # |surface_epoch`): a desktop target is three fields, not one uuid, and the schema allows only
            # one of `backing_id`/`backing_text` at a time. Re-listed and re-matched fresh, right now, under
            # THIS request -- never trusted from whatever the renderer's own, separately-timed listing showed.
            if backing_id is not None:
                raise OrchestrationRefusal("resources_invalid")
            clean_backing_text = _require_text(backing_text, code="resources_invalid", limit=100)
            target = parse_desktop_target_backing(clean_backing_text)
            listing = await self._desktop.list_surfaces()
            if str(listing.worker_generation) != target.worker_generation or not any(
                surface.surface_ref == target.surface_ref and surface.surface_epoch == target.surface_epoch
                for surface in listing.surfaces
            ):
                raise OrchestrationRefusal("desktop_target_unavailable")
            async with self._engine.begin() as connection:
                repository = OrchestrationRepository(connection)
                await self._live(repository, orchestration_id, expected_revision)
                resource_repository = OrchestrationResourceRepository(connection)
                ref = next_ref(await resource_repository.count(orchestration_id))
                await resource_repository.mint(
                    orchestration_id=orchestration_id, ref=ref, kind="desktop_target_ref", producing_step_id=None,
                    privacy_class="private", safe_label=label, backing_text=clean_backing_text,
                )
                return await self._view(connection, orchestration_id)

        if kind == "app_ref":
            # Milestone 12 S4. The app id names a registered application only; `DesktopActionService
            # .propose_launch` independently re-checks the registry again at dispatch, so a registration Lumi
            # accepted here can never itself widen what may be launched.
            if backing_id is not None:
                raise OrchestrationRefusal("resources_invalid")
            app_id = _require_text(backing_text, code="resources_invalid", limit=32)
            if not APP_ID_PATTERN.fullmatch(app_id):
                raise OrchestrationRefusal("resources_invalid")
            async with self._engine.begin() as connection:
                repository = OrchestrationRepository(connection)
                await self._live(repository, orchestration_id, expected_revision)
                resource_repository = OrchestrationResourceRepository(connection)
                ref = next_ref(await resource_repository.count(orchestration_id))
                await resource_repository.mint(
                    orchestration_id=orchestration_id, ref=ref, kind="app_ref", producing_step_id=None,
                    privacy_class="none", safe_label=label, backing_text=app_id,
                )
                return await self._view(connection, orchestration_id)

        if kind == "project_ref":
            # Milestone 12 S4. `backing_id` is the owned run's own task id. Re-confirmed live right now,
            # under this request, exactly like `desktop_target_ref`'s own fresh re-check --
            # `ProjectService.stop()` independently re-resolves and re-checks ownership/phase again at
            # dispatch, but a registration should not itself mint a resource naming a task that does not
            # exist, is not a project run, or has already ended.
            if backing_text is not None:
                raise OrchestrationRefusal("resources_invalid")
            task_id = _require_uuid(backing_id, code="resources_invalid")
            run_view = await self._project.describe(task_id)
            if run_view.phase not in ("starting", "running", "ready"):
                raise OrchestrationRefusal("project_run_not_live")
            async with self._engine.begin() as connection:
                repository = OrchestrationRepository(connection)
                await self._live(repository, orchestration_id, expected_revision)
                resource_repository = OrchestrationResourceRepository(connection)
                ref = next_ref(await resource_repository.count(orchestration_id))
                await resource_repository.mint(
                    orchestration_id=orchestration_id, ref=ref, kind="project_ref", producing_step_id=None,
                    privacy_class="none", safe_label=label, backing_id=task_id,
                )
                return await self._view(connection, orchestration_id)

        # public_url_ref
        if backing_id is not None:
            raise OrchestrationRefusal("resources_invalid")
        url = _require_text(backing_text, code="resources_invalid", limit=2048)
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            await self._live(repository, orchestration_id, expected_revision)
            resource_repository = OrchestrationResourceRepository(connection)
            ref = next_ref(await resource_repository.count(orchestration_id))
            await resource_repository.mint(
                orchestration_id=orchestration_id, ref=ref, kind="public_url_ref", producing_step_id=None,
                privacy_class="public", safe_label=label, backing_text=url,
            )
            return await self._view(connection, orchestration_id)

    # ---- planner-call budget -------------------------------------------------------------------------

    async def record_planner_call(self, orchestration_id: uuid.UUID, *, expected_revision: int) -> OrchestrationView:
        """Counted the moment a planner call is about to be made, whatever it returns. Pauses on budget
        exhaustion instead of letting an unbounded number of calls run up against nothing."""
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            record = await self._live_running(repository, orchestration_id, expected_revision)
            if record.planner_calls >= MAX_PLANNER_CALLS:
                await repository.pause(orchestration_id, reason="budget_exhausted")
                return await self._view(connection, orchestration_id)
            await repository.record_planner_call(orchestration_id)
            return await self._view(connection, orchestration_id)

    # ---- advancing one step ---------------------------------------------------------------------------

    async def advance(
        self,
        orchestration_id: uuid.UUID,
        *,
        expected_revision: int,
        capability_id: object,
        task_id: object = None,
        resolved_summary: object = None,
        resources: object = None,
        result_backing_id: object = None,
    ) -> OrchestrationView:
        capability = validate_capability_id(capability_id)
        resource_refs = validate_resource_refs(resources)

        if capability not in COMPOSED_CAPABILITY_IDS:
            # A real catalog id (Milestone 11 S1); this runtime has not composed it yet. Pause with an
            # honest reason rather than refusing outright or pretending to execute it.
            return await self._pause(orchestration_id, expected_revision=expected_revision, reason="capability_unavailable")

        # Milestone 12 S1: a capability's own resource requirement is closed, static data -- checked before
        # any resource is even looked up, so citing a resource against a capability that accepts none is
        # refused the same way whether or not the cited ref happens to exist. "The planner can see r1" is
        # never, by itself, "the planner may use r1 with this capability".
        requirement = CAPABILITY_RESOURCE_REQUIREMENTS.get(capability, ())
        if resource_refs and not requirement:
            raise OrchestrationRefusal("resources_not_supported")
        if requirement and len(resource_refs) != len(requirement):
            raise OrchestrationRefusal("resources_not_supported")

        # Milestone 12 S2: some outputs (document_read's document_result_ref) need a backing id only main
        # can supply, since main already performed the real extraction/comparison. Required exactly when the
        # capability's own output spec says so -- never optional, never guessed, never model-supplied.
        output_spec = CAPABILITY_OUTPUT_RESOURCE.get(capability)
        result_backing_uuid: uuid.UUID | None = None
        if output_spec is not None and output_spec.needs_backing_id:
            result_backing_uuid = _require_uuid(result_backing_id, code="result_backing_id_required")
        elif result_backing_id is not None:
            raise OrchestrationRefusal("result_backing_id_not_allowed")

        if capability in TASK_BACKED_CAPABILITY_IDS:
            if resolved_summary is not None:
                raise OrchestrationRefusal("resolved_summary_not_allowed")
            task_uuid = _require_uuid(task_id, code="task_id_required")
            status, summary, pause_reason, pending_note = await self._read_task_backed_resolution(capability, task_uuid)
            return await self._commit_step(
                orchestration_id, expected_revision=expected_revision, capability=capability,
                child_task_id=task_uuid, status=status, summary=summary, pause_reason=pause_reason,
                pending_note=pending_note,
                resource_refs=resource_refs, requirement=requirement, result_backing_id=result_backing_uuid,
            )

        # Synchronous (SYNCHRONOUS_CAPABILITY_IDS): the caller already computed the result through that
        # capability's own existing read method.
        if task_id is not None:
            raise OrchestrationRefusal("task_id_not_allowed")
        summary = bounded_summary(_require_text(resolved_summary, code="resolved_summary_required"))
        return await self._commit_step(
            orchestration_id, expected_revision=expected_revision, capability=capability,
            child_task_id=None, status=StepStatus.SUCCEEDED, summary=summary, pause_reason=None,
            resource_refs=resource_refs, requirement=requirement, result_backing_id=result_backing_uuid,
        )

    async def _read_task_backed_resolution(
        self, capability: str, task_id: uuid.UUID
    ) -> tuple[StepStatus, str | None, str | None, str | None]:
        """Read-only, outside any write transaction: this runtime's own current record of a task-backed
        capability's resolution. Never creates, grants or advances anything. The third element is the pause
        reason to use if the step stays unresolved (`AWAITING_APPROVAL` always -> `approval_required`;
        `PENDING`'s reason depends on *why* it is still pending). The fourth (Milestone 12 S3) is a bounded,
        controller-authored note for a step that is STILL unresolved (`manual_handoff_required`'s safe
        instruction) -- always `None` when `status` is `SUCCEEDED`/`FAILED`, since only the second element
        (`summary`) ever describes a resolved step."""
        async with self._engine.connect() as connection:
            task = await TaskRepository(connection).get_task(task_id)
        if task is None:
            raise OrchestrationRefusal("task_not_found")
        if task.request.get("type") != EXPECTED_TASK_TYPE[capability]:
            raise OrchestrationRefusal("task_kind_mismatch")
        if capability == "public_research":
            try:
                view = await self._research.describe(task_id)
            except TaskNotFoundError:
                raise OrchestrationRefusal("task_not_found") from None
            if view.answer is not None:
                text = f"{view.answer.answer.status}: {view.answer.answer.answer}"
                return StepStatus.SUCCEEDED, bounded_summary(text), None, None
            if view.grant is None or view.grant.status.value == "PENDING":
                return StepStatus.AWAITING_APPROVAL, None, "approval_required", None
            if view.grant.status.value in ("REVOKED", "EXPIRED"):
                # Declined, cancelled, or its window closed unused. Never revived; a new attempt is a new
                # step, not a resumed one -- exactly like a fresh research grant is a new confirmation.
                return StepStatus.FAILED, bounded_summary("The research scope was declined, expired, or the task was cancelled."), None, None
            if view.unresolved_step:
                # A step of this task has no outcome Lumi can stand behind (interrupted mid-flight, a lost
                # response). Distinct from "still working": a human may need to look, not just wait.
                return StepStatus.PENDING, None, "outcome_unknown", None
            # ACTIVE, no answer recorded yet, and nothing unresolved: ordinary work in progress. Stay
            # unresolved; the caller's own research loop, and a later `resume`, decide when this changes.
            return StepStatus.PENDING, None, "approval_required", None
        if capability == "project_start":
            try:
                run_view = await self._project.describe(task_id)
            except TaskNotFoundError:
                raise OrchestrationRefusal("task_not_found") from None
            if run_view.phase == "awaiting_approval":
                return StepStatus.AWAITING_APPROVAL, None, "approval_required", None
            if run_view.phase in _PROJECT_RUN_FAILED_PHASES:
                return StepStatus.FAILED, bounded_summary(f"The project run ended: {run_view.phase}."), None, None
            if run_view.phase in _PROJECT_RUN_ALIVE_PHASES:
                text = f"Project run started (phase: {run_view.phase}){', ready' if run_view.ready else ''}."
                return StepStatus.SUCCEEDED, bounded_summary(text), None, None
            if run_view.phase == "outcome_unknown":
                return StepStatus.PENDING, None, "outcome_unknown", None
            # "approved": the grant is active but the caller has not yet called start(), or start() has not
            # finished. Stay unresolved; never guessed here. The caller (Electron main) is what actually
            # calls start() once the grant is active -- this read never does, matching "it never creates,
            # grants or advances that child task itself".
            return StepStatus.PENDING, None, "approval_required", None
        if capability == "account_read":
            try:
                account_view = await self._authenticated.describe(task_id)
            except TaskNotFoundError:
                raise OrchestrationRefusal("task_not_found") from None
            if account_view.answer is not None:
                # Template-only, exactly like `document_read`'s own character-count summary: the answer's
                # closed-vocabulary status and a quoted-evidence count, never the answer text itself. Unlike
                # `public_research` (public web content), this is `account_private` -- the confirmed scope
                # card's own recipient is the only provider the user approved to see it, and `result_summary`
                # is what `resultLines` feeds back into a SEPARATE, independently-configured provider
                # (`orchestration_planning`) on every later planner tick.
                answer = account_view.answer.answer
                evidence_count = len(answer.evidence)
                text = (
                    f"Account read finished: {answer.status} "
                    f"({evidence_count} quoted item{'s' if evidence_count != 1 else ''} of evidence)."
                )
                return StepStatus.SUCCEEDED, bounded_summary(text), None, None
            if account_view.grant is None or account_view.grant.status.value == "PENDING":
                # The ordinary "waiting on the trusted scope card" pause -- identical in shape to
                # `public_research`'s own first pause, never `manual_handoff_required` (nothing outside Lumi
                # is needed yet; the person just has not clicked Confirm on this task's own card).
                return StepStatus.AWAITING_APPROVAL, None, "approval_required", None
            if account_view.grant.status.value in ("REVOKED", "EXPIRED"):
                # Declined, expired, or made permanently unusable by a re-observe that found the wrong
                # account or a dead grant (Electron main's own Continue-time nudge revokes it explicitly --
                # see `docs/reviews/milestone-12-s3.md`). Never revived: a fresh account_read step would need
                # a brand new scope card over a fresh `account_context_ref`, exactly like research.
                return StepStatus.FAILED, bounded_summary(
                    "The account-reading permission was declined, expired, or could no longer be used, and "
                    "was not continued."
                ), None, None
            if account_view.pause_reason is not None and account_view.pause_reason in _MANUAL_HANDOFF_REASONS:
                return (
                    StepStatus.PENDING, None, "manual_handoff_required",
                    bounded_summary(_MANUAL_HANDOFF_INSTRUCTIONS[account_view.pause_reason]),
                )
            if account_view.unresolved_step:
                return StepStatus.PENDING, None, "outcome_unknown", None
            # ACTIVE, no answer recorded yet, no pause, nothing unresolved: ordinary work in progress.
            return StepStatus.PENDING, None, "approval_required", None
        if capability == "desktop_reason":
            try:
                read_view = await self._desktop_disclosure.describe(task_id)
            except TaskNotFoundError:
                raise OrchestrationRefusal("task_not_found") from None
            if read_view.answer is not None:
                # Template-only, exactly like `account_read`'s own fix (`docs/reviews/milestone-12-s3.md`):
                # the answer's closed-vocabulary kind and a quoted-evidence count, never `answer.answer`
                # itself -- that text is `desktop_private`, and the confirmed disclosure card's one named
                # recipient is the only provider the user approved to see it. `result_summary` is what later
                # feeds the separately-configured `orchestration_planning` model on every planner tick.
                evidence_count = len(read_view.answer.evidence)
                text = (
                    f"Desktop reasoning finished: {read_view.answer.kind} "
                    f"({evidence_count} quoted item{'s' if evidence_count != 1 else ''} of evidence)."
                )
                return StepStatus.SUCCEEDED, bounded_summary(text), None, None
            if read_view.phase in ("failed", "declined", "expired"):
                # Declined, expired, or a failed/ungrounded provider attempt. Never revived: a fresh
                # `desktop_reason` step needs a brand new observation and a brand new disclosure card, over a
                # fresh `desktop_target_ref`, exactly like `account_read` and `public_research`.
                return StepStatus.FAILED, bounded_summary(
                    "Desktop reasoning was declined, expired, or did not complete, and was not continued."
                ), None, None
            if read_view.phase == "outcome_unknown":
                return StepStatus.PENDING, None, "outcome_unknown", None
            # awaiting_approval / approved / reasoning: the ordinary wait on the existing desktop-read card
            # (Allow, then Send) -- identical in shape to `public_research`'s own first pause.
            return StepStatus.PENDING, None, "approval_required", None
        if capability in ("desktop_safe_action", "launch_registered_app"):
            # Both link the SAME `desktop_action` task type S3's focus/scroll/launch and S4's mutations all
            # share -- so the linked task's own `operation` is re-checked here, never trusted from
            # `EXPECTED_TASK_TYPE` alone, before this capability is allowed to resolve as its result. A
            # `set_control_value`/`select_control`/`invoke_control` task can never satisfy either check.
            allowed_operations = DESKTOP_SAFE_ACTION_OPERATIONS if capability == "desktop_safe_action" else DESKTOP_LAUNCH_OPERATIONS
            if task.request.get("operation") not in allowed_operations:
                raise OrchestrationRefusal("task_kind_mismatch")
            try:
                action_view = await self._desktop_action.describe_task(task_id)
            except DesktopActionError:
                raise OrchestrationRefusal("task_not_found") from None
            status = action_view.status
            if status in (ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL):
                return StepStatus.AWAITING_APPROVAL, None, "approval_required", None
            if status in (ActionStatus.APPROVED, ActionStatus.AUTHORIZED, ActionStatus.EXECUTING):
                # Momentary: `approve()` runs the whole effect synchronously from the same trusted click.
                # Genuinely unresolved if seen mid-flight (a crash between the claim and the answer), so
                # `outcome_unknown` is the honest reason, exactly like a research step interrupted mid-call.
                return StepStatus.PENDING, None, "outcome_unknown", None
            if status in (ActionStatus.OUTCOME_UNKNOWN, ActionStatus.RECONCILING):
                return StepStatus.PENDING, None, "outcome_unknown", None
            if status is ActionStatus.REJECTED:
                return StepStatus.FAILED, bounded_summary("The desktop action was declined."), None, None
            if status is ActionStatus.FAILED:
                return StepStatus.FAILED, bounded_summary(
                    f"Desktop action failed: {action_view.operation.value}."
                ), None, None
            # SUCCEEDED. Built only from closed, controller-known vocabulary (the operation, and for a
            # scroll its closed step) -- never `application_label`/`window_title`/`control_name`, which are
            # untrusted application text this summary must never carry into a second provider's context.
            detail = f" ({action_view.proposal.step.value})" if isinstance(action_view.proposal, ScrollProposal) else ""
            text = f"Desktop action succeeded: {action_view.operation.value}{detail}."
            return StepStatus.SUCCEEDED, bounded_summary(text), None, None
        raise AssertionError(f"unreachable: capability {capability!r} is task-backed but has no reader")  # pragma: no cover

    async def _commit_step(
        self,
        orchestration_id: uuid.UUID,
        *,
        expected_revision: int,
        capability: str,
        child_task_id: uuid.UUID | None,
        status: StepStatus,
        summary: str | None,
        pause_reason: str | None,
        pending_note: str | None = None,
        resource_refs: tuple[str, ...] = (),
        requirement: tuple[str, ...] = (),
        result_backing_id: uuid.UUID | None = None,
    ) -> OrchestrationView:
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            resource_repository = OrchestrationResourceRepository(connection)
            record = await self._live_running(repository, orchestration_id, expected_revision)
            steps = await repository.steps(orchestration_id)
            if len(steps) >= MAX_STEPS:
                await repository.pause(orchestration_id, reason="budget_exhausted")
                return await self._view(connection, orchestration_id)
            if capability not in REPEATABLE_CAPABILITY_IDS and any(
                step.capability_id == capability and step.status == StepStatus.SUCCEEDED for step in steps
            ):
                # Loop guard: re-choosing a capability that already produced a result is not progress --
                # except the few (document_read, document_compare) that legitimately answer more than once.
                await repository.pause(orchestration_id, reason="loop_detected")
                return await self._view(connection, orchestration_id)
            if child_task_id is not None:
                if await repository.step_for_task(child_task_id) is not None:
                    raise OrchestrationRefusal("child_task_already_linked")
                child_count = len({item.child_task_id for item in steps if item.child_task_id is not None})
                if child_count >= MAX_CHILD_TASKS:
                    await repository.pause(orchestration_id, reason="budget_exhausted")
                    return await self._view(connection, orchestration_id)

            # Milestone 12 S1: resolve every cited resource fresh, under the same orchestration row lock
            # `_live_running` just took -- never from an earlier, now-possibly-stale read. A ref that does not
            # resolve for THIS orchestration (invented, another orchestration's, or already consumed/expired)
            # refuses the whole step; nothing is partially consumed.
            resolved_resources: list[ResourceRecord] = []
            for position, ref in enumerate(resource_refs):
                resource = await resource_repository.get(orchestration_id, ref)
                if resource is None:
                    raise OrchestrationRefusal("resource_not_found")
                if resource.consumed_at is not None:
                    raise OrchestrationRefusal("resource_consumed")
                if resource.expires_at is not None and resource.expires_at <= _now_utc():
                    raise OrchestrationRefusal("resource_expired")
                if position < len(requirement) and resource.kind != requirement[position]:
                    raise OrchestrationRefusal("resource_kind_mismatch")
                resolved_resources.append(resource)
            for resource in resolved_resources:
                if resource.single_use:
                    if await resource_repository.consume(resource.id) is None:
                        raise OrchestrationRefusal("resource_consumed")

            sequence = len(steps) + 1
            handle = result_handle(_OUTPUT_CLASS[capability], sequence) if status == StepStatus.SUCCEEDED else None
            step = await repository.insert_step(
                orchestration_id=orchestration_id, sequence=sequence, capability_id=capability, status=status.value,
                child_task_id=child_task_id, result_handle=handle, result_summary=summary,
                pending_note=pending_note if status in (StepStatus.PENDING, StepStatus.AWAITING_APPROVAL) else None,
            )
            await repository.record_step_added(orchestration_id, new_child_task=child_task_id is not None)
            if status == StepStatus.SUCCEEDED:
                output = CAPABILITY_OUTPUT_RESOURCE.get(capability)
                if output is not None:
                    ref = next_ref(await resource_repository.count(orchestration_id))
                    await resource_repository.mint(
                        orchestration_id=orchestration_id, ref=ref, kind=output.kind,
                        producing_step_id=step.id, privacy_class=output.privacy_class,
                        safe_label=_capability_safe_label(capability, sequence), single_use=output.single_use,
                        backing_id=result_backing_id,
                    )
            if status in (StepStatus.PENDING, StepStatus.AWAITING_APPROVAL):
                await repository.pause(orchestration_id, reason=pause_reason or "approval_required")
            return await self._view(connection, orchestration_id)

    # ---- resuming a paused orchestration ---------------------------------------------------------------

    async def resume(self, orchestration_id: uuid.UUID, *, expected_revision: int) -> OrchestrationView:
        """Re-check the current unresolved task-backed step's live state. Idempotent: if nothing has moved,
        the orchestration stays paused (possibly with an updated, more accurate reason). Revalidates
        freshness every time -- it never assumes a child task resolved just because time passed."""
        async with self._engine.connect() as connection:
            repository = OrchestrationRepository(connection)
            record = await repository.get(orchestration_id)
            if record is None:
                raise OrchestrationRefusal("orchestration_not_found")
            if record.revision != expected_revision:
                raise OrchestrationRefusal("revision_conflict")
            if record.status != OrchestrationStatus.PAUSED or record.pause_reason not in (
                "approval_required", "outcome_unknown", "manual_handoff_required"
            ):
                return await self._view(connection, orchestration_id)
            # A PAUSED orchestration is never revived once its TTL has passed -- checked before any of the
            # capability reads below, matching `_live_running`'s own fail-fast shape for every other write.
            if not await repository.is_live(orchestration_id):
                raise OrchestrationRefusal("orchestration_expired")
            step = await repository.last_step(orchestration_id)
        if step is None or step.status not in ("PENDING", "AWAITING_APPROVAL") or step.child_task_id is None:
            return await self.describe(orchestration_id)
        status, summary, pause_reason, pending_note = await self._read_task_backed_resolution(step.capability_id, step.child_task_id)
        if status in (StepStatus.PENDING, StepStatus.AWAITING_APPROVAL):
            # Still unresolved. Update the pause reason if it became more (or less) specific, so a
            # transient outcome_unknown that later needs a fresh approval is relabeled honestly. Separately
            # (Milestone 12 S3), refresh the step's OWN `pending_note` whenever the reason changed OR a note
            # is due -- `manual_handoff_required`'s safe instruction can change (a different underlying
            # reason, e.g. login_required -> account_identity_unknown) without the coarse orchestration-level
            # reason changing at all, and a transition AWAY from a noted reason must clear the old note
            # rather than leave it stale, so this always writes both together whenever either moved.
            reason_changed = pause_reason is not None and pause_reason != record.pause_reason
            if reason_changed or pending_note is not None:
                async with self._engine.begin() as connection:
                    repository = OrchestrationRepository(connection)
                    current = await repository.get(orchestration_id, lock=True)
                    if current is not None and current.revision == expected_revision and current.status == OrchestrationStatus.PAUSED:
                        if reason_changed:
                            assert pause_reason is not None
                            await repository.relabel_pause(orchestration_id, reason=pause_reason)
                        fresh_step = await repository.last_step(orchestration_id)
                        if fresh_step is not None and fresh_step.id == step.id and fresh_step.status in ("PENDING", "AWAITING_APPROVAL"):
                            await repository.update_pending_note(
                                fresh_step.id, status=fresh_step.status, pending_note=pending_note
                            )
                    return await self._view(connection, orchestration_id)
            return await self.describe(orchestration_id)
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            current = await repository.get(orchestration_id, lock=True)
            if current is None:
                raise OrchestrationRefusal("orchestration_not_found")
            if current.revision != expected_revision:
                raise OrchestrationRefusal("revision_conflict")
            fresh_step = await repository.last_step(orchestration_id)
            if fresh_step is None or fresh_step.id != step.id or fresh_step.status not in ("PENDING", "AWAITING_APPROVAL"):
                # Someone else already resolved (or the step changed) between the read and this lock.
                return await self._view(connection, orchestration_id)
            # Re-checked here too, under the same lock as the write: the capability reads above
            # (`_read_task_backed_resolution`) are real I/O and could themselves cross the TTL boundary.
            if not await repository.is_live(orchestration_id):
                raise OrchestrationRefusal("orchestration_expired")
            handle = result_handle(_OUTPUT_CLASS[step.capability_id], step.sequence) if status == StepStatus.SUCCEEDED else None
            resolved = await repository.resolve_step(step.id, status=status.value, result_handle=handle, result_summary=summary)
            if resolved is None:
                return await self._view(connection, orchestration_id)
            if status == StepStatus.SUCCEEDED:
                output = CAPABILITY_OUTPUT_RESOURCE.get(step.capability_id)
                if output is not None:
                    resource_repository = OrchestrationResourceRepository(connection)
                    ref = next_ref(await resource_repository.count(orchestration_id))
                    await resource_repository.mint(
                        orchestration_id=orchestration_id, ref=ref, kind=output.kind,
                        producing_step_id=step.id, privacy_class=output.privacy_class,
                        safe_label=_capability_safe_label(step.capability_id, step.sequence), single_use=output.single_use,
                    )
            await repository.resume_running(orchestration_id)
            return await self._view(connection, orchestration_id)

    # ---- ending the orchestration ------------------------------------------------------------------------

    async def finish(self, orchestration_id: uuid.UUID, *, expected_revision: int) -> OrchestrationView:
        """The planner decided the objective is satisfied. Requires at least one succeeded step: an
        orchestration cannot finish having done nothing."""
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            record = await self._live_running(repository, orchestration_id, expected_revision)
            steps = await repository.steps(orchestration_id)
            if not any(step.status == StepStatus.SUCCEEDED for step in steps):
                raise OrchestrationRefusal("nothing_to_finish")
            settled = await repository.succeed(orchestration_id)
            if settled is None:
                raise OrchestrationRefusal("orchestration_not_active")
            return await self._view(connection, orchestration_id)

    async def stop(self, orchestration_id: uuid.UUID, *, expected_revision: int) -> OrchestrationView:
        """Stops future scheduling only. Never marks an in-flight step failed, never compensates, never
        touches a child task's own state -- the same rule Milestone 10's Stop already established."""
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            record = await repository.get(orchestration_id, lock=True)
            if record is None:
                raise OrchestrationRefusal("orchestration_not_found")
            if record.revision != expected_revision:
                raise OrchestrationRefusal("revision_conflict")
            if record.status not in (OrchestrationStatus.RUNNING, OrchestrationStatus.PAUSED):
                raise OrchestrationRefusal("orchestration_not_active")
            await repository.stop(orchestration_id)
            return await self._view(connection, orchestration_id)

    # ---- helpers -------------------------------------------------------------------------------------

    async def _pause(self, orchestration_id: uuid.UUID, *, expected_revision: int, reason: str) -> OrchestrationView:
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            await self._live_running(repository, orchestration_id, expected_revision)
            await repository.pause(orchestration_id, reason=reason)
            return await self._view(connection, orchestration_id)

    @staticmethod
    async def _live_running(repository: OrchestrationRepository, orchestration_id: uuid.UUID, expected_revision: int) -> OrchestrationRecord:
        record = await repository.get(orchestration_id, lock=True)
        if record is None:
            raise OrchestrationRefusal("orchestration_not_found")
        if record.revision != expected_revision:
            raise OrchestrationRefusal("revision_conflict")
        if record.status != OrchestrationStatus.RUNNING:
            raise OrchestrationRefusal("orchestration_not_active")
        if not await repository.is_live(orchestration_id):
            raise OrchestrationRefusal("orchestration_expired")
        return record

    @staticmethod
    async def _live(repository: OrchestrationRepository, orchestration_id: uuid.UUID, expected_revision: int) -> OrchestrationRecord:
        """Like `_live_running`, but for Milestone 12 S2's `register_resource`: a trusted action outside the
        planner loop, allowed while RUNNING or PAUSED (e.g. attaching a document while a different step
        awaits approval) -- never once the orchestration has concluded (succeeded/failed/stopped/expired)."""
        record = await repository.get(orchestration_id, lock=True)
        if record is None:
            raise OrchestrationRefusal("orchestration_not_found")
        if record.revision != expected_revision:
            raise OrchestrationRefusal("revision_conflict")
        if record.status not in (OrchestrationStatus.RUNNING, OrchestrationStatus.PAUSED):
            raise OrchestrationRefusal("orchestration_not_active")
        if not await repository.is_live(orchestration_id):
            raise OrchestrationRefusal("orchestration_expired")
        return record


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _capability_safe_label(capability: str, sequence: int) -> str:
    """Controller-authored, template-only text -- never a capability's own result content. Deliberately the
    same shape for every capability so adding a new `CAPABILITY_OUTPUT_RESOURCE` entry later cannot
    accidentally start passing through page/document/account/desktop text just by matching this signature."""
    return safe_label(f"{capability} result (step {sequence})")


def _require_uuid(value: object, *, code: str) -> uuid.UUID:
    if value is None:
        raise OrchestrationRefusal(code)
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise OrchestrationRefusal(code) from None


def _require_text(value: object, *, code: str, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise OrchestrationRefusal(code)
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise OrchestrationRefusal(code)
    return text


__all__ = ["OrchestrationService", "OrchestrationView"]
