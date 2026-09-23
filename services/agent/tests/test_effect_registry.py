"""Milestone 10 S5: the closed effect registry and the shared cross-executor lock.

The invariant under test: an uncertain consequential effect cannot be repeated or bypassed by switching
task, executor, provider, model or route. Keys come only from the registry (never a model, a page or a
caller), every path into EXECUTING takes the same lock, the generic ledger routes cannot mint, start or
settle an effect tool, Stop never erases uncertainty, reconciliation is read-only and bounded, and a
non-authoritative "not found" never becomes a failure (and so never a safe retry).

These are real-PostgreSQL service tests; the booking crash itself (a hard-killed runtime and worker, a
real fixture site) is `test_acceptance_f_booking_recovery.py`.
"""

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.browser.client import BrowserWorkerClient
from app.domain.action_status import ActionStatus, AttemptOutcome, RiskTier
from app.domain.browser_dispatch import DispatchStatus
from app.domain.effects import (
    DESKTOP_MUTATION_KEY,
    EFFECT_TOOLS,
    GLOBAL_TIER,
    RECONCILIATION_REGISTRY,
    AbsenceAuthority,
    EffectKey,
    EffectKeysError,
    EffectKind,
    EffectLockedError,
    ReconciliationLimitedError,
    booking_key,
    download_keys,
    is_effect_tool_name,
    lookup_allowance,
    placement_key,
    resolve_effect_keys,
)
from app.domain.errors import (
    ActionAlreadyOpenError,
    ApprovalNotUsableError,
    InvalidActionTransitionError,
    TaskHasUnresolvedActionError,
)
from app.domain.task_status import TaskStatus
from app.repositories.actions import ActionRecord
from app.repositories.browser import BrowserRepository
from app.repositories.effects import EffectLockRepository
from app.services.actions import ActionService, ActionView
from app.services.booking_tasks import BookingTaskService
from app.services.browser_execution import BrowserExecutionService, Outcome
from app.services.recovery import RecoveryService
from app.services.runtime import register_runtime_generation
from app.services.tasks import TaskService

BOOKING: dict[str, Any] = {
    "site": "appointment_fixture",
    "slot_id": "slot-a-1830",
    "doctor": "Dr A",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
    "currency": "INR",
}
ROOT = str(uuid.UUID(int=7))
#: The table's CHECK, copied: every key the registry can derive must satisfy it.
KEY_SHAPE = re.compile(r"^[a-z_]+:[a-z_]+:[A-Za-z0-9:._-]{1,140}$")


# ---- helpers ---------------------------------------------------------------------------------------------


async def approved_booking(
    actions: ActionService, tasks: TaskService, proposal: dict[str, Any] = BOOKING, *, request: dict[str, Any] | None = None
) -> ActionView:
    task = await tasks.create_task(request or {"type": "appointment_booking"})
    view = await actions.propose_exclusive_action(task.id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=proposal)
    view = await actions.request_approval(view.action.id)
    return await actions.approve_action(view.action.id)


async def unknown_booking(actions: ActionService, tasks: TaskService, proposal: dict[str, Any] = BOOKING) -> ActionView:
    view = await approved_booking(actions, tasks, proposal)
    await actions.start_attempt(view.action.id)
    return await actions.finish_attempt(view.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN, error_code="lost_response")


async def desktop_mutation_card(actions: ActionService, tasks: TaskService, tool: str = "DESKTOP_SET_VALUE") -> ActionView:
    task = await tasks.create_task({"type": "desktop_action", "operation": "set_control_value"})
    view = await actions.propose_exclusive_action(task.id, tool_name=tool, risk_tier=RiskTier.R2, proposal={"operation": "x"})
    return await actions.request_approval(view.action.id)


async def _no_guard(_connection: AsyncConnection, _action: ActionRecord) -> None:
    return None


class _NeverMinted:
    """A scoped authorizer for refusal tests: the lock refuses BEFORE anything is minted."""

    minted = 0

    def authorization_payload(self) -> dict[str, Any]:
        return {"grant_id": "test"}

    async def mint(self, connection: AsyncConnection, *, action: ActionRecord) -> uuid.UUID:
        type(self).minted += 1
        raise AssertionError("the effect lock must refuse before any authorization is minted")

    async def consume(self, connection: AsyncConnection, **_: Any) -> bool:  # pragma: no cover - never reached
        return False


