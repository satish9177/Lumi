"""The network-frozen local form draft, inside the isolated worker (Milestone 8b S6).

**Lumi fills the form in its own browser with the network frozen, verifies the
values are in the fields, and hands you the browser. It never submits.**

This is the only module in the worker that may contain a call that changes a page,
and it contains exactly three: `fill`, `select_option` and a checked-state setter
(`set_checked` / `check`). They are worker-internal primitives inside *one* approved
dispatch; they are not planner operations and there is no other way to reach them.
`tests/test_local_form_draft_source.py` allow-lists those names in this file alone and
fails on any click, key press, typing, upload, submit, drag, hover, focus, dispatched
event or script evaluation, anywhere in the worker -- including here.

The ordering, and why each step is where it is:

```text
enter freeze        settle -> guard frozen -> guard in_flight == 0 -> broker FROZEN
                    -> every relay cancelled -> broker active_connections == 0
                    (any failure restores the open state; nothing was written)
per field           freeze verified -> account/credential re-checked -> element re-derived
                    from the LIVE DOM -> identity compared with the approved identity
                    -> ONE primitive -> value read back from the DOM -> structure
                    fingerprint compared -> next
final               every written field re-verified (a later field's handler may have
                    reset an earlier one) -> counters checked
stay frozen         the page is dirty; the network is NOT restored
discard             the dirty document is destroyed WHILE FROZEN, verified gone, and only
                    then does the network come back
handover            (second exact approval) live draft re-verified, then the network is
                    restored for the human
```

**Discard never "lifts the freeze and reloads".** A dirty page thawed before its document is
destroyed can autosave the protected values from a timer or an event handler the
instant a request can leave. So the tab is closed while frozen, `is_closed()` is
checked, and only then are the broker and the guard opened, in that order.

**The freeze is not the assumption that nobody touches the window.** The page is
visible and the human may click it. So a handover re-reads every written field
immediately before the network is restored, and refuses (`draft_changed`, still
frozen) if the page is not the one that was approved.

What leaves this module: refs, counts, hashes and stable codes. Never a value, a
selector, an option `value=`, HTML, a URL, a cookie or page text.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import ElementHandle, Page
from playwright.async_api import Error as PlaywrightError

from app.browser import site_scope
from app.browser.authenticated_session import AuthenticatedReadSession, AuthenticatedTab
from app.browser.credential_signals import account_fingerprint, detect_credential_surface
from app.browser.egress_broker import BrokerMode, EgressBroker
from app.browser.form_observation import (
    CollectedInventory,
    ElementLocator,
    build_inventory,
    read_element_state,
    resolve_element,
    resolve_member,
)
from app.browser.profile_session import PersistentProfileSession
from app.browser.protocol import OperationStatus
from app.browser.registry import OperationContext, OperationResult
from app.browser.research_session import SessionError
from app.browser.takeover_guard import TakeoverNetworkGuard
from app.domain.authenticated_forms import ElementProjection
from app.domain.form_prepare import element_identity_hash, option_identity_hash
from app.domain.local_form_draft import (
    FillFieldInput,
    FillFormInput,
    FillResult,
    VerifiedField,
    check_verified_hash,
    choice_verified_hash,
    text_verified_hash,
)

logger = logging.getLogger("lumi.browser.local_form_draft")

WRITE_TIMEOUT_MS = 5_000
#: Let the page's own input handlers run before the value is read back: a
#: controlled input that resets what was written does so in one of them.
AFTER_WRITE_MS = 150
SETTLE_POLL_MS = 100
SETTLE_STABLE_POLLS = 3
DEFAULT_SETTLE_SECONDS = 5.0
DEFAULT_DRAIN_SECONDS = 3.0

_TEXT = frozenset({"text", "email", "tel", "number", "textarea"})
_CHOICE = frozenset({"select_single", "radiogroup"})


class DraftError(Exception):
    """A refusal with a stable code. Never carries a value, a label or a URL."""

    def __init__(self, code: str, *, element_ref: str | None = None) -> None:
        super().__init__(f"The form draft was refused ({code}).")
        self.code = code
        #: The element ref the refusal is about, when it is about one. A ref, not a label.
        self.element_ref = element_ref


@dataclass(frozen=True, slots=True)
class FreezeOwner:
    """Who holds the worker-global freeze. One at a time; nobody else may thaw it."""

    profile_id: uuid.UUID
    dispatch_id: uuid.UUID
    worker_generation: uuid.UUID


@dataclass(frozen=True, slots=True)
class FreezeProof:
    """Safe metadata proving both layers were frozen before any write."""

    worker_generation: uuid.UUID
    guard_in_flight: int
    broker_active_connections: int
    resolution_count: int
    dial_count: int
    freeze_duration_ms: int


@dataclass(slots=True)
class WrittenField:
    """Worker memory only: what was written where, as hashes and a locator description."""

    element_ref: str
    element_identity_hash: str
    control_type: str
    verified_hash: str
    #: How to find the control again in the live DOM. Never persisted or returned.
    locator: ElementLocator | None
    #: Where an option lives (select index / radio member) or the checkbox state.
    option_index: int | None = None
    checked: bool | None = None
    text_digest: str | None = None


@dataclass(slots=True)
class WorkerDraft:
    """Everything the worker knows about the page it made dirty."""

    owner: FreezeOwner
    tab_ref: str
    #: The structure fingerprint of the page at the moment it was left frozen.
    fingerprint: str
    fields: dict[str, WrittenField] = field(default_factory=dict)
    written_at: dict[str, datetime] = field(default_factory=dict)


class FormFreezeController:
    """The worker-global two-layer network freeze, and who owns it.

    The broker is worker-global, so the freeze is too: there is one owner or none.
    **This is not a boolean anybody can toggle.** Entering needs a dispatch id, a
    profile and a session; releasing is only reachable from the reviewed lifecycle
    functions below (`release_clean`, `discard_draft`, `handover_draft`), each of
    which is bound to the owner's dispatch id. While an owner exists every other
    dispatch, profile open, takeover and research session is refused.
    """

    def __init__(self, broker: EgressBroker, generation: uuid.UUID) -> None:
        self._broker = broker
        self._generation = generation
        self.owner: FreezeOwner | None = None
        self.proof: FreezeProof | None = None

    @property
    def broker(self) -> EgressBroker:
        return self._broker

    def refusal_for(self, *, profile_id: uuid.UUID | None, dispatch_id: uuid.UUID | None) -> str | None:
        """The stable code a dispatch/route must be refused with, if the freeze forbids it."""
        owner = self.owner
        if owner is None:
            return None
        if dispatch_id is not None and dispatch_id == owner.dispatch_id:
            return None
        if profile_id is not None and profile_id == owner.profile_id:
            return "form_is_dirty"
        return "freeze_owned"

    # ---- entering -----------------------------------------------------------------

    async def enter(
        self,
        session: AuthenticatedReadSession,
        *,
        dispatch_id: uuid.UUID,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        drain_seconds: float = DEFAULT_DRAIN_SECONDS,
    ) -> FreezeProof:
        """Freeze both layers and prove it, or restore the open state and refuse.

        Nothing is written here and nothing is thawed by mistake: on any failure the
        broker is opened first and the guard second, the owner is cleared, and the
        caller learns a stable code. The page was never touched.
        """
        if self.owner is not None:
            raise DraftError(
                "form_is_dirty" if self.owner.profile_id == session.profile_id else "freeze_owned"
            )
        if session.dirty or session.handed_over:
            raise DraftError("form_is_dirty")
        if not session.preparation_mode:
            raise DraftError("preparation_mode_required")
        if session.active is None or session.active not in session.tabs:
            raise DraftError("element_changed")
        tab = session.tabs[session.active]
        guard = session.guard
        started = time.monotonic()
        self.owner = FreezeOwner(
            profile_id=session.profile_id, dispatch_id=dispatch_id, worker_generation=self._generation
        )
        try:
            # 1. The page is quiet before anything is frozen on top of it.
            if not await _page_is_quiet(tab, lambda: guard.in_flight, settle_seconds):
                raise DraftError("page_never_settles")
            # 2. The Playwright guard: nothing new can enter, and what is inside drains.
            if not await guard.freeze(settle_timeout_seconds=settle_seconds):
                raise DraftError("page_never_settles")
            if not await _wait_for(lambda: not tab.epochs.pending, settle_seconds):
                raise DraftError("page_never_settles")
            # 3. The broker: frozen first, then every relay opened before it is cancelled.
            active = await self._broker.freeze_and_drain(timeout_seconds=drain_seconds)
            if active != 0 or self._broker.mode is not BrokerMode.FROZEN:
                raise DraftError("freeze_failed")
            counters = self._broker.counters
            proof = FreezeProof(
                worker_generation=self._generation,
                guard_in_flight=guard.in_flight,
                broker_active_connections=self._broker.active_connections,
                resolution_count=counters.resolutions,
                dial_count=counters.dial_count,
                freeze_duration_ms=int((time.monotonic() - started) * 1_000),
            )
            if proof.guard_in_flight != 0 or proof.broker_active_connections != 0:
                raise DraftError("freeze_failed")
        except BaseException:
            self._restore(session)
            raise
        self.proof = proof
        return proof

    def _restore(self, session: AuthenticatedReadSession | None) -> None:
        """Broker OPEN, then guard OPEN, owner cleared. Only for a page that is clean."""
        self._broker.thaw()
        if session is not None:
            session.guard.thaw()
        self.owner = None
        self.proof = None

    # ---- the check made before every write ------------------------------------------

    def verify(self, session: AuthenticatedReadSession, dispatch_id: uuid.UUID) -> None:
        """`not_frozen` unless the owner is this dispatch and BOTH layers are frozen.

        Called immediately before every field write. It reads live state rather than
        a remembered answer: the guard flag, the broker mode, the guard's in-flight
        count and the broker's relay count.
        """
        owner = self.owner
        if (
            owner is None
            or owner.dispatch_id != dispatch_id
            or owner.profile_id != session.profile_id
            or owner.worker_generation != self._generation
            or not session.guard.frozen
            or self._broker.mode is not BrokerMode.FROZEN
            or session.guard.in_flight != 0
            or self._broker.active_connections != 0
        ):
            raise DraftError("not_frozen")

    # ---- leaving ----------------------------------------------------------------------

    def release_clean(self, session: AuthenticatedReadSession, dispatch_id: uuid.UUID) -> None:
        """Thaw a page no mutating primitive was ever invoked on."""
        if self.owner is None or self.owner.dispatch_id != dispatch_id:
            raise DraftError("freeze_owned")
        if session.dirty:
            raise DraftError("form_is_dirty")
        self._restore(session)

    def release_after_destroy(self, session: AuthenticatedReadSession) -> None:
        self._restore(session)

    def release_for_handover(self) -> None:
        """Broker OPEN. The guard swap that completes a handover is the caller's."""
        self._broker.thaw()
        self.owner = None
        self.proof = None


