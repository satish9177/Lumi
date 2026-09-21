"""The five reviewed authenticated-read operations, run in the isolated worker.

Each one drives *one* bounded thing in the read session of one persistent
profile and returns one of exactly three shapes: a redacted, bounded
observation, a signals-only credential-surface result, or an identity-check
result. What a caller -- and therefore a model, since the controller copies a
validated planner choice into these inputs -- can say:

    authenticated_navigate   a tab, a link ref, an expected document epoch
    authenticated_observe    a tab
    authenticated_reveal     a tab and a link, block or (S4) element ref of the current epoch
    authenticated_history    a tab and "back" or "forward"
    authenticated_tab        "open", "activate" or "close", and a tab ref

There is no URL field, no selector, no XPath, no script, no coordinate, no
method, no header, no cookie, no key press and no text to type. The address a
link ref means exists only in this process's memory, for the epoch that issued
it. Scrolling is `scroll_into_view_if_needed` on a located element -- no
key event, so a focused control is never fed a key.

**Form observation (Milestone 8b S4) is observation only.** A page observation
also carries a bounded, value-free inventory of the page's form controls (see
`form_observation`), built after -- never before -- the checks below. Nothing in
this module can type into, choose in, check, click, upload to or submit a control:
the only thing an element ref can do is be *revalidated* against the live DOM and
scrolled into view, exactly like a block ref.

**The order of checks is the security property.** Before any page content is
projected, in this order and with no exception:

1. the tab is on a real document inside the profile's site;
2. the credential-surface detector runs (bounded DOM *counts*);
3. the account identity is derived and compared with the grant's fingerprint;
4. only then is text read, **redacted line by line**, split and bounded, and the
   form inventory listed.

A credential surface returns signals only. A missing or different identity
returns a closed enum only. In neither case does a title, a block, a link or a
character of page text leave this function. Raw text never leaves it at all:
what is returned is the redacted projection the provider will receive.
"""

import hashlib
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urldefrag, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.browser import site_scope
from app.browser.authenticated_session import (
    AuthenticatedReadSession,
    AuthenticatedTab,
    LinkEntry,
)
from app.browser.credential_signals import account_fingerprint, detect_credential_surface
from app.browser.form_observation import build_inventory, resolve_element
from app.browser.protocol import OperationStatus
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationContext,
    OperationResult,
    OperationTarget,
    Reconciliation,
    RetryPolicy,
)
from app.browser.research_session import SessionError
from app.domain.authenticated import (
    AUTH_TAB_REF,
    MAX_AUTH_BLOCKS,
    MAX_AUTH_LINKS,
    MAX_AUTH_TEXT_CHARS,
    AuthenticatedObservation,
    AuthOperation,
    CredentialSurfaceResult,
    IdentityCheckResult,
    WorkerReadResult,
)
from app.domain.authenticated_forms import ELEMENT_REF
from app.domain.redaction import Redactor
from app.domain.research import (
    BLOCK_REF,
    LINK_REF,
    MAX_BLOCK_CHARS,
    MAX_LINK_TEXT_CHARS,
    MAX_REDIRECTS,
    MAX_SEQUENCE,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    ObservedLink,
    TextBlock,
    compute_content_hash,
)

SETTLE_SECONDS = 8.0
SETTLE_POLL_MS = 300
SETTLED_AFTER_EQUAL_READS = 5
LOAD_WAIT_MS = 10_000
READ_TIMEOUT_MS = 5_000
LINK_CANDIDATES = 250
MAX_RAW_TEXT_CHARS = 400_000
REVEAL_TIMEOUT_MS = 5_000
#: How much of a block's raw text is used to locate it for `reveal`.
REVEAL_LOCATOR_CHARS = 80

NAVIGATE = "authenticated_navigate"
OBSERVE = "authenticated_observe"
REVEAL = "authenticated_reveal"
HISTORY = "authenticated_history"
TAB = "authenticated_tab"