async def key_rows(engine: AsyncEngine, action_id: uuid.UUID) -> list[tuple[str, str]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT effect_key, effect_kind FROM action_effect_keys WHERE action_id = :a ORDER BY effect_key"),
            {"a": action_id},
        )
        return [(row.effect_key, row.effect_kind) for row in rows]


async def count(engine: AsyncEngine, sql: str, **params: Any) -> int:
    async with engine.connect() as connection:
        value = (await connection.execute(text(sql), params)).scalar_one()
    return int(value)


# ---- 1. the registry is closed, and only it decides ------------------------------------------------------


def test_the_registry_tool_names_are_the_ones_their_owners_execute() -> None:
    from app.domain.desktop_actions import MODEL_PROPOSED_OPERATIONS, TOOL_FOR
    from app.domain.projects import TOOL_PROJECT_START
    from app.domain.transfers import TOOL_DOWNLOAD, TOOL_PLACE
    from app.services.browser_execution import COMMIT_BOOKING

    assert set(EFFECT_TOOLS) == {
        COMMIT_BOOKING,
        TOOL_DOWNLOAD,
        TOOL_PLACE,
        TOOL_PROJECT_START,
        *(TOOL_FOR[operation] for operation in MODEL_PROPOSED_OPERATIONS),
    }
    assert GLOBAL_TIER == {EffectKind.EXTERNAL_MUTATION, EffectKind.PROJECT_RUN}
    # Every kind has exactly one reviewed reconciliation rule, and each says who may call absence authoritative.
    assert set(RECONCILIATION_REGISTRY) == set(EffectKind)
    assert RECONCILIATION_REGISTRY[EffectKind.EXTERNAL_MUTATION].absence is AbsenceAuthority.SITE_DECLARED
    assert RECONCILIATION_REGISTRY[EffectKind.PROJECT_RUN].absence is AbsenceAuthority.NEVER
    assert RECONCILIATION_REGISTRY[EffectKind.DESKTOP_MUTATION].absence is AbsenceAuthority.NEVER
    assert RECONCILIATION_REGISTRY[EffectKind.DOWNLOAD].absence is AbsenceAuthority.AUTHORITATIVE
    assert RECONCILIATION_REGISTRY[EffectKind.FILE_CREATE].absence is AbsenceAuthority.AUTHORITATIVE


def test_keys_come_from_the_registry_and_ignore_anything_a_model_or_caller_adds() -> None:
    base = resolve_effect_keys("commit_booking", BOOKING)
    assert base == (EffectKey(key="booking:site:appointment_fixture", kind=EffectKind.EXTERNAL_MUTATION),)
    # Provider, model, a smuggled key or kind: none of it changes the lock.
    noisy = {**BOOKING, "provider": "gemini", "model": "model-b", "effect_key": "file:create:x", "effect_kind": "download"}
    assert resolve_effect_keys("commit_booking", noisy) == base
    assert booking_key("../../x") == booking_key(None) == EffectKey("booking:site:unparsed", EffectKind.EXTERNAL_MUTATION)
    for tool in ("DESKTOP_SET_VALUE", "DESKTOP_SELECT", "DESKTOP_INVOKE"):
        assert resolve_effect_keys(tool, {"provider": "claude", "model": "model-a"}) == (DESKTOP_MUTATION_KEY,)

    download = {"source_url": "https://example.test/a.pdf", "dest_root_id": ROOT, "dest_name": "Resume.pdf"}
    assert resolve_effect_keys("transfer_download", download) == download_keys(
        source_url="https://example.test/a.pdf", root_id=ROOT, file_name="Resume.pdf"
    )
    # Supplied keys must be exactly the registry's.
    with pytest.raises(EffectKeysError):
        resolve_effect_keys("transfer_download", download, (placement_key(root_id=ROOT, file_name="other.pdf"),))
    # A project start's keys come from the controller, one per registered kind -- never an unregistered kind.
    with pytest.raises(EffectKeysError):
        resolve_effect_keys("project_start", {"run_id": "x"})
    with pytest.raises(EffectKeysError):
        resolve_effect_keys("project_start", {}, (EffectKey("project_run:project:x", EffectKind.DOWNLOAD),))
    project = (EffectKey("project_run:project:x", EffectKind.PROJECT_RUN),)
    assert resolve_effect_keys("project_start", {}, project) == project
    # An unregistered tool holds no keys, and cannot be given any.
    assert resolve_effect_keys("record_note", {"a": 1}) == ()
    with pytest.raises(EffectKeysError):
        resolve_effect_keys("record_note", {}, project)
    # A derivable tool whose proposal lacks the identity is refused, not keyed by a guess.
    with pytest.raises(EffectKeysError):
        resolve_effect_keys("transfer_place", {"dest_root_id": ROOT})


