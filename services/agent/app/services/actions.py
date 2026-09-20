import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

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


AttemptEvidenceWriter = Callable[[AsyncConnection, AttemptRecord], Awaitable[None]]
#: Re-checks, inside the approving transaction, the facts an approval depends on.
ApprovalGuard = Callable[[AsyncConnection, ActionRecord], Awaitable[None]]


class ScopedAuthorizer(Protocol):
    """How a reusable grant funds one step, without this module knowing grants.

    The ledger stays the authority on transitions; the caller stays the
    authority on what its scope means. `mint` runs only after the action is
    AUTHORIZED, so the authorization can bind the revision that produced, and
    `consume` must re-check every binding in the statement that consumes it.
    """

    def authorization_payload(self) -> dict[str, Any]:
        """Facts for the `action.authorized` event: grant id, scope digest, ..."""
        ...

    async def mint(self, connection: AsyncConnection, *, action: ActionRecord) -> uuid.UUID: ...

    async def consume(
        self,
        connection: AsyncConnection,
        *,
        authorization_id: uuid.UUID,
        action_revision: int,
        proposal_digest: str,
    ) -> bool: ...


#: Actions that still hold, or may still use, an approval.
_OPEN_STATUSES = frozenset(
    {
        ActionStatus.PROPOSED,
        ActionStatus.WAITING_APPROVAL,
        ActionStatus.APPROVED,
        ActionStatus.AUTHORIZED,
        ActionStatus.EXECUTING,
        ActionStatus.RECONCILING,
    }
)


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

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

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
        self,
        connection: AsyncConnection,
        task: TaskRecord,
        created: ActionRecord,
        *,
        scoped: bool = False,
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
                # A scoped action does not wait for an exact approval; it waits
                # for a single-use authorization derived from a confirmed
                # grant. Saying `requires_approval` here would claim the user
                # is about to review this exact step, which they are not.
                "requires_approval": False if scoped else requires_approval(created.risk_tier),
                **({"authorization": "task_grant"} if scoped else {}),
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

    async def propose_or_reuse_open_action(
        self,
        task_id: uuid.UUID,
        *,
        tool_name: str,
        risk_tier: RiskTier,
        proposal: dict[str, Any],
    ) -> ActionView:
        """Propose, unless this exact proposal is already open for review.

        For read-only work whose earlier attempts may legitimately stay
        OUTCOME_UNKNOWN or SUCCEEDED: under the task lock, an open action of
        this tool with the same digest that is still reviewable is returned as
        it is (a duplicate request shows the same card). A different open
        action is refused, except that an approval request which has expired
        is withdrawn in this transaction so a fresh one can be made. Finished
        actions never block, and are never reused: a repeat is a new action
        with a new approval.
        """
        digest = compute_digest(proposal)
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            task = await self._lock_task(TaskRepository(connection), task_id)
            self._check_task_open(task)
            existing = [
                action
                for action in await repository.list_actions(task_id, limit=500)
                if action.tool_name == tool_name
            ]
            for action in existing:
                if action.status not in _OPEN_STATUSES:
                    continue
                if action.status in (ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL):
                    approval = await repository.get_open_approval(action.id)
                    expired = approval is not None and await repository.is_expired(approval.id)
                    if action.proposal_digest == digest and not expired:
                        return await self._view(connection, action)
                    if expired:
                        task = await self.reject_in_transaction(
                            connection, task=task, action=action, reason="approval_expired"
                        )
                        continue
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

    async def start_scoped_attempt(
        self,
        task_id: uuid.UUID,
        *,
        tool_name: str,
        idempotency_key: str,
        risk_tier: RiskTier,
        proposal: dict[str, Any],
        authorizer: "ScopedAuthorizer",
    ) -> tuple[ActionView, bool]:
        """PROPOSED -> AUTHORIZED -> EXECUTING, funded by a scoped grant.

        The Milestone 7b counterpart of `request_approval` + `approve_action` +
        `start_attempt`, collapsed into one transaction because there is no
        human in the middle of it: the human already confirmed the scope, and
        this is a step inside it.

        Every invariant of the exact-approval path is kept:

        * the action is immutable, and its digest is computed here from the
          proposal the server built -- never handed in;
        * the authorization is minted only after the action is AUTHORIZED, bound
          to the revision that transition produced and to that digest;
        * consuming it re-checks every binding in SQL, so grant expiry,
          revocation, a superseded revision and a second claim are all decided
          by the database;
        * the attempt is durable before anything outside this process is asked
          to act, and no transaction is open while it acts.

        Returns `(view, created)`. `created` is False when this idempotency key
        already has an action, which is how a duplicated planner request
        becomes a no-op instead of a second execution.
        """
        digest = compute_digest(proposal)
        async with self._engine.begin() as connection:
            repository = ActionRepository(connection)
            tasks_repository = TaskRepository(connection)
            task = await self._lock_task(tasks_repository, task_id)
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

            await self._record_proposed(connection, task, created, scoped=True)
            task = await self._reload_task(tasks_repository, task_id)
            authorized = await self._transition(
                connection,
                task=task,
                action=created,
                target=ActionStatus.AUTHORIZED,
                event_type=TaskEventType.ACTION_AUTHORIZED,
                payload=authorizer.authorization_payload(),
            )
            authorization_id = await authorizer.mint(connection, action=authorized)
            claimed = await authorizer.consume(
                connection,
                authorization_id=authorization_id,
                action_revision=authorized.revision,
                proposal_digest=authorized.proposal_digest,
            )
            if not claimed:
                raise ApprovalNotUsableError(
                    authorized.id, "the scoped authorization for this step is not usable"
                )
            attempt = await repository.insert_attempt(
                attempt_id=uuid.uuid4(),
                action_id=authorized.id,
                attempt_number=await repository.next_attempt_number(authorized.id),
                step_authorization_id=authorization_id,
                runtime_generation=self._runtime_generation,
            )
            task = await self._reload_task(tasks_repository, task_id)
            moved = await self._transition(
                connection,
                task=task,
                action=authorized,
                target=ActionStatus.EXECUTING,
                event_type=TaskEventType.ACTION_EXECUTION_STARTED,
                payload={
                    "attempt_id": str(attempt.id),
                    "attempt_number": attempt.attempt_number,
                    "step_authorization_id": str(authorization_id),
                    "runtime_generation": str(self._runtime_generation),
                },
            )
            return await self._view(connection, moved), True

    @staticmethod
    async def _reload_task(repository: TaskRepository, task_id: uuid.UUID) -> TaskRecord:
        """The task as it stands after an event bumped its revision (lock held)."""
        task = await repository.get_task(task_id)
        if task is None:  # pragma: no cover - tasks are never deleted.
            raise TaskNotFoundError(task_id)
        return task

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
            task, action = await self._lock_task_for_action(connection, action_id)
            self._check_revision(action, expected_revision)
            moved = await self._reject_locked(connection, task=task, action=action, reason=reason)
            return await self._view(connection, moved)

    async def _reject_locked(
        self,
        connection: AsyncConnection,
        *,
        task: TaskRecord,
        action: ActionRecord,
        reason: str | None,
    ) -> ActionRecord:
        repository = ActionRepository(connection)
        self._check_transition(action, ActionStatus.REJECTED)
        approval = await repository.get_open_approval(action.id)
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
        return moved

    async def reject_in_transaction(
        self,
        connection: AsyncConnection,
        *,
        task: TaskRecord,
        action: ActionRecord,
        reason: str,
    ) -> TaskRecord:
        """Reject inside a caller's transaction that already holds the task lock.

        For task-level changes (criteria revision, cancellation) that must
        invalidate a not-yet-executed booking atomically with the change itself.
        Returns the task as it stands afterwards, for the caller's next write.
        """
        await self._reject_locked(connection, task=task, action=action, reason=reason)
        current = await TaskRepository(connection).get_task(task.id)
        if current is None:  # pragma: no cover - tasks are never deleted.
            raise TaskNotFoundError(task.id)
        return current

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
        record: AttemptEvidenceWriter | None = None,
    ) -> ActionView:
        """Record what the executor observed.

        SUCCEEDED and FAILED are claims of knowledge. OUTCOME_UNKNOWN is how an
        executor says it lost the response and does not know whether the side
        effect happened; it is never retried automatically from here.

        `record` writes evidence that belongs to the finished attempt (a page
        observation) in this same transaction, so an attempt can never be
        SUCCEEDED without its evidence, or its evidence stored without it.
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
            if record is not None:
                await record(connection, finished)

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

    async def settle_exact_approval(
        self,
        action_id: uuid.UUID,
        *,
        expected_revision: int,
        guard: ApprovalGuard,
        result: dict[str, Any],
    ) -> ActionView:
        """Approve, consume and terminalise one exact approval, in ONE transaction.

        For an approval whose only consequence is the approval itself
        (Milestone 8b S5's disclosure manifest): nothing executes, so there is no
        worker, no dispatch and no page. The three ordinary steps -- grant the
        approval, claim it (single-use, re-checked in SQL against revision, digest
        and expiry), record a finished attempt -- still happen, in the ledger's
        own order, so the approval is spent exactly as an executed one would be
        and can never fund anything else.

        `guard` runs under the task lock, inside the transaction, after the
        approval is found pending and before it is granted. It is where a caller
        re-checks the facts the approval depends on (saved values, account,
        grants); if it raises, nothing at all is written.
        """
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
            await guard(connection, action)

            approved = await self._transition(
                connection,
                task=task,
                action=action,
                target=ActionStatus.APPROVED,
                event_type=TaskEventType.ACTION_APPROVED,
                payload={"approval_id": str(approval.id)},
            )
            granted = await repository.grant_approval(
                approval_id=approval.id, action_revision=approved.revision
            )
            if granted is None:  # pragma: no cover - the task row lock is held.
                raise ActionConcurrencyError(action_id)
            claimed = await repository.claim_approval(
                approval_id=approval.id,
                action_revision=approved.revision,
                proposal_digest=approved.proposal_digest,
            )
            if claimed is None:  # pragma: no cover - granted a moment ago, task lock held.
                raise ApprovalNotUsableError(action_id, "the approval no longer matches this action")
            attempt = await repository.insert_attempt(
                attempt_id=uuid.uuid4(),
                action_id=action_id,
                attempt_number=await repository.next_attempt_number(action_id),
                approval_id=claimed.id,
                runtime_generation=self._runtime_generation,
            )
            executing = await self._transition(
                connection,
                task=(await self._reload_task(TaskRepository(connection), task.id)),
                action=approved,
                target=ActionStatus.EXECUTING,
                event_type=TaskEventType.ACTION_EXECUTION_STARTED,
                payload={
                    "attempt_id": str(attempt.id),
                    "attempt_number": attempt.attempt_number,
                    "approval_id": str(claimed.id),
                    "runtime_generation": str(self._runtime_generation),
                },
            )
            finished = await repository.finish_attempt(
                attempt_id=attempt.id, outcome=AttemptOutcome.SUCCEEDED, result=result, error_code=None
            )
            if finished is None:  # pragma: no cover - the task row lock is held.
                raise ActionConcurrencyError(action_id)
            moved = await self._transition(
                connection,
                task=(await self._reload_task(TaskRepository(connection), task.id)),
                action=executing,
                target=ActionStatus.SUCCEEDED,
                event_type=TaskEventType.ACTION_SUCCEEDED,
                payload={
                    "attempt_id": str(finished.id),
                    "attempt_number": finished.attempt_number,
                    "outcome": AttemptOutcome.SUCCEEDED.value,
                    "error_code": None,
                    "reason": "approval_settled",
                    "result_code": result.get("code"),
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
