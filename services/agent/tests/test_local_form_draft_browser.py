"""The network-frozen local form draft (Milestone 8b S6), against a real Chromium.

Worker-level, with the S3 rig: a real persistent profile, a real S0 broker and the
synthetic account fixture -- now serving `/app/apply/draft`, a form built to make a
naive fill **send something**. Every claim here is checked from the *server's* side:
the fixture counts what reached it, and the broker counts what it resolved, dialled
and held open.

What is proven:

* every adversarial counter is zero while Lumi fills (autosave on `input`/`change`/
  `blur`, GET image/fetch/beacon exfiltration, third-party, DNS-name, popup, redirect,
  submission), while the page is provably *trying* (the guard's refusal counter rises);
* the values are in the fields, read back from the DOM, and the page stays frozen;
* discard destroys the dirty document **before** the network returns -- with a
  negative control that proves the fixture *would* have autosaved;
* handover re-verifies the live page, refuses a page a human changed, and only then
  restores the network;
* a page that needs the network is stopped as `unsupported_under_freeze`, without ever
  turning the network on for one request;
* nothing was written before both layers were frozen, and every agent operation is
  refused while the page is dirty.
"""

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any

import pytest

from app.browser import local_form_draft
from app.browser.authenticated_session import AuthenticatedReadSession
from app.browser.egress_broker import BrokerMode
from app.browser.local_form_draft import (
    DraftError,
    FormFreezeController,
    discard_draft,
    handover_draft,
    run_fill,
)
from app.browser.operations import authenticated as ops
from app.browser.profile_session import ProfileSessionError
from app.browser.protocol import OperationStatus
from app.browser.registry import OperationContext, OperationResult
from app.domain.authenticated import AuthenticatedObservation
from app.domain.authenticated_forms import ElementProjection
from app.domain.form_prepare import element_identity_hash, option_identity_hash
from app.domain.local_form_draft import FillFieldInput, FillFormInput, FillResult
from tests.test_authenticated_browser import (  # noqa: F401 - fixtures and helpers
    FINGERPRINT,
    Rig,
    _host_port_site_scope,
    observation_of,
    rig,
)

pytestmark = [pytest.mark.browser]

NAME = "NAME_SECRET_S6_71A Person"
EMAIL = "EMAIL_SECRET_S6_82B@example.test"
PHONE = "+91 93000 01234"
NOTE = "NOTE_SECRET_S6_C4D"
CONTROLLED = "CONTROLLED_SECRET_S6_D5E"
MARKERS = ("NAME_SECRET_S6_71A", "EMAIL_SECRET_S6_82B", "93000 01234", "NOTE_SECRET_S6_C4D", "CONTROLLED_SECRET_S6_D5E")
ZERO_EFFECTS = ("submissions", "autosave", "blur_save", "exfiltration", "third_party", "popup_hits", "mutations")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def element(observation: AuthenticatedObservation, name: str) -> ElementProjection:
    for candidate in observation.inventory.elements:
        if candidate.accessible_name == name:
            return candidate
    raise AssertionError(f"no element named {name!r}: {[e.accessible_name for e in observation.inventory.elements]}")


def field_input(
    observation: AuthenticatedObservation, name: str, value: Any
) -> FillFieldInput:
    """The strict per-field execution input the runtime builds from an approved manifest."""
    target = element(observation, name)
    identity = element_identity_hash(
        observation_id=observation.observation_id, tab=observation.tab,
        document_epoch=observation.document_epoch, form_epoch=observation.form_epoch, element=target,
    )
    if target.control_type in ("text", "email", "tel", "number", "textarea"):
        return FillFieldInput(
            element_ref=target.element_ref, element_identity_hash=identity,
            control_type=target.control_type, text_value=value, value_digest=sha(value),
        )
    if target.control_type in ("select_single", "radiogroup"):
        option = target.option_refs[value]
        return FillFieldInput(
            element_ref=target.element_ref, element_identity_hash=identity,
            control_type=target.control_type, option_ref=option.ref,
            option_identity_hash=option_identity_hash(
                element_hash=identity, option_ref=option.ref, option_label=option.label
            ),
        )
    return FillFieldInput(
        element_ref=target.element_ref, element_identity_hash=identity,
        control_type="checkbox", checked=value,
    )