#: A registrable domain, the shape `browser_profiles.site` already enforces.
_SITE_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$"
_DIGEST = r"^[0-9a-f]{64}$"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The observation number the controller allocated for this step.
    sequence: int = Field(ge=1, le=MAX_SEQUENCE)
    #: The profile's own bound site. Never a URL.
    site: str = Field(min_length=1, max_length=253, pattern=_SITE_PATTERN)
    #: What the grant bound. The worker compares the page's own identity signal
    #: with this and never returns the identity it read.
    expected_account_fingerprint: str = Field(pattern=_DIGEST)
    max_text_chars: int = Field(default=MAX_AUTH_TEXT_CHARS, ge=1, le=MAX_AUTH_TEXT_CHARS)
    max_blocks: int = Field(default=MAX_AUTH_BLOCKS, ge=1, le=MAX_AUTH_BLOCKS)


class NavigateInput(_Input):
    tab: str = Field(pattern=AUTH_TAB_REF)
    target_ref: str = Field(pattern=LINK_REF)
    expected_document_epoch: int = Field(ge=1, le=MAX_SEQUENCE)


class ObserveInput(_Input):
    tab: str = Field(pattern=AUTH_TAB_REF)


_REVEAL_REF = r"^(l[1-9][0-9]?|b[1-9][0-9]{0,2}|" + ELEMENT_REF[1:-1] + r")$"


class RevealInput(_Input):
    tab: str = Field(pattern=AUTH_TAB_REF)
    target_kind: Literal["link", "block", "element"]
    target_ref: str = Field(pattern=_REVEAL_REF)
    expected_document_epoch: int = Field(ge=1, le=MAX_SEQUENCE)
    #: Required for an element ref, which is valid only for one form epoch as well
    #: as one document epoch. The planner cannot produce an element target in S4;
    #: this is the revalidation primitive S5/S6 will use, proven read-only here.
    expected_form_epoch: int | None = Field(default=None, ge=1, le=MAX_SEQUENCE)

    @model_validator(mode="after")
    def _element_needs_its_epoch(self) -> "RevealInput":
        if (self.target_kind == "element") != (self.expected_form_epoch is not None):
            raise ValueError("an element ref names its form epoch; a link or block ref does not")
        return self


class HistoryInput(_Input):
    tab: str = Field(pattern=AUTH_TAB_REF)
    direction: Literal["back", "forward"]


class TabInput(_Input):
    action: Literal["open", "activate", "close"]
    tab: str | None = Field(default=None, pattern=AUTH_TAB_REF)


# ---- reading -------------------------------------------------------------------


def _clean(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())


def _chunk(text: str) -> list[str]:
    pieces: list[str] = []
    while len(text) > MAX_BLOCK_CHARS:
        cut = text.rfind(" ", 0, MAX_BLOCK_CHARS)
        cut = cut if cut > MAX_BLOCK_CHARS // 2 else MAX_BLOCK_CHARS
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        pieces.append(text)
    return pieces


def project_text(
    raw: str, redactor: Redactor, *, max_chars: int, max_blocks: int
) -> tuple[list[tuple[str, str]], bool, int]:
    """Visible text -> redacted, numbered blocks within budget.

    Returns ``([(redacted piece, raw line)], truncated, total raw chars)``.

    **Each whole line is redacted before it is split**, so an identifier cannot
    be cut in half by a block boundary and slip past the patterns as two short
    runs. The raw line travels only as far as this process's own reveal table.
    """
    text = raw[:MAX_RAW_TEXT_CHARS]
    truncated = len(raw) > MAX_RAW_TEXT_CHARS
    pieces: list[tuple[str, str]] = []
    total = 0
    for line in text.splitlines():
        cleaned = _clean(line)
        if not cleaned:
            continue
        total += len(cleaned)
        for piece in _chunk(redactor.redact(cleaned)):
            pieces.append((piece, cleaned))
    blocks: list[tuple[str, str]] = []
    used = 0
    for piece, source in pieces:
        if len(blocks) >= max_blocks or used + len(piece) > max_chars:
            truncated = True
            break
        blocks.append((piece, source))
        used += len(piece)
    return blocks, truncated, total


