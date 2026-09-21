"""Milestone 9 S3: trusted focus, semantic scroll and registered-application launch, through the action ledger.

Nothing here is a parallel ledger. A desktop effect is an `actions` row with an exact `approvals` row, an
`action_attempts` row and (new) a `desktop_dispatches` row, exactly as a browser effect is. The order is the
same one that makes a browser booking safe:

    build the proposal from live, trusted facts        (the person picked a surface / control / app)
    -> request an exact approval for that proposal     (the card shows exactly what will happen)
    -> the person clicks Approve
    -> take the human-input baseline                   (after the click, so the click is not "takeover")
    -> ONE transaction: claim the approval, insert the attempt, action EXECUTING   (`begin_exact_execution`)
    -> ONE transaction: insert the dispatch, COMMIT    (the durable intent exists before the worker is touched)
    -> call the worker, outside every transaction
    -> ONE transaction: finish the dispatch AND the attempt together   (known success / known failure / unknown)

A worker answer that is lost, late or addressed to somebody else is `OUTCOME_UNKNOWN` and is never retried. A
runtime that dies between the commit and the answer leaves an unfinished attempt that `RecoveryService` turns
into `OUTCOME_UNKNOWN` and closes this dispatch. Focus and scroll are recoverable by a fresh observation and a
NEW approval; nothing here ever repeats an effect on its own.

Desktop disclosure (S2) is not authority for any of this: there is no path from a `desktop_disclose` grant to
these actions, and these actions take no grant.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import FocusRequest, LaunchRequest, ScrollRequest, ScrollStep, clean_text
from app.desktop.registry import AppRegistry
from app.domain.action_status import ActionStatus, AttemptOutcome
from app.domain.desktop_actions import (
    ACTION_OBSERVATION_MAX_AGE_SECONDS,
    DESKTOP_ACTION_TASK_TYPE,
    OPERATION_FOR_TOOL,
    RISK_FOR,
    TOOL_FOR,
    DesktopOperation,
    DesktopProposal,
    FocusProposal,
    LaunchProposal,
    ScrollProposal,
    dump_proposal,
    outcome_for_refusal,
    parse_proposal,
)
from app.repositories.actions import ActionRecord, AttemptRecord
from app.repositories.desktop import DesktopRepository
from app.repositories.desktop_actions import DesktopActionRepository
from app.services.actions import ActionService, ActionView
from app.services.desktop import DesktopService
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.desktop.actions")

_VERIFIED_NO_CHANGE: Final = frozenset({"not_focused", "scroll_no_change"})


class DesktopActionError(Exception):
    """A closed, text-free refusal. `code` is safe to log and to show."""

    def __init__(self, code: str, *, action_id: uuid.UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.action_id = action_id


@dataclass(frozen=True, slots=True)
class DesktopActionView:
    action_id: uuid.UUID
    task_id: uuid.UUID
    revision: int
    status: ActionStatus
    operation: DesktopOperation
    proposal: DesktopProposal
    approval_expires_at: datetime | None
    attempt_outcome: AttemptOutcome | None
    error_code: str | None
    result: dict[str, Any] | None


class DesktopActionService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        actions: ActionService,
        tasks: TaskService,
        desktop: DesktopService,
        registry: AppRegistry,
    ) -> None:
        self._engine = engine
        self._actions = actions
        self._tasks = tasks
        self._desktop = desktop
        self._registry = registry
        # One runtime process, one desktop, one effect at a time.
        self._execution = asyncio.Lock()
        self._proposals = asyncio.Lock()

    # ---- proposals (built by the runtime from live facts; the caller supplies only opaque choices) --------

    def registered_apps(self) -> list[tuple[str, str]]:
        return [(app.app_id, app.label) for app in self._registry.all()]

    async def propose_focus(
        self, *, worker_generation: uuid.UUID, surface_ref: str, surface_epoch: int
    ) -> DesktopActionView:
        label, title = await self._surface_facts(worker_generation, surface_ref, surface_epoch)
        proposal = FocusProposal(
            worker_generation=worker_generation,
            surface_ref=surface_ref,
            surface_epoch=surface_epoch,
            application_label=label,
            window_title=title,
        )
        return await self._open(proposal)

    async def propose_scroll(
        self,
        *,
        worker_generation: uuid.UUID,
        observation_id: uuid.UUID,
        control_ref: str,
        step: ScrollStep,
    ) -> DesktopActionView:
        async with self._engine.connect() as connection:
            repository = DesktopRepository(connection)
            observation = await repository.get_observation(observation_id)
            latest = (
                await repository.latest_observation_id(observation.worker_generation, observation.surface_ref)
                if observation is not None
                else None
            )
        if (
            observation is None
            or observation.worker_generation != worker_generation
            or latest != observation.id
            or _age_seconds(observation.created_at) > ACTION_OBSERVATION_MAX_AGE_SECONDS
        ):
            raise DesktopActionError("desktop_action_observation_stale")
        node = next(
            (item for item in observation.snapshot.get("nodes", []) if item.get("controlRef", item.get("control_ref")) == control_ref),
            None,
        )
        if node is None or "scroll" not in node.get("patterns", []):
            raise DesktopActionError("desktop_action_invalid")
        label, title = await self._surface_facts(worker_generation, observation.surface_ref, observation.surface_epoch)
        proposal = ScrollProposal(
            worker_generation=worker_generation,
            surface_ref=observation.surface_ref,
            surface_epoch=observation.surface_epoch,
            observation_id=observation.id,
            snapshot_digest=observation.snapshot_digest,
            control_ref=control_ref,
            step=step,
            application_label=label,
            window_title=title,
            control_role=clean_text(str(node.get("role", "unknown")))[:32],
            control_name=clean_text(str(node.get("name") or ""))[:120],
        )
        return await self._open(proposal)

    async def scroll_targets(
        self, *, worker_generation: uuid.UUID, surface_ref: str, surface_epoch: int
    ) -> tuple[uuid.UUID, list[tuple[str, str, str]]]:
        """Observe one surface locally and list ONLY its scrollable controls: `(control_ref, role, name)`.

        The observation id is opaque and short-lived (a scroll proposal against it must land within the
        action freshness window); nothing else of the observation leaves the runtime.
        """
        observation = await self._desktop.observe(worker_generation, surface_ref, surface_epoch)
        targets = [
            (
                node.control_ref,
                node.role.value,
                clean_text(node.name or "")[:120],
            )
            for node in observation.nodes
            if any(pattern.value == "scroll" for pattern in node.patterns)
        ]
        return observation.observation_id, targets[:20]

    async def propose_launch(self, *, app_id: str) -> DesktopActionView:
        app = self._registry.get(app_id)  # raises app_not_registered; the id is the whole input
        listing = await self._desktop.list_surfaces()
        proposal = LaunchProposal(
            worker_generation=listing.worker_generation, app_id=app.app_id, application_label=app.label
        )
        return await self._open(proposal)

    async def _surface_facts(self, worker_generation: uuid.UUID, ref: str, epoch: int) -> tuple[str, str]:
        listing = await self._desktop.list_surfaces()
        if listing.worker_generation != worker_generation:
            raise DesktopActionError("desktop_action_stale")
        for surface in listing.surfaces:
            if surface.surface_ref == ref and surface.surface_epoch == epoch:
                return surface.application_label, surface.window_title
        raise DesktopActionError("desktop_action_stale")

    async def _open(self, proposal: DesktopProposal) -> DesktopActionView:
        # Serialized with other proposals so two concurrent ones cannot both leave a card waiting. It is NOT held
        # during an effect: a proposal made meanwhile is refused (`desktop_action_open`), not queued.
        async with self._proposals:
            return await self._open_locked(proposal)

    async def _open_locked(self, proposal: DesktopProposal) -> DesktopActionView:
        async with self._engine.connect() as connection:
            live = await DesktopActionRepository(connection).live_desktop_actions()
        for other_id, _tool, status in live:
            if status in (ActionStatus.PROPOSED.value, ActionStatus.WAITING_APPROVAL.value):
                # A card the person never answered is superseded by the new request, never left to be
                # approved later against a screen that has moved on.
                stale = await self._actions.get_action(other_id)
                await self._actions.reject_action(other_id, expected_revision=stale.action.revision, reason="superseded")
            else:
                # Approved or running: one desktop action at a time.
                raise DesktopActionError("desktop_action_open", action_id=other_id)
        operation = DesktopOperation(proposal.operation)
        task = await self._tasks.create_task({"type": DESKTOP_ACTION_TASK_TYPE, "operation": operation.value})
        created = await self._actions.propose_exclusive_action(
            task.id, tool_name=TOOL_FOR[operation], risk_tier=RISK_FOR[operation], proposal=dump_proposal(proposal)
        )
        requested = await self._actions.request_approval(created.action.id, expected_revision=created.action.revision)
        return self._view(requested)

    # ---- reads -------------------------------------------------------------------------------------------

    async def get(self, action_id: uuid.UUID) -> DesktopActionView:
        view = await self._actions.get_action(action_id)
        if view.action.tool_name not in OPERATION_FOR_TOOL:
            raise DesktopActionError("desktop_action_not_found")
        return self._view(view)

    async def latest(self) -> DesktopActionView | None:
        async with self._engine.connect() as connection:
            row = await DesktopActionRepository(connection).latest_desktop_action_id()
        return await self.get(row) if row is not None else None

    async def decline(self, action_id: uuid.UUID, *, expected_revision: int) -> DesktopActionView:
        current = await self.get(action_id)
        if current.status not in (ActionStatus.PROPOSED, ActionStatus.WAITING_APPROVAL):
            raise DesktopActionError("desktop_action_not_declinable", action_id=action_id)
        rejected = await self._actions.reject_action(
            action_id, expected_revision=expected_revision, reason="declined_by_user"
        )
        return self._view(rejected)

    # ---- approval and execution ----------------------------------------------------------------------------

    async def approve(self, action_id: uuid.UUID, *, expected_revision: int) -> DesktopActionView:
        """The exact-approval click. The ONLY way any desktop effect can begin."""
        async with self._execution:
            current = await self.get(action_id)
            proposal = current.proposal
            if current.status is not ActionStatus.WAITING_APPROVAL or current.revision != expected_revision:
                raise DesktopActionError("desktop_action_not_approvable", action_id=action_id)

            # The human-input baseline is taken AFTER the click and BEFORE anything durable, in the very worker
            # the proposal names. If that worker is gone the proposal can never run: reject it.
            try:
                baseline = await self._desktop.input_baseline(proposal.worker_generation)
            except DesktopRefusal as refusal:
                await self._reject_quietly(action_id, expected_revision, refusal.code.value)
                raise DesktopActionError(refusal.code.value, action_id=action_id) from None

            async def guard(connection: AsyncConnection, action: ActionRecord) -> None:
                await self._guard(connection, action, proposal)

            started = await self._actions.begin_exact_execution(
                action_id, expected_revision=expected_revision, guard=guard
            )
            attempt = started.attempts[-1]
            dispatch_id = uuid.uuid4()
            try:
                async with self._engine.begin() as connection:
                    await DesktopActionRepository(connection).insert_dispatch(
                        dispatch_id=dispatch_id,
                        action_id=action_id,
                        attempt_id=attempt.id,
                        worker_generation=proposal.worker_generation,
                        operation=proposal.operation.value,
                        input_tick=baseline,
                        **_identity(proposal),
                    )
            except Exception:  # noqa: BLE001 - nothing has been sent; the intent could not be recorded.
                logger.error("a desktop dispatch could not be recorded; no effect was requested")
                done = await self._finish(
                    action_id, attempt, dispatch_id, dispatched=False,
                    outcome=AttemptOutcome.FAILED, error_code="dispatch_not_recorded", result={},
                )
                return self._view(done)

            # The effect and its bookkeeping run as ONE task that a cancelled request cannot interrupt: a caller that
            # goes away mid-call must not leave an action EXECUTING (which would block every other desktop action).
            settle = asyncio.ensure_future(self._settle(action_id, attempt, dispatch_id, proposal, baseline))
            done = await asyncio.shield(settle)
            return self._view(done)

    async def _settle(
        self, action_id: uuid.UUID, attempt: AttemptRecord, dispatch_id: uuid.UUID, proposal: DesktopProposal, baseline: int
    ) -> ActionView:
        outcome, error_code, result = await self._perform(proposal, dispatch_id, baseline)
        return await self._finish(
            action_id, attempt, dispatch_id, dispatched=True, outcome=outcome, error_code=error_code, result=result
        )

    async def _guard(self, connection: AsyncConnection, action: ActionRecord, proposal: DesktopProposal) -> None:
        """Runs under the task lock, in the claim transaction. If it raises, nothing at all is written."""
        repository = DesktopActionRepository(connection)
        if await repository.executing_desktop_actions(excluding=action.id):
            raise DesktopActionError("desktop_action_busy")
        for other_id, _tool, status in await repository.live_desktop_actions(excluding=action.id):
            # Another approved or running desktop action is never raced by this one.
            if status in (ActionStatus.APPROVED.value, ActionStatus.EXECUTING.value):
                raise DesktopActionError("desktop_action_open", action_id=other_id)
        if isinstance(proposal, ScrollProposal):
            observation = await DesktopRepository(connection).get_observation(proposal.observation_id)
            if (
                observation is None
                or observation.snapshot_digest != proposal.snapshot_digest
                or _age_seconds(observation.created_at) > ACTION_OBSERVATION_MAX_AGE_SECONDS
            ):
                raise DesktopActionError("desktop_action_observation_stale")

    async def _perform(
        self, proposal: DesktopProposal, dispatch_id: uuid.UUID, baseline: int
    ) -> tuple[AttemptOutcome, str | None, dict[str, Any]]:
        """Call the worker (no transaction open) and say only what is actually known."""
        try:
            if isinstance(proposal, FocusProposal):
                focus = await self._desktop.focus(
                    FocusRequest(
                        expected_worker_generation=proposal.worker_generation,
                        dispatch_id=dispatch_id,
                        surface_ref=proposal.surface_ref,
                        surface_epoch=proposal.surface_epoch,
                        input_tick=baseline,
                    )
                )
                result = {"operation": proposal.operation.value, "outcome": focus.outcome, "human_input_during": focus.input_changed}
                if focus.outcome == "focused":
                    return AttemptOutcome.SUCCEEDED, None, result
                return AttemptOutcome.FAILED, "not_focused", result
            if isinstance(proposal, ScrollProposal):
                scroll = await self._desktop.scroll(
                    ScrollRequest(
                        expected_worker_generation=proposal.worker_generation,
                        dispatch_id=dispatch_id,
                        surface_ref=proposal.surface_ref,
                        surface_epoch=proposal.surface_epoch,
                        observation_id=proposal.observation_id,
                        control_ref=proposal.control_ref,
                        step=proposal.step,
                        input_tick=baseline,
                    )
                )
                result = {
                    "operation": proposal.operation.value,
                    "outcome": scroll.outcome,
                    "percent_before": scroll.percent_before,
                    "percent_after": scroll.percent_after,
                    "human_input_during": scroll.input_changed,
                    # The refs of the observation this was proposed against are dead. A fresh read is the
                    # only thing that can name the new state.
                    "observation_invalidated": True,
                }
                if scroll.outcome == "unchanged":
                    return AttemptOutcome.FAILED, "scroll_no_change", result
                result["follow_up_observation_id"] = await self._reobserve(proposal)
                return AttemptOutcome.SUCCEEDED, None, result
            launch = await self._desktop.launch(
                LaunchRequest(
                    expected_worker_generation=proposal.worker_generation,
                    dispatch_id=dispatch_id,
                    app_id=proposal.app_id,
                    input_tick=baseline,
                )
            )
            return (
                AttemptOutcome.SUCCEEDED,
                None,
                {
                    "operation": proposal.operation.value,
                    "outcome": launch.outcome,
                    "surface_ref": launch.surface_ref,
                    "surface_epoch": launch.surface_epoch,
                    "focused": launch.focused,
                    "human_input_during": launch.input_changed,
                },
            )
        except DesktopRefusal as refusal:
            return outcome_for_refusal(refusal.code), refusal.code.value, {"operation": proposal.operation.value}
        except Exception:  # noqa: BLE001 - the answer was lost or garbled; the effect may have happened.
            logger.warning("a desktop effect ended without a usable answer")
            return (
                AttemptOutcome.OUTCOME_UNKNOWN,
                DesktopReason.EFFECT_UNCERTAIN.value,
                {"operation": proposal.operation.value},
            )

    async def _reobserve(self, proposal: ScrollProposal) -> str | None:
        """A fresh S1 observation of the same window. The old one is never extended or reused."""
        try:
            listing = await self._desktop.list_surfaces()
            surface = next(
                (
                    item for item in listing.surfaces
                    if item.surface_ref == proposal.surface_ref
                    and item.surface_epoch >= proposal.surface_epoch
                    and item.application_label == proposal.application_label
                ),
                None,
            )
            if surface is None:
                return None
            observation = await self._desktop.observe(
                proposal.worker_generation, surface.surface_ref, surface.surface_epoch
            )
            return str(observation.observation_id)
        except Exception:  # noqa: BLE001 - the scroll happened; the re-read is best effort and says so.
            return None

    async def _finish(
        self,
        action_id: uuid.UUID,
        attempt: AttemptRecord,
        dispatch_id: uuid.UUID,
        *,
        dispatched: bool,
        outcome: AttemptOutcome,
        error_code: str | None,
        result: dict[str, Any],
    ) -> ActionView:
        if outcome is AttemptOutcome.SUCCEEDED:
            status = "OK"
        elif outcome is AttemptOutcome.OUTCOME_UNKNOWN:
            status = "OUTCOME_UNKNOWN"
        else:
            # The worker answered and verified nothing changed (`OK`), or refused before any effect.
            status = "OK" if error_code in _VERIFIED_NO_CHANGE else "FAILED_BEFORE_EFFECT"

        async def record(connection: AsyncConnection, _finished: AttemptRecord) -> None:
            if dispatched:
                await DesktopActionRepository(connection).finish_dispatch(
                    dispatch_id=dispatch_id, status=status, error_code=error_code, result=result
                )

        return await self._actions.finish_attempt(
            action_id, outcome=outcome, result=result, error_code=error_code, record=record
        )

    async def _reject_quietly(self, action_id: uuid.UUID, revision: int, reason: str) -> None:
        try:
            await self._actions.reject_action(action_id, expected_revision=revision, reason=reason)
        except Exception:  # noqa: BLE001 - it stays waiting and expires; no effect either way.
            logger.warning("a desktop action could not be rejected after its worker went away")

    @staticmethod
    def _view(view: ActionView) -> DesktopActionView:
        action = view.action
        proposal = parse_proposal(action.proposal)
        attempt = view.attempts[-1] if view.attempts else None
        return DesktopActionView(
            action_id=action.id,
            task_id=action.task_id,
            revision=action.revision,
            status=action.status,
            operation=DesktopOperation(proposal.operation),
            proposal=proposal,
            approval_expires_at=view.approval.expires_at if view.approval is not None else None,
            attempt_outcome=attempt.outcome if attempt is not None else None,
            error_code=attempt.error_code if attempt is not None else None,
            result=attempt.result if attempt is not None else None,
        )


def _identity(proposal: DesktopProposal) -> dict[str, Any]:
    if isinstance(proposal, FocusProposal):
        return {"surface_ref": proposal.surface_ref, "surface_epoch": proposal.surface_epoch}
    if isinstance(proposal, ScrollProposal):
        return {
            "surface_ref": proposal.surface_ref,
            "surface_epoch": proposal.surface_epoch,
            "observation_id": proposal.observation_id,
            "snapshot_digest": proposal.snapshot_digest,
            "control_ref": proposal.control_ref,
        }
    return {"app_id": proposal.app_id}


def _age_seconds(moment: datetime) -> float:
    return (datetime.now(UTC) - moment).total_seconds()