def build(
    rig: Rig, observation: AuthenticatedObservation, spec: list[tuple[str, Any]]
) -> FillFormInput:
    fields = [field_input(observation, name, value) for name, value in spec]
    return FillFormInput(
        site=rig.site.site, expected_account_fingerprint=FINGERPRINT,
        observation_id=observation.observation_id, tab=observation.tab or "t1",
        document_epoch=observation.document_epoch, form_epoch=observation.form_epoch,
        form_ref=element(observation, spec[0][0]).form_ref, manifest_digest="a" * 64, fields=fields,
    )


FULL = [
    ("Full name", NAME), ("Email address", EMAIL), ("Phone", PHONE), ("Cover note", NOTE),
    ("Country", 1), ("Preferred contact", 0), ("I agree to the terms", True),
    ("Controlled name", CONTROLLED),
]


class Prepared:
    """One observed form in a preparation-mode session, plus the freeze controller."""

    def __init__(self, rig: Rig, read: AuthenticatedReadSession, observation: AuthenticatedObservation) -> None:
        self.rig = rig
        self.read = read
        self.observation = observation
        self.freeze = FormFreezeController(rig.broker, uuid.uuid4())
        self.dispatch_id = uuid.uuid4()

    @property
    def page(self) -> Any:
        return self.read.tabs["t1"].page

    async def enter(self, *, settle: float = 3.0) -> Any:
        return await self.freeze.enter(self.read, dispatch_id=self.dispatch_id, settle_seconds=settle, drain_seconds=3.0)

    def context(self) -> OperationContext:
        return OperationContext(
            page=self.page, origin="", dispatch_id=self.dispatch_id, observation_id=uuid.uuid4(),
            authenticated_session=self.read, form_freeze=self.freeze,
        )

    async def fill(self, spec: list[tuple[str, Any]]) -> tuple[OperationResult, FillResult]:
        result = await run_fill(self.context(), build(self.rig, self.observation, spec))
        return result, FillResult.model_validate(result.observation["result"])

    async def effects(self) -> dict[str, Any]:
        return await self.rig.site.effects()

    async def value(self, selector: str) -> str:
        return str(await self.page.eval_on_selector(selector, "(e) => e.value"))


async def prepared(rig: Rig, query: str = "") -> Prepared:
    await rig.sign_in("/app")
    read = await rig.read_session()
    # The form is loaded THROUGH the read session's guard, as it always is in production:
    # a request the page starts on load is then one the guard can see and count.
    await read.tabs["t1"].page.goto(f"{rig.site.origin}/app/apply/draft{query}", wait_until="load")
    observation = observation_of(await rig.observe(read))
    read.preparation_mode = True
    return Prepared(rig, read, observation)


async def zero_effects(p: Prepared) -> None:
    effects = await p.effects()
    for name in ZERO_EFFECTS:
        assert effects[name] == 0, (name, effects)
    assert (await p.rig.external.effects())["third_party"] == 0


# ---- the core promise: fill, verify, stay frozen, send nothing --------------------------------


