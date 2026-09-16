import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.action_status import (
    ActionStatus,
    ApprovalStatus,
    AttemptOutcome,
    RiskTier,
    action_status_for,
    can_transition,
    event_type_for_outcome,
    requires_approval,
    task_status_for,
)
from app.domain.digest import proposal_digest as compute_digest
from app.domain.errors import (
    ActionAlreadyOpenError,
    ActionConcurrencyError,
    ActionNotFoundError,
    ActionProposalConflictError,
    ApprovalNotUsableError,
    AttemptNotFoundError,
    InvalidActionTransitionError,
    StaleActionRevisionError,
    TaskNotAcceptingActionsError,
    TaskNotFoundError,
)
from app.domain.task_status import TaskEventType, accepts_actions, is_terminal
from app.repositories.actions import (
    ActionRecord,
    ActionRepository,
    ApprovalRecord,
    AttemptRecord,
)
from app.repositories.tasks import TaskRecord, TaskRepository


@dataclass(frozen=True, slots=True)
class ActionView:
    """An action with the approval and attempts that explain its current state."""

    action: ActionRecord
    approval: ApprovalRecord | None
    attempts: tuple[AttemptRecord, ...]


class ActionService:
    """Action ledger use cases.

    Each method is one short PostgreSQL transaction with no external I/O inside
    it. Every method that touches an action first takes the owning task's row
    lock, so all work on one task runs in a single order: event sequences stay
    ordered, and duplicate approvals or execution starts serialize instead of
    racing.

    There is no external executor in this milestone. `start_attempt` persists
    the intent to execute and stops; a later browser worker reads that intent,
    acts outside any transaction, and reports back through `finish_attempt`.
    """

    def __init__(
        self, engine: AsyncEngine, *, runtime_generation: uuid.UUID, approval_ttl_seconds: int
    ) -> None:
        self._engine = engine
        self._runtime_generation = runtime_generation
        self._approval_ttl = timedelta(seconds=approval_ttl_seconds)

    # ---- helpers ------------------------------------------------------------

    async def _lock_task(self, repository: TaskRepository, task_id: uuid.UUID) -> TaskRecord:
        task = await repository.lock_task(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    async def _lock_task_for_action(
        self, connection: AsyncConnection, action_id: uuid.UUID
    ) -> tuple[TaskRecord, ActionRecord]:
        """Take the owning task's lock, then re-read the action under it.

        The first read is only used to find the task. The action is read again
        afterwards so the caller always decides on state that cannot change
        underneath it.
        """
        actions_repository = ActionRepository(connection)
        action = await actions_repository.get_action(action_id)
        if action is None:
            raise ActionNotFoundError(action_id)
        task = await self._lock_task(TaskRepository(connection), action.task_id)
        locked = await actions_repository.get_action(action_id)
        if locked is None:  # pragma: no cover - actions are never deleted.
            raise ActionNotFoundError(action_id)
        return task, locked

    @staticmethod
    def _check_revision(action: ActionRecord, expected_revision: int | None) -> None:
        if expected_revision is not None and action.revision != expected_revision:
            raise StaleActionRevisionError(action.id, expected_revision, action.revision)

    @staticmethod
    def _check_transition(action: ActionRecord, target: ActionStatus) -> None:
        if not can_transition(action.status, target):
            raise InvalidActionTransitionError(action.id, action.status, target)

    @staticmethod
    def _check_task_open(task: TaskRecord) -> None:
        if not accepts_actions(task.status):
            raise TaskNotAcceptingActionsError(task.id, task.status)

    async def _transition(
        self,
        connection: AsyncConnection,
        *,
        task: TaskRecord,
        action: ActionRecord,
        target: ActionStatus,
        event_type: TaskEventType,
        payload: dict[str, Any],
    ) -> ActionRecord:
        """Move the action, mirror it onto the task, and append one event.

        All three happen in the caller's transaction, so a crash can never leave
        an action state that the timeline does not explain.
        """
        actions_repository = ActionRepository(connection)
        tasks_repository = TaskRepository(connection)
        moved = await actions_repository.update_action_status(
            action_id=action.id, expected_revision=action.revision, status=target
        )
        if moved is None:  # The task lock is held, so nothing else can have moved it.
            raise ActionConcurrencyError(action.id)
        # A terminal task's status never changes. An action can still be
        # rejected or report its outcome after the task was cancelled -- that
        # belongs in the timeline -- but it must not revive the task.
        task_status = None if is_terminal(task.status) else task_status_for(target)
        advanced = await tasks_repository.advance_task(
            task_id=task.id, expected_revision=task.revision, status=task_status
        )
        if advanced is None:  # pragma: no cover - the task row lock is held.
            raise ActionConcurrencyError(action.id)
        await tasks_repository.append_event(
            task=advanced,
            event_type=event_type,
            payload={
                "action_id": str(moved.id),
                "tool_name": moved.tool_name,
                "risk_tier": moved.risk_tier.value,
                "proposal_digest": moved.proposal_digest,
                "action_status": moved.status.value,
                "action_revision": moved.revision,
                **payload,
            },
        )
        return moved

    async def _view(self, connection: AsyncConnection, action: ActionRecord) -> ActionView:
        repository = ActionRepository(connection)
        return ActionView(
            action=action,
            approval=await repository.get_open_approval(action.id),
            attempts=tuple(await repository.list_attempts(action.id)),
        )

    # ---- proposal -----------------------------------------------------------

    async def propose_action(
        self,
        task_id: uuid.UUID,
        *,
        idempotency_key: str,
        tool_name: str,
        risk_tier: RiskTier,
        proposal: dict[str, Any],
    ) -> tuple[ActionView, bool]:
        """Record a proposed action. Returns the action and whether it is new.

        The digest is computed here from the proposal the server received; a
        digest supplied by a caller is never accepted. Re-proposing the same key
        with the same proposal returns the stored action and writes nothing. The
        same key with a different proposal is a conflict: the stored proposal is
        never replaced, because an approval may already be bound to it.
        """
        digest = compute_digest(proposal)
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task = await self._lock_task(TaskRepository(connection), task_id)
            self._check_task_open(task)

            created = await repository.insert_action_if_absent(
                action_id=uuid.uuid4(),
                task_id=task_id,
                idempotency_key=idempotency_key,
                tool_name=tool_name,
                risk_tier=risk_tier,
                proposal=proposal,
                proposal_digest=digest,
                status=ActionStatus.PROPOSED,
            )
            if created is None:
                existing = await repository.get_action_by_idempotency_key(
                    task_id=task_id, idempotency_key=idempotency_key
                )
                assert existing is not None  # The insert conflicted, so a row exists.
                if existing.proposal_digest != digest or existing.tool_name != tool_name:
                    raise ActionProposalConflictError(task_id, idempotency_key, existing.id)
                return await self._view(connection, existing), False

            await self._record_proposed(connection, task, created)
            return await self._view(connection, created), True

    async def _record_proposed(
        self, connection: AsyncConnection, task: TaskRecord, created: ActionRecord
    ) -> None:
        tasks_repository = TaskRepository(connection)
        # Proposing does not move the task; it only adds to its timeline.
        advanced = await tasks_repository.advance_task(
            task_id=task.id, expected_revision=task.revision
        )
        assert advanced is not None  # The task row lock is held.
        await tasks_repository.append_event(
            task=advanced,
            event_type=TaskEventType.ACTION_PROPOSED,
            payload={
                "action_id": str(created.id),
                "tool_name": created.tool_name,
                "risk_tier": created.risk_tier.value,
                "proposal_digest": created.proposal_digest,
                "action_status": created.status.value,
                "action_revision": created.revision,
                "idempotency_key": created.idempotency_key,
                "requires_approval": requires_approval(created.risk_tier),
            },
        )

    async def propose_exclusive_action(
        self,
        task_id: uuid.UUID,
        *,
        tool_name: str,
        risk_tier: RiskTier,
        proposal: dict[str, Any],
    ) -> ActionView:
        """Propose a tool action only if the task has no other live one.

        Under the task row lock: any action of this tool that is not FAILED or
        REJECTED -- waiting, approved, executing, of unknown outcome, being
        reconciled, or already succeeded -- blocks a new proposal. That keeps an
        unresolved booking from being side-stepped by preparing a fresh one.
        The idempotency key is the tool's ordinal within the task, so two
        concurrent requests serialize on the lock and the second is refused.
        """
        digest = compute_digest(proposal)
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task = await self._lock_task(TaskRepository(connection), task_id)
            self._check_task_open(task)
            limit = 500
            existing = [
                action
                for action in await repository.list_actions(task_id, limit=limit)
                if action.tool_name == tool_name
            ]
            if len(existing) >= limit:  # pragma: no cover - defensive bound.
                raise ActionAlreadyOpenError(task_id, existing[-1].id, existing[-1].status)
            for action in existing:
                if action.status not in (ActionStatus.FAILED, ActionStatus.REJECTED):
                    raise ActionAlreadyOpenError(task_id, action.id, action.status)
            created = await repository.insert_action_if_absent(
                action_id=uuid.uuid4(),
                task_id=task_id,
                idempotency_key=f"{tool_name}-{len(existing) + 1}",
                tool_name=tool_name,
                risk_tier=risk_tier,
                proposal=proposal,
                proposal_digest=digest,
                status=ActionStatus.PROPOSED,
            )
            if created is None:  # pragma: no cover - the ordinal is taken under the lock.
                raise ActionConcurrencyError(task_id)
            await self._record_proposed(connection, task, created)
            return await self._view(connection, created)

    async def get_action(self, action_id: uuid.UUID) -> ActionView:
        async with self._engine.connect() as connection:
            action = await ActionRepository(connection).get_action(action_id)
            if action is None:
                raise ActionNotFoundError(action_id)
            return await self._view(connection, action)

    async def list_actions(self, task_id: uuid.UUID, *, limit: int = 100) -> list[ActionView]:
        async with self._engine.connect() as connection:
            if await TaskRepository(connection).get_task(task_id) is None:
                raise TaskNotFoundError(task_id)
            repository = ActionRepository(connection)
            return [
                await self._view(connection, action)
                for action in await repository.list_actions(task_id, limit=limit)
            ]

    # ---- approval -----------------------------------------------------------

    async def request_approval(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Open a durable, expiring approval request for exactly this proposal.

        The approval is created by the server from persisted action state. No
        caller ever hands in a proposal payload to be approved.
        """
        async with self._engine.begin() as connection:
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_task_open(task)
            self._check_revision(action, expected_revision)
            self._check_transition(action, ActionStatus.WAITING_APPROVAL)

            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.WAITING_APPROVAL,
                event_type=TaskEventType.ACTION_APPROVAL_REQUESTED,
                payload={"approval_ttl_seconds": int(self._approval_ttl.total_seconds())},
            )
            await ActionRepository(connection).insert_pending_approval(
                approval_id=uuid.uuid4(),
                action_id=moved.id,
                action_revision=moved.revision,
                proposal_digest=moved.proposal_digest,
                ttl=self._approval_ttl,
            )
            return await self._view(connection, moved)

    async def approve_action(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_task_open(task)
            self._check_revision(action, expected_revision)
            self._check_transition(action, ActionStatus.APPROVED)

            approval = await repository.get_open_approval(action_id)
            if approval is None or approval.status is not ApprovalStatus.PENDING:
                raise ApprovalNotUsableError(action_id, "there is no pending approval request")
            if approval.proposal_digest != action.proposal_digest:
                raise ApprovalNotUsableError(action_id, "the approval is for a different proposal")
            if await repository.is_expired(approval.id):
                raise ApprovalNotUsableError(action_id, "the approval request has expired")

            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.APPROVED,
                event_type=TaskEventType.ACTION_APPROVED,
                payload={"approval_id": str(approval.id)},
            )
            # Re-bind to the revision this approval now authorizes. Any later
            # change to the action moves the revision past it, and the approval
            # can no longer be claimed.
            granted = await repository.grant_approval(
                approval_id=approval.id, action_revision=moved.revision
            )
            if granted is None:  # pragma: no cover - the task row lock is held.
                raise ActionConcurrencyError(action_id)
            return await self._view(connection, moved)

    async def reject_action(
        self,
        action_id: uuid.UUID,
        *,
        expected_revision: int | None = None,
        reason: str | None = None,
    ) -> ActionView:
        """Refuse the action. A rejected action can never execute."""
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_revision(action, expected_revision)
            self._check_transition(action, ActionStatus.REJECTED)

            approval = await repository.get_open_approval(action_id)
            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.REJECTED,
                event_type=TaskEventType.ACTION_REJECTED,
                payload={
                    "approval_id": str(approval.id) if approval is not None else None,
                    "reason": reason,
                },
            )
            if approval is not None:
                await repository.reject_approval(approval.id)
            return await self._view(connection, moved)

    # ---- execution ----------------------------------------------------------

    async def start_attempt(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Claim the approval and persist the intent to execute, atomically.

        In one transaction: verify the action is approved, claim the approval
        (which re-checks expiry, revision binding and proposal digest inside the
        claiming statement), move the action to EXECUTING, create the attempt,
        move the task, and record the event. Only after this commits may an
        external executor act. Nothing here talks to the outside world.
        """
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_task_open(task)
            self._check_revision(action, expected_revision)
            self._check_transition(action, ActionStatus.EXECUTING)

            approval = await repository.get_open_approval(action_id)
            if approval is None:
                raise ApprovalNotUsableError(
                    action_id, "no approval is available; it was never granted, or already used"
                )
            if approval.status is not ApprovalStatus.APPROVED:
                raise ApprovalNotUsableError(action_id, "the approval has not been granted")
            claimed = await repository.claim_approval(
                approval_id=approval.id,
                action_revision=action.revision,
                proposal_digest=action.proposal_digest,
            )
            if claimed is None:
                # The claim re-checked expiry and both bindings in SQL, so the
                # database, not an earlier read, decided this.
                reason = (
                    "the approval has expired"
                    if await repository.is_expired(approval.id)
                    else "the approval no longer matches this action"
                )
                raise ApprovalNotUsableError(action_id, reason)

            attempt_number = await repository.next_attempt_number(action_id)
            attempt = await repository.insert_attempt(
                attempt_id=uuid.uuid4(),
                action_id=action_id,
                attempt_number=attempt_number,
                approval_id=claimed.id,
                runtime_generation=self._runtime_generation,
            )
            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.EXECUTING,
                event_type=TaskEventType.ACTION_EXECUTION_STARTED,
                payload={
                    "attempt_id": str(attempt.id),
                    "attempt_number": attempt.attempt_number,
                    "approval_id": str(claimed.id),
                    "runtime_generation": str(self._runtime_generation),
                },
            )
            return await self._view(connection, moved)

    async def finish_attempt(
        self,
        action_id: uuid.UUID,
        *,
        outcome: AttemptOutcome,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        expected_revision: int | None = None,
    ) -> ActionView:
        """Record what the executor observed.

        SUCCEEDED and FAILED are claims of knowledge. OUTCOME_UNKNOWN is how an
        executor says it lost the response and does not know whether the side
        effect happened; it is never retried automatically from here.
        """
        target = action_status_for(outcome)
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_revision(action, expected_revision)
            self._check_transition(action, target)

            attempt = await repository.get_unfinished_attempt(action_id)
            if attempt is None:
                raise AttemptNotFoundError(action_id)
            finished = await repository.finish_attempt(
                attempt_id=attempt.id, outcome=outcome, result=result, error_code=error_code
            )
            if finished is None:  # pragma: no cover - the task row lock is held.
                raise ActionConcurrencyError(action_id)

            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=target,
                event_type=event_type_for_outcome(outcome),
                payload={
                    "attempt_id": str(finished.id),
                    "attempt_number": finished.attempt_number,
                    "outcome": outcome.value,
                    "error_code": error_code,
                    "reason": "executor_reported",
                },
            )
            return await self._view(connection, moved)

    # ---- reconciliation -----------------------------------------------------

    async def begin_reconciliation(
        self, action_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> ActionView:
        """Start establishing what actually happened. Only from OUTCOME_UNKNOWN.

        This never starts a second execution attempt. A later browser adapter
        will answer the question by reading authoritative state (`lookup_booking`,
        `lookup_submission`), not by acting again.
        """
        async with self._engine.begin() as connection:
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_revision(action, expected_revision)
            self._check_transition(action, ActionStatus.RECONCILING)
            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.RECONCILING,
                event_type=TaskEventType.ACTION_RECONCILIATION_STARTED,
                payload={},
            )
            return await self._view(connection, moved)

    async def finish_reconciliation(
        self,
        action_id: uuid.UUID,
        *,
        result: AttemptOutcome,
        evidence: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> ActionView:
        """Record the authoritative answer.

        SUCCEEDED or FAILED resolve the action and return the task to READY.
        OUTCOME_UNKNOWN means reconciliation could not tell either, so the action
        goes back to OUTCOME_UNKNOWN and waits for a better answer. It is never
        downgraded to FAILED just because looking was inconclusive.
        """
        target = action_status_for(result)
        async with self._engine.begin() as connection:
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_revision(action, expected_revision)
            self._check_transition(action, target)
            moved = await self._transition(
                connection,
                task=task,
                action=action,
                target=target,
                event_type=TaskEventType.ACTION_RECONCILED,
                payload={
                    "result": result.value,
                    "evidence": evidence,
                    "reason": "reconciliation",
                },
            )
            return await self._view(connection, moved)