async def _visible_text(page: Page) -> str:
    body = page.locator("body")
    if await body.count() == 0:
        return ""
    return await body.inner_text(timeout=READ_TIMEOUT_MS)


async def _settle(page: Page, tab: AuthenticatedTab) -> bool:
    """Wait until the document stops changing. Compares hashes, keeps no text."""
    try:
        await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
    except PlaywrightError:
        pass
    deadline = time.monotonic() + SETTLE_SECONDS
    previous: tuple[int, str] | None = None
    equal = 0
    while time.monotonic() < deadline:
        try:
            digest = hashlib.sha256((await _visible_text(page)).encode("utf-8")).hexdigest()
            current = (tab.epoch, digest)
        except PlaywrightError:
            current = (tab.epoch, "unreadable")
        if current == previous and not tab.epochs.in_flight:
            equal += 1
            if equal >= SETTLED_AFTER_EQUAL_READS:
                return True
        else:
            equal = 0
        previous = current
        await page.wait_for_timeout(SETTLE_POLL_MS)
    return False


async def _read_links(
    page: Page, base_url: str, session: AuthenticatedReadSession, redactor: Redactor
) -> tuple[list[ObservedLink], dict[str, LinkEntry], int]:
    """Bounded in-site links, as refs plus a redacted label and a host.

    Only destinations this guard would allow as a top-level document become
    refs, so a page cannot get an out-of-site address in front of the planner
    even as a suggestion. Addresses stay here, in the returned table.
    """
    anchors = page.locator("a[href]")
    count = min(await anchors.count(), LINK_CANDIDATES)
    seen: set[str] = set()
    links: list[ObservedLink] = []
    table: dict[str, LinkEntry] = {}
    for index in range(count):
        anchor = anchors.nth(index)
        try:
            href = await anchor.get_attribute("href", timeout=1_000)
            label = _clean(await anchor.inner_text(timeout=1_000))
        except PlaywrightError:
            continue
        if not href:
            continue
        absolute, _ = urldefrag(urljoin(base_url, href.strip()))
        if (
            not absolute.startswith(("https://", "http://"))
            or len(absolute) > MAX_URL_CHARS
            or not absolute.isascii()
            or any(character.isspace() for character in absolute)
            or absolute in seen
            or not session.guard.allows_top_level(absolute)
        ):
            continue
        seen.add(absolute)
        if len(links) < MAX_AUTH_LINKS:
            ref = f"l{len(links) + 1}"
            host = urlsplit(absolute).hostname or session.site
            links.append(
                ObservedLink(
                    id=ref,
                    text=redactor.redact(label)[:MAX_LINK_TEXT_CHARS],
                    host=host,
                )
            )
            table[ref] = LinkEntry(href=absolute, index=index)
    return links, table, len(seen)


def _failed(
    code: str,
    *,
    status: OperationStatus = OperationStatus.FAILED_BEFORE_EFFECT,
    **observation: Any,
) -> OperationResult:
    return OperationResult(status=status, observation=observation, error_code=code)


def _read_result(result: WorkerReadResult) -> OperationResult:
    observation: dict[str, Any] = {"result": result.model_dump(mode="json")}
    if result.observation is not None:
        observation["observation_id"] = str(result.observation.observation_id)
    return OperationResult(status=OperationStatus.OK, observation=observation)


def _now() -> datetime:
    return datetime.now(UTC)