async def _wait_for(predicate: Any, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(SETTLE_POLL_MS / 1_000)
    return bool(predicate())


async def _page_is_quiet(tab: AuthenticatedTab, in_flight: Any, timeout_seconds: float) -> bool:
    """No request in flight, at the page or in the guard, for several polls in a row."""
    deadline = time.monotonic() + timeout_seconds
    stable = 0
    while time.monotonic() < deadline:
        if not tab.epochs.pending and in_flight() == 0:
            stable += 1
            if stable >= SETTLE_STABLE_POLLS:
                return True
        else:
            stable = 0
        await asyncio.sleep(SETTLE_POLL_MS / 1_000)
    return False


# ---- checks that run before, and between, writes -------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


async def _recheck_account(page: Page, payload: FillFormInput) -> None:
    """Site scope, credential surface and account identity, on the live page."""
    if not site_scope.in_site(page.url, payload.site):
        raise DraftError("left_site_scope")
    if await detect_credential_surface(page):
        raise DraftError("login_required")
    fingerprint = await account_fingerprint(page)
    if fingerprint is None:
        raise DraftError("account_identity_unknown")
    if fingerprint != payload.expected_account_fingerprint:
        raise DraftError("account_changed")


def _projection_for(
    collected: CollectedInventory, field_input: FillFieldInput
) -> ElementProjection:
    for element in collected.inventory.elements:
        if element.element_ref == field_input.element_ref:
            return element
    raise DraftError("element_changed")


def _check_projection(
    projection: ElementProjection,
    field_input: FillFieldInput,
    payload: FillFormInput,
) -> int | None:
    """The live element must be, exactly, the one that was approved.

    Returns the option index for a choice field. Every mismatch is `element_changed`:
    no nearest match, no fuzzy label, no adaptation. A control that could never
    have been approved (submit-like, a button, multi-select, read-only, hidden or
    disabled) is `unsupported_control` -- persisted state is not trusted to have
    excluded it.
    """
    if (
        projection.submit_like
        or projection.role in ("button", "link", "option")
        or projection.control_type not in (_TEXT | _CHOICE | {"checkbox"})
    ):
        raise DraftError("unsupported_control")
    if not projection.enabled or projection.read_only or not projection.visible:
        raise DraftError("element_changed")
    if projection.control_type != field_input.control_type or projection.form_ref != payload.form_ref:
        raise DraftError("element_changed")
    identity = element_identity_hash(
        observation_id=payload.observation_id,
        tab=payload.tab,
        document_epoch=payload.document_epoch,
        form_epoch=payload.form_epoch,
        element=projection,
    )
    if identity != field_input.element_identity_hash:
        raise DraftError("element_changed")
    if field_input.control_type in _CHOICE:
        assert field_input.option_ref is not None and field_input.option_identity_hash is not None
        option = next(
            (item for item in projection.option_refs if item.ref == field_input.option_ref), None
        )
        if option is None:
            raise DraftError("unsupported_under_freeze")
        expected = option_identity_hash(
            element_hash=identity, option_ref=option.ref, option_label=option.label
        )
        if expected != field_input.option_identity_hash:
            raise DraftError("element_changed")
        return int(option.ref[2:]) - 1
    if field_input.control_type in _TEXT:
        assert field_input.text_value is not None
        if projection.max_length is not None and len(field_input.text_value) > projection.max_length:
            raise DraftError("element_changed")
    return None


# ---- the three primitives -----------------------------------------------------------------


async def _write(
    page: Page,
    handle: ElementHandle,
    locator: ElementLocator,
    field_input: FillFieldInput,
    option_index: int | None,
) -> None:
    """Exactly one primitive per field. Nothing else in the worker may do this."""
    control = field_input.control_type
    if control in _TEXT:
        assert field_input.text_value is not None
        await handle.fill(field_input.text_value, timeout=WRITE_TIMEOUT_MS)
    elif control == "select_single":
        assert option_index is not None
        await handle.select_option(index=option_index, timeout=WRITE_TIMEOUT_MS)
    elif control == "radiogroup":
        assert option_index is not None
        member = await resolve_member(page, locator, option_index)
        if member is None:
            raise DraftError("element_changed")
        try:
            await member.check(timeout=WRITE_TIMEOUT_MS)
        finally:
            await member.dispose()
    else:
        assert field_input.checked is not None
        await handle.set_checked(field_input.checked, timeout=WRITE_TIMEOUT_MS)


async def _read_back(
    page: Page,
    locator: ElementLocator,
    written: WrittenField,
    *,
    expected_text: str | None = None,
) -> str:
    """The verified hash of what the LIVE control holds now, or a refusal.

    The control is re-derived from the DOM (never the handle used to write it), read
    inside this process, hashed, and the value is dropped. It must be exactly what
    was approved. `aria-invalid` on a control Lumi just wrote is the mark of a form
    that validates over the network, which a frozen browser cannot satisfy.
    """
    state = await read_element_state(page, locator)
    if state is None:
        raise DraftError("element_changed")
    if not state.native:
        raise DraftError("unsupported_control")
    if state.invalid:
        raise DraftError("unsupported_under_freeze")
    control = written.control_type
    if control in _TEXT:
        handle = await resolve_element(page, locator)
        if handle is None:
            raise DraftError("element_changed")
        try:
            actual = await handle.input_value(timeout=WRITE_TIMEOUT_MS)
        finally:
            await handle.dispose()
        digest = text_verified_hash(actual)
        if digest != written.text_digest or (expected_text is not None and actual != expected_text):
            raise DraftError("unsupported_under_freeze")
        return digest
    if control in _CHOICE:
        if state.selected != written.option_index:
            raise DraftError("unsupported_under_freeze")
        assert written.option_index is not None
        return written.verified_hash
    if state.checked is None or state.checked != written.checked:
        raise DraftError("unsupported_under_freeze")
    return written.verified_hash


def _blocked_total(session: AuthenticatedReadSession) -> int:
    return sum(session.guard.blocked.values())


# ---- the LOCAL_DRAFT operation ---------------------------------------------------------------


async def run_fill(context: OperationContext, payload: FillFormInput) -> OperationResult:
    """Write every approved field inside one frozen dispatch, or stop and say why.

    Returns a `FillResult` always: the runtime needs to know whether the page is dirty
    even when the run failed. No planner, no provider and no page text is involved at
    any point, and the result carries no page text.
    """
    session = context.authenticated_session
    freeze = context.form_freeze
    if session is None or freeze is None:
        return _result(_refusal(FillResult, "not_frozen"))
    started = time.monotonic()
    attempted = 0
    blocked_before = _blocked_total(session)
    proof = freeze.proof
    baseline_resolutions = proof.resolution_count if proof else 0
    baseline_dials = proof.dial_count if proof else 0
    error_code: str | None = None
    first_failed: str | None = None
    try:
        tab = await _preflight(session, freeze, context.dispatch_id, payload)
        for field_input in payload.fields:
            attempted += 1
            first_failed = field_input.element_ref
            await _one_field(session, freeze, context.dispatch_id, tab, payload, field_input)
            first_failed = None
        await _final_pass(session, freeze, context.dispatch_id, tab, payload)
    except DraftError as error:
        error_code = error.code
        first_failed = error.element_ref or first_failed
    except PlaywrightError:
        # An actionability timeout or a page that went away: not a value the page
        # refused, but nothing Lumi can vouch for either. The page is treated as
        # dirty if a primitive ran; the cause is only ever a stable code.
        error_code = "unsupported_under_freeze"
    except SessionError as error:
        error_code = error.code
    if error_code is not None and session.dirty:
        await _stabilize(session, payload.tab)
    draft = session.draft
    verified = [
        VerifiedField(element_ref=ref, verified_local_value_hash=item.verified_hash)
        for ref, item in sorted(
            (draft.fields.items() if draft else []), key=lambda pair: int(pair[0][1:])
        )
    ]
    counters = freeze.broker.counters
    resolution_delta = max(0, counters.resolutions - baseline_resolutions)
    dial_delta = max(0, counters.dial_count - baseline_dials)
    if error_code is None and (resolution_delta or dial_delta):
        error_code = "not_frozen"
    complete = error_code is None
    dirty = session.dirty
    discarded = False

    if not complete:
        if not dirty:
            # Nothing was written: the page is exactly what it was. Give the network back.
            if freeze.owner is not None and freeze.owner.dispatch_id == context.dispatch_id:
                freeze.release_clean(session, context.dispatch_id)
        elif not verified:
            # A primitive ran but nothing is verified: the page may hold a value Lumi
            # cannot vouch for, so it is destroyed while frozen rather than kept.
            await _destroy_and_thaw(session, freeze, context.dispatch_id)
            dirty = False
            discarded = True
    fields_verified = len(verified) if dirty else 0
    result = FillResult(
        draft_complete=complete,
        fields_attempted=min(attempted, len(payload.fields)),
        fields_verified=fields_verified,
        verified_fields=verified if dirty else [],
        first_failed_element_ref=first_failed if not complete else None,
        error_code=error_code,
        dirty=dirty,
        blocked_request_count=max(0, _blocked_total(session) - blocked_before),
        resolution_delta=resolution_delta,
        dial_delta=dial_delta,
        guard_in_flight=session.guard.in_flight,
        broker_active_connections=freeze.broker.active_connections,
    )
    logger.info(
        "local form draft dispatch finished",
        extra={
            "dispatch_id": str(context.dispatch_id),
            "fields_attempted": result.fields_attempted,
            "fields_verified": result.fields_verified,
            "draft_complete": complete,
            "error_code": error_code,
            "dirty": dirty,
            "page_discarded": discarded,
            "blocked_request_count": result.blocked_request_count,
            "duration_ms": int((time.monotonic() - started) * 1_000),
        },
    )
    return _result(result, discarded=discarded)


async def _stabilize(session: AuthenticatedReadSession, tab_ref: str) -> None:
    """Leave a stopped draft in a state that can be verified again, or say it cannot.

    The page is left frozen and dirty. Here the structure it was left in is recorded,
    every element ref of the tab is killed if that structure is not the one that was
    observed, and each field that no longer holds what was written is dropped from the
    draft -- so what remains is only what can still be vouched for, and a later handover
    approval binds exactly that. Best effort: a page that has gone away leaves nothing
    to verify.
    """
    draft = session.draft
    tab = session.tabs.get(tab_ref)
    if draft is None or tab is None or tab.page.is_closed():
        return
    try:
        collected = await build_inventory(tab.page)
        if collected.fingerprint != tab.inventory_fingerprint:
            _snapshot(session, tab, collected)
            tab.drop_elements()
        draft.fingerprint = collected.fingerprint
        for ref, item in list(draft.fields.items()):
            if item.locator is None:
                continue
            try:
                await _read_back(tab.page, item.locator, item)
            except DraftError:
                del draft.fields[ref]
                draft.written_at.pop(ref, None)
    except PlaywrightError:
        return


def _refusal(model: type[FillResult], code: str) -> FillResult:
    return model(
        draft_complete=False, fields_attempted=0, fields_verified=0, error_code=code, dirty=False
    )


def _result(result: FillResult, *, discarded: bool = False) -> OperationResult:
    body: dict[str, Any] = {"result": result.model_dump(mode="json"), "page_discarded": discarded}
    if result.draft_complete:
        return OperationResult(status=OperationStatus.OK, observation=body)
    return OperationResult(
        status=OperationStatus.FAILED_BEFORE_EFFECT, observation=body, error_code=result.error_code
    )


async def _preflight(
    session: AuthenticatedReadSession,
    freeze: FormFreezeController,
    dispatch_id: uuid.UUID,
    payload: FillFormInput,
) -> AuthenticatedTab:
    """Every check that can refuse before a single write."""
    if not session.preparation_mode:
        raise DraftError("preparation_mode_required")
    if session.dirty or session.handed_over:
        raise DraftError("form_is_dirty")
    freeze.verify(session, dispatch_id)
    tab = session.tabs.get(payload.tab)
    if tab is None:
        raise DraftError("element_changed")
    if payload.observation_id not in tab.form_observation_ids:
        # An approval made from a document this worker never observed for this form
        # -- a headless page, a previous worker, a re-rendered form.
        raise DraftError("stale_observation")
    for field_input in payload.fields:
        tab.resolve_element(
            document_epoch=payload.document_epoch,
            form_epoch=payload.form_epoch,
            ref=field_input.element_ref,
        )
    await _recheck_account(tab.page, payload)
    collected = await build_inventory(tab.page)
    if collected.fingerprint != tab.inventory_fingerprint:
        tab.drop_elements()
        raise DraftError("element_changed")
    for field_input in payload.fields:
        _check_projection(_projection_for(collected, field_input), field_input, payload)
    return tab


async def _one_field(
    session: AuthenticatedReadSession,
    freeze: FormFreezeController,
    dispatch_id: uuid.UUID,
    tab: AuthenticatedTab,
    payload: FillFormInput,
    field_input: FillFieldInput,
) -> None:
    page = tab.page
    # The double check: immediately before THIS write, both layers are still frozen.
    freeze.verify(session, dispatch_id)
    locator = tab.resolve_element(
        document_epoch=payload.document_epoch,
        form_epoch=payload.form_epoch,
        ref=field_input.element_ref,
    )
    await _recheck_account(page, payload)
    collected = await build_inventory(page)
    if collected.fingerprint != tab.inventory_fingerprint:
        raise DraftError("element_changed")
    option_index = _check_projection(_projection_for(collected, field_input), field_input, payload)
    handle = await resolve_element(page, locator)
    if handle is None:
        raise DraftError("element_changed")
    state = await read_element_state(page, locator)
    if state is None or not state.native:
        await handle.dispose()
        raise DraftError("unsupported_control")

    written = WrittenField(
        element_ref=field_input.element_ref,
        element_identity_hash=field_input.element_identity_hash,
        control_type=field_input.control_type,
        verified_hash="",
        locator=locator,
        option_index=option_index,
        checked=field_input.checked,
        text_digest=field_input.value_digest,
    )
    blocked_before = _blocked_total(session)
    # From here the page may be dirty, so it is treated as dirty even if the
    # primitive then raises: the conservative answer is the safe one.
    _mark_dirty(session, freeze, dispatch_id, tab)
    try:
        await _write(page, handle, locator, field_input, option_index)
    finally:
        await handle.dispose()
    await page.wait_for_timeout(AFTER_WRITE_MS)

    freeze.verify(session, dispatch_id)
    written.verified_hash = await _read_back(
        page, locator, written, expected_text=field_input.text_value
    )
    if field_input.control_type in _CHOICE:
        assert field_input.option_identity_hash is not None
        written.verified_hash = choice_verified_hash(field_input.option_identity_hash)
    elif field_input.control_type == "checkbox":
        assert field_input.checked is not None
        written.verified_hash = check_verified_hash(
            field_input.element_identity_hash, field_input.checked
        )
    assert session.draft is not None
    session.draft.fields[field_input.element_ref] = written
    session.draft.written_at[field_input.element_ref] = _now()

    # After every write the value-free structure is compared with the observed one.
    after = await build_inventory(page)
    if after.fingerprint != tab.inventory_fingerprint:
        # A write that changed the form's structure. Remaining refs are NOT re-mapped.
        # If the page also tried, and was refused, to use the network, this is a form
        # that needs the network; otherwise it simply re-rendered itself.
        network_dependent = _blocked_total(session) > blocked_before
        _snapshot(session, tab, after)
        tab.drop_elements()
        raise DraftError("unsupported_under_freeze" if network_dependent else "element_changed")


def _mark_dirty(
    session: AuthenticatedReadSession,
    freeze: FormFreezeController,
    dispatch_id: uuid.UUID,
    tab: AuthenticatedTab,
) -> None:
    session.dirty = True
    if session.draft is None:
        assert freeze.owner is not None and freeze.owner.dispatch_id == dispatch_id
        session.draft = WorkerDraft(
            owner=freeze.owner, tab_ref=tab.ref, fingerprint=tab.inventory_fingerprint or ""
        )


def _snapshot(session: AuthenticatedReadSession, tab: AuthenticatedTab, collected: CollectedInventory) -> None:
    """Record the structure the page was left in, and re-derive each written field.

    Used when a write changed the form: the draft is then verified against *this*
    structure at handover. A written field whose identity no longer sits at its
    ref is left without a locator (`None`) and is not re-read live -- never
    re-pointed at a control that merely resembles it.
    """
    draft = session.draft
    if draft is None:
        return
    draft.fingerprint = collected.fingerprint
    for ref, item in draft.fields.items():
        current = collected.locators.get(ref)
        previous = item.locator
        if current is not None and previous is not None and current.identity == previous.identity and (
            current.form_key == previous.form_key and current.frame_slot == previous.frame_slot
        ):
            item.locator = ElementLocator(
                frame_slot=current.frame_slot,
                form_key=current.form_key,
                ordinal=current.ordinal,
                frame_record_count=current.frame_record_count,
                identity=current.identity,
                document_epoch=previous.document_epoch,
                form_epoch=previous.form_epoch,
            )
        else:
            item.locator = None


async def _final_pass(
    session: AuthenticatedReadSession,
    freeze: FormFreezeController,
    dispatch_id: uuid.UUID,
    tab: AuthenticatedTab,
    payload: FillFormInput,
) -> None:
    """Re-verify every written field: a later field's handler may have reset an earlier one."""
    draft = session.draft
    if draft is None:
        raise DraftError("unsupported_under_freeze")
    freeze.verify(session, dispatch_id)
    collected = await build_inventory(tab.page)
    if collected.fingerprint != tab.inventory_fingerprint:
        _snapshot(session, tab, collected)
        raise DraftError("element_changed")
    for ref, item in list(draft.fields.items()):
        if item.locator is None:
            raise DraftError("element_changed")
        try:
            await _read_back(tab.page, item.locator, item)
        except DraftError as error:
            del draft.fields[ref]
            draft.written_at.pop(ref, None)
            raise DraftError(error.code, element_ref=ref) from None
    draft.fingerprint = collected.fingerprint
    await _recheck_account(tab.page, payload)


# ---- discard ----------------------------------------------------------------------------------


async def _destroy_and_thaw(
    session: AuthenticatedReadSession, freeze: FormFreezeController, dispatch_id: uuid.UUID
) -> bool:
    """Destroy the dirty document WHILE FROZEN, verify it is gone, then thaw.

    Order is the whole property:

    1. the network is *verified* frozen (re-frozen if anything was not);
    2. a blank tab is opened first, so the profile keeps a window;
    3. every dirty tab -- and every other window of the context, popups included -- is closed
       with `run_before_unload=False` (a page's own `beforeunload` handler must not be able to
       veto this), `is_closed()` is asserted, and the guard finishes closing its popups;
    4. draft ownership is cleared;
    5. **only then** the broker opens, then the guard.

    Returns whether anything was destroyed.
    """
    guard = session.guard
    owner = freeze.owner
    if owner is None or owner.dispatch_id != dispatch_id or owner.profile_id != session.profile_id:
        raise DraftError("freeze_owned")
    if not guard.frozen or freeze.broker.mode is not BrokerMode.FROZEN or freeze.broker.active_connections:
        await guard.freeze(settle_timeout_seconds=DEFAULT_SETTLE_SECONDS)
        if await freeze.broker.freeze_and_drain(timeout_seconds=DEFAULT_DRAIN_SECONDS) != 0:
            raise DraftError("freeze_failed")
    old_tabs = list(session.tabs.values())
    guard.expect_page()
    try:
        blank = await session.context.new_page()
        guard.track(blank)
    finally:
        guard.stop_expecting()
    destroyed = False
    for tab in old_tabs:
        guard.untrack(tab.page)
        try:
            await tab.page.close(run_before_unload=False)
        except PlaywrightError:
            pass
        if not tab.page.is_closed():
            raise DraftError("freeze_failed")
        destroyed = True
    # Every OTHER window of the context goes too: a popup the dirty page opened has a navigation
    # of its own (which could carry a value in its address), and it must be gone, and the guard
    # must have finished closing it, before the network can return.
    stragglers = [page for page in list(session.context.pages) if page is not blank]
    for page in stragglers:
        try:
            await page.close(run_before_unload=False)
        except PlaywrightError:
            pass
    await guard.drain_popups()
    if any(not page.is_closed() for page in stragglers):
        raise DraftError("freeze_failed")
    session.replace_with(blank)
    session.draft = None
    session.dirty = False
    session.handed_over = False
    freeze.release_after_destroy(session)
    return destroyed


async def discard_draft(
    session: AuthenticatedReadSession, freeze: FormFreezeController, dispatch_id: uuid.UUID
) -> bool:
    """The trusted Discard / Stop: destroy the dirty page, then give the network back."""
    return await _destroy_and_thaw(session, freeze, dispatch_id)


# ---- handover ---------------------------------------------------------------------------------


async def verify_draft(
    session: AuthenticatedReadSession, expected: dict[str, str]
) -> int:
    """Re-read the LIVE page and require it to be the approved draft.

    `expected` maps each approved field to the verified hash the approval bound.
    The worker's own record must match it exactly; the page's structure must be the
    one it was left in; and every field that can be re-derived must hold the same
    value now. A human who changed a field while it was frozen fails here, before
    the network is restored.
    """
    draft = session.draft
    if draft is None or not session.dirty:
        raise DraftError("draft_changed")
    if {ref: item.verified_hash for ref, item in draft.fields.items()} != expected:
        raise DraftError("draft_changed")
    tab = session.tabs.get(draft.tab_ref)
    if tab is None or tab.page.is_closed():
        raise DraftError("draft_changed")
    collected = await build_inventory(tab.page)
    if collected.fingerprint != draft.fingerprint:
        raise DraftError("draft_changed")
    checked = 0
    for ref, item in draft.fields.items():
        if item.locator is None:
            continue
        try:
            digest = await _read_back(tab.page, item.locator, item)
        except DraftError:
            raise DraftError("draft_changed") from None
        if item.control_type in _TEXT and digest != item.verified_hash:
            raise DraftError("draft_changed")
        checked += 1
    return checked


async def handover_draft(
    profile_session: PersistentProfileSession,
    session: AuthenticatedReadSession,
    freeze: FormFreezeController,
    dispatch_id: uuid.UUID,
    expected: dict[str, str],
) -> int:
    """Second exact approval consumed: re-verify the live draft, then restore the network.

    The browser is NOT closed or reopened -- that would destroy the draft. The wide
    human-mode guard is installed first (it answers every request while the broker is
    still frozen), then the broker opens, then the read guard is opened and removed.
    Nothing is submitted, and nothing here can tell whether the site saves what it
    now receives.
    """
    owner = freeze.owner
    if owner is None or owner.dispatch_id != dispatch_id or owner.profile_id != session.profile_id:
        raise DraftError("freeze_owned")
    freeze.verify(session, dispatch_id)
    checked = await verify_draft(session, expected)
    tab = session.tabs[session.draft.tab_ref] if session.draft else None
    if tab is None:  # pragma: no cover - verify_draft refused already.
        raise DraftError("draft_changed")
    takeover = TakeoverNetworkGuard()
    await takeover.install(session.context)
    freeze.release_for_handover()
    session.guard.thaw()
    await session.guard.uninstall()
    profile_session.takeover_guard = takeover
    profile_session.takeover_page = tab.page
    session.handed_over = True
    session.draft = None
    try:
        await tab.page.bring_to_front()
    except PlaywrightError:  # pragma: no cover - the window is the human's now.
        pass
    return checked


__all__ = [
    "DEFAULT_DRAIN_SECONDS",
    "DEFAULT_SETTLE_SECONDS",
    "DraftError",
    "FormFreezeController",
    "FreezeOwner",
    "FreezeProof",
    "WorkerDraft",
    "WrittenField",
    "discard_draft",
    "handover_draft",
    "run_fill",
    "verify_draft",
]