def test_windows_spellings_of_one_destination_are_one_key() -> None:
    assert placement_key(root_id=ROOT, file_name="Resume.pdf") == placement_key(root_id=ROOT, file_name="resume.PDF")
    assert placement_key(root_id=ROOT, file_name="RESUME.PDF") == placement_key(root_id=ROOT.upper(), file_name="resume.pdf")
    assert placement_key(root_id=ROOT, file_name="Resume.pdf") != placement_key(root_id=ROOT, file_name="Resume2.pdf")
    assert placement_key(root_id=ROOT, file_name="Resume.pdf") != placement_key(root_id=str(uuid.UUID(int=8)), file_name="Resume.pdf")


def test_every_derivable_key_satisfies_the_database_shape() -> None:
    samples = [
        *resolve_effect_keys("commit_booking", BOOKING),
        *resolve_effect_keys("commit_booking", {"site": "x" * 64}),
        *resolve_effect_keys("transfer_download", {"source_url": "https://e.test/" + "a" * 900, "dest_root_id": ROOT, "dest_name": "n" * 200}),
        DESKTOP_MUTATION_KEY,
    ]
    for key in samples:
        assert KEY_SHAPE.match(key.key), key


def test_generic_callers_cannot_reach_an_effect_tool_by_another_spelling() -> None:
    for name in EFFECT_TOOLS:
        assert is_effect_tool_name(name) and is_effect_tool_name(name.upper()) and is_effect_tool_name(name.lower())
    assert not is_effect_tool_name("record_note")


def test_reconciliation_lookups_are_bounded_with_backoff() -> None:
    rule = RECONCILIATION_REGISTRY[EffectKind.EXTERNAL_MUTATION]
    assert rule.max_lookups == 8
    assert lookup_allowance(rule, previous_ages_seconds=[]) == (True, "", 0)
    assert lookup_allowance(rule, previous_ages_seconds=[1.0])[0] is True  # a second immediate look
    allowed, reason, retry = lookup_allowance(rule, previous_ages_seconds=[1.0, 2.0])
    assert (allowed, reason) == (False, "lookup_backoff") and 1 <= retry <= 15
    assert lookup_allowance(rule, previous_ages_seconds=[20.0, 40.0])[0] is True
    hour = 3600.0
    allowed, reason, _ = lookup_allowance(rule, previous_ages_seconds=[hour] * 8)
    assert (allowed, reason) == (False, "lookup_budget_exhausted")
    # The budget is a rolling day: old looks stop counting.
    assert lookup_allowance(rule, previous_ages_seconds=[2 * 86400.0] * 8)[0] is True


# ---- 2. the generic routes hold only inert actions -------------------------------------------------------


async def test_the_generic_proposal_route_refuses_every_controller_owned_tool(client: httpx.AsyncClient) -> None:
    task = (await client.post("/tasks", json={"request": {"type": "appointment_booking", "text": "x"}})).json()
    refused = [
        *EFFECT_TOOLS,
        "Commit_booking".lower(),
        "lookup_booking",
        "inspect_public_page",
        "research_navigate",
        "authenticated_observe",
        "prepare_form",
        "handover_form",
        "adopt_workflow_value",
    ]
    for index, tool in enumerate(refused):
        response = await client.post(
            f"/tasks/{task['id']}/actions",
            json={"idempotency_key": f"k-{index}", "tool_name": tool, "risk_tier": "R2", "proposal": BOOKING},
        )
        assert response.status_code == 422, (tool, response.text)
    booking = await client.post(
        f"/tasks/{task['id']}/actions",
        json={"idempotency_key": "k-b", "tool_name": "commit_booking", "risk_tier": "R2", "proposal": BOOKING},
    )
    error = booking.json()["error"]
    assert (error["code"], error["reason"]) == ("effect_route_refused", "use_booking_route")
    assert (await client.get(f"/tasks/{task['id']}/actions")).json()["actions"] == []