async def test_the_form_is_filled_verified_and_nothing_is_sent(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    p = await prepared(rig)
    assert len(FULL) == 8
    proof = await p.enter()
    assert (proof.guard_in_flight, proof.broker_active_connections) == (0, 0)
    # Measured from the freeze PROOF, not from before it: Chromium's own background traffic may
    # resolve a name in the instant before the freeze, and the claim is about what happens after.
    resolutions, dials = proof.resolution_count, proof.dial_count
    assert p.read.guard.frozen and rig.broker.mode is BrokerMode.FROZEN

    result, parsed = await p.fill(FULL)

    assert result.status is OperationStatus.OK, (result.error_code, parsed)
    assert parsed.draft_complete and parsed.dirty and parsed.error_code is None
    assert (parsed.fields_attempted, parsed.fields_verified) == (8, 8)
    assert (parsed.resolution_delta, parsed.dial_delta) == (0, 0)
    assert (parsed.guard_in_flight, parsed.broker_active_connections) == (0, 0)
    # The page really did try to send; the guard is what said no, and only the guard.
    assert parsed.blocked_request_count > 0

    # The values are in the fields, read from the DOM by the test itself.
    assert await p.value("#name") == NAME and await p.value("#email") == EMAIL
    assert await p.value("#phone") == PHONE and await p.value("#note") == NOTE
    assert await p.value("#controlled") == CONTROLLED
    assert await p.page.eval_on_selector("#country", "(e) => e.selectedIndex") == 1
    assert await p.page.eval_on_selector("input[name=contact]", "(e) => e.checked") is True
    assert await p.page.eval_on_selector("input[name=terms]", "(e) => e.checked") is True

    # Zero effects at the SERVER, zero at the resolver, zero relays, still frozen.
    await asyncio.sleep(0.5)
    await zero_effects(p)
    assert rig.broker.counters.resolutions == resolutions
    assert rig.broker.counters.dial_count == dials
    assert rig.broker.active_connections == 0
    assert p.read.guard.frozen and rig.broker.mode is BrokerMode.FROZEN
    assert list(p.read.tabs) == ["t1"] and len(p.read.context.pages) == 1  # the popup was not adopted
    counters = await p.page.evaluate("window.__lumiForm")
    assert counters["submit"] == 0

    # The raw values appear nowhere Lumi returns or logs.
    blob = json.dumps(result.observation, default=str) + caplog.text
    for marker in MARKERS:
        assert marker not in blob
    assert p.read.dirty and p.read.draft is not None


async def test_fields_are_verified_by_reading_the_dom_not_by_trusting_the_call(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL)])
    hashes = {item.element_ref: item.verified_local_value_hash for item in parsed.verified_fields}
    assert hashes[element(p.observation, "Full name").element_ref] == sha(NAME)
    assert hashes[element(p.observation, "Email address").element_ref] == sha(EMAIL)


# ---- nothing is written unless BOTH layers are frozen ------------------------------------------


async def test_a_write_is_refused_when_nothing_is_frozen(rig: Rig) -> None:
    p = await prepared(rig)
    # No freeze was entered at all.
    result, parsed = await p.fill([("Full name", NAME)])
    assert result.status is OperationStatus.FAILED_BEFORE_EFFECT and parsed.error_code == "not_frozen"
    assert not parsed.dirty and await p.value("#name") == ""
    counters = await p.page.evaluate("window.__lumiForm")
    assert counters["input"] == 0 and counters["change"] == 0


async def test_the_guard_frozen_alone_is_not_enough(rig: Rig) -> None:
    p = await prepared(rig)
    p.freeze.owner = local_form_draft.FreezeOwner(
        profile_id=p.read.profile_id, dispatch_id=p.dispatch_id, worker_generation=uuid.uuid4()
    )
    assert await p.read.guard.freeze(settle_timeout_seconds=1.0)
    # Broker still OPEN: the second layer is missing.
    with pytest.raises(DraftError) as raised:
        p.freeze.verify(p.read, p.dispatch_id)
    assert raised.value.code == "not_frozen"


async def test_a_thaw_between_two_writes_stops_the_second(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await prepared(rig)
    await p.enter()
    original = local_form_draft._read_back
    calls = 0

    async def thaw_after_first(*args: Any, **kwargs: Any) -> str:
        nonlocal calls
        digest: str = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            p.read.guard.thaw()  # one layer quietly opens between two fields
        return digest

    monkeypatch.setattr(local_form_draft, "_read_back", thaw_after_first)
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL)])
    assert calls >= 1 and parsed.error_code == "not_frozen"  # `_stabilize` re-reads what was written
    assert parsed.fields_verified == 1 and parsed.first_failed_element_ref == element(p.observation, "Email address").element_ref
    assert await p.value("#name") == NAME and await p.value("#email") == ""


# ---- what the freeze refuses to sit on top of ---------------------------------------------------


