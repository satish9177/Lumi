"""Authenticated form element observation (Milestone 8b S4), against a real Chromium.

**S4 observes form structure only.** These tests prove what an observation
carries and, more importantly, what it never carries and never does:

* no current value, option value, id, name, class, selector, frame URL, HTML or
  geometry leaves the worker (planted secrets, asserted absent from every string
  the worker returns);
* password / file / one-time-code controls, and every control of a page the S2
  detector flags, receive no ref and their labels do not exist in the result;
* cross-origin frames are not read at all;
* the bounds hold (5 forms, 5 frames, 40 elements, 25 options);
* `form_epoch` rises when a page re-renders its form *without navigating*, and an
  old element ref then fails closed (`element_changed`) -- and never re-points;
* observing a form triggers no input, change, focus, click, keydown, submit or
  autosave event and no request the fixture counts as a mutation.

The rig, the fixture server and the host:port site-scope substitution are those
of `test_authenticated_browser.py`, imported rather than copied.
"""

import json
from typing import Any

import pytest

from app.browser.authenticated_session import AuthenticatedReadSession
from app.browser.form_observation import build_inventory
from app.browser.operations import authenticated as ops
from app.browser.protocol import OperationStatus
from app.browser.registry import OperationResult
from app.domain.authenticated import AuthenticatedObservation
from app.domain.authenticated_forms import ElementProjection
from evals.sites.account_fixture import (
    CONTROL_CLASS_SECRET,
    CONTROL_ID_SECRET,
    CONTROL_NAME_SECRET,
    CROSS_ORIGIN_FIELD_SECRET,
    CURRENT_VALUE_SECRET,
    FILE_LABEL_SECRET,
    FORM_FIELD_SECRET_MARKER,
    FRAME_FIELD_LABEL,
    OPTION_VALUE_SECRET,
    OTP_LABEL_SECRET,
    PASSWORD_LABEL_SECRET,
    PLANTED_EMAIL,
    SECURE_FRAME_SIBLING_SECRET,
    PLANTED_LONG_ID,
)
from tests.test_authenticated_browser import (  # noqa: F401 - fixtures and helpers
    Rig,
    _host_port_site_scope,
    observation_of,
    parsed,
    rig,
)

pytestmark = [pytest.mark.browser]

PLANTED = (
    CURRENT_VALUE_SECRET,
    OPTION_VALUE_SECRET,
    CONTROL_ID_SECRET,
    CONTROL_NAME_SECRET,
    CONTROL_CLASS_SECRET,
    CROSS_ORIGIN_FIELD_SECRET,
    FILE_LABEL_SECRET,
    PASSWORD_LABEL_SECRET,
    OTP_LABEL_SECRET,
    SECURE_FRAME_SIBLING_SECRET,
    "CONTACT_VALUE_SECRET",
    PLANTED_EMAIL,
    PLANTED_LONG_ID,
)
ELEMENT_KEYS = {
    "element_ref", "form_ref", "frame_ref", "role", "control_type", "accessible_name",
    "label_ref", "value_state", "required", "enabled", "visible", "read_only", "max_length",
    "option_refs", "submit_like",
}


async def open_page(rig: Rig, path: str) -> AuthenticatedReadSession:
    await rig.sign_in(path)
    return await rig.read_session()


async def observe(rig: Rig, read: AuthenticatedReadSession, tab: str = "t1") -> AuthenticatedObservation:
    return observation_of(await rig.observe(read, tab))


def element(observation: AuthenticatedObservation, name: str) -> ElementProjection:
    for candidate in observation.inventory.elements:
        if candidate.accessible_name == name:
            return candidate
    raise AssertionError(
        f"no element named {name!r}: {[e.accessible_name for e in observation.inventory.elements]}"
    )


