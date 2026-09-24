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

from app.domain.errors import TaskNotFoundError
from app.domain.orchestration import (
    COMPOSED_CAPABILITY_IDS,
    EXPECTED_TASK_TYPE,
    MAX_CHILD_TASKS,
    MAX_PLANNER_CALLS,
    MAX_STEPS,
    ORCHESTRATION_TTL_SECONDS,
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
    next_ref,
    safe_label,
    validate_resource_refs,
)
from app.repositories.orchestration import OrchestrationRecord, OrchestrationRepository, StepRecord
from app.repositories.orchestration_resources import OrchestrationResourceRepository, ResourceRecord
from app.repositories.tasks import TaskRepository
from app.services.projects import ProjectService
from app.services.research_tasks import ResearchService

#: capability_id -> the label a result handle carries (`research_result:3`). Closed, spelled here.
_OUTPUT_CLASS: dict[str, str] = {
    "public_research": "research_result", "project_status": "project_status", "project_start": "project_run_result"
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
    def __init__(self, engine: AsyncEngine, *, research: ResearchService, project: ProjectService) -> None:
        self._engine = engine
        self._research = research
        self._project = project

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
            already_succeeded = {step.capability_id for step in steps if step.status == StepStatus.SUCCEEDED}
            available = tuple(sorted(COMPOSED_CAPABILITY_IDS - already_succeeded))
        resources = await OrchestrationResourceRepository(connection).available(orchestration_id)
        return OrchestrationView(
            orchestration=record, live=live, steps=tuple(steps), available_capabilities=available,
            resources=tuple(resources),
        )

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

        if capability in TASK_BACKED_CAPABILITY_IDS:
            if resolved_summary is not None:
                raise OrchestrationRefusal("resolved_summary_not_allowed")
            task_uuid = _require_uuid(task_id, code="task_id_required")
            status, summary, pause_reason = await self._read_task_backed_resolution(capability, task_uuid)
            return await self._commit_step(
                orchestration_id, expected_revision=expected_revision, capability=capability,
                child_task_id=task_uuid, status=status, summary=summary, pause_reason=pause_reason,
                resource_refs=resource_refs, requirement=requirement,
            )

        # Synchronous (SYNCHRONOUS_CAPABILITY_IDS): the caller already computed the result through that
        # capability's own existing read method.
        if task_id is not None:
            raise OrchestrationRefusal("task_id_not_allowed")
        summary = bounded_summary(_require_text(resolved_summary, code="resolved_summary_required"))
        return await self._commit_step(
            orchestration_id, expected_revision=expected_revision, capability=capability,
            child_task_id=None, status=StepStatus.SUCCEEDED, summary=summary, pause_reason=None,
            resource_refs=resource_refs, requirement=requirement,
        )

    async def _read_task_backed_resolution(
        self, capability: str, task_id: uuid.UUID
    ) -> tuple[StepStatus, str | None, str | None]:
        """Read-only, outside any write transaction: this runtime's own current record of a task-backed
        capability's resolution. Never creates, grants or advances anything. The third element is the pause
        reason to use if the step stays unresolved (`AWAITING_APPROVAL` always -> `approval_required`;
        `PENDING`'s reason depends on *why* it is still pending)."""
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
                return StepStatus.SUCCEEDED, bounded_summary(text), None
            if view.grant is None or view.grant.status.value == "PENDING":
                return StepStatus.AWAITING_APPROVAL, None, "approval_required"
            if view.grant.status.value in ("REVOKED", "EXPIRED"):
                # Declined, cancelled, or its window closed unused. Never revived; a new attempt is a new
                # step, not a resumed one -- exactly like a fresh research grant is a new confirmation.
                return StepStatus.FAILED, bounded_summary("The research scope was declined, expired, or the task was cancelled."), None
            if view.unresolved_step:
                # A step of this task has no outcome Lumi can stand behind (interrupted mid-flight, a lost
                # response). Distinct from "still working": a human may need to look, not just wait.
                return StepStatus.PENDING, None, "outcome_unknown"
            # ACTIVE, no answer recorded yet, and nothing unresolved: ordinary work in progress. Stay
            # unresolved; the caller's own research loop, and a later `resume`, decide when this changes.
            return StepStatus.PENDING, None, "approval_required"
        if capability == "project_start":
            try:
                run_view = await self._project.describe(task_id)
            except TaskNotFoundError:
                raise OrchestrationRefusal("task_not_found") from None
            if run_view.phase == "awaiting_approval":
                return StepStatus.AWAITING_APPROVAL, None, "approval_required"
            if run_view.phase in _PROJECT_RUN_FAILED_PHASES:
                return StepStatus.FAILED, bounded_summary(f"The project run ended: {run_view.phase}."), None
            if run_view.phase in _PROJECT_RUN_ALIVE_PHASES:
                text = f"Project run started (phase: {run_view.phase}){', ready' if run_view.ready else ''}."
                return StepStatus.SUCCEEDED, bounded_summary(text), None
            if run_view.phase == "outcome_unknown":
                return StepStatus.PENDING, None, "outcome_unknown"
            # "approved": the grant is active but the caller has not yet called start(), or start() has not
            # finished. Stay unresolved; never guessed here. The caller (Electron main) is what actually
            # calls start() once the grant is active -- this read never does, matching "it never creates,
            # grants or advances that child task itself".
            return StepStatus.PENDING, None, "approval_required"
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
        resource_refs: tuple[str, ...] = (),
        requirement: tuple[str, ...] = (),
    ) -> OrchestrationView:
        async with self._engine.begin() as connection:
            repository = OrchestrationRepository(connection)
            resource_repository = OrchestrationResourceRepository(connection)
            record = await self._live_running(repository, orchestration_id, expected_revision)
            steps = await repository.steps(orchestration_id)
            if len(steps) >= MAX_STEPS:
                await repository.pause(orchestration_id, reason="budget_exhausted")
                return await self._view(connection, orchestration_id)
            if any(step.capability_id == capability and step.status == StepStatus.SUCCEEDED for step in steps):
                # Loop guard: re-choosing a capability that already produced a result is not progress.
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
            if record.status != OrchestrationStatus.PAUSED or record.pause_reason not in ("approval_required", "outcome_unknown"):
                return await self._view(connection, orchestration_id)
            # A PAUSED orchestration is never revived once its TTL has passed -- checked before any of the
            # capability reads below, matching `_live_running`'s own fail-fast shape for every other write.
            if not await repository.is_live(orchestration_id):
                raise OrchestrationRefusal("orchestration_expired")
            step = await repository.last_step(orchestration_id)
        if step is None or step.status not in ("PENDING", "AWAITING_APPROVAL") or step.child_task_id is None:
            return await self.describe(orchestration_id)
        status, summary, pause_reason = await self._read_task_backed_resolution(step.capability_id, step.child_task_id)
        if status in (StepStatus.PENDING, StepStatus.AWAITING_APPROVAL):
            # Still unresolved. Update the pause reason if it became more (or less) specific, so a
            # transient outcome_unknown that later needs a fresh approval is relabeled honestly.
            if pause_reason is not None and pause_reason != record.pause_reason:
                async with self._engine.begin() as connection:
                    repository = OrchestrationRepository(connection)
                    current = await repository.get(orchestration_id, lock=True)
                    if current is not None and current.revision == expected_revision and current.status == OrchestrationStatus.PAUSED:
                        await repository.relabel_pause(orchestration_id, reason=pause_reason)
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


def _require_text(value: object, *, code: str) -> str:
    if not isinstance(value, str):
        raise OrchestrationRefusal(code)
    text = " ".join(value.split())
    if not text or len(text) > _MAX_TEXT:
        raise OrchestrationRefusal(code)
    return text


__all__ = ["OrchestrationService", "OrchestrationView"]