async def test_an_open_streaming_request_is_never_frozen_over(rig: Rig) -> None:
    p = await prepared(rig, "?stream=1")
    await asyncio.sleep(0.5)
    with pytest.raises(DraftError) as raised:
        await p.enter(settle=1.5)
    assert raised.value.code == "page_never_settles"
    # Fields changed: none. The open state was restored, so nothing stays frozen.
    assert await p.value("#name") == ""
    assert not p.read.guard.frozen and rig.broker.mode is BrokerMode.OPEN and p.freeze.owner is None

    # Once the request is gone, a fresh attempt is possible. The guard counts a request
    # until its own fetch ends (the page abandoning it is not the same thing), which is
    # bounded by the guard's request timeout.
    await p.page.goto(f"{rig.site.origin}/app/apply/draft", wait_until="load")
    for _ in range(300):
        if p.read.guard.in_flight == 0:
            break
        await asyncio.sleep(0.1)
    assert p.read.guard.in_flight == 0
    observation = observation_of(await rig.observe(p.read))
    again = Prepared(rig, p.read, observation)
    again.freeze = p.freeze
    assert (await again.enter()).broker_active_connections == 0


# ---- a form that needs the network is stopped, not helped ----------------------------------------


async def test_a_value_the_form_resets_without_the_network_is_unsupported_under_freeze(rig: Rig) -> None:
    p = await prepared(rig, "?reset=1")
    await p.enter()
    result, parsed = await p.fill([("Controlled name", CONTROLLED)])
    assert parsed.error_code == "unsupported_under_freeze" and not parsed.draft_complete
    # Nothing verified, so the page was destroyed while frozen and the network restored --
    # and at no point did a request reach the validation endpoint.
    assert result.observation["page_discarded"] is True
    assert (await p.effects())["validate_hits"] == 0
    assert not p.read.dirty and not p.read.guard.frozen and rig.broker.mode is BrokerMode.OPEN
    await zero_effects(p)


async def test_a_dependent_select_that_needs_the_network_stops_the_fill(rig: Rig) -> None:
    p = await prepared(rig, "?dependent=1")
    await asyncio.sleep(0.5)
    states_before = (await p.effects())["states_hits"]
    observation = observation_of(await rig.observe(p.read))
    p.observation = observation
    await p.enter()
    _, parsed = await p.fill([("Country", 1), ("State", 1)])
    assert parsed.error_code == "unsupported_under_freeze" and not parsed.draft_complete
    # Country was written and verified; the dependent state could not be produced.
    assert parsed.fields_verified == 1 and parsed.dirty
    assert p.read.guard.frozen and rig.broker.mode is BrokerMode.FROZEN
    assert (await p.effects())["states_hits"] == states_before  # no temporary network access
    await zero_effects(p)


async def test_asynchronous_validation_that_cannot_answer_is_unsupported(rig: Rig) -> None:
    p = await prepared(rig, "?validate=1")
    await p.enter()
    _, parsed = await p.fill([("Username", "someone")])
    assert parsed.error_code == "unsupported_under_freeze"
    assert (await p.effects())["validate_hits"] == 0


async def test_a_form_that_rerenders_itself_after_a_write_is_left_frozen_and_not_remapped(rig: Rig) -> None:
    p = await prepared(rig, "?rerender=1")
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL), ("Phone", PHONE), ("Cover note", NOTE)])
    assert parsed.error_code in ("element_changed", "unsupported_under_freeze") and not parsed.draft_complete
    # Two fields were verified before the phone write re-rendered the form. It stays
    # frozen and dirty; the remaining refs were never used on the new structure.
    assert parsed.dirty and parsed.fields_verified >= 1 and parsed.fields_verified < 4
    assert await p.page.query_selector("#note") is None  # the form was replaced; its refs were not reused
    assert p.read.guard.frozen and rig.broker.mode is BrokerMode.FROZEN
    assert p.read.tabs["t1"].elements == {}  # every old ref died
    await zero_effects(p)


# ---- the approved identity is re-derived from the live DOM --------------------------------------