async def reveal(
    rig: Rig, read: AuthenticatedReadSession, observation: AuthenticatedObservation, ref: str,
    *, form_epoch: int | None = None, document_epoch: int | None = None, tab: str = "t1",
) -> OperationResult:
    return await ops.authenticated_reveal(
        rig.context(read, tab if tab in read.tabs else "t1"),
        ops.RevealInput(
            **rig.common(
                tab=tab,
                target_kind="element",
                target_ref=ref,
                expected_document_epoch=document_epoch or observation.document_epoch,
                expected_form_epoch=form_epoch or observation.form_epoch,
            )
        ),
    )


async def probe(read: AuthenticatedReadSession, tab: str = "t1") -> dict[str, int]:
    counters: dict[str, int] = await read.tabs[tab].page.evaluate("window.__lumiForm")
    return counters


def everything_returned(result: OperationResult) -> str:
    return json.dumps(result.observation, ensure_ascii=False, default=str)


# ---- what an observation carries -------------------------------------------------------


async def test_the_apply_form_is_observed_as_semantic_elements(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    inventory = observation.inventory
    assert observation.schema_version == 2 and observation.form_epoch >= 1
    assert inventory.form_count > 0 and inventory.element_count > 0

    name = element(observation, "Full legal name")
    assert (name.role, name.control_type, name.required) == ("textbox", "text", True)
    assert (name.value_state, name.max_length, name.enabled, name.visible, name.read_only) == (
        "filled", 80, True, True, False,
    )
    assert element(observation, "Email address").control_type == "email"
    assert element(observation, "Email address").value_state == "filled"
    phone = element(observation, "Phone")
    assert (phone.control_type, phone.value_state) == ("tel", "empty")
    assert element(observation, "Years of experience").control_type == "number"
    note = element(observation, "Cover note")
    assert (note.control_type, note.max_length, note.value_state) == ("textarea", 500, "filled")

    country = element(observation, "Country")
    assert (country.role, country.control_type, country.required) == ("combobox", "select_single", True)
    assert [(o.ref, o.label) for o in country.option_refs] == [
        ("op1", "India"), ("op2", "United States"), ("op3", "Germany"),
    ]
    contact = element(observation, "Preferred contact")
    assert (contact.role, contact.control_type) == ("radiogroup", "radiogroup")
    assert [o.label for o in contact.option_refs] == ["Email", "Phone"]
    terms = element(observation, "I agree to the terms")
    assert (terms.role, terms.control_type, terms.value_state) == ("checkbox", "checkbox", "unknown")
    assert element(observation, "Reference code").read_only is True
    assert element(observation, "Employee number").enabled is False

    draft = element(observation, "Save draft")
    assert (draft.role, draft.control_type, draft.submit_like) == ("button", "other", False)
    assert element(observation, "Continue").submit_like is True
    assert element(observation, "Continue").control_type == "submit_like"
    hidden = element(observation, "Hidden continue")
    assert (hidden.submit_like, hidden.visible) == (True, False)


async def test_a_form_group_has_only_a_ref_and_an_optional_label(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    forms = observation.inventory.forms
    assert [form.model_dump() for form in forms][0] == {"ref": "f1", "label": "Application"}
    assert all(set(form.model_dump()) == {"ref", "label"} for form in forms)


async def test_names_are_redacted_and_bounded(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    reply = next(e for e in observation.inventory.elements if e.accessible_name.startswith("Reply"))
    assert "⟦email:1⟧" in reply.accessible_name and "⟦digits:2345⟧" in reply.accessible_name
    assert all(len(e.accessible_name) <= 120 for e in observation.inventory.elements)


# ---- what an observation never carries -----------------------------------------------


async def test_no_planted_secret_leaves_the_worker(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    result = await rig.observe(read)
    observation = observation_of(result)
    returned = everything_returned(result)
    inventory = observation.inventory.model_dump_json()
    # Values, option values and DOM identity appear nowhere in what the worker
    # returns -- not in the inventory and not in the page text either.
    for secret in (
        CURRENT_VALUE_SECRET, OPTION_VALUE_SECRET, CONTROL_ID_SECRET, CONTROL_NAME_SECRET,
        CONTROL_CLASS_SECRET, CROSS_ORIGIN_FIELD_SECRET, SECURE_FRAME_SIBLING_SECRET,
        PASSWORD_LABEL_SECRET, OTP_LABEL_SECRET, "CONTACT_VALUE_SECRET",
    ):
        assert secret not in returned, secret
    # The inventory itself additionally holds no file label and no unredacted identifier.
    for secret in (*PLANTED, "resume"):
        assert secret not in inventory, secret
    # The frame's own controls are listed, by label, without their values.
    assert FRAME_FIELD_LABEL in inventory


async def test_an_element_carries_only_reviewed_fields(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    for item in observation.inventory.model_dump()["elements"]:
        assert set(item) == ELEMENT_KEYS
    dumped = observation.inventory.model_dump_json()
    for forbidden in (
        "selector", "xpath", "outerHTML", "innerHTML", "class", "dataset", "action", "method",
        "bounding", "coordinates", "http://", "https://", "127.0.0.1",
    ):
        assert forbidden not in dumped, forbidden


async def test_file_controls_get_no_ref_and_no_label(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    assert all(FILE_LABEL_SECRET not in e.accessible_name for e in observation.inventory.elements)
    assert not any("resume" in e.accessible_name.lower() for e in observation.inventory.elements)


async def test_a_frame_with_credential_controls_contributes_nothing(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/secure")
    result = await rig.observe(read)
    observation = observation_of(result)
    returned = everything_returned(result)
    for secret in (PASSWORD_LABEL_SECRET, OTP_LABEL_SECRET, SECURE_FRAME_SIBLING_SECRET):
        assert secret not in returned
    assert [e.accessible_name for e in observation.inventory.elements] == ["Visible field"]
    assert [frame.ref for frame in observation.inventory.frames] == ["fr0"]


async def test_credential_and_file_controls_are_never_listed_even_amid_other_controls(
    rig: Rig,
) -> None:
    read = await open_page(rig, "/app/apply")
    page = read.tabs["t1"].page
    await page.set_content(
        "<form>"
        "<label>Plain <input type='text'></label>"
        "<label>PW_LABEL_1 <input type='password'></label>"
        "<label>FILE_LABEL_1 <input type='file'></label>"
        "<label>HID_LABEL_1 <input type='hidden' value='x'></label>"
        "</form>"
    )
    collected = await build_inventory(page)
    # A credential-shaped control anywhere in a frame: that frame yields nothing.
    assert collected.inventory.elements == []
    await page.set_content(
        "<form><label>Plain <input type='text'></label>"
        "<label>FILE_LABEL_1 <input type='file'></label>"
        "<label>HID_LABEL_1 <input type='hidden' value='x'></label></form>"
    )
    names = [e.accessible_name for e in (await build_inventory(page)).inventory.elements]
    assert names == ["Plain"]


@pytest.mark.parametrize(
    "markup",
    [
        "<input type='password' aria-label='CRED_X'>",
        "<input type='text' autocomplete='current-password' aria-label='CRED_X'>",
        "<input type='text' autocomplete='new-password' aria-label='CRED_X'>",
        "<input type='text' autocomplete='one-time-code' aria-label='CRED_X'>",
        "<input type='text' autocomplete='webauthn' aria-label='CRED_X'>",
        "<input type='text' autocomplete='section-a new-password' aria-label='CRED_X'>",
        "<input type='text' name='login_otp' aria-label='CRED_X'>",
    ],
)
async def test_every_credential_shape_is_excluded_while_listing(rig: Rig, markup: str) -> None:
    read = await open_page(rig, "/app/apply")
    page = read.tabs["t1"].page
    await page.set_content(f"<form><label>Kept <input type='text'></label>{markup}</form>")
    collected = await build_inventory(page)
    assert "CRED_X" not in collected.inventory.model_dump_json()
    assert collected.inventory.elements == []


async def test_a_credential_surface_produces_no_inventory_at_all(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    await read.tabs["t1"].page.goto(f"{rig.site.origin}/app/reauth")
    result = await rig.observe(read)
    parsed_result = parsed(result)
    assert parsed_result.credential_surface is not None and parsed_result.observation is None
    assert "inventory" not in everything_returned(result)
    assert read.tabs["t1"].elements == {}


async def test_an_identity_failure_produces_no_inventory(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    await read.tabs["t1"].page.goto(f"{rig.site.origin}/app/no-identity")
    parsed_result = parsed(await rig.observe(read))
    assert parsed_result.identity is not None and parsed_result.observation is None
    assert read.tabs["t1"].elements == {}


# ---- frames -----------------------------------------------------------------------------


async def test_same_origin_frames_are_inventoried_and_cross_origin_frames_are_not(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    page = read.tabs["t1"].page
    frame_origins = {frame.url.split("/", 3)[2] for frame in page.frames if frame.url.startswith("http")}
    assert len(frame_origins) == 2  # the cross-origin frame really loaded
    result = await rig.observe(read)
    observation = observation_of(result)
    assert [frame.ref for frame in observation.inventory.frames] == ["fr0", "fr1"]
    assert {e.frame_ref for e in observation.inventory.elements} == {"fr0", "fr1"}
    assert element(observation, FRAME_FIELD_LABEL).frame_ref == "fr1"
    assert CROSS_ORIGIN_FIELD_SECRET not in everything_returned(result)
    assert "/app/apply/frame" not in observation.inventory.model_dump_json()


# ---- bounds -------------------------------------------------------------------------------


async def test_every_bound_holds(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/big")
    observation = await observe(rig, read)
    inventory = observation.inventory
    assert inventory.truncated is True
    assert inventory.form_count <= 5 and len(inventory.frames) <= 5
    assert inventory.element_count <= 40
    assert [e.element_ref for e in inventory.elements] == [f"e{n}" for n in range(1, 41)]
    assert [f.ref for f in inventory.forms] == [f"f{n}" for n in range(1, 6)]
    assert [f.ref for f in inventory.frames] == [f"fr{n}" for n in range(0, 5)]
    many = element(observation, "Many")
    assert len(many.option_refs) == 25 and many.option_refs[-1].ref == "op25"
    assert all(len(e.option_refs) <= 25 for e in inventory.elements)


# ---- no interaction, ever ----------------------------------------------------------------


async def test_observing_a_form_triggers_no_event_and_no_submission(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    for _ in range(3):
        await observe(rig, read)
    counters = await probe(read)
    assert counters == {
        "input": 0, "change": 0, "focus": 0, "blur": 0, "click": 0, "keydown": 0,
        "submit": 0, "autosave": 0,
    }
    state = await rig.site.effects()
    assert state["submissions"] == 0 and state["mutations"] == 0
    # A hit on the apply page is a GET of the page itself, nothing more.
    assert "/app/apply/submit" not in state["hits"]


async def test_revealing_an_element_scrolls_and_does_nothing_else(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    ref = element(observation, "Country").element_ref
    result = await reveal(rig, read, observation, ref)
    assert result.status is OperationStatus.OK, result.error_code
    again = observation_of(result)
    assert again.form_epoch == observation.form_epoch
    assert await probe(read) == {
        "input": 0, "change": 0, "focus": 0, "blur": 0, "click": 0, "keydown": 0,
        "submit": 0, "autosave": 0,
    }
    active = await read.tabs["t1"].page.evaluate("document.activeElement === document.body")
    assert active is True
    state = await rig.site.effects()
    assert state["submissions"] == 0 and state["mutations"] == 0


# ---- epochs and stale refs --------------------------------------------------------------


async def test_a_rerender_without_navigation_bumps_the_form_epoch(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    page = read.tabs["t1"].page
    first = await observe(rig, read)
    assert [e.accessible_name for e in first.inventory.elements] == ["Full name", "Email"]
    assert first.inventory.forms[0].label is None  # nothing fabricated for unowned controls

    await page.evaluate("window.lumiRerender('changed')")
    second = await observe(rig, read)
    assert second.document_epoch == first.document_epoch
    assert second.form_epoch > first.form_epoch
    assert [e.accessible_name for e in second.inventory.elements] == [
        "Full name", "Work email", "Country",
    ]

    stale = await reveal(rig, read, first, "e1", form_epoch=first.form_epoch)
    assert stale.status is OperationStatus.FAILED_BEFORE_EFFECT and stale.error_code == "element_changed"
    fresh = await reveal(rig, read, second, "e3")
    assert fresh.status is OperationStatus.OK, fresh.error_code
    assert observation_of(fresh).form_epoch == second.form_epoch
    assert await probe(read) == {
        "input": 0, "change": 0, "focus": 0, "blur": 0, "click": 0, "keydown": 0,
        "submit": 0, "autosave": 0,
    }


async def test_a_rerender_after_the_last_look_is_caught_by_revalidation(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    page = read.tabs["t1"].page
    first = await observe(rig, read)
    await page.evaluate("window.lumiRerender('changed')")  # nobody observed this
    result = await reveal(rig, read, first, "e1")
    assert result.error_code == "element_changed"
    assert read.tabs["t1"].elements == {}
    second = await observe(rig, read)
    assert second.form_epoch == first.form_epoch + 1
    # And the old epoch stays dead even for a ref number that exists again.
    assert (await reveal(rig, read, first, "e1")).error_code == "element_changed"


async def test_an_identical_replacement_is_indistinguishable_by_construction(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    page = read.tabs["t1"].page
    first = await observe(rig, read)
    # Every node is removed and equivalent ones inserted. The reviewed fingerprint
    # is identical, so S4 does not (and does not claim to) detect the replacement.
    await page.evaluate("window.lumiRerender('again')")
    second = await observe(rig, read)
    assert second.form_epoch == first.form_epoch
    assert second.inventory == first.inventory
    assert (await reveal(rig, read, first, "e1")).status is OperationStatus.OK


async def test_a_document_navigation_kills_element_refs(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    first = await observe(rig, read)
    ref = element(first, "Country").element_ref
    await read.tabs["t1"].page.goto(f"{rig.site.origin}/app/apply/react")
    result = await reveal(rig, read, first, ref)
    assert result.error_code == "stale_document_epoch"
    # Even against the new document's epoch, the old form epoch's ref is dead
    # until a fresh observation issues new ones.
    second = await observe(rig, read)
    assert (await reveal(rig, read, second, "e1")).status is OperationStatus.OK


async def test_a_ref_that_was_never_issued_is_refused(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    observation = await observe(rig, read)
    assert (await reveal(rig, read, observation, "e40")).error_code == "unknown_target_ref"


async def test_closing_a_tab_kills_its_refs(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    first = await observe(rig, read)
    opened = await ops.authenticated_tab(
        rig.context(read), ops.TabInput(**rig.common(action="open"))
    )
    assert opened.status is OperationStatus.OK
    second_tab = read.tabs["t2"]
    await second_tab.page.goto(f"{rig.site.origin}/app/apply/react")
    second = await observe(rig, read, "t2")
    assert (await reveal(rig, read, second, "e1", tab="t2")).status is OperationStatus.OK
    await ops.authenticated_tab(
        rig.context(read), ops.TabInput(**rig.common(action="close", tab="t2"))
    )
    assert (await reveal(rig, read, second, "e1", tab="t2")).error_code == "unknown_tab"
    # The other tab's refs are its own and are untouched.
    assert (await reveal(rig, read, first, "e1")).status is OperationStatus.OK


async def test_a_new_worker_session_has_no_refs_from_the_old_one(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    first = await observe(rig, read)
    await rig.store.close(rig.profile_id)
    await rig.store.open(
        playwright=rig.playwright, broker=rig.broker, paths=rig.paths,  # type: ignore[arg-type]
        profile_id=rig.profile_id, recorded=None, current=rig.current, headless=True,
        timeout_seconds=30.0,
    )
    await rig.sign_in("/app/apply/react")
    restarted = await rig.read_session()
    assert restarted is not read
    result = await reveal(rig, restarted, first, "e1")
    assert result.status is OperationStatus.FAILED_BEFORE_EFFECT
    assert result.error_code in ("unknown_target_ref", "element_changed", "stale_document_epoch")


# ---- the element model refuses what would be a leak --------------------------------------


def test_the_reveal_input_only_takes_an_element_with_its_form_epoch() -> None:
    common: dict[str, Any] = {
        "sequence": 1, "site": "example.test", "expected_account_fingerprint": "0" * 64,
        "tab": "t1", "expected_document_epoch": 1,
    }
    ops.RevealInput(**common, target_kind="element", target_ref="e3", expected_form_epoch=2)
    with pytest.raises(ValueError):
        ops.RevealInput(**common, target_kind="element", target_ref="e3")
    with pytest.raises(ValueError):
        ops.RevealInput(**common, target_kind="link", target_ref="l1", expected_form_epoch=2)
    with pytest.raises(ValueError):
        ops.RevealInput(**common, target_kind="element", target_ref="e41", expected_form_epoch=2)
    with pytest.raises(ValueError):
        ops.RevealInput(
            **common, target_kind="element", target_ref="e1", expected_form_epoch=1,
            **{"selector": "#x"},
        )


async def test_the_form_marker_is_in_the_inventory_and_not_in_the_page_text(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    observation = await observe(rig, read)
    assert any(FORM_FIELD_SECRET_MARKER in e.accessible_name for e in observation.inventory.elements)
    assert FORM_FIELD_SECRET_MARKER not in "\n".join(block.text for block in observation.blocks)


async def test_listing_controls_mutates_no_dom_and_reads_no_value_back(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply")
    page = read.tabs["t1"].page
    await page.evaluate(
        "window.__mutations = 0;"
        "new MutationObserver((records) => { window.__mutations += records.length; })"
        ".observe(document, {subtree: true, childList: true, attributes: true, characterData: true});"
    )
    collected = await build_inventory(page)
    await page.evaluate("new Promise((resolve) => setTimeout(resolve, 100))")
    assert await page.evaluate("window.__mutations") == 0
    assert collected.inventory.element_count > 0
    assert await probe(read) == {
        "input": 0, "change": 0, "focus": 0, "blur": 0, "click": 0, "keydown": 0,
        "submit": 0, "autosave": 0,
    }


async def test_an_account_change_kills_every_element_ref_and_builds_no_inventory(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    first = await observe(rig, read)
    # The page now presents another account's identity signal (no navigation).
    await read.tabs["t1"].page.evaluate(
        "document.querySelector('[data-lumi-account-id]').setAttribute('data-lumi-account-id', 'someone-else')"
    )
    result = parsed(await rig.observe(read))
    assert result.identity is not None and result.identity.kind == "account_changed"
    assert result.observation is None
    assert read.tabs["t1"].elements == {}
    assert (await reveal(rig, read, first, "e1")).status is OperationStatus.FAILED_BEFORE_EFFECT


async def test_a_session_that_expires_kills_every_element_ref(rig: Rig) -> None:
    read = await open_page(rig, "/app/apply/react")
    first = await observe(rig, read)
    # The page turns into a re-authentication surface without navigating.
    await read.tabs["t1"].page.evaluate(
        "document.body.insertAdjacentHTML('beforeend', \"<input type='password'>\")"
    )
    assert parsed(await rig.observe(read)).credential_surface is not None
    assert read.tabs["t1"].elements == {}
    stale = await reveal(rig, read, first, "e1")
    assert stale.status is OperationStatus.FAILED_BEFORE_EFFECT
