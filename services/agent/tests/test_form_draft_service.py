"""The durable half of the S6 local draft, against a real PostgreSQL ledger.

The browser worker is a scripted HTTP stand-in that also acts as a *measuring instrument*:
when the runtime's `prepare_form` dispatch arrives, it asks the database -- from its own
connection, at that instant -- whether the attempt exists, whether the dispatch row exists
and whether `frozen_at` was already recorded. That is the ordering claim of S6, observed from
the worker's side of the wire rather than asserted from the runtime's:

    durable attempt  ->  durable dispatch (frozen_at NULL)  ->  worker enters the freeze
      ->  runtime records frozen_at  ->  ONLY THEN the worker is asked to write

The real browser half is `test_local_form_draft_browser.py` and the service-browser
acceptance; what is proven *here* is what the runtime writes down, refuses, and never claims.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from sqlalchemy import text

from app.browser.protocol import (
    DispatchRequest,
    DispatchResponse,
    FormDiscardResponse,
    FormFreezeResponse,
    FormHandoverResponse,
    OperationStatus,
    WorkerIdentity,
)
from app.domain.action_status import ActionStatus, ApprovalStatus
from app.domain.errors import ApprovalNotUsableError
from app.domain.form_prepare import DisclosureManifest, FormPrepareRefusal, parse_manifest
from app.domain.local_form_draft import (
    DraftStatus,
    FillFormInput,
    FillResult,
    VerifiedField,
    check_verified_hash,
    choice_verified_hash,
)
from app.domain.task_status import TaskStatus
from app.repositories.form_drafts import FormDraftRepository
from app.services.browser_execution import BrowserWorkerConfig
from app.services.form_draft import FormDraftService, _expected_hash
from app.services.form_prepare import FormPrepareService
from app.services.form_state import FormPhase, FormStateRegistry
from tests.test_authenticated_service_browser import InProcessServer
from tests.test_form_prepare import (  # noqa: F401 - fixtures and helpers this module builds on
    SECRETS,
    FormRig,
    form,
    scalar,
)
from tests.test_authenticated_read import Rig, profile_root, rig  # noqa: F401

MARKERS = ("LEGAL_NAME_SECRET_S5_71A", "EMAIL_SECRET_S5_82B", "9300001234")


@dataclass
class HttpWorker:
    """A scripted worker that records what the runtime asked, and what the DB said then."""

    generation: uuid.UUID
    engine: Any
    calls: list[str] = field(default_factory=list)
    freeze: str = "FROZEN"  # FROZEN | REFUSED | LOST
    freeze_code: str = "page_never_settles"
    fill: str = "complete"  # complete | partial | clean_failure | lost | rejected
    handover: str = "HANDED_OVER"  # HANDED_OVER | REFUSED | LOST
    seen_at_write: dict[str, Any] = field(default_factory=dict)
    dispatched_inputs: list[dict[str, Any]] = field(default_factory=list)
    discards: int = 0

    def app(self) -> FastAPI:
        api = FastAPI()

        @api.get("/health")
        async def health() -> Any:
            self.calls.append("health")
            return WorkerIdentity(
                worker_generation=self.generation, started_at=datetime.now(UTC).isoformat(),
                headless=False, sites=[], operations=[],
            ).model_dump(mode="json")

        @api.post("/v1/profiles/form-freeze")
        async def freeze(request: Request) -> Any:
            body = await request.json()
            self.calls.append("form-freeze")
            if self.freeze == "LOST":
                await asyncio.sleep(5)
            return FormFreezeResponse(
                profile_id=body["profile_id"], dispatch_id=body["dispatch_id"],
                worker_generation=self.generation,
                status="FROZEN" if self.freeze == "FROZEN" else "REFUSED",
                error_code=None if self.freeze == "FROZEN" else self.freeze_code,
            ).model_dump(mode="json")

        @api.post("/v1/dispatch")
        async def dispatch(request: Request) -> Any:
            body = DispatchRequest.model_validate(await request.json())
            self.calls.append("dispatch")
            self.dispatched_inputs.append(body.input)
            async with self.engine.connect() as connection:
                row = (await connection.execute(
                    text("SELECT frozen_at IS NOT NULL AS frozen, status, attempt_id FROM browser_dispatches WHERE id = :i"),
                    {"i": body.dispatch_id},
                )).one_or_none()
                attempt = await connection.scalar(
                    text("SELECT count(*) FROM action_attempts WHERE id = :a AND finished_at IS NULL"), {"a": body.attempt_id}
                )
            self.seen_at_write = {
                "dispatch_row": row is not None, "frozen_at_set": bool(row and row.frozen),
                "status": row.status if row else None, "attempt_open": attempt == 1,
            }
            if self.fill == "rejected":
                return JSONResponse(status_code=409, content={"code": "freeze_owned", "message": "held", "worker_generation": str(self.generation)})
            if self.fill == "lost":
                await asyncio.sleep(5)
            fill_input = FillFormInput.model_validate(body.input)
            expected: dict[str, str] = {}
            for item in fill_input.fields:
                if item.text_value is not None:
                    expected[item.element_ref] = item.value_digest or ""
                elif item.option_identity_hash is not None:
                    expected[item.element_ref] = choice_verified_hash(item.option_identity_hash)
                else:
                    assert item.checked is not None
                    expected[item.element_ref] = check_verified_hash(item.element_identity_hash, item.checked)
            refs = list(expected)
            if self.fill == "complete":
                verified, code, dirty, done = refs, None, True, True
            elif self.fill == "partial":
                verified, code, dirty, done = refs[:2], "unsupported_under_freeze", True, False
            else:
                verified, code, dirty, done = [], "element_changed", False, False
            result = FillResult(
                draft_complete=done, fields_attempted=len(refs) if done else min(len(refs), 3),
                fields_verified=len(verified),
                verified_fields=[VerifiedField(element_ref=r, verified_local_value_hash=expected[r]) for r in verified],
                first_failed_element_ref=None if done else refs[min(len(verified), len(refs) - 1)],
                error_code=code, dirty=dirty, blocked_request_count=4,
            )
            return DispatchResponse(
                dispatch_id=body.dispatch_id, runtime_generation=body.runtime_generation,
                worker_generation=self.generation, operation=body.operation,
                status=OperationStatus.OK if done else OperationStatus.FAILED_BEFORE_EFFECT,
                observation={"result": result.model_dump(mode="json"), "page_discarded": False},
                error_code=code, duration_ms=5,
            ).model_dump(mode="json")

        @api.post("/v1/profiles/form-discard")
        async def discard(request: Request) -> Any:
            body = await request.json()
            self.calls.append("form-discard")
            self.discards += 1
            return FormDiscardResponse(
                profile_id=body["profile_id"], worker_generation=self.generation, status="DISCARDED"
            ).model_dump(mode="json")

        @api.post("/v1/profiles/form-handover")
        async def handover(request: Request) -> Any:
            body = await request.json()
            self.calls.append("form-handover")
            self.seen_at_write["handover_fields"] = len(body["fields"])
            if self.handover == "LOST":
                await asyncio.sleep(5)
            return FormHandoverResponse(
                profile_id=body["profile_id"], worker_generation=self.generation,
                status="HANDED_OVER" if self.handover == "HANDED_OVER" else "REFUSED",
                error_code=None if self.handover == "HANDED_OVER" else "draft_changed",
                verified_count=len(body["fields"]),
            ).model_dump(mode="json")

        return api


@dataclass
class Draft:
    rig: Rig
    form: FormRig
    worker: HttpWorker
    drafts: FormDraftService
    state: FormStateRegistry
    server: InProcessServer

    async def waiting(self) -> tuple[uuid.UUID, Any]:
        """A disclosure built from an observation taken in (simulated) preparation mode."""
        task_id = await self.form.granted_plan()
        async with self.rig.engine.connect() as connection:
            from app.repositories.authenticated import AuthenticatedRepository

            records = await AuthenticatedRepository(connection).list_observations(task_id)
        profile_id = self.rig.profile_id
        state = self.state.begin_preparation(
            profile_id, task_id=task_id, worker_generation=self.worker.generation
        )
        state.observation_ids.add(records[-1].observation.observation_id)
        view = await self.form.proposed(task_id)
        assert view.disclosure is not None
        return task_id, view.disclosure

    async def approve(self, disclosure: Any) -> Any:
        return await self.form.service.approve(
            disclosure.action.id, expected_revision=disclosure.action.revision
        )

    async def latest_draft(self, task_id: uuid.UUID) -> Any:
        return await self.drafts.latest_draft(task_id)

    async def dump(self) -> str:
        """Every table except `protected_values`, as text."""
        parts = []
        for table in ("actions", "approvals", "action_attempts", "task_events", "browser_dispatches", "form_drafts", "task_grants", "authenticated_observations", "tasks"):
            parts.append(str(await scalar(self.rig, f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")))
        return " ".join(parts)


@pytest.fixture
async def draft(form: FormRig, rig: Rig) -> AsyncIterator[Draft]:  # noqa: F811
    worker = HttpWorker(generation=rig.browser.worker_generation, engine=rig.engine)
    server = InProcessServer(worker.app())
    await server.start()
    state = FormStateRegistry()
    # The same registry guards the read service and the profile service, exactly as in `main.py`.
    rig.service._forms = state
    rig.profiles._forms = state
    service = FormPrepareService(rig.engine, actions=rig.actions, grant_ttl_seconds=600, forms=state)
    form.service = service
    drafts = FormDraftService(
        rig.engine, actions=rig.actions, reads=rig.service, profiles=rig.profiles, form=service,
        runtime_generation=rig.generation.id,
        worker=BrowserWorkerConfig(
            base_url=server.base_url, token=SecretStr("t" * 24), timeout_seconds=1.5
        ),
        state=state,
    )
    service.attach_drafts(drafts)
    try:
        yield Draft(rig=rig, form=form, worker=worker, drafts=drafts, state=state, server=server)
    finally:
        await server.kill()


# ---- the order: intent, freeze, frozen_at, and only then a write ------------------------------------------


async def test_the_worker_is_asked_to_write_only_after_frozen_at_is_recorded(
    draft: Draft, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    task_id, disclosure = await draft.waiting()

    settled = await draft.approve(disclosure)

    # At the instant the write was requested, the DATABASE already held the attempt, the
    # dispatch row, and a recorded frozen_at.
    assert draft.worker.seen_at_write == {
        "dispatch_row": True, "frozen_at_set": True, "status": "DISPATCHED", "attempt_open": True,
    }
    assert draft.worker.calls == ["health", "form-freeze", "dispatch"]
    assert settled.action.status is ActionStatus.SUCCEEDED
    assert settled.attempts[-1].result is not None and settled.attempts[-1].result["code"] == "local_draft_prepared"
    record = await draft.latest_draft(task_id)
    assert record is not None and record.status is DraftStatus.PREPARED and len(record.fields) == 5
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.DIRTY  # type: ignore[union-attr]
    task = await draft.rig.tasks.get_task(task_id)
    assert task.status is TaskStatus.PAUSED
    assert await scalar(draft.rig, "SELECT count(*) FROM browser_dispatches WHERE frozen_at IS NOT NULL AND effect = 'LOCAL_DRAFT'") == 1
    # The approval is spent: it cannot fund a second attempt.
    with pytest.raises(Exception):  # noqa: B017 - InvalidActionTransition or ApprovalNotUsable.
        await draft.approve(disclosure)


async def test_the_raw_values_reach_the_worker_and_nowhere_else(
    draft: Draft, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    task_id, disclosure = await draft.waiting()
    await draft.approve(disclosure)

    sent = json.dumps(draft.worker.dispatched_inputs)
    for kind in ("legal_name", "email", "phone"):
        assert SECRETS[kind] in sent  # the worker gets the exact approved bytes, in memory
    dumped = await draft.dump()
    for marker in MARKERS:
        assert marker not in dumped and marker not in caplog.text
    # The approved digest, not the value, is what the draft row keeps.
    record = await draft.latest_draft(task_id)
    assert record is not None and all(marker not in record.model_dump_json() if hasattr(record, "model_dump_json") else True for marker in MARKERS)


async def test_the_draft_row_holds_hashes_and_refs_only(draft: Draft) -> None:
    task_id, disclosure = await draft.waiting()
    await draft.approve(disclosure)
    manifest = parse_manifest(disclosure.action.proposal)
    record = await draft.latest_draft(task_id)
    assert record is not None and record.manifest_digest == manifest.manifest_digest
    for stored, approved in zip(record.fields, manifest.fields, strict=True):
        assert stored.verified_local_value_hash == _expected_hash(approved)
        assert stored.element_identity_hash == approved.element_identity_hash
    columns = await scalar(draft.rig, "SELECT string_agg(column_name, ',') FROM information_schema.columns WHERE table_name = 'form_drafts'")
    assert not {"value", "text_value", "selector", "locator", "html"} & set(columns.split(","))


# ---- failures before any write ------------------------------------------------------------------------------


async def test_a_refused_freeze_writes_nothing_and_leaves_no_frozen_at(draft: Draft) -> None:
    draft.worker.freeze, draft.worker.freeze_code = "REFUSED", "page_never_settles"
    task_id, disclosure = await draft.waiting()

    settled = await draft.approve(disclosure)

    assert settled.action.status is ActionStatus.FAILED
    assert draft.worker.calls == ["health", "form-freeze"]  # the write was never asked for
    assert settled.attempts[-1].error_code == "page_never_settles"
    assert await scalar(draft.rig, "SELECT count(*) FROM browser_dispatches WHERE frozen_at IS NOT NULL") == 0
    assert await draft.latest_draft(task_id) is None
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.PREPARATION  # type: ignore[union-attr]
    with pytest.raises(Exception):  # noqa: B017 - the spent approval cannot be retried.
        await draft.approve(disclosure)


async def test_a_lost_freeze_answer_is_unknown_and_makes_no_claim_about_the_remote_effect(draft: Draft) -> None:
    draft.worker.freeze = "LOST"
    task_id, disclosure = await draft.waiting()

    settled = await draft.approve(disclosure)

    assert settled.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert draft.worker.calls[-1] == "form-discard"  # whatever the worker holds is released
    assert "dispatch" not in draft.worker.calls
    result = settled.attempts[-1].result
    assert result is not None and result["frozen_at_present"] is False
    assert "remote_effect" not in result  # frozen_at is NULL: nothing is claimed
    assert await scalar(draft.rig, "SELECT count(*) FROM browser_dispatches WHERE frozen_at IS NOT NULL") == 0


async def test_a_changed_saved_value_refuses_the_approval_before_the_browser_is_touched(draft: Draft) -> None:
    task_id, disclosure = await draft.waiting()
    await draft.form.service.save_detail("email", "another@example.test")

    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.approve(disclosure)

    assert raised.value.code == "protected_value_changed"
    assert "form-freeze" not in draft.worker.calls and "dispatch" not in draft.worker.calls
    approval = (await draft.rig.actions.get_action(disclosure.action.id)).approval
    assert approval is not None and approval.status is ApprovalStatus.PENDING  # nothing was consumed


async def test_preparation_mode_is_required(draft: Draft) -> None:
    task_id, disclosure = await draft.waiting()
    draft.state.clear(draft.rig.profile_id)
    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.approve(disclosure)
    assert raised.value.code == "preparation_mode_required"
    assert draft.worker.calls == []


async def test_a_worker_restart_since_preparation_refuses_before_consuming_the_approval(draft: Draft) -> None:
    task_id, disclosure = await draft.waiting()
    draft.state.get(draft.rig.profile_id).worker_generation = uuid.uuid4()  # type: ignore[union-attr]
    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.approve(disclosure)
    assert raised.value.code == "preparation_mode_required"
    approval = (await draft.rig.actions.get_action(disclosure.action.id)).approval
    assert approval is not None and approval.status is ApprovalStatus.PENDING


# ---- historical S5 approvals are terminal ----------------------------------------------------------------------


async def test_a_historical_s5_manifest_is_never_executable(draft: Draft) -> None:
    task_id, disclosure = await draft.waiting()
    current = parse_manifest(disclosure.action.proposal)
    legacy = DisclosureManifest.build(
        **{
            **{name: getattr(current, name) for name in (
                "task_id", "profile_id", "form_prepare_grant_id", "planning_recipient", "site_display",
                "recipient_origin", "account_binding", "profile_revoke_epoch", "observation_id",
                "observation_ref", "tab", "document_epoch", "form_epoch", "form_ref", "form_label",
            )},
            "fields": list(current.fields),
            "policy_version": "form-prepare-v1",
        }
    )
    assert legacy.policy_version == "form-prepare-v1" and legacy.manifest_digest != current.manifest_digest
    await draft.form.service._open_approval(task_id, legacy)
    view = await draft.form.service.describe(task_id)
    assert view.disclosure is not None and view.disclosure.executable is False

    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.approve(view.disclosure)
    assert raised.value.code == "legacy_manifest_not_executable"
    assert draft.worker.calls == []


# ---- partial and clean failures ----------------------------------------------------------------------------------


async def test_a_partial_fill_leaves_a_stale_draft_and_a_paused_task(draft: Draft) -> None:
    draft.worker.fill = "partial"
    task_id, disclosure = await draft.waiting()

    settled = await draft.approve(disclosure)

    assert settled.action.status is ActionStatus.FAILED
    record = await draft.latest_draft(task_id)
    assert record is not None and record.status is DraftStatus.STALE and len(record.fields) == 2
    assert settled.attempts[-1].result is not None and settled.attempts[-1].result["code"] == "local_draft_partial"
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.DIRTY  # type: ignore[union-attr]
    assert (await draft.rig.tasks.get_task(task_id)).status is TaskStatus.PAUSED


async def test_a_failure_that_wrote_nothing_verified_leaves_no_draft(draft: Draft) -> None:
    draft.worker.fill = "clean_failure"
    task_id, disclosure = await draft.waiting()
    settled = await draft.approve(disclosure)
    assert settled.action.status is ActionStatus.FAILED and await draft.latest_draft(task_id) is None
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.PREPARATION  # type: ignore[union-attr]


async def test_a_lost_fill_answer_is_unknown_never_retried_and_the_page_is_destroyed(draft: Draft) -> None:
    draft.worker.fill = "lost"
    task_id, disclosure = await draft.waiting()

    settled = await draft.approve(disclosure)

    assert settled.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert draft.worker.calls.count("dispatch") == 1 and draft.worker.calls[-1] == "form-discard"
    result = settled.attempts[-1].result
    # frozen_at IS set, so -- and only so -- Lumi may say the remote effect was impossible.
    assert result is not None and result["remote_effect"] == "impossible_under_verified_freeze"
    assert result["local_state"] == "lost"
    assert await draft.latest_draft(task_id) is None
    assert (await draft.rig.tasks.get_task(task_id)).status is TaskStatus.PAUSED


# ---- discard ---------------------------------------------------------------------------------------------------------


async def prepared(draft: Draft) -> tuple[uuid.UUID, Any]:
    task_id, disclosure = await draft.waiting()
    await draft.approve(disclosure)
    record = await draft.latest_draft(task_id)
    assert record is not None
    return task_id, record


async def test_discard_asks_the_worker_to_destroy_the_page_then_marks_it_discarded(draft: Draft) -> None:
    task_id, record = await prepared(draft)
    with pytest.raises(FormPrepareRefusal):  # a stale revision
        await draft.drafts.discard(record.id, expected_revision=record.revision + 5)

    moved = await draft.drafts.discard(record.id, expected_revision=record.revision)

    assert moved.status is DraftStatus.DISCARDED and draft.worker.calls[-1] == "form-discard"
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.PREPARATION  # type: ignore[union-attr]
    with pytest.raises(FormPrepareRefusal):  # it cannot be discarded twice
        await draft.drafts.discard(record.id, expected_revision=moved.revision)


# ---- handover: a second exact approval --------------------------------------------------------------------------------


async def test_handover_is_a_second_exact_single_use_approval_bound_to_the_draft(draft: Draft) -> None:
    task_id, record = await prepared(draft)
    view = await draft.drafts.request_handover(record.id, expected_revision=record.revision)
    assert view.action.tool_name == "handover_form" and view.action.status is ActionStatus.WAITING_APPROVAL
    proposal = json.dumps(view.action.proposal)
    for marker in MARKERS:
        assert marker not in proposal  # safe references only

    settled = await draft.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)

    assert settled.action.status is ActionStatus.SUCCEEDED
    assert draft.worker.calls[-1] == "form-handover" and draft.worker.seen_at_write["handover_fields"] == 5
    assert (await draft.drafts.latest_draft(task_id)).status is DraftStatus.HANDED_OVER  # type: ignore[union-attr]
    assert draft.state.get(draft.rig.profile_id) is None
    task = await draft.rig.tasks.get_task(task_id)
    assert task.status is TaskStatus.PAUSED
    # Single use, and never claimed as a submission.
    with pytest.raises(Exception):  # noqa: B017
        await draft.drafts.approve_handover(view.action.id, expected_revision=settled.action.revision)
    assert "submitted" not in json.dumps(settled.attempts[-1].result)


async def test_a_refused_handover_leaves_the_network_frozen_and_the_draft_live(draft: Draft) -> None:
    draft.worker.handover = "REFUSED"
    task_id, record = await prepared(draft)
    view = await draft.drafts.request_handover(record.id, expected_revision=record.revision)
    settled = await draft.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)
    assert settled.action.status is ActionStatus.FAILED and settled.attempts[-1].error_code == "draft_changed"
    assert (await draft.drafts.latest_draft(task_id)).status is DraftStatus.PREPARED  # type: ignore[union-attr]
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.DIRTY  # type: ignore[union-attr]


async def test_a_lost_handover_response_is_unknown_and_blocks_everything(draft: Draft) -> None:
    draft.worker.handover = "LOST"
    task_id, record = await prepared(draft)
    view = await draft.drafts.request_handover(record.id, expected_revision=record.revision)

    settled = await draft.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)

    assert settled.action.status is ActionStatus.OUTCOME_UNKNOWN
    assert draft.worker.calls.count("form-handover") == 1  # never retried
    assert draft.state.get(draft.rig.profile_id).phase is FormPhase.UNKNOWN  # type: ignore[union-attr]
    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.drafts.discard(record.id, expected_revision=record.revision)
    assert raised.value.code == "form_is_dirty"


async def test_a_handover_approval_for_another_draft_or_a_changed_digest_is_refused(draft: Draft) -> None:
    task_id, record = await prepared(draft)
    view = await draft.drafts.request_handover(record.id, expected_revision=record.revision)
    async with draft.rig.engine.begin() as connection:
        await connection.execute(
            text("UPDATE form_drafts SET draft_digest = :d WHERE id = :i"), {"d": "f" * 64, "i": record.id}
        )
    with pytest.raises(FormPrepareRefusal) as raised:
        await draft.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)
    assert raised.value.code == "draft_changed" and "form-handover" not in draft.worker.calls
    with pytest.raises(Exception):  # noqa: B017 - a stale revision.
        await draft.drafts.approve_handover(view.action.id, expected_revision=view.action.revision + 3)


async def test_the_generic_action_routes_cannot_approve_or_reject_either_approval(draft: Draft) -> None:
    from app.api.routes import _refuse_disclosure_tool

    task_id, record = await prepared(draft)
    view = await draft.drafts.request_handover(record.id, expected_revision=record.revision)
    with pytest.raises(FormPrepareRefusal):
        await _refuse_disclosure_tool(draft.rig.actions, view.action.id)


# ---- dirty: nothing else may touch the profile ---------------------------------------------------------------------------


async def test_a_dirty_profile_cannot_be_read_closed_or_replanned(draft: Draft) -> None:
    task_id, record = await prepared(draft)
    calls_before = len(draft.rig.worker.dispatched)
    with pytest.raises(FormPrepareRefusal) as read:
        await draft.rig.service.execute_step(  # a read through the guarded service
            task_id, draft.rig.envelope({"operation": "observe", "tab": "t1"})
        )
    assert read.value.code == "form_is_dirty"
    assert len(draft.rig.worker.dispatched) == calls_before  # no worker call, so no provider input either
    with pytest.raises(FormPrepareRefusal):
        await draft.rig.profiles.close_profile(draft.rig.profile_id)
    with pytest.raises(FormPrepareRefusal):
        await draft.form.service.prepare_scope(task_id, allowed_data_refs=["email"])
    with pytest.raises(FormPrepareRefusal) as planning:  # nor can it be planned against again
        await draft.form.service.planning_context(task_id)
    assert planning.value.code == "form_is_dirty"
    with pytest.raises(FormPrepareRefusal) as proposing:  # nor a new prepare_form proposed
        await draft.form.service.propose(task_id, {"operation": "prepare_form", "observation": "o1", "form_ref": "f1", "entries": []}, provider="gemini")
    assert proposing.value.code in ("form_is_dirty", "no_entries")


# ---- restart: the draft is lost, not restored ------------------------------------------------------------------------------


async def test_startup_closes_a_live_draft_as_lost_and_never_refills_it(draft: Draft) -> None:
    from app.services.recovery import FormDraftRecovery

    task_id, record = await prepared(draft)
    assert await FormDraftRecovery(draft.rig.engine).discard_lost_drafts() == 1
    lost = await draft.latest_draft(task_id)
    assert lost is not None and lost.status is DraftStatus.DISCARDED
    assert (await draft.rig.tasks.get_task(task_id)).status is TaskStatus.PAUSED
    events = await scalar(draft.rig, "SELECT string_agg(event_type || payload::text, ' ') FROM task_events WHERE task_id = :t", t=task_id)
    assert "task.form_draft_lost" in events and "browser_lost" in events
    assert draft.worker.calls.count("dispatch") == 1  # nothing was re-filled


@pytest.mark.parametrize("frozen", [True, False])
async def test_a_crash_is_read_through_frozen_at(draft: Draft, frozen: bool) -> None:
    """`frozen_at` is the only thing that lets Lumi say the remote effect was impossible."""
    from app.repositories.actions import ActionRepository
    from app.services.recovery import RecoveryService

    draft.worker.fill = "lost"
    task_id, disclosure = await draft.waiting()
    # Simulate the runtime dying mid-fill: intent and dispatch durable, no outcome recorded.
    manifest = parse_manifest(disclosure.action.proposal)
    await draft.rig.actions.begin_exact_execution(
        disclosure.action.id, expected_revision=disclosure.action.revision,
        guard=lambda connection, action: draft.form.service._guard(connection, action, manifest),
    )
    other = uuid.uuid4()
    async with draft.rig.engine.begin() as connection:
        await connection.execute(text("INSERT INTO runtime_generations (id) VALUES (:g)"), {"g": other})
        attempt = (await ActionRepository(connection).list_attempts(disclosure.action.id))[0]
        await connection.execute(text("UPDATE action_attempts SET runtime_generation = :g WHERE id = :a"), {"g": other, "a": attempt.id})
        await connection.execute(
            text(
                "INSERT INTO browser_dispatches (id, action_id, attempt_id, worker_generation, operation, site, effect, status, submitted, frozen_at) "
                "VALUES (:d, :ac, :at, :w, 'authenticated_prepare_form', 'authenticated', 'LOCAL_DRAFT', 'DISPATCHED', false, "
                + ("now()" if frozen else "NULL") + ")"
            ),
            {"d": uuid.uuid4(), "ac": disclosure.action.id, "at": attempt.id, "w": draft.worker.generation},
        )

    recovered = await RecoveryService(draft.rig.engine).recover_unfinished_attempts(draft.rig.generation.id)

    assert len(recovered) == 1
    view = await draft.rig.actions.get_action(disclosure.action.id)
    assert view.action.status is ActionStatus.OUTCOME_UNKNOWN  # never FAILED, never retried
    payload = json.loads(await scalar(
        draft.rig,
        "SELECT payload::text FROM task_events WHERE task_id = :t AND event_type = 'action.outcome_unknown' ORDER BY sequence DESC LIMIT 1",
        t=task_id,
    ))
    assert payload["frozen_at_present"] is frozen and payload["local_state"] == "lost"
    assert payload["remote_effect"] == ("impossible_under_verified_freeze" if frozen else "unknown")
    assert (await draft.rig.tasks.get_task(task_id)).status is TaskStatus.PAUSED