async def _tab_state(
    *,
    context: OperationContext,
    session: AuthenticatedReadSession,
    tab: AuthenticatedTab | None,
    operation: AuthOperation,
    payload: _Input,
) -> OperationResult:
    """Which tabs exist, for a tab that holds no document (or none at all)."""
    try:
        observation = AuthenticatedObservation(
            observation_id=context.observation_id,
            kind="tab_state",
            operation=operation,
            sequence=payload.sequence,
            profile_id=session.profile_id,
            tab=tab.ref if tab is not None else session.active,
            document_epoch=tab.epoch if tab is not None else 1,
            form_epoch=tab.form_epoch if tab is not None else 0,
            settled=True,
            observed_at=_now(),
            open_tabs=session.open_tabs,
            content_hash=compute_content_hash(
                kind="tab_state", final_url="", title="", blocks=[], links=[], results=[]
            ),
        )
    except ValidationError:  # pragma: no cover - the shape is fixed here.
        return _failed("observation_invalid")
    return _read_result(WorkerReadResult(observation=observation))


async def _page_result(
    *,
    context: OperationContext,
    session: AuthenticatedReadSession,
    tab: AuthenticatedTab,
    operation: AuthOperation,
    payload: _Input,
    settled: bool,
) -> OperationResult:
    """The gate every projection passes through. See the module docstring."""
    page = tab.page
    for _ in range(2):
        epoch_before = tab.epoch
        current_url, _fragment = urldefrag(page.url)
        if not current_url.startswith(("http://", "https://")):
            return await _tab_state(
                context=context, session=session, tab=tab, operation=operation, payload=payload
            )
        # 1. Scope. A tab that ended somewhere else is refused, never read.
        if not site_scope.in_site(current_url, session.site):
            return _failed("left_site_scope")
        # 2. Credential surface: bounded counts, and nothing else, on a hit.
        signals = await detect_credential_surface(page)
        if signals:
            tab.drop_elements()
            return _read_result(
                WorkerReadResult(
                    credential_surface=CredentialSurfaceResult(
                        signals=signals,
                        tab=tab.ref,
                        document_epoch=tab.epoch,
                        observed_at=_now(),
                    )
                )
            )
        # 3. Identity. Read, hashed at once, compared, and never returned.
        fingerprint = await account_fingerprint(page)
        if fingerprint != payload.expected_account_fingerprint:
            tab.drop_elements()
            return _read_result(
                WorkerReadResult(
                    identity=IdentityCheckResult(
                        kind="account_identity_unknown" if fingerprint is None else "account_changed",
                        tab=tab.ref,
                        document_epoch=tab.epoch,
                        observed_at=_now(),
                    )
                )
            )
        # 4. Only now is text read, and it is redacted before it is anything.
        redactor = Redactor()
        raw_text = await _visible_text(page)
        raw_title = _clean(await page.title())
        title = redactor.redact(raw_title)[:MAX_TITLE_CHARS]
        links, link_table, total_links = await _read_links(page, current_url, session, redactor)
        # The form inventory: passive, value-free, and only after both gates.
        collected = await build_inventory(page)
        if tab.epoch == epoch_before:
            break
    else:
        return _failed("document_unstable")

    projected, truncated, total_text = project_text(
        raw_text, redactor, max_chars=payload.max_text_chars, max_blocks=payload.max_blocks
    )
    blocks = [TextBlock(id=f"b{index + 1}", text=text) for index, (text, _) in enumerate(projected)]
    form_epoch = tab.adopt_inventory(collected)
    tab.note_observation(context.observation_id)
    tab.record(
        links=link_table,
        blocks={f"b{index + 1}": raw for index, (_, raw) in enumerate(projected)},
    )
    host = urlsplit(current_url).hostname or session.site
    try:
        observation = AuthenticatedObservation(
            observation_id=context.observation_id,
            kind="page",
            operation=operation,
            sequence=payload.sequence,
            profile_id=session.profile_id,
            tab=tab.ref,
            document_epoch=tab.epoch,
            host=host,
            title=title,
            settled=settled,
            truncated=truncated or total_links > len(links),
            form_epoch=form_epoch,
            inventory=collected.inventory,
            observed_at=_now(),
            blocks=blocks,
            links=links,
            open_tabs=session.open_tabs,
            total_text_chars=total_text,
            total_link_count=total_links,
            redactions=dict(redactor.counts),
            content_hash=compute_content_hash(
                kind="page", final_url=host, title=title, blocks=blocks, links=links, results=[]
            ),
        )
    except ValidationError:
        return _failed("observation_invalid")
    return _read_result(WorkerReadResult(observation=observation))