async def test_a_control_that_changed_since_it_was_observed_is_not_written(rig: Rig) -> None:
    p = await prepared(rig)
    await p.page.evaluate("document.getElementById('name').closest('label').firstChild.textContent = 'Somebody else '")
    await p.enter()
    result, parsed = await p.fill([("Full name", NAME)])
    assert parsed.error_code == "element_changed" and not parsed.dirty
    assert await p.value("#name") == "" and not p.read.guard.frozen  # a clean page is given back


async def test_a_submit_like_or_button_target_is_refused_even_if_state_was_tampered(rig: Rig) -> None:
    p = await prepared(rig)
    submit = element(p.observation, "Submit application")
    assert submit.submit_like
    tampered = FillFormInput(
        site=rig.site.site, expected_account_fingerprint=FINGERPRINT, observation_id=p.observation.observation_id,
        tab="t1", document_epoch=p.observation.document_epoch, form_epoch=p.observation.form_epoch,
        form_ref=submit.form_ref, manifest_digest="a" * 64,
        fields=[FillFieldInput(
            element_ref=submit.element_ref, element_identity_hash="b" * 64, control_type="checkbox", checked=True,
        )],
    )
    await p.enter()
    result = await run_fill(p.context(), tampered)
    parsed = FillResult.model_validate(result.observation["result"])
    assert parsed.error_code == "unsupported_control" and not parsed.dirty
    counters = await p.page.evaluate("window.__lumiForm")
    assert counters["click"] == 0 and counters["submit"] == 0
    assert (await p.effects())["submissions"] == 0


async def test_an_observation_the_worker_never_issued_is_refused(rig: Rig) -> None:
    p = await prepared(rig)
    other = build(rig, p.observation, [("Full name", NAME)]).model_copy(update={"observation_id": uuid.uuid4()})
    await p.enter()
    result = await run_fill(p.context(), other)
    assert FillResult.model_validate(result.observation["result"]).error_code == "stale_observation"


# ---- dirty: nothing else may touch the page -------------------------------------------------------


async def test_every_agent_operation_is_refused_while_the_page_is_dirty(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    await p.fill([("Full name", NAME)])
    read = p.read
    common = rig.common
    results = [
        await ops.authenticated_observe(rig.context(read), ops.ObserveInput(**common(tab="t1"))),
        await ops.authenticated_history(rig.context(read), ops.HistoryInput(**common(tab="t1", direction="back"))),
        await ops.authenticated_tab(rig.context(read), ops.TabInput(**common(action="open"))),
        await ops.authenticated_tab(rig.context(read), ops.TabInput(**common(action="close", tab="t1"))),
    ]
    assert [item.error_code for item in results] == ["form_is_dirty"] * 4
    with pytest.raises(ProfileSessionError) as raised:
        await rig.store.close(rig.profile_id)
    assert raised.value.code == "form_is_dirty"
    assert len(read.tabs) == 1 and await p.value("#name") == NAME


async def test_a_second_owner_cannot_take_the_freeze(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    assert p.freeze.refusal_for(profile_id=uuid.uuid4(), dispatch_id=uuid.uuid4()) == "freeze_owned"
    assert p.freeze.refusal_for(profile_id=p.read.profile_id, dispatch_id=uuid.uuid4()) == "form_is_dirty"
    with pytest.raises(DraftError) as raised:
        await p.freeze.enter(p.read, dispatch_id=uuid.uuid4(), settle_seconds=1.0)
    assert raised.value.code == "form_is_dirty"
    with pytest.raises(DraftError) as thief:
        await discard_draft(p.read, p.freeze, uuid.uuid4())  # somebody else's dispatch id
    assert thief.value.code == "freeze_owned" and p.read.guard.frozen


# ---- discard: destroy the dirty document BEFORE the network returns -----------------------------------


async def test_discard_destroys_a_page_that_would_autosave_and_it_never_gets_the_chance(rig: Rig) -> None:
    p = await prepared(rig, "?timer=1")
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL)])
    assert parsed.draft_complete
    old_page = p.page
    await asyncio.sleep(1.0)  # the page's timer has been trying to autosave the whole time
    assert (await p.effects())["autosave"] == 0

    assert await discard_draft(p.read, p.freeze, p.dispatch_id) is True

    assert old_page.is_closed()  # the dirty document is gone
    assert len(p.read.context.pages) == 1  # ... and so is every popup it opened: only the blank tab remains
    assert not p.read.dirty and p.read.draft is None and p.freeze.owner is None
    assert rig.broker.mode is BrokerMode.OPEN and not p.read.guard.frozen
    await asyncio.sleep(1.2)  # long enough for any surviving timer to fire once the network is back
    await zero_effects(p)
    assert p.read.tabs["t1"].page.url == "about:blank"


