"""Milestone 8b S5 acceptance: a real form, a real worker, and **zero** changes.

The S5 chain, every link real: the synthetic authenticated `/app/apply` page is
observed by the real worker (S4), the user's trusted click enables form planning,
a scripted planner proposes `prepare_form`, the controller validates it against the
persisted inventory, builds the exact manifest, a trusted approval spends it -- and
then the assertions that make S5 what it is:

    browser dispatches created by S5 == 0, worker mutation operations == 0,
    input / change / focus / click / keydown / submit / autosave events == 0,
    submissions == 0, and the approval cannot be spent a second time.
"""

import uuid
from typing import Any

import pytest

from app.api.form_prepare_schemas import PlanningContextResponse
from app.domain.action_status import ActionStatus
from app.domain.errors import ApprovalNotUsableError
from app.services.form_prepare import FormPrepareService
from tests.test_authenticated_service_browser import (  # noqa: F401 - fixtures the acceptance builds on.
    World,
    _host_site_scope,
    world,
)

pytestmark = [pytest.mark.browser]

SAVED = {
    "legal_name": "LEGAL_NAME_SECRET_S5_71A",
    "email": "EMAIL_SECRET_S5_82B@example.test",
    "phone": "+91 93000 01234",
    "country": "India",
}
MARKERS = ("LEGAL_NAME_SECRET_S5_71A", "EMAIL_SECRET_S5_82B", "93000 01234")


async def counters(world: World) -> dict[str, int]:
    values: dict[str, int] = await world.page().evaluate("window.__lumiForm")
    return values


async def test_an_apply_form_is_planned_and_approved_and_nothing_on_the_page_changes(world: World) -> None:
    form = FormPrepareService(world.engine, actions=world.actions, grant_ttl_seconds=600)
    for kind, value in SAVED.items():
        await form.save_detail(kind, value)

    await world.goto("/app/apply")
    task_id, _ = await world.granted()
    step = await world.observe(task_id)
    assert step.observation is not None
    inventory = step.observation.observation.inventory
    refs = {element.accessible_name: element for element in inventory.elements}
    dispatches_before = await world.sql("SELECT count(*) FROM browser_dispatches")
    effects_before = await world.site.effects()
    events_before = await counters(world)
    assert set(events_before.values()) == {0}

    # The trusted click enables planning; nothing is disclosed before it.
    view = await form.prepare_scope(task_id, allowed_data_refs=["legal_name", "email", "phone", "country"])
    assert view.grant is not None
    context_refused: Any = None
    try:
        await form.planning_context(task_id)
    except Exception as error:  # noqa: BLE001
        context_refused = error
    assert context_refused is not None
    await form.confirm(task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)

    # Exactly one provider gets structure and masked previews, never a raw value.
    context = await form.planning_context(task_id)
    payload = PlanningContextResponse.from_context(context).model_dump_json()
    assert context.recipient == "gemini"
    assert all(marker not in payload for marker in MARKERS)
    assert "Full legal name" in payload and "E***@e***.test" in payload
    # The page's current values and locators are absent by construction.
    for planted in ("CURRENT_VALUE_SECRET_71A", "OPTION_VALUE_SECRET_82B", "CONTROL_ID_SECRET_93C", "CONTROL_NAME_SECRET_A4D"):
        assert planted not in payload

    # The scripted planner names elements by ref, exactly as it saw them.
    proposal = {
        "operation": "prepare_form", "observation": context.observation_ref, "form_ref": "f1",
        "entries": [
            {"element_ref": refs["Full legal name"].element_ref, "data_ref": "legal_name"},
            {"element_ref": refs["Email address"].element_ref, "data_ref": "email"},
            {"element_ref": refs["Phone"].element_ref, "data_ref": "phone"},
            {"element_ref": refs["Country"].element_ref, "option_ref": refs["Country"].option_refs[0].ref},
            {"element_ref": refs["I agree to the terms"].element_ref, "checked": True},
        ],
    }
    proposed = await form.propose(task_id, proposal, provider="gemini")
    assert proposed.disclosure is not None
    disclosure = proposed.disclosure
    assert disclosure.action.status is ActionStatus.WAITING_APPROVAL

    settled = await form.approve(disclosure.action.id, expected_revision=disclosure.action.revision)
    assert settled.action.status is ActionStatus.SUCCEEDED
    assert settled.attempts[-1].result is not None and settled.attempts[-1].result["code"] == "prepared_nothing"

    # The whole S5 claim: nothing was written, dispatched, focused, submitted or saved.
    assert await world.sql("SELECT count(*) FROM browser_dispatches") == dispatches_before
    assert await world.sql(
        f"SELECT count(*) FROM browser_dispatches WHERE action_id = '{disclosure.action.id}'"
    ) == 0
    assert await counters(world) == events_before
    effects_after = await world.site.effects()
    assert effects_after["submissions"] == 0 == effects_before["submissions"]
    assert effects_after["mutations"] == effects_before["mutations"]
    for name in ("input", "change", "focus", "click", "keydown", "submit", "autosave"):
        assert (await counters(world))[name] == 0
    # The saved values stay in their table and nowhere else.
    for table in ("actions", "approvals", "task_events", "authenticated_observations", "action_attempts", "task_grants"):
        dumped = await world.sql(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
        assert all(marker not in dumped for marker in MARKERS), table

    # A spent approval cannot be spent again.
    with pytest.raises((ApprovalNotUsableError, Exception)):
        await form.approve(disclosure.action.id, expected_revision=settled.action.revision)
    assert isinstance(task_id, uuid.UUID)