# ---- operations -----------------------------------------------------------------


def _session(context: OperationContext) -> AuthenticatedReadSession:
    session = context.authenticated_session
    if session is None:
        raise SessionError("unknown_session")
    if session.dirty or session.handed_over:
        # Milestone 8b S6. A page holding a local draft can reflect a protected
        # value anywhere in its text, so no agent read, navigation, history move
        # or tab change may run against it: only the reviewed discard and
        # handover operations touch a dirty session, and neither is one of these.
        raise SessionError("form_is_dirty")
    return session


def _take_block(session: AuthenticatedReadSession) -> str | None:
    """Why the main document's last navigation was refused -- once, then cleared.

    Cleared on read so a refusal is reported by the step that saw it and is not
    replayed onto every later observation of the same tab.
    """
    block = session.guard.main_frame_block
    session.guard.main_frame_block = None
    return block


async def authenticated_navigate(context: OperationContext, payload: NavigateInput) -> OperationResult:
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
        entry = tab.resolve_link(epoch=payload.expected_document_epoch, ref=payload.target_ref)
    except SessionError as error:
        return _failed(error.code)
    guard = session.guard
    if not guard.allows_top_level(entry.href):
        # Defence in depth: a ref only ever names an in-site link, but this is
        # the step that would send the request, so it asks again.
        return _failed("left_site_scope")

    session.activate(tab.ref)
    url = entry.href
    guard.following_redirects = True
    response = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            guard.pending_redirect = None
            guard.main_frame_block = None
            # A GET may reach the site from here on. A timeout or a lost
            # worker after this line is an unknown outcome, not a failure.
            context.submitted = True
            try:
                response = await tab.page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                block = _take_block(session)
                if block is not None:
                    return _failed(block)
                raise
            block = _take_block(session)
            if block is not None:
                return _failed(block)
            target = guard.pending_redirect
            if target is None:
                break
            if hop >= MAX_REDIRECTS:
                return _failed("too_many_redirects")
            url = target
        else:  # pragma: no cover - the loop always breaks or returns first.
            return _failed("too_many_redirects")
    finally:
        guard.following_redirects = False

    if response is None:
        return _failed("no_document")
    if response.status >= 400:
        return _failed(
            "page_http_error", status=OperationStatus.RESOURCE_UNAVAILABLE, http_status=response.status
        )
    settled = await _settle(tab.page, tab)
    block = _take_block(session)
    if block is not None:
        return _failed(block)
    return await _page_result(
        context=context,
        session=session,
        tab=tab,
        operation=AuthOperation.NAVIGATE,
        payload=payload,
        settled=settled,
    )


async def authenticated_observe(context: OperationContext, payload: ObserveInput) -> OperationResult:
    """Re-read a tab. Issues no request of its own."""
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
    except SessionError as error:
        return _failed(error.code)
    settled = await _settle(tab.page, tab)
    # A navigation the page started on its own was refused since the last look
    # (a script leaving the site, a meta refresh). Say so.
    if _take_block(session) == "left_site_scope":
        return _failed("left_site_scope")
    return await _page_result(
        context=context,
        session=session,
        tab=tab,
        operation=AuthOperation.OBSERVE,
        payload=payload,
        settled=settled,
    )