async def test_negative_control_thawing_a_live_dirty_page_does_send(rig: Rig) -> None:
    """Proves the discard test above has teeth: the same page, thawed while alive, autosaves."""
    p = await prepared(rig, "?timer=1")
    await p.enter()
    await p.fill([("Full name", NAME)])
    p.freeze.release_after_destroy(p.read)  # the WRONG order: thaw first, destroy never
    await asyncio.sleep(1.5)
    assert (await p.effects())["autosave"] > 0


# ---- handover: the second exact approval, verified live -----------------------------------------------


def approved(p: Prepared, parsed: FillResult) -> dict[str, str]:
    return {item.element_ref: item.verified_local_value_hash for item in parsed.verified_fields}


async def test_handover_restores_the_network_only_after_the_live_draft_is_verified(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL), ("I agree to the terms", True)])
    await asyncio.sleep(0.3)
    await zero_effects(p)  # before handover: every server counter is zero
    page = p.page
    profile_session = rig.store.get(rig.profile_id)

    checked = await handover_draft(profile_session, p.read, p.freeze, p.dispatch_id, approved(p, parsed))

    assert checked >= 2
    assert p.read.handed_over and p.freeze.owner is None
    assert rig.broker.mode is BrokerMode.OPEN and p.page is page and not page.is_closed()
    assert await p.value("#name") == NAME and await p.value("#email") == EMAIL  # the same page, the same values
    # The human can now use the network: the wide guard answers, the read guard is gone.
    await page.evaluate("fetch('/app/apply/autosave', {method: 'POST', body: 'human'})")
    await asyncio.sleep(0.8)
    assert (await p.effects())["autosave"] >= 1
    assert (await p.effects())["submissions"] == 0  # Lumi never submits


async def test_handover_refuses_a_draft_a_human_changed_while_it_was_frozen(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME), ("Email address", EMAIL)])
    await p.page.fill("#name", "Somebody typed something else")  # the human, in the visible window

    with pytest.raises(DraftError) as raised:
        await handover_draft(rig.store.get(rig.profile_id), p.read, p.freeze, p.dispatch_id, approved(p, parsed))

    assert raised.value.code == "draft_changed"
    # Still frozen, still owned, network still refused.
    assert p.read.guard.frozen and rig.broker.mode is BrokerMode.FROZEN and p.freeze.owner is not None
    assert not p.read.handed_over


async def test_handover_refuses_an_approval_for_a_different_draft(rig: Rig) -> None:
    p = await prepared(rig)
    await p.enter()
    _, parsed = await p.fill([("Full name", NAME)])
    wrong = {ref: "c" * 64 for ref in approved(p, parsed)}
    with pytest.raises(DraftError) as raised:
        await handover_draft(rig.store.get(rig.profile_id), p.read, p.freeze, p.dispatch_id, wrong)
    assert raised.value.code == "draft_changed" and rig.broker.mode is BrokerMode.FROZEN


# ---- nothing leaves through Lumi's own outputs ----------------------------------------------------------


async def test_no_raw_value_leaves_the_worker_on_any_path(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    p = await prepared(rig, "?reset=1")
    await p.enter()
    result, _ = await p.fill([("Full name", NAME), ("Controlled name", CONTROLLED)])
    blob = json.dumps(result.observation, default=str) + caplog.text + repr(build(rig, p.observation, [("Full name", NAME)]))
    for marker in MARKERS:
        assert marker not in blob
