"""The whole S6 chain, every link real: PostgreSQL, the worker, and a headed Chromium.

    read headless  ->  preparation mode: the SAME profile reopened HEADED, back to the same
    in-site page (internally), a completely fresh observation  ->  the S5 planning grant,
    proposal and exact manifest  ->  the trusted approval  ->  durable attempt, freeze at two
    layers, frozen_at, the writes, local verification  ->  discard  OR  a second exact
    approval and a handover.

The assertions that make S6 what it is are made from the *server's* side and the broker's:
every adversarial counter of the fixture (`/app/apply/draft`) stays 0 while Lumi fills and
while the draft waits frozen, the broker resolves and dials nothing, discard destroys the
dirty document before the network returns, and handover keeps the SAME page and values.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from app.domain.action_status import ActionStatus
from app.domain.errors import ApprovalNotUsableError, InvalidActionTransitionError
from app.domain.local_form_draft import DraftStatus
from app.domain.task_status import TaskStatus
from app.services.form_draft import FormDraftService
from app.services.form_prepare import FormPrepareService
from app.services.form_state import FormPhase, FormStateRegistry
from tests.test_authenticated_service_browser import (  # noqa: F401 - fixtures the acceptance builds on.
    World,
    _host_site_scope,
    world,
)

pytestmark = [pytest.mark.browser]

SAVED = {
    "legal_name": "LEGAL_NAME_SECRET_S6_71A Person",
    "email": "EMAIL_SECRET_S6_82B@example.test",
    "phone": "+91 93000 01234",
    "preferred_name": "PREFERRED_SECRET_S6_C4D",
}
MARKERS = ("LEGAL_NAME_SECRET_S6_71A", "EMAIL_SECRET_S6_82B", "93000 01234", "PREFERRED_SECRET_S6_C4D")
ZERO = ("submissions", "autosave", "blur_save", "exfiltration", "third_party", "popup_hits", "mutations")


class Chain:
    def __init__(self, world: World) -> None:
        self.world = world
        self.state = FormStateRegistry()
        world.service._forms = self.state
        world.profiles._forms = self.state
        self.form = FormPrepareService(
            world.engine, actions=world.actions, grant_ttl_seconds=600, forms=self.state
        )
        self.drafts = FormDraftService(
            world.engine, actions=world.actions, reads=world.service, profiles=world.profiles,
            form=self.form, runtime_generation=world.service._runtime_generation,
            worker=world.service._worker, state=self.state,
        )
        self.form.attach_drafts(self.drafts)
        self.task_id: uuid.UUID | None = None

    @property
    def freeze(self) -> Any:
        return self.world.worker_app.state.freeze

    @property
    def broker(self) -> Any:
        return self.world.worker_app.state.broker

    async def start(self) -> uuid.UUID:
        for kind, value in SAVED.items():
            await self.form.save_detail(kind, value)
        await self.world.goto("/app/apply/draft")
        self.task_id, _ = await self.world.granted()
        step = await self.world.observe(self.task_id)  # a headless read of the form
        assert step.observation is not None
        return self.task_id

    async def prepare_mode(self) -> None:
        assert self.task_id is not None
        await self.drafts.start_preparation(self.task_id)

    async def observation(self) -> Any:
        assert self.task_id is not None
        return (await self.world.service.describe(self.task_id)).observations[-1].observation

    async def plan_and_propose(self) -> Any:
        assert self.task_id is not None
        view = await self.form.prepare_scope(
            self.task_id, allowed_data_refs=["legal_name", "email", "phone", "preferred_name"]
        )
        assert view.grant is not None
        await self.form.confirm(self.task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
        observation = await self.observation()
        by_name = {e.accessible_name: e for e in observation.inventory.elements}
        proposal = {
            "operation": "prepare_form", "observation": observation.ref, "form_ref": by_name["Full name"].form_ref,
            "entries": [
                {"element_ref": by_name["Full name"].element_ref, "data_ref": "legal_name"},
                {"element_ref": by_name["Email address"].element_ref, "data_ref": "email"},
                {"element_ref": by_name["Phone"].element_ref, "data_ref": "phone"},
                {"element_ref": by_name["Controlled name"].element_ref, "data_ref": "preferred_name"},
                {"element_ref": by_name["Country"].element_ref, "option_ref": by_name["Country"].option_refs[1].ref},
                {"element_ref": by_name["Preferred contact"].element_ref, "option_ref": by_name["Preferred contact"].option_refs[0].ref},
                {"element_ref": by_name["I agree to the terms"].element_ref, "checked": True},
            ],
        }
        proposed = await self.form.propose(self.task_id, proposal, provider="gemini")
        assert proposed.disclosure is not None
        return proposed.disclosure

    async def effects(self) -> dict[str, Any]:
        return await self.world.site.effects()

    async def assert_zero(self) -> None:
        effects = await self.effects()
        for name in ZERO:
            assert effects[name] == 0, (name, effects)


async def filled(chain: Chain) -> tuple[Any, Any]:
    await chain.start()
    await chain.prepare_mode()
    disclosure = await chain.plan_and_propose()
    settled = await chain.form.approve(disclosure.action.id, expected_revision=disclosure.action.revision)
    assert settled.action.status is ActionStatus.SUCCEEDED, settled.attempts[-1]
    draft = await chain.drafts.latest_draft(chain.task_id)  # type: ignore[arg-type]
    assert draft is not None
    return disclosure, draft


async def test_headless_to_headed_preparation_fill_verify_and_discard(world: World) -> None:
    chain = Chain(world)
    await chain.start()
    headless_page = world.page()
    assert world.worker_app.state.profiles.get(world.profile_id).headless is True

    await chain.prepare_mode()

    # The SAME profile is now open HEADED, on the same in-site page, freshly observed; the
    # old headless context is gone.
    session = world.worker_app.state.profiles.get(world.profile_id)
    assert session.headless is False and session.read_session is not None and session.read_session.preparation_mode
    assert headless_page.is_closed() and chain.state.is_preparing(world.profile_id)
    assert world.page().url.endswith("/app/apply/draft")
    events = (await world.service.describe(chain.task_id)).observations  # type: ignore[arg-type]
    assert events[-1].observation.inventory.forms

    disclosure = await chain.plan_and_propose()
    settled = await chain.form.approve(disclosure.action.id, expected_revision=disclosure.action.revision)
    # The baseline is the worker's own freeze proof (taken after both layers were frozen), not a
    # reading from before it: background Chromium traffic may resolve a name just before the freeze.
    assert chain.freeze.proof is not None
    resolutions, dials = chain.freeze.proof.resolution_count, chain.freeze.proof.dial_count

    assert settled.action.status is ActionStatus.SUCCEEDED
    assert settled.attempts[-1].result is not None and settled.attempts[-1].result["code"] == "local_draft_prepared"
    draft = await chain.drafts.latest_draft(chain.task_id)  # type: ignore[arg-type]
    assert draft is not None and draft.status is DraftStatus.PREPARED and len(draft.fields) == 7
    # Values are in the fields; nothing was sent; nothing was resolved or dialled; still frozen.
    page = world.page()
    assert await page.eval_on_selector("#name", "(e) => e.value") == SAVED["legal_name"]
    assert await page.eval_on_selector("#controlled", "(e) => e.value") == SAVED["preferred_name"]
    assert await page.eval_on_selector("input[name=terms]", "(e) => e.checked") is True
    await asyncio.sleep(0.6)
    await chain.assert_zero()
    assert (chain.broker.counters.resolutions, chain.broker.counters.dial_count) == (resolutions, dials)
    assert chain.broker.active_connections == 0 and chain.freeze.owner is not None
    assert session.read_session.guard.frozen and chain.broker.mode.value == "frozen"
    # `frozen_at` was recorded before the write, and no reconciliation exists for it.
    assert await world.sql("SELECT count(*) FROM browser_dispatches WHERE effect = 'LOCAL_DRAFT' AND frozen_at IS NOT NULL") == 1
    assert (await world.tasks.get_task(chain.task_id)).status is TaskStatus.PAUSED  # type: ignore[arg-type]
    # No raw value is in any table but its own, and none in the runtime's card.
    for table in ("actions", "approvals", "task_events", "browser_dispatches", "form_drafts", "action_attempts"):
        dumped = await world.sql(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
        assert all(marker not in dumped for marker in MARKERS), table

    # An agent read of a dirty page is refused, and so is closing it.
    with pytest.raises(Exception) as dirty:  # noqa: B017 - FormPrepareRefusal(form_is_dirty).
        await world.observe(chain.task_id)  # type: ignore[arg-type]
    assert getattr(dirty.value, "code", "") == "form_is_dirty"

    # Discard: the dirty document is destroyed BEFORE the network returns.
    dirty_page = page
    moved = await chain.drafts.discard(draft.id, expected_revision=draft.revision)
    assert moved.status is DraftStatus.DISCARDED and dirty_page.is_closed()
    assert chain.freeze.owner is None and chain.broker.mode.value == "open"
    await asyncio.sleep(1.0)
    await chain.assert_zero()
    assert world.page().url == "about:blank"
    assert chain.state.get(world.profile_id).phase is FormPhase.PREPARATION  # type: ignore[union-attr]


async def test_handover_keeps_the_same_page_and_values_and_only_then_restores_the_network(world: World) -> None:
    chain = Chain(world)
    disclosure, draft = await filled(chain)
    page = world.page()
    await chain.assert_zero()  # before handover every server counter is zero

    view = await chain.drafts.request_handover(draft.id, expected_revision=draft.revision)
    assert chain.freeze.owner is not None and chain.broker.mode.value == "frozen"  # requesting changes nothing
    settled = await chain.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)

    assert settled.action.status is ActionStatus.SUCCEEDED
    assert (await chain.drafts.latest_draft(chain.task_id)).status is DraftStatus.HANDED_OVER  # type: ignore[union-attr,arg-type]
    assert world.page() is page and not page.is_closed()  # not closed, not reopened
    assert await page.eval_on_selector("#email", "(e) => e.value") == SAVED["email"]
    assert chain.freeze.owner is None and chain.broker.mode.value == "open"
    task = await world.tasks.get_task(chain.task_id)  # type: ignore[arg-type]
    assert task.status is TaskStatus.PAUSED
    reason = await world.sql("SELECT payload->>'reason' FROM task_events WHERE task_id = :t AND event_type = 'task.authenticated_paused' ORDER BY sequence DESC LIMIT 1".replace(":t", f"'{chain.task_id}'"))
    assert reason == "user_takeover"
    # The network is available to the human now -- and to nothing before the approval.
    await page.evaluate("fetch('/app/apply/autosave', {method: 'POST', body: 'human'})")
    await asyncio.sleep(1.0)
    assert (await chain.effects())["autosave"] >= 1 and (await chain.effects())["submissions"] == 0
    with pytest.raises(Exception):  # noqa: B017 - the second approval is single use.
        await chain.drafts.approve_handover(view.action.id, expected_revision=settled.action.revision)


async def test_a_human_who_changed_the_frozen_page_cannot_be_handed_over(world: World) -> None:
    chain = Chain(world)
    _, draft = await filled(chain)
    await world.page().fill("#name", "the human typed something else")  # in the visible window
    view = await chain.drafts.request_handover(draft.id, expected_revision=draft.revision)

    settled = await chain.drafts.approve_handover(view.action.id, expected_revision=view.action.revision)

    assert settled.action.status is ActionStatus.FAILED and settled.attempts[-1].error_code == "draft_changed"
    assert chain.freeze.owner is not None and chain.broker.mode.value == "frozen"  # the network stayed off
    assert (await chain.drafts.latest_draft(chain.task_id)).status is DraftStatus.PREPARED  # type: ignore[union-attr,arg-type]
    await asyncio.sleep(0.5)
    await chain.assert_zero()
    with pytest.raises((ApprovalNotUsableError, InvalidActionTransitionError)):
        await chain.drafts.approve_handover(view.action.id, expected_revision=settled.action.revision)


async def test_the_headless_approval_cannot_be_used_after_preparation_mode_begins(world: World) -> None:
    """An approval made from the headless document does not survive the transition."""
    chain = Chain(world)
    await chain.start()
    # Plan against the HEADLESS observation, exactly as an S5-era flow would have.
    view = await chain.form.prepare_scope(chain.task_id, allowed_data_refs=["email"])  # type: ignore[arg-type]
    assert view.grant is not None
    await chain.form.confirm(chain.task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)  # type: ignore[arg-type]
    observation = await chain.observation()
    by_name = {e.accessible_name: e for e in observation.inventory.elements}
    with pytest.raises(Exception) as refused:  # noqa: B017 - FormPrepareRefusal.
        await chain.form.propose(
            chain.task_id,  # type: ignore[arg-type]
            {"operation": "prepare_form", "observation": observation.ref, "form_ref": by_name["Email address"].form_ref,
             "entries": [{"element_ref": by_name["Email address"].element_ref, "data_ref": "email"}]},
            provider="gemini",
        )
    assert getattr(refused.value, "code", "") == "preparation_mode_required"
    assert json.dumps(str(refused.value)).count("SECRET") == 0
    # The worker itself refuses a write in a headless window, whatever the runtime believes.
    assert world.worker_app.state.profiles.get(world.profile_id).headless is True