async def _reveal_element(tab: AuthenticatedTab, payload: RevealInput) -> None:
    """Revalidate one element ref and scroll it into view. **Nothing else.**

    This is the read-only proof of the revalidation mechanism S5/S6 will build on.
    It never focuses, clicks, types, hovers or dispatches anything; the only thing
    done to the control is `scroll_into_view_if_needed`. In order:

    1. the document epoch matches exactly (`stale_document_epoch`);
    2. the form epoch matches exactly (`element_changed`);
    3. the ref was issued for this inventory (`unknown_target_ref`);
    4. the control is re-derived from the *current* DOM: one control at the ordinal,
       the same number of controls in its frame, the same semantic identity
       (role, control type, accessible name, required, read-only, enabled,
       visible). Anything else is `element_changed`, every element ref of the tab
       is invalidated and a fresh observation is required.

    No force, no nearest match, no search by similar label. A DOM replacement with
    an identical observed fingerprint is indistinguishable by construction: that
    is the reviewed residual, stated in `docs/reviews/milestone-8-s4.md`.
    """
    locator = tab.resolve_element(
        document_epoch=payload.expected_document_epoch,
        form_epoch=payload.expected_form_epoch or 0,
        ref=payload.target_ref,
    )
    page = tab.page
    if await detect_credential_surface(page):
        # A login surface appeared without navigation. Touch nothing; the read
        # that follows reports it and no element ref survives.
        tab.drop_elements()
        return
    handle = await resolve_element(page, locator)
    if handle is None:
        tab.drop_elements()
        raise SessionError("element_changed")
    try:
        await handle.scroll_into_view_if_needed(timeout=REVEAL_TIMEOUT_MS)
    finally:
        await handle.dispose()


async def authenticated_reveal(context: OperationContext, payload: RevealInput) -> OperationResult:
    """Scroll one observed block, link or element into view. No key press, no coordinates."""
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
        session.activate(tab.ref)
        page = tab.page
        if payload.target_kind == "element":
            if not payload.target_ref.startswith("e"):
                return _failed("target_mismatch")
            await _reveal_element(tab, payload)
        elif payload.target_kind == "link":
            if not payload.target_ref.startswith("l"):
                return _failed("target_mismatch")
            entry = tab.resolve_link(epoch=payload.expected_document_epoch, ref=payload.target_ref)
            locator = page.locator("a[href]").nth(entry.index)
            current = await locator.get_attribute("href", timeout=READ_TIMEOUT_MS)
            if current is None or urldefrag(urljoin(page.url, current.strip()))[0] != entry.href:
                # The document changed underneath the ref. Never nearest-match.
                return _failed("stale_target_ref")
        else:
            if not payload.target_ref.startswith("b"):
                return _failed("target_mismatch")
            raw = tab.resolve_block(epoch=payload.expected_document_epoch, ref=payload.target_ref)
            candidates = page.get_by_text(raw[:REVEAL_LOCATOR_CHARS], exact=False)
            if await candidates.count() == 0:
                return _failed("reveal_target_not_found")
            locator = candidates.first
        if payload.target_kind != "element":
            await locator.scroll_into_view_if_needed(timeout=REVEAL_TIMEOUT_MS)
    except SessionError as error:
        return _failed(error.code)
    except PlaywrightError:
        return _failed("reveal_failed")
    settled = await _settle(page, tab)
    return await _page_result(
        context=context,
        session=session,
        tab=tab,
        operation=AuthOperation.REVEAL,
        payload=payload,
        settled=settled,
    )


async def authenticated_history(context: OperationContext, payload: HistoryInput) -> OperationResult:
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
    except SessionError as error:
        return _failed(error.code)
    session.activate(tab.ref)
    guard = session.guard
    guard.main_frame_block = None
    guard.pending_redirect = None
    context.submitted = True
    try:
        if payload.direction == "back":
            await tab.page.go_back(wait_until="domcontentloaded")
        else:
            await tab.page.go_forward(wait_until="domcontentloaded")
    except PlaywrightError:
        block = _take_block(session)
        if block is not None:
            return _failed(block)
        return _failed("history_unavailable")
    settled = await _settle(tab.page, tab)
    block = _take_block(session)
    if block is not None:
        return _failed(block)
    return await _page_result(
        context=context,
        session=session,
        tab=tab,
        operation=AuthOperation.HISTORY,
        payload=payload,
        settled=settled,
    )