async def test_the_generic_attempt_and_reconciliation_routes_never_move_a_booking(
    client: httpx.AsyncClient, action_service: ActionService, task_service: TaskService
) -> None:
    approved = await approved_booking(action_service, task_service)
    action_id = approved.action.id
    started = await client.post(f"/actions/{action_id}/attempts")
    assert started.status_code == 422 and started.json()["error"]["reason"] == "use_booking_route"
    assert (await action_service.get_action(action_id)).action.status is ActionStatus.APPROVED

    await action_service.start_attempt(action_id)
    for path, body in (
        ("attempts/finish", {"outcome": "FAILED"}),
        ("attempts/finish", {"outcome": "SUCCEEDED"}),
    ):
        response = await client.post(f"/actions/{action_id}/{path}", json=body)
        assert response.status_code == 422, path
    await action_service.finish_attempt(action_id, outcome=AttemptOutcome.OUTCOME_UNKNOWN, error_code="lost_response")
    # A generic "reconciliation finished: FAILED" would turn a non-authoritative absence into a safe retry.
    settle: list[tuple[str, dict[str, str] | None]] = [
        ("reconciliation", None),
        ("reconciliation/finish", {"result": "FAILED"}),
        ("reconciliation/finish", {"result": "SUCCEEDED"}),
    ]
    for path, payload in settle:
        response = await client.post(f"/actions/{action_id}/{path}", json=payload)
        assert response.status_code == 422, path
    current = await action_service.get_action(action_id)
    assert current.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert len(current.attempts) == 1


async def test_settling_can_never_spend_an_effect_tools_approval(action_service: ActionService, task_service: TaskService) -> None:
    task = await task_service.create_task({"type": "appointment_booking"})
    view = await action_service.propose_exclusive_action(task.id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=BOOKING)
    view = await action_service.request_approval(view.action.id)
    with pytest.raises(EffectKeysError):
        await action_service.settle_exact_approval(view.action.id, expected_revision=view.action.revision, guard=_no_guard, result={"code": "x"})
    assert (await action_service.get_action(view.action.id)).action.status is ActionStatus.WAITING_APPROVAL


# ---- 3. Task A's unresolved booking, attacked from everywhere ---------------------------------------------


