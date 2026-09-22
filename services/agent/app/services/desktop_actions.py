"""Milestone 9 S3/S4: trusted focus, semantic scroll, registered-application launch, and bounded
semantic mutations (set value / select / invoke), through the action ledger.

Nothing here is a parallel ledger. A desktop effect is an `actions` row with an exact `approvals` row, an
`action_attempts` row and a `desktop_dispatches` row, exactly as a browser effect is. The order is the
same one that makes a browser booking safe:

    build the proposal from live, trusted facts        (S3: the person picked a surface/control/app.
                                                          S4: a validated model proposal, re-verified
                                                          against a fresh projection and re-resolved
                                                          live facts -- see `propose_from_plan`)
    -> request an exact approval for that proposal     (the card shows exactly what will happen)
    -> the person clicks Approve
    -> take the human-input baseline                   (after the click, so the click is not "takeover")
    -> ONE transaction: claim the approval, insert the attempt, action EXECUTING   (`begin_exact_execution`)
    -> ONE transaction: insert the dispatch, COMMIT    (the durable intent exists before the worker is touched)
    -> call the worker, outside every transaction
    -> ONE transaction: finish the dispatch AND the attempt together   (known success / known failure / unknown)

A worker answer that is lost, late or addressed to somebody else is `OUTCOME_UNKNOWN` and is never retried. A
runtime that dies between the commit and the answer leaves an unfinished attempt that `RecoveryService` turns
into `OUTCOME_UNKNOWN` and closes this dispatch. Focus, scroll and launch are recoverable by a fresh
observation and a NEW approval; nothing here ever repeats an effect on its own. The three S4 mutations are
NOT recoverable this way: an unresolved (`OUTCOME_UNKNOWN`/`RECONCILING`) mutation blocks every new desktop
action -- of any operation, in any task -- until it is reconciled (see `reconcile`).

Desktop disclosure (S2) and desktop action planning (S4, `app.services.desktop_planning`) are not execution
authority for any of this: there is no path from a `desktop_disclose` or `desktop_action_plan` grant straight
to an effect. A plan's validated proposal only ever reaches `propose_from_plan`, which independently re-reads
and re-verifies everything before it opens an ordinary WAITING_APPROVAL card -- a second, separate, exact
approval the plan itself cannot grant.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    FocusRequest,
    InvokeEffect,
    InvokeRequest,
    LaunchRequest,
    ScrollRequest,
    ScrollStep,
    SelectRequest,
    SetValueRequest,
    clean_text,
)
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
    InvokeProposal,
    LaunchProposal,
    ScrollProposal,
    SelectProposal,
    SetValueProposal,
    dump_proposal,
    outcome_for_refusal,
    parse_proposal,
)
from app.domain.desktop_disclosure import build_projection
from app.domain.desktop_planning import InvokeAction, SelectAction, SetValueAction, parse_planned_action
from app.repositories.actions import ActionRecord, AttemptRecord
from app.repositories.desktop import DesktopRepository
from app.repositories.desktop_actions import DesktopActionRepository
from app.repositories.desktop_planning import DesktopPlanningRepository
from app.services.actions import ActionService, ActionView
from app.services.desktop import DesktopService
from app.services.tasks import TaskService

logger = logging.getLogger("lumi.desktop.actions")

_VERIFIED_NO_CHANGE: Final = frozenset({"not_focused", "scroll_no_change", "not_set", "not_selected", "no_change"})
#: Proposal types whose target must be re-verified against a fresh observation at claim time: every S4
#: mutation, exactly like S3's scroll.
_FRESHNESS_CHECKED = (ScrollProposal, SetValueProposal, SelectProposal, InvokeProposal)
#: Proposal types built from a plan (`propose_from_plan`): the only ones carrying `plan_id`.
_PLAN_DERIVED = (SetValueProposal, SelectProposal, InvokeProposal)


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

    # ---- S4: from a validated plan to an ordinary, separately-approvable execution proposal --------

    async def propose_from_plan(self, plan_id: uuid.UUID) -> DesktopActionView:
        """Turn one SUCCEEDED `desktop_action_plans` row into an ordinary `DESKTOP_SET_VALUE` /
        `DESKTOP_SELECT` / `DESKTOP_INVOKE` action, awaiting its own separate exact approval.

        Disclosure to a model is not execution authority: nothing here trusts the plan's own recorded
        `proposed_action` blindly. Every fact that matters is re-read and re-verified from scratch --
        the plan is STARTED->SUCCEEDED and unconsumed by an action already, the observation still
        exists at exactly the digest the plan claimed, the projection rebuilt from it still contains
        every ref the action names, and (for `set_value`) the value ref still resolves inside the
        grant's own immutable scope. A live worker generation and a fresh identity are re-proved again,
        a second time, by the worker itself immediately before the effect -- this method only decides
        whether a card may be shown at all.
        """
        async with self._engine.connect() as connection:
            plan_repository = DesktopPlanningRepository(connection)
            plan = await plan_repository.get_plan(plan_id)
            grant = await plan_repository.get_grant(plan.grant_id) if plan is not None else None
        if plan is None or grant is None or plan.status != "SUCCEEDED" or plan.proposed_action is None:
            raise DesktopActionError("desktop_plan_not_ready")
        async with self._engine.connect() as connection:
            existing = await DesktopActionRepository(connection).action_for_plan(plan_id)
        if existing is not None:
            # A plan proposes at most one execution card, ever: replaying `record_result`'s caller
            # (a retried HTTP request, for instance) must not open a second one.
            raise DesktopActionError("desktop_plan_already_opened", action_id=existing)
        scope = grant.scope
        if _age_seconds(scope.observed_at) > ACTION_OBSERVATION_MAX_AGE_SECONDS:
            raise DesktopActionError("desktop_action_observation_stale")
        async with self._engine.connect() as connection:
            record = await DesktopRepository(connection).get_observation(scope.observation_id)
        if record is None or record.snapshot_digest != scope.snapshot_digest:
            raise DesktopActionError("desktop_action_observation_stale")
        projection = build_projection(
            record.snapshot, observed_at=scope.observed_at, max_nodes=scope.max_nodes,
            max_text_bytes=scope.max_text_bytes, withhold=scope.withheld(),
        )
        action = parse_planned_action(plan.proposed_action)
        label, title = scope.display.application_label, scope.display.window_title
        proposal: DesktopProposal
        if isinstance(action, InvokeAction):
            node = projection.node(action.control_ref)
            if node is None:
                raise DesktopActionError("desktop_action_invalid")
            proposal = InvokeProposal(
                worker_generation=scope.worker_generation, plan_id=plan_id, surface_ref=scope.surface_ref,
                surface_epoch=scope.surface_epoch, observation_id=scope.observation_id,
                snapshot_digest=scope.snapshot_digest, control_ref=action.control_ref,
                effect=InvokeEffect.NAME_TOGGLE, application_label=label, window_title=title,
                control_role=str(node.get("role", "unknown"))[:32],
                control_name=clean_text(str(node.get("name") or ""))[:120],
            )
        elif isinstance(action, SetValueAction):
            raw = scope.resolve_value(action.value_ref)
            node = projection.node(action.control_ref)
            if raw is None or node is None:
                raise DesktopActionError("desktop_action_invalid")
            proposal = SetValueProposal(
                worker_generation=scope.worker_generation, plan_id=plan_id, surface_ref=scope.surface_ref,
                surface_epoch=scope.surface_epoch, observation_id=scope.observation_id,
                snapshot_digest=scope.snapshot_digest, control_ref=action.control_ref,
                value_ref=action.value_ref, value=raw, application_label=label, window_title=title,
                control_role=str(node.get("role", "unknown"))[:32],
                control_name=clean_text(str(node.get("name") or ""))[:120],
            )
        else:
            container = projection.node(action.container_ref)
            option = projection.node(action.option_ref)
            if container is None or option is None:
                raise DesktopActionError("desktop_action_invalid")
            proposal = SelectProposal(
                worker_generation=scope.worker_generation, plan_id=plan_id, surface_ref=scope.surface_ref,
                surface_epoch=scope.surface_epoch, observation_id=scope.observation_id,
                snapshot_digest=scope.snapshot_digest, container_ref=action.container_ref,
                option_ref=action.option_ref, application_label=label, window_title=title,
                container_role=str(container.get("role", "unknown"))[:32],
                container_name=clean_text(str(container.get("name") or ""))[:120],
                option_role=str(option.get("role", "unknown"))[:32],
                option_name=clean_text(str(option.get("name") or ""))[:120],
            )
        return await self._open(proposal)

    # ---- S4: getting unstuck after an unresolved mutation -------------------------------------------

    async def reconcile(
        self, action_id: uuid.UUID, *, expected_revision: int, outcome: Literal["succeeded", "failed", "still_unknown"]
    ) -> DesktopActionView:
        """The only way out of an unresolved (`OUTCOME_UNKNOWN`) S4 mutation. Never retries the effect
        and never re-derives it automatically: the person looked at the actual application and says
        what they saw. `still_unknown` leaves the action, and the block on every other desktop action,
        exactly where it was -- a look that could not tell is not progress."""
        current = await self.get(action_id)
        if current.operation not in (DesktopOperation.SET_VALUE, DesktopOperation.SELECT, DesktopOperation.INVOKE):
            raise DesktopActionError("desktop_action_not_reconcilable", action_id=action_id)
        if current.status is not ActionStatus.OUTCOME_UNKNOWN:
            raise DesktopActionError("desktop_action_not_reconcilable", action_id=action_id)
        started = await self._actions.begin_reconciliation(action_id, expected_revision=expected_revision)
        mapped = {
            "succeeded": AttemptOutcome.SUCCEEDED,
            "failed": AttemptOutcome.FAILED,
            "still_unknown": AttemptOutcome.OUTCOME_UNKNOWN,
        }[outcome]
        finished = await self._actions.finish_reconciliation(
            action_id, result=mapped, evidence={"source": "user_observed"},
            expected_revision=started.action.revision,
        )
        return await self._view(finished)

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
            repository = DesktopActionRepository(connection)
            live = await repository.live_desktop_actions()
            unresolved = await repository.unresolved_mutation()
            existing_for_plan = (
                await repository.action_for_plan(proposal.plan_id) if isinstance(proposal, _PLAN_DERIVED) else None
            )
        if unresolved is not None:
            # A set-value, select or invoke Lumi cannot account for blocks EVERY new desktop action --
            # not just another mutation -- until it is reconciled. No new task, plan, observation or
            # route can side-step it.
            raise DesktopActionError("desktop_action_unresolved", action_id=unresolved)
        if existing_for_plan is not None:
            # Found by an independent integration review: `propose_from_plan`'s OWN `action_for_plan`
            # check races the `_proposals` lock this method holds -- two concurrent calls for the SAME
            # plan (a retried HTTP request, say) can both observe "no existing action" before either
            # reaches here. Re-checked a second time, now serialized by the lock every `_open_locked`
            # call already holds, so a plan can fund at most one execution card, ever, exactly as
            # documented -- not merely "at most one that can ever be approved" (which the live-action
            # supersession below already guaranteed on its own).
            raise DesktopActionError("desktop_plan_already_opened", action_id=existing_for_plan)
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
        return await self._view(requested)

    # ---- reads -------------------------------------------------------------------------------------------

    async def get(self, action_id: uuid.UUID) -> DesktopActionView:
        view = await self._actions.get_action(action_id)
        if view.action.tool_name not in OPERATION_FOR_TOOL:
            raise DesktopActionError("desktop_action_not_found")
        return await self._view(view)

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
        return await self._view(rejected)

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
                return await self._view(done)

            # The effect and its bookkeeping run as ONE task that a cancelled request cannot interrupt: a caller that
            # goes away mid-call must not leave an action EXECUTING (which would block every other desktop action).
            settle = asyncio.ensure_future(self._settle(action_id, attempt, dispatch_id, proposal, baseline))
            done = await asyncio.shield(settle)
            return await self._view(done)

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
        if isinstance(proposal, _FRESHNESS_CHECKED):
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
            if isinstance(proposal, LaunchProposal):
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
            if isinstance(proposal, SetValueProposal):
                set_value = await self._desktop.set_value(
                    SetValueRequest(
                        expected_worker_generation=proposal.worker_generation,
                        dispatch_id=dispatch_id,
                        surface_ref=proposal.surface_ref,
                        surface_epoch=proposal.surface_epoch,
                        observation_id=proposal.observation_id,
                        control_ref=proposal.control_ref,
                        value=proposal.value,
                        input_tick=baseline,
                    )
                )
                result = {
                    "operation": proposal.operation.value,
                    "outcome": set_value.outcome,
                    "human_input_during": set_value.input_changed,
                    "observation_invalidated": True,
                }
                if set_value.outcome == "uncertain":
                    # The write itself may have happened; the verifying re-read did not. This is
                    # never treated as a known non-effect: it blocks every later desktop action until
                    # a human reconciles it, exactly like a lost worker answer.
                    return AttemptOutcome.OUTCOME_UNKNOWN, "set_value_uncertain", result
                if set_value.outcome == "not_set":
                    return AttemptOutcome.FAILED, "not_set", result
                result["follow_up_observation_id"] = await self._reobserve(proposal)
                return AttemptOutcome.SUCCEEDED, None, result
            if isinstance(proposal, SelectProposal):
                select = await self._desktop.select(
                    SelectRequest(
                        expected_worker_generation=proposal.worker_generation,
                        dispatch_id=dispatch_id,
                        surface_ref=proposal.surface_ref,
                        surface_epoch=proposal.surface_epoch,
                        observation_id=proposal.observation_id,
                        container_ref=proposal.container_ref,
                        option_ref=proposal.option_ref,
                        input_tick=baseline,
                    )
                )
                result = {
                    "operation": proposal.operation.value,
                    "outcome": select.outcome,
                    "human_input_during": select.input_changed,
                    "observation_invalidated": True,
                }
                if select.outcome == "uncertain":
                    return AttemptOutcome.OUTCOME_UNKNOWN, "select_uncertain", result
                if select.outcome == "not_selected":
                    return AttemptOutcome.FAILED, "not_selected", result
                result["follow_up_observation_id"] = await self._reobserve(proposal)
                return AttemptOutcome.SUCCEEDED, None, result
            invoke = await self._desktop.invoke(
                InvokeRequest(
                    expected_worker_generation=proposal.worker_generation,
                    dispatch_id=dispatch_id,
                    surface_ref=proposal.surface_ref,
                    surface_epoch=proposal.surface_epoch,
                    observation_id=proposal.observation_id,
                    control_ref=proposal.control_ref,
                    effect=proposal.effect,
                    input_tick=baseline,
                )
            )
            result = {
                "operation": proposal.operation.value,
                "outcome": invoke.outcome,
                "human_input_during": invoke.input_changed,
                "observation_invalidated": True,
            }
            if invoke.outcome == "no_change":
                return AttemptOutcome.FAILED, "no_change", result
            result["follow_up_observation_id"] = await self._reobserve(proposal)
            return AttemptOutcome.SUCCEEDED, None, result
        except DesktopRefusal as refusal:
            return outcome_for_refusal(refusal.code), refusal.code.value, {"operation": proposal.operation.value}
        except Exception:  # noqa: BLE001 - the answer was lost or garbled; the effect may have happened.
            logger.warning("a desktop effect ended without a usable answer")
            return (
                AttemptOutcome.OUTCOME_UNKNOWN,
                DesktopReason.EFFECT_UNCERTAIN.value,
                {"operation": proposal.operation.value},
            )

    async def _reobserve(
        self, proposal: ScrollProposal | SetValueProposal | SelectProposal | InvokeProposal
    ) -> str | None:
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

    async def _resolve_set_value(self, proposal: SetValueProposal) -> SetValueProposal:
        """`dump_proposal()` never persists a `SetValueProposal`'s raw text (see the field's own
        docstring in `app.domain.desktop_actions`): the durable ledger row only ever carries
        `value_ref`. Every read of a set-value action -- the initial card, a later status poll, and
        the actual `approve()` call that performs the write -- re-resolves the text fresh from
        `task_grants.scope` (`StoredValue.raw`), the one place S4 keeps it at rest, by `plan_id` and
        `value_ref`. The grant row is immutable and never deleted, so this resolves identically
        whether the mutation already ran or not; if it ever cannot resolve, that is treated as
        `desktop_action_invalid` rather than silently writing or displaying an empty value."""
        async with self._engine.connect() as connection:
            plan_repository = DesktopPlanningRepository(connection)
            plan = await plan_repository.get_plan(proposal.plan_id)
            grant = await plan_repository.get_grant(plan.grant_id) if plan is not None else None
        resolved = grant.scope.resolve_value(proposal.value_ref) if grant is not None else None
        if resolved is None:
            raise DesktopActionError("desktop_action_invalid")
        return proposal.model_copy(update={"value": resolved})

    async def _view(self, view: ActionView) -> DesktopActionView:
        action = view.action
        proposal = parse_proposal(action.proposal)
        if isinstance(proposal, SetValueProposal):
            proposal = await self._resolve_set_value(proposal)
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
    if isinstance(proposal, LaunchProposal):
        return {"app_id": proposal.app_id}
    if isinstance(proposal, SetValueProposal):
        return {
            "surface_ref": proposal.surface_ref,
            "surface_epoch": proposal.surface_epoch,
            "observation_id": proposal.observation_id,
            "snapshot_digest": proposal.snapshot_digest,
            "control_ref": proposal.control_ref,
            "value_ref": proposal.value_ref,
        }
    if isinstance(proposal, SelectProposal):
        return {
            "surface_ref": proposal.surface_ref,
            "surface_epoch": proposal.surface_epoch,
            "observation_id": proposal.observation_id,
            "snapshot_digest": proposal.snapshot_digest,
            "control_ref": proposal.option_ref,
            "option_container_ref": proposal.container_ref,
        }
    return {
        "surface_ref": proposal.surface_ref,
        "surface_epoch": proposal.surface_epoch,
        "observation_id": proposal.observation_id,
        "snapshot_digest": proposal.snapshot_digest,
        "control_ref": proposal.control_ref,
        "invoke_effect": proposal.effect.value,
    }


def _age_seconds(moment: datetime) -> float:
    return (datetime.now(UTC) - moment).total_seconds()

