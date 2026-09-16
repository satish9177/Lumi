import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.action_status import ActionStatus, AttemptOutcome, task_status_for
from app.domain.task_status import TaskEventType
from app.repositories.actions import ActionRepository
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
                },
            )
            return RecoveredAction(
                action_id=moved.id,
                task_id=task.id,
                attempt_id=finished.id,
                attempt_number=finished.attempt_number,
            )