async def test_an_unknown_booking_blocks_a_new_booking_task_until_it_is_reconciled(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    assert await key_rows(engine, first.action.id) == [("booking:site:appointment_fixture", "external_mutation")]

    # A new task -- and one that names another provider and model -- is still refused, before any attempt.
    second = await approved_booking(
        action_service, task_service, request={"type": "appointment_booking", "provider": "gemini", "model": "model-b"}
    )
    with pytest.raises(EffectLockedError) as locked:
        await action_service.start_attempt(second.action.id)
    assert locked.value.blocking_action_id == str(first.action.id)
    blocked = await action_service.get_action(second.action.id)
    assert blocked.action.status is ActionStatus.APPROVED and blocked.attempts == ()
    assert blocked.approval is not None  # the refusal rolled back: nothing was consumed

    # The same task cannot prepare a second booking beside the unknown one.
    with pytest.raises(ActionAlreadyOpenError):
        await action_service.propose_exclusive_action(first.action.task_id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=BOOKING)

    # Reconciled (read-only; here the service-level verdict) -> the new task's own approval can now run.
    reconciling = await action_service.begin_reconciliation(first.action.id)
    await action_service.finish_reconciliation(first.action.id, result=AttemptOutcome.SUCCEEDED, expected_revision=reconciling.action.revision)
    started = await action_service.start_attempt(second.action.id)
    assert started.action.status is ActionStatus.EXECUTING


@pytest.mark.parametrize(
    ("tool", "proposal", "supplied"),
    [
        ("transfer_download", {"transfer_id": "t", "source_url": "https://e.test/a.pdf", "dest_root_id": ROOT, "dest_name": "a.pdf"}, ()),
        ("transfer_place", {"transfer_id": "t", "sha256": "0" * 64, "dest_root_id": ROOT, "dest_name": "a.pdf"}, ()),
        ("project_start", {"run_id": "r"}, (EffectKey("project_run:project:p", EffectKind.PROJECT_RUN),)),
    ],
)
async def test_an_unknown_booking_blocks_every_scoped_effect_on_every_other_executor(
    engine: AsyncEngine,
    action_service: ActionService,
    task_service: TaskService,
    tool: str,
    proposal: dict[str, Any],
    supplied: tuple[EffectKey, ...],
) -> None:
    first = await unknown_booking(action_service, task_service)
    task = await task_service.create_task({"type": "file_transfer"})
    with pytest.raises(EffectLockedError) as locked:
        await action_service.start_scoped_attempt(
            task.id, tool_name=tool, idempotency_key="step", risk_tier=RiskTier.R2, proposal=proposal,
            authorizer=_NeverMinted(), effect_keys=supplied,
        )
    assert locked.value.reason == "consequential_effect_unresolved"
    assert locked.value.blocking_action_id == str(first.action.id)
    # The refusal rolled the whole transaction back: no action, no keys, no attempt.
    assert await count(engine, "SELECT count(*) FROM actions WHERE task_id = :t", t=task.id) == 0
    assert _NeverMinted.minted == 0


async def test_an_unknown_booking_blocks_a_desktop_mutation_and_the_card_stays_answerable(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    card = await desktop_mutation_card(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.begin_exact_execution(card.action.id, expected_revision=card.action.revision, guard=_no_guard)
    after = await action_service.get_action(card.action.id)
    assert after.action.status is ActionStatus.WAITING_APPROVAL and after.attempts == ()
    assert await key_rows(engine, card.action.id) == []  # nothing at all was written
    assert (await action_service.get_action(first.action.id)).action.status is ActionStatus.OUTCOME_UNKNOWN


async def test_an_unknown_desktop_mutation_blocks_another_mutation_but_is_not_global(
    action_service: ActionService, task_service: TaskService
) -> None:
    card = await desktop_mutation_card(action_service, task_service)
    await action_service.begin_exact_execution(card.action.id, expected_revision=card.action.revision, guard=_no_guard)
    await action_service.finish_attempt(card.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN, error_code="lost_response")
    other = await desktop_mutation_card(action_service, task_service, "DESKTOP_INVOKE")
    with pytest.raises(EffectLockedError) as locked:
        await action_service.begin_exact_execution(other.action.id, expected_revision=other.action.revision, guard=_no_guard)
    assert locked.value.reason == "same_effect_unresolved"
    # Not global tier (the reviewed policy): an unrelated booking is not held hostage by a desktop unknown.
    booking = await approved_booking(action_service, task_service)
    assert (await action_service.start_attempt(booking.action.id)).action.status is ActionStatus.EXECUTING


async def test_an_in_flight_booking_blocks_new_consequential_work_too(
    action_service: ActionService, task_service: TaskService
) -> None:
    first = await approved_booking(action_service, task_service)
    await action_service.start_attempt(first.action.id)  # EXECUTING: dispatched, no answer yet
    card = await desktop_mutation_card(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.begin_exact_execution(card.action.id, expected_revision=card.action.revision, guard=_no_guard)
    second = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(second.action.id)


async def test_a_pre_registry_unknown_booking_is_keyed_at_startup_idempotently(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    async with engine.begin() as connection:  # an action recorded before S5 held no keys
        await connection.execute(text("DELETE FROM action_effect_keys WHERE action_id = :a"), {"a": first.action.id})
    recovery = RecoveryService(engine)
    assert await recovery.backfill_effect_keys() == [first.action.id]
    assert await recovery.backfill_effect_keys() == []
    assert await key_rows(engine, first.action.id) == [("booking:site:appointment_fixture", "external_mutation")]
    second = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(second.action.id)


async def test_a_claim_with_keys_the_registry_does_not_derive_is_refused(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    approved = await approved_booking(action_service, task_service)
    async with engine.begin() as connection:
        await EffectLockRepository(connection).insert_keys(
            action_id=approved.action.id, keys=(EffectKey("file:create:other", EffectKind.FILE_CREATE),)
        )
    with pytest.raises(EffectKeysError):
        await action_service.start_attempt(approved.action.id)
    assert (await action_service.get_action(approved.action.id)).action.status is ActionStatus.APPROVED


# ---- 4. restart, twice ------------------------------------------------------------------------------------


async def test_restarting_twice_recovers_an_executing_booking_once_and_keeps_its_lock(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    approved = await approved_booking(action_service, task_service)
    await action_service.start_attempt(approved.action.id)
    for _ in range(2):
        generation = await register_runtime_generation(engine)
        recovery = RecoveryService(engine)
        await recovery.recover_unfinished_attempts(generation.id)
        await recovery.recover_interrupted_reconciliations()
        await recovery.backfill_effect_keys()
    current = await action_service.get_action(approved.action.id)
    assert current.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert len(current.attempts) == 1 and current.attempts[0].outcome is AttemptOutcome.OUTCOME_UNKNOWN
    assert await count(
        engine,
        "SELECT count(*) FROM task_events WHERE task_id = :t AND event_type = 'action.outcome_unknown'",
        t=approved.action.task_id,
    ) == 1
    assert len(await key_rows(engine, approved.action.id)) == 1
    other = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)


async def test_a_crash_while_reconciling_keeps_the_uncertainty_and_the_lock(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    await action_service.begin_reconciliation(first.action.id)
    for _ in range(2):
        await RecoveryService(engine).recover_interrupted_reconciliations()
    assert (await action_service.get_action(first.action.id)).action.status is ActionStatus.OUTCOME_UNKNOWN
    assert await count(
        engine,
        "SELECT count(*) FROM task_events WHERE task_id = :t AND payload->>'reason' = 'reconciliation_interrupted'",
        t=first.action.task_id,
    ) == 1
    other = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)


# ---- 5. Stop / cancel --------------------------------------------------------------------------------------


async def test_cancelling_keeps_the_unknown_booking_its_evidence_and_its_lock(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    # The booking's own cancel refuses outright while the outcome is unknown.
    with pytest.raises(TaskHasUnresolvedActionError):
        await BookingTaskService(action_service).cancel(first.action.task_id)
    # The generic Stop closes the task -- and nothing else: no FAILED, no compensation, no lost evidence.
    cancelled = await task_service.cancel_task(first.action.task_id)
    assert cancelled.status is TaskStatus.CANCELLED
    current = await action_service.get_action(first.action.id)
    assert current.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert current.attempts[0].outcome is AttemptOutcome.OUTCOME_UNKNOWN
    assert len(await key_rows(engine, first.action.id)) == 1
    other = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)
    # Read-only reconciliation is still possible after Stop, and its verdict does not revive the task.
    reconciling = await action_service.begin_reconciliation(first.action.id)
    done = await action_service.finish_reconciliation(
        first.action.id, result=AttemptOutcome.SUCCEEDED, evidence={"source": "test"}, expected_revision=reconciling.action.revision
    )
    assert done.action.status is ActionStatus.SUCCEEDED
    assert (await task_service.get_task(first.action.task_id)).status is TaskStatus.CANCELLED


async def test_stop_during_reconciliation_never_settles_the_booking_itself(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService
) -> None:
    first = await unknown_booking(action_service, task_service)
    reconciling = await action_service.begin_reconciliation(first.action.id)
    await task_service.cancel_task(first.action.task_id)
    # Stop did not decide anything; the action is still RECONCILING and still blocks.
    assert (await action_service.get_action(first.action.id)).action.status is ActionStatus.RECONCILING
    other = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)
    # The look that was already running may still report -- an inconclusive one keeps the uncertainty.
    back = await action_service.finish_reconciliation(
        first.action.id, result=AttemptOutcome.OUTCOME_UNKNOWN, evidence={"lookup": "UNKNOWN"}, expected_revision=reconciling.action.revision
    )
    assert back.action.status is ActionStatus.OUTCOME_UNKNOWN
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)


async def test_stop_while_a_booking_is_in_flight_never_marks_it_failed(
    action_service: ActionService, task_service: TaskService
) -> None:
    approved = await approved_booking(action_service, task_service)
    await action_service.start_attempt(approved.action.id)
    await task_service.cancel_task(approved.action.task_id)
    assert (await action_service.get_action(approved.action.id)).action.status is ActionStatus.EXECUTING
    # The executor's honest answer is still recorded after Stop.
    finished = await action_service.finish_attempt(approved.action.id, outcome=AttemptOutcome.OUTCOME_UNKNOWN, error_code="lost_response")
    assert finished.action.status is ActionStatus.OUTCOME_UNKNOWN


# ---- 6. concurrency and lock order ---------------------------------------------------------------------


async def test_racing_bookings_in_different_tasks_start_exactly_one(action_service: ActionService, task_service: TaskService) -> None:
    approved = [await approved_booking(action_service, task_service) for _ in range(6)]
    results = await asyncio.wait_for(
        asyncio.gather(*(action_service.start_attempt(view.action.id) for view in approved), return_exceptions=True),
        timeout=60,
    )
    started = [result for result in results if isinstance(result, ActionView)]
    refused = [result for result in results if isinstance(result, EffectLockedError)]
    assert len(started) == 1 and len(refused) == 5, results


async def test_advisory_locks_are_taken_in_one_order_and_the_global_tier_is_exclusive(engine: AsyncEngine) -> None:
    async def hold(keys: list[str], global_tier: bool, entered: asyncio.Event, release: asyncio.Event) -> None:
        async with engine.begin() as connection:
            await EffectLockRepository(connection).lock(keys, global_tier=global_tier)
            entered.set()
            await release.wait()

    # Opposite caller orders over the same keys never deadlock: the repository sorts.
    events = [asyncio.Event() for _ in range(4)]
    await asyncio.wait_for(
        asyncio.gather(hold(["file:create:b", "file:create:a"], False, events[0], events[1]),
                       hold(["file:create:a", "file:create:b"], False, events[2], events[3]),
                       _release_after(events[0], events[1]), _release_after(events[2], events[3])),
        timeout=30,
    )

    # A global-tier claim waits for an ordinary claim on a DIFFERENT key (shared vs exclusive global lock).
    ordinary_in, ordinary_out, global_in, global_out = (asyncio.Event() for _ in range(4))
    ordinary = asyncio.create_task(hold(["file:create:c"], False, ordinary_in, ordinary_out))
    await asyncio.wait_for(ordinary_in.wait(), timeout=10)
    booking = asyncio.create_task(hold(["booking:site:x"], True, global_in, global_out))
    await asyncio.sleep(0.5)
    assert not global_in.is_set(), "the global-tier claim ran beside an uncommitted ordinary claim"
    ordinary_out.set()
    await asyncio.wait_for(global_in.wait(), timeout=10)
    global_out.set()
    await asyncio.wait_for(asyncio.gather(ordinary, booking), timeout=10)


async def _release_after(entered: asyncio.Event, release: asyncio.Event) -> None:
    await entered.wait()
    release.set()


# ---- 7. booking reconciliation: read-only, bounded, absence per site -------------------------------------


class _LookupOnly(BrowserExecutionService):
    """The real reconciliation code with the worker replaced by a scripted READ-ONLY lookup answer."""

    def __init__(self, *args: Any, answer: Outcome, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answer = answer
        self.operations: list[str] = []
        #: A fixed worker generation to answer as (default: a fresh one per call, i.e. a new worker).
        self.fixed_worker: uuid.UUID | None = None

    async def _client(self) -> BrowserWorkerClient:
        class _Closed:
            async def aclose(self) -> None:
                return None

        return _Closed()  # type: ignore[return-value]

    async def _bind_worker(self, client: BrowserWorkerClient) -> uuid.UUID:
        generation = self.fixed_worker or uuid.uuid4()
        async with self._engine.begin() as connection:
            repository = BrowserRepository(connection)
            if await repository.get_worker_generation(generation) is None:
                await repository.register_worker_generation(
                    worker_generation=generation, runtime_generation=self._runtime_generation, worker_started_at=datetime.now(UTC)
                )
        return generation

    async def _dispatch(self, client: BrowserWorkerClient, *, request: Any) -> Outcome:
        self.operations.append(request.operation)
        return self.answer


def _answer(result: str) -> Outcome:
    return Outcome(
        outcome=AttemptOutcome.SUCCEEDED, dispatch_status=DispatchStatus.OK, submitted=False,
        error_code=None, observation_id=None, result={"result": result},
    )


def _lookup_service(engine: AsyncEngine, actions: ActionService, generation: Any, answer: Outcome) -> _LookupOnly:
    return _LookupOnly(engine, actions=actions, runtime_generation=generation.id, worker=None, answer=answer)


async def test_found_resolves_the_same_action_without_a_second_attempt(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    service = _lookup_service(engine, action_service, runtime_generation, _answer("FOUND"))
    done = await service.reconcile_booking(first.action.id)
    assert done.action.id == first.action.id and done.action.status is ActionStatus.SUCCEEDED
    assert len(done.attempts) == 1 and service.operations == ["lookup_booking"]


async def test_authoritative_absence_is_a_known_failure_and_a_retry_is_a_new_exact_approval(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    service = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    done = await service.reconcile_booking(first.action.id)
    assert done.action.status is ActionStatus.FAILED  # the fixture's declaration says absence is a fact
    # The consumed approval can never fund anything again.
    with pytest.raises((ApprovalNotUsableError, InvalidActionTransitionError)):
        await action_service.start_attempt(first.action.id)
    with pytest.raises(InvalidActionTransitionError):
        await action_service.request_approval(first.action.id)
    # A retry is a NEW action in the same task (linked by the task and its ordinal), with its own approval.
    retry = await action_service.propose_exclusive_action(first.action.task_id, tool_name="commit_booking", risk_tier=RiskTier.R2, proposal=BOOKING)
    assert retry.action.id != first.action.id and retry.action.idempotency_key == "commit_booking-2"
    assert retry.approval is None and retry.action.status is ActionStatus.PROPOSED


async def test_a_non_authoritative_not_found_stays_unknown_and_keeps_the_lock(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    unreviewed = {**BOOKING, "site": "eventually_consistent_clinic"}
    first = await unknown_booking(action_service, task_service, unreviewed)
    service = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    done = await service.reconcile_booking(first.action.id)
    assert done.action.status is ActionStatus.OUTCOME_UNKNOWN  # "not found" is not "safe to retry"
    other = await approved_booking(action_service, task_service)
    with pytest.raises(EffectLockedError):
        await action_service.start_attempt(other.action.id)


async def test_a_failed_lookup_never_clears_the_uncertainty(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service)
    failed = Outcome(
        outcome=AttemptOutcome.FAILED, dispatch_status=DispatchStatus.FAILED_BEFORE_EFFECT, submitted=False,
        error_code="site_unavailable", observation_id=None, result={},
    )
    service = _lookup_service(engine, action_service, runtime_generation, failed)
    assert (await service.reconcile_booking(first.action.id)).action.status is ActionStatus.OUTCOME_UNKNOWN


async def test_reconciliation_is_bounded_and_the_bound_changes_nothing(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService, runtime_generation: Any
) -> None:
    first = await unknown_booking(action_service, task_service, {**BOOKING, "site": "eventually_consistent_clinic"})
    service = _lookup_service(engine, action_service, runtime_generation, _answer("NOT_FOUND"))
    await service.reconcile_booking(first.action.id)
    await service.reconcile_booking(first.action.id)  # two immediate looks are fine
    before = await action_service.get_action(first.action.id)
    with pytest.raises(ReconciliationLimitedError) as limited:
        await service.reconcile_booking(first.action.id)
    assert limited.value.reason == "lookup_backoff"
    after = await action_service.get_action(first.action.id)
    assert after.action.status is ActionStatus.OUTCOME_UNKNOWN and after.action.revision == before.action.revision

    async def age_lookups(interval: str) -> None:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"UPDATE browser_dispatches SET started_at = now() - interval '{interval}' WHERE action_id = :a"),
                {"a": first.action.id},
            )

    for _ in range(6):  # up to the budget, each after the backoff
        await age_lookups("1 hour")
        await service.reconcile_booking(first.action.id)
    await age_lookups("1 hour")
    with pytest.raises(ReconciliationLimitedError) as exhausted:
        await service.reconcile_booking(first.action.id)
    assert exhausted.value.reason == "lookup_budget_exhausted"
    lookups = await count(engine, "SELECT count(*) FROM browser_dispatches WHERE action_id = :a AND operation = 'lookup_booking'", a=first.action.id)
    consequential = await count(engine, "SELECT count(*) FROM browser_dispatches WHERE action_id = :a AND effect = 'CONSEQUENTIAL'", a=first.action.id)
    assert (lookups, consequential) == (8, 0)
    assert service.operations == ["lookup_booking"] * 8
    await age_lookups("2 days")  # a rolling day
    assert (await service.reconcile_booking(first.action.id)).action.status is ActionStatus.OUTCOME_UNKNOWN