async def authenticated_tab(context: OperationContext, payload: TabInput) -> OperationResult:
    try:
        session = _session(context)
        if payload.action == "open":
            if payload.tab is not None:
                return _failed("tab_not_expected")
            tab: AuthenticatedTab | None = await session.open_tab()
        else:
            if payload.tab is None:
                return _failed("tab_required")
            if payload.action == "activate":
                tab = session.activate(payload.tab)
            else:
                await session.close_tab(payload.tab)
                tab = session.tabs.get(session.active) if session.active else None
    except SessionError as error:
        return _failed(error.code)
    if payload.action == "activate" and tab is not None:
        return await _page_result(
            context=context,
            session=session,
            tab=tab,
            operation=AuthOperation.TAB,
            payload=payload,
            settled=True,
        )
    return await _tab_state(
        context=context, session=session, tab=tab, operation=AuthOperation.TAB, payload=payload
    )


def _operation(
    name: str,
    description: str,
    input_model: type[BaseModel],
    handler: Any,
    postconditions: tuple[str, ...],
) -> BrowserOperation:
    return BrowserOperation(
        name=name,
        description=description,
        input_model=input_model,
        output_model=WorkerReadResult,
        effect=Effect.ACCOUNT_READ,
        retry=RetryPolicy.OBSERVE_THEN_REPLAN,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=60.0,
        timeout_meaning=(
            "A request may have reached the site, and Lumi cannot tell whether it recorded "
            "the visit. Nothing is retried: the task re-observes the tab and plans again."
        ),
        preconditions=(
            "an ACTIVE authenticated_read grant authorised this operation and a single-use "
            "step authorization was consumed by a persisted execution attempt",
            "the profile is open in this worker with no takeover, and the grant's account "
            "fingerprint and revoke epoch still hold",
        ),
        postconditions=postconditions,
        handler=handler,
        target=OperationTarget.AUTHENTICATED_SESSION,
    )


_COMMON_POSTCONDITIONS = (
    "no request other than GET or HEAD left the browser context",
    "no top-level document outside the profile's site was contacted or followed",
    "the credential surface and account identity were checked before any text was read",
    "any text returned is the redacted projection, bounded by the grant",
)

OPERATIONS: tuple[BrowserOperation, ...] = (
    _operation(
        NAVIGATE,
        "Open one in-site link the worker itself issued for the tab's current document. GET "
        "only; no clicks, typing, form submissions, uploads, downloads or non-GET requests. "
        "account_scoped_read means Lumi performs no intentional change; the website may still "
        "record the visit.",
        NavigateInput,
        authenticated_navigate,
        ("the link ref resolved against the tab's current document epoch", *_COMMON_POSTCONDITIONS),
    ),
    _operation(
        OBSERVE,
        "Re-read one task-owned tab: bounded redacted text and bounded in-site links. Issues no "
        "request of its own.",
        ObserveInput,
        authenticated_observe,
        _COMMON_POSTCONDITIONS,
    ),
    _operation(
        REVEAL,
        "Scroll one observed block, link or (revalidated) form element of the current document "
        "into view, then read the tab again. No key press, no focus, no click, no coordinates, no "
        "nearest match.",
        RevealInput,
        authenticated_reveal,
        ("the ref resolved against the tab's current document epoch", *_COMMON_POSTCONDITIONS),
    ),
    _operation(
        HISTORY,
        "Move one task-owned tab back or forward in its own history and read it again.",
        HistoryInput,
        authenticated_history,
        _COMMON_POSTCONDITIONS,
    ),
    _operation(
        TAB,
        "Open, activate or close an account-reading tab, within the grant's tab budget. Never "
        "touches a tab this session did not create.",
        TabInput,
        authenticated_tab,
        ("only tabs this session created were opened, activated or closed",),
    ),
)

__all__ = [
    "HISTORY",
    "NAVIGATE",
    "OBSERVE",
    "OPERATIONS",
    "REVEAL",
    "TAB",
    "HistoryInput",
    "NavigateInput",
    "ObserveInput",
    "RevealInput",
    "TabInput",
    "project_text",
]
