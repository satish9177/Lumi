import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, AttemptOutcome, task_status_for
from app.domain.authenticated import PauseReason
from app.domain.local_form_draft import FILL_OPERATION, HANDOVER_OPERATION
from app.domain.task_status import TaskEventType, TaskStatus, accepts_actions, is_terminal
from app.repositories.actions import ActionRepository
from app.repositories.browser import BrowserRepository
from app.repositories.desktop_actions import DesktopActionRepository
from app.repositories.form_drafts import FormDraftRepository
from app.repositories.tasks import TaskRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveredAction:
    action_id: uuid.UUID
    task_id: uuid.UUID
    attempt_id: uuid.UUID
    attempt_number: int


class RecoveryService:
    """Startup recovery for execution that a dead runtime left unfinished.

    An attempt that was started but never finished is the one case where Lumi
    genuinely does not know what happened: the intent to act was persisted, the
    process died, and the side effect may or may not have reached the outside
    world. There is no safe way to find out from the database alone.

    So the attempt becomes OUTCOME_UNKNOWN, not FAILED (which would claim
    knowledge Lumi does not have) and not APPROVED (which would invite a blind
    retry of an action that may already have happened). No new attempt is
    created. The only way out is authoritative reconciliation.

    Ownership is decided by `runtime_generation`, not by a timeout: an attempt
    belongs to the process that started it, and only attempts from a *different*
    generation are recovered. The current process's own in-flight work is never
    touched. This is correct as long as one runtime runs at a time, which is the
    current architecture; when workers are added, this check becomes an expired
    lease instead and nothing else has to change.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def recover_unfinished_attempts(
        self, current_generation: uuid.UUID
    ) -> list[RecoveredAction]:
        async with self._engine.connect() as connection:
            stale = await ActionRepository(connection).list_attempts_from_other_generations(
                current_generation
            )
        recovered: list[RecoveredAction] = []
        for attempt in stale:
            # One short transaction per attempt: a failure part-way through
            # recovery leaves the rest recoverable on the next start.
            result = await self._recover_one(attempt.id, attempt.action_id)
            if result is not None:
                recovered.append(result)
        if recovered:
            logger.warning(
                "Recovered %d unfinished execution attempt(s) from a previous runtime as "
                "OUTCOME_UNKNOWN; they need reconciliation and will not be retried.",
                len(recovered),
            )
        return recovered

    async def recover_interrupted_reconciliations(self) -> list[uuid.UUID]:
        """Startup: an action a dead process left in `RECONCILING`.

        `begin_reconciliation` and `finish_reconciliation` are two separate committed transactions
        (recording the reconciliation attempt durably before anything asks a person what they saw, and
        never spanning that question with an open transaction). A crash between them leaves an action
        at `RECONCILING` with no attempt, dispatch or worker call to recover -- reconciliation itself
        never touches the desktop -- and, unlike `EXECUTING`, `RECONCILING` is not `runtime_generation`
        scoped, because nothing about *which* runtime asked the question matters here. Every action
        still `RECONCILING` at startup, from any previous process, goes back to `OUTCOME_UNKNOWN`: the
        one status a reconciliation route will accept, so the person can be asked again instead of the
        action being stuck forever.
        """
        async with self._engine.connect() as connection:
            stuck = await ActionRepository(connection).list_actions_by_status(ActionStatus.RECONCILING)
        recovered: list[uuid.UUID] = []
        for action in stuck:
            moved_id = await self._recover_one_reconciliation(action.id)
            if moved_id is not None:
                recovered.append(moved_id)
        if recovered:
            logger.warning(
                "Recovered %d action(s) stuck in RECONCILING from a previous process as "
                "OUTCOME_UNKNOWN; they need reconciliation again and were not retried.",
                len(recovered),
            )
        return recovered

    async def _recover_one_reconciliation(self, action_id: uuid.UUID) -> uuid.UUID | None:
        async with self._engine.begin() as connection:
            actions_repository = ActionRepository(connection)
            tasks_repository = TaskRepository(connection)
            action = await actions_repository.get_action(action_id)
            if action is None:  # pragma: no cover - actions are never deleted.
                return None
            # Re-read under the lock: another process may have finished reconciling it already.
            if action.status is not ActionStatus.RECONCILING:
                return None
            task = await tasks_repository.lock_task(action.task_id)
            if task is None:  # pragma: no cover - tasks are never deleted.
                return None
            moved = await actions_repository.update_action_status(
                action_id=action.id, expected_revision=action.revision, status=ActionStatus.OUTCOME_UNKNOWN
            )
            if moved is None:
                return None
            # Mirrors `_transition`: the task's own status moved when `begin_reconciliation` set the
            # action to RECONCILING, so it must move back the same way here, unless the task itself
            # became terminal in the meantime (cancelled, say) -- a terminal task's status never changes.
            task_status = None if is_terminal(task.status) else task_status_for(ActionStatus.OUTCOME_UNKNOWN)
            advanced = await tasks_repository.advance_task(
                task_id=task.id, expected_revision=task.revision, status=task_status
            )
            if advanced is None:  # pragma: no cover - the task row lock is held.
                return None
            await tasks_repository.append_event(
                task=advanced,
                event_type=TaskEventType.ACTION_OUTCOME_UNKNOWN,
                payload={
                    "action_id": str(moved.id),
                    "tool_name": moved.tool_name,
                    "risk_tier": moved.risk_tier.value,
                    "proposal_digest": moved.proposal_digest,
                    "action_status": moved.status.value,
                    "action_revision": moved.revision,
                    "outcome": AttemptOutcome.OUTCOME_UNKNOWN.value,
                    "error_code": "runtime_restart",
                    "reason": "reconciliation_interrupted",
                },
            )
            return moved.id

    async def _recover_one(
        self, attempt_id: uuid.UUID, action_id: uuid.UUID
    ) -> RecoveredAction | None:
        async with self._engine.begin() as connection:
            actions_repository = ActionRepository(connection)
            tasks_repository = TaskRepository(connection)

            action = await actions_repository.get_action(action_id)
            if action is None:  # pragma: no cover - actions are never deleted.
                return None
            task = await tasks_repository.lock_task(action.task_id)
            if task is None:  # pragma: no cover - tasks are never deleted.
                return None

            # Re-read under the lock: another start may have finished it between
            # the scan and here.
            attempt = await actions_repository.get_unfinished_attempt(action_id)
            if attempt is None or attempt.id != attempt_id:
                return None
            action = await actions_repository.get_action(action_id)
            assert action is not None
            if action.status is not ActionStatus.EXECUTING:  # pragma: no cover
                return None

            finished = await actions_repository.finish_attempt(
                attempt_id=attempt.id,
                outcome=AttemptOutcome.OUTCOME_UNKNOWN,
                result=None,
                error_code="runtime_restart",
            )
            assert finished is not None
            # Browser work the dead runtime dispatched for this attempt is
            # closed in the same transaction, so the audit trail never shows a
            # dispatch still in flight on a process that no longer exists.
            dispatch = await BrowserRepository(connection).close_orphaned_dispatch(attempt.id)
            # Milestone 9 S3: a desktop dispatch the dead runtime left in flight is closed the same way.
            # Nothing is claimed about what the desktop did and nothing is retried.
            await DesktopActionRepository(connection).close_orphaned_dispatch(attempt.id)
            # Milestone 8b S6. A local draft lives only in the browser, and the browser died
            # with the runtime. `frozen_at` is the one fact that lets Lumi say anything about
            # the remote effect: set, it proves both network layers were frozen before any
            # field was written; NULL, it proves nothing and nothing is claimed. There is no
            # reconciliation and no retry either way.
            local_draft = dispatch is not None and dispatch.operation in (
                FILL_OPERATION,
                HANDOVER_OPERATION,
            )
            frozen = dispatch is not None and dispatch.frozen_at is not None
            moved = await actions_repository.update_action_status(
                action_id=action.id,
                expected_revision=action.revision,
                status=ActionStatus.OUTCOME_UNKNOWN,
            )
            assert moved is not None
            advanced = await tasks_repository.advance_task(
                task_id=task.id,
                expected_revision=task.revision,
                status=task_status_for(ActionStatus.OUTCOME_UNKNOWN),
            )
            assert advanced is not None
            await tasks_repository.append_event(
                task=advanced,
                event_type=TaskEventType.ACTION_OUTCOME_UNKNOWN,
                payload={
                    "action_id": str(moved.id),
                    "tool_name": moved.tool_name,
                    "risk_tier": moved.risk_tier.value,
                    "proposal_digest": moved.proposal_digest,
                    "action_status": moved.status.value,
                    "action_revision": moved.revision,
                    "attempt_id": str(finished.id),
                    "attempt_number": finished.attempt_number,
                    "outcome": AttemptOutcome.OUTCOME_UNKNOWN.value,
                    "error_code": "runtime_restart",
                    "reason": "runtime_restart",
                    "recovered_from_generation": str(finished.runtime_generation),
                    "browser_dispatch_id": str(dispatch.id) if dispatch is not None else None,
                    **(
                        {
                            "frozen_at_present": frozen,
                            "remote_effect": (
                                "impossible_under_verified_freeze"
                                if frozen and dispatch is not None and dispatch.operation == FILL_OPERATION
                                else "unknown"
                            ),
                            "local_state": "lost",
                        }
                        if local_draft
                        else {}
                    ),
                },
            )
            if local_draft:
                await _pause_browser_lost(tasks_repository, task.id)
            return RecoveredAction(
                action_id=moved.id,
                task_id=task.id,
                attempt_id=finished.id,
                attempt_number=finished.attempt_number,
            )


async def _pause_browser_lost(tasks: TaskRepository, task_id: uuid.UUID) -> None:
    """The task waits, paused, for a fresh observation and a fresh exact approval."""
    task = await tasks.get_task(task_id)
    if task is None or not accepts_actions(task.status):
        return
    advanced = await tasks.advance_task(
        task_id=task.id, expected_revision=task.revision, status=TaskStatus.PAUSED
    )
    if advanced is not None:
        await tasks.append_event(
            task=advanced,
            event_type=TaskEventType.TASK_AUTHENTICATED_PAUSED,
            payload={"reason": PauseReason.BROWSER_LOST.value},
        )


class FormDraftRecovery:
    """Startup: a local draft is browser state, and the browser is gone.

    Every live `form_drafts` row belongs to a browser that no longer exists, so it is closed as
    `DISCARDED` -- never re-filled, never restored, and its approval is never reused. The task
    is paused `browser_lost`, so the user is told the draft is lost and must prepare the form
    again (a fresh observation and a fresh exact approval).
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def discard_lost_drafts(self) -> int:
        async with self._engine.begin() as connection:
            lost = await FormDraftRepository(connection).discard_all_live()
            tasks = TaskRepository(connection)
            for draft in lost:
                task = await tasks.lock_task(draft.task_id)
                if task is None or not accepts_actions(task.status):
                    continue
                advanced = await tasks.advance_task(
                    task_id=task.id, expected_revision=task.revision, status=TaskStatus.PAUSED
                )
                if advanced is None:
                    continue
                await tasks.append_event(
                    task=advanced,
                    event_type=TaskEventType.TASK_FORM_DRAFT_LOST,
                    payload={"draft_id": str(draft.id), "local_state": "lost"},
                )
                task = await tasks.get_task(draft.task_id)
                if task is not None:
                    again = await tasks.advance_task(task_id=task.id, expected_revision=task.revision)
                    if again is not None:
                        await tasks.append_event(
                            task=again,
                            event_type=TaskEventType.TASK_AUTHENTICATED_PAUSED,
                            payload={"reason": PauseReason.BROWSER_LOST.value},
                        )
        if lost:
            logger.warning(
                "Closed %d local form draft(s) whose browser is gone; they are lost, not restored.",
                len(lost),
            )
        return len(lost)
