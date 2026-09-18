"""The five reviewed research operations, executed in the isolated worker.

Each one drives *one* bounded thing in a task-owned public browser session and
returns a bounded, hashed observation. Read the input models below and notice
what a caller -- and therefore a model, since the controller copies a
validated planner choice into them -- can say:

    research_navigate   a tab, a target kind, a target ref, an expected
                        document epoch, and an address the *controller*
                        resolved from its own persisted ref table
    research_observe    a tab
    research_scroll     a tab and "down" or "up"
    research_history    a tab and "back" or "forward"
    research_tab        "open", "activate" or "close", and a tab ref

There is no selector, no XPath, no script, no `evaluate`, no coordinate, no
method, no header, no cookie, no browser flag and no free-form URL field
anywhere in that vocabulary. Scrolling is a fixed key press, not JavaScript.
Reading uses Playwright locators only.

Two independent checks stand between a link ref and a request:

1. the tab's live document epoch must equal the epoch the ref was issued for,
   and the worker's own ref table for that epoch must contain the ref;
2. the address that table yields must be exactly the address the controller
   resolved from its persisted copy of the same observation.

Disagreement is refused, never reconciled. Everything read here is data: no
page text can change which operation runs, where the browser goes next, which
tab is open, or what Lumi is authorised to do.
"""

import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urldefrag, urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
from app.browser.research_session import ResearchBrowserSession, ResearchTab, SessionError
from app.domain.public_url import UrlPolicyError
from app.domain.research import (
    MAX_BLOCK_CHARS,
    MAX_BLOCKS,
    MAX_LINK_TEXT_CHARS,
    MAX_LINKS,
    MAX_REDIRECTS,
    MAX_SEQUENCE,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    TAB_REF,
    ObservedLink,
    ResearchObservation,
    ResearchOperation,
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
#: A scroll is this key press and nothing else. No JavaScript, no coordinates,
#: no arbitrary key vocabulary.
SCROLL_KEYS = {"down": "PageDown", "up": "PageUp"}

NAVIGATE = "research_navigate"
OBSERVE = "research_observe"
SCROLL = "research_scroll"
HISTORY = "research_history"
TAB = "research_tab"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The observation number the controller allocated for this step, so the
    #: hashed observation carries the ref (`o<sequence>`) a planner will cite.
    sequence: int = Field(ge=1, le=MAX_SEQUENCE)


class NavigateInput(_Input):
    tab: str = Field(pattern=TAB_REF)
    #: Resolved by the controller from its persisted ref table. Checked again
    #: here against this worker's destination policy and, for a link, against
    #: the worker's own table for the tab's current document.
    url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    target_kind: Literal["link", "result", "seed"]
    target_ref: str | None = Field(default=None, max_length=8)
    expected_document_epoch: int | None = Field(default=None, ge=1, le=MAX_SEQUENCE)


class ObserveInput(_Input):
    tab: str = Field(pattern=TAB_REF)


class ScrollInput(_Input):
    tab: str = Field(pattern=TAB_REF)
    direction: Literal["down", "up"]


class HistoryInput(_Input):
    tab: str = Field(pattern=TAB_REF)
    direction: Literal["back", "forward"]


class TabInput(_Input):
    action: Literal["open", "activate", "close"]
    tab: str | None = Field(default=None, pattern=TAB_REF)


# ---- reading -------------------------------------------------------------------


def _clean(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())


def split_blocks(raw: str) -> tuple[list[TextBlock], bool, int]:
    """Visible text -> numbered blocks within budget. (blocks, truncated, total)."""
    text = raw[:MAX_RAW_TEXT_CHARS]
    truncated = len(raw) > MAX_RAW_TEXT_CHARS
    pieces: list[str] = []
    total = 0
    for line in text.splitlines():
        cleaned = _clean(line)
        if not cleaned:
            continue
        total += len(cleaned)
        while len(cleaned) > MAX_BLOCK_CHARS:
            cut = cleaned.rfind(" ", 0, MAX_BLOCK_CHARS)
            cut = cut if cut > MAX_BLOCK_CHARS // 2 else MAX_BLOCK_CHARS
            pieces.append(cleaned[:cut].rstrip())
            cleaned = cleaned[cut:].lstrip()
        if cleaned:
            pieces.append(cleaned)
    blocks: list[TextBlock] = []
    used = 0
    for piece in pieces:
        if len(blocks) >= MAX_BLOCKS or used + len(piece) > MAX_TEXT_CHARS:
            truncated = True
            break
        blocks.append(TextBlock(id=f"b{len(blocks) + 1}", text=piece))
        used += len(piece)
    return blocks, truncated, total


async def _visible_text(page: Page) -> str:
    body = page.locator("body")
    if await body.count() == 0:
        return ""
    return await body.inner_text(timeout=READ_TIMEOUT_MS)


async def _read_links(
    page: Page, base_url: str, session: ResearchBrowserSession
) -> tuple[list[ObservedLink], dict[str, str], int]:
    """Bounded links, as refs plus a label and a host. Addresses stay here.

    Only destinations this worker's own policy would allow become refs, so a
    page cannot get a refused address in front of the planner even as a
    suggestion, and following a ref can never be the first check of it.
    """
    anchors = page.locator("a[href]")
    count = min(await anchors.count(), LINK_CANDIDATES)
    seen: set[str] = set()
    links: list[ObservedLink] = []
    targets: dict[str, str] = {}
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
        ):
            continue
        try:
            checked = session.policy.check(absolute)
        except UrlPolicyError:
            continue
        seen.add(absolute)
        if len(links) < MAX_LINKS:
            ref = f"l{len(links) + 1}"
            links.append(
                ObservedLink(id=ref, text=label[:MAX_LINK_TEXT_CHARS], host=checked.host)
            )
            targets[ref] = checked.url
    return links, targets, len(seen)


async def _settle(page: Page, tab: ResearchTab) -> bool:
    try:
        await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
    except PlaywrightError:
        pass  # Slow subresources do not prevent reading what is visible.
    deadline = time.monotonic() + SETTLE_SECONDS
    previous: tuple[int, str] | None = None
    equal = 0
    while time.monotonic() < deadline:
        try:
            current = (tab.epoch, await _visible_text(page))
        except PlaywrightError:
            current = (tab.epoch, "\x00unreadable")
        if current == previous and not tab.epochs.in_flight:
            equal += 1
            if equal >= SETTLED_AFTER_EQUAL_READS:
                return True
        else:
            equal = 0
        previous = current
        await page.wait_for_timeout(SETTLE_POLL_MS)
    return False


def _failed(code: str, **observation: Any) -> OperationResult:
    return OperationResult(
        status=OperationStatus.FAILED_BEFORE_EFFECT, observation=observation, error_code=code
    )


def _built(
    observation: ResearchObservation, targets: dict[str, str]
) -> OperationResult:
    payload = observation.model_dump(mode="json")
    # `targets` travels beside the observation, never inside it: the projection
    # is the part a planner may see, and it carries refs, not addresses. The
    # observation id is repeated at the top level so the dispatch ledger
    # records which observation this dispatch produced, exactly as a booking
    # or an inspection does.
    return OperationResult(
        status=OperationStatus.OK,
        observation={
            "observation_id": str(observation.observation_id),
            "observation": payload,
            "targets": targets,
        },
    )


async def _page_observation(
    *,
    context: OperationContext,
    session: ResearchBrowserSession,
    tab: ResearchTab,
    sequence: int,
    operation: ResearchOperation,
    requested_url: str | None,
    redirects: list[str],
    settled: bool,
) -> OperationResult:
    """Read the tab twice if the document moves underneath the first read."""
    policy = session.policy
    for _ in range(2):
        epoch_before = tab.epoch
        raw_final, _fragment = urldefrag(tab.page.url)
        if not raw_final.startswith(("http://", "https://")):
            return await _tab_state_observation(
                context=context,
                session=session,
                tab=tab,
                sequence=sequence,
                operation=operation,
            )
        try:
            final = policy.check(raw_final)
        except UrlPolicyError as error:
            return _failed("final_destination_not_allowed", refusal=error.code)
        raw_text = await _visible_text(tab.page)
        title = _clean(await tab.page.title())[:MAX_TITLE_CHARS]
        links, targets, total_links = await _read_links(tab.page, final.url, session)
        if tab.epoch == epoch_before:
            break
    else:
        return _failed("document_unstable")

    blocks, truncated, total_text = split_blocks(raw_text)
    tab.record_links(targets)
    try:
        observation = ResearchObservation(
            observation_id=context.observation_id,
            kind="page",
            operation=operation,
            sequence=sequence,
            session_id=session.id,
            tab=tab.ref,
            document_epoch=tab.epoch,
            requested_url=requested_url,
            final_url=final.url,
            final_host=final.host,
            redirects=redirects,
            title=title,
            settled=settled,
            truncated=truncated or total_links > len(links),
            observed_at=datetime.now(UTC),
            blocks=blocks,
            links=links,
            open_tabs=session.open_tabs,
            total_text_chars=total_text,
            total_link_count=total_links,
            content_hash=compute_content_hash(
                kind="page", final_url=final.url, title=title, blocks=blocks, links=links, results=[]
            ),
        )
    except ValidationError:
        return _failed("observation_invalid")
    return _built(observation, targets)


async def _tab_state_observation(
    *,
    context: OperationContext,
    session: ResearchBrowserSession,
    tab: ResearchTab | None,
    sequence: int,
    operation: ResearchOperation,
) -> OperationResult:
    """What tabs exist, for a tab that holds no document yet (or none at all)."""
    try:
        observation = ResearchObservation(
            observation_id=context.observation_id,
            kind="tab_state",
            operation=operation,
            sequence=sequence,
            session_id=session.id,
            tab=tab.ref if tab is not None else session.active,
            document_epoch=tab.epoch if tab is not None else 1,
            settled=True,
            observed_at=datetime.now(UTC),
            open_tabs=session.open_tabs,
            content_hash=compute_content_hash(
                kind="tab_state", final_url="", title="", blocks=[], links=[], results=[]
            ),
        )
    except ValidationError:  # pragma: no cover - the shape is fixed here.
        return _failed("observation_invalid")
    return _built(observation, {})


# ---- operations -----------------------------------------------------------------


def _session(context: OperationContext) -> ResearchBrowserSession:
    session = context.research_session
    if session is None:
        raise SessionError("unknown_session")
    return session


async def research_navigate(context: OperationContext, payload: NavigateInput) -> OperationResult:
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
        if payload.target_kind == "link":
            if payload.target_ref is None or payload.expected_document_epoch is None:
                return _failed("target_incomplete")
            resolved = tab.resolve_link(
                epoch=payload.expected_document_epoch, ref=payload.target_ref
            )
            if resolved != payload.url:
                # The controller's persisted table and the worker's live table
                # disagree. Never reconciled: one of them is looking at a
                # document the other is not.
                return _failed("target_mismatch")
    except SessionError as error:
        return _failed(error.code)

    policy, guard = session.policy, session.guard
    try:
        requested = policy.check(payload.url)
    except UrlPolicyError as error:
        return _failed(error.code)

    session.activate(tab.ref)
    redirects: list[str] = []
    url = requested.url
    guard.following_redirects = True
    response = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            guard.pending_redirect = None
            guard.main_frame_block = None
            try:
                response = await tab.page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                if guard.main_frame_block is not None:
                    return _failed(guard.main_frame_block, requested_url=requested.url)
                raise
            target = guard.pending_redirect
            if target is None:
                break
            try:
                next_hop = policy.check(target)
            except UrlPolicyError as error:
                return _failed(
                    "redirect_blocked", requested_url=requested.url, redirect_refusal=error.code
                )
            if len(redirects) >= MAX_REDIRECTS:
                return _failed("too_many_redirects", requested_url=requested.url)
            redirects.append(next_hop.url)
            url = next_hop.url
        else:  # pragma: no cover - the loop always breaks or returns first.
            return _failed("too_many_redirects", requested_url=requested.url)
    finally:
        guard.following_redirects = False

    if response is None:
        return _failed("no_document", requested_url=requested.url)
    if response.status >= 400:
        return _failed("page_http_error", requested_url=requested.url, http_status=response.status)
    settled = await _settle(tab.page, tab)
    if guard.main_frame_block is not None or guard.pending_redirect is not None:
        return _failed("navigation_blocked", requested_url=requested.url)
    return await _page_observation(
        context=context,
        session=session,
        tab=tab,
        sequence=payload.sequence,
        operation=ResearchOperation.NAVIGATE,
        requested_url=requested.url,
        redirects=redirects,
        settled=settled,
    )


async def research_observe(context: OperationContext, payload: ObserveInput) -> OperationResult:
    """Re-read a tab. The authoritative answer to "where am I now".

    This is the operation a task uses after any uncertainty -- a lost reply, a
    restart, an unexpected navigation -- because it asks the browser rather
    than assuming the last observation still holds.
    """
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
    except SessionError as error:
        return _failed(error.code)
    settled = await _settle(tab.page, tab)
    return await _page_observation(
        context=context,
        session=session,
        tab=tab,
        sequence=payload.sequence,
        operation=ResearchOperation.OBSERVE,
        requested_url=None,
        redirects=[],
        settled=settled,
    )


async def research_scroll(context: OperationContext, payload: ScrollInput) -> OperationResult:
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
    except SessionError as error:
        return _failed(error.code)
    session.activate(tab.ref)
    try:
        await tab.page.keyboard.press(SCROLL_KEYS[payload.direction])
    except PlaywrightError:
        return _failed("scroll_failed")
    settled = await _settle(tab.page, tab)
    if session.guard.main_frame_block is not None:
        return _failed("navigation_blocked")
    return await _page_observation(
        context=context,
        session=session,
        tab=tab,
        sequence=payload.sequence,
        operation=ResearchOperation.SCROLL,
        requested_url=None,
        redirects=[],
        settled=settled,
    )


async def research_history(context: OperationContext, payload: HistoryInput) -> OperationResult:
    try:
        session = _session(context)
        tab = session.tab(payload.tab)
    except SessionError as error:
        return _failed(error.code)
    session.activate(tab.ref)
    session.guard.main_frame_block = None
    try:
        if payload.direction == "back":
            await tab.page.go_back(wait_until="domcontentloaded")
        else:
            await tab.page.go_forward(wait_until="domcontentloaded")
    except PlaywrightError:
        if session.guard.main_frame_block is not None:
            return _failed(session.guard.main_frame_block)
        return _failed("history_unavailable")
    settled = await _settle(tab.page, tab)
    if session.guard.main_frame_block is not None or session.guard.pending_redirect is not None:
        return _failed("navigation_blocked")
    return await _page_observation(
        context=context,
        session=session,
        tab=tab,
        sequence=payload.sequence,
        operation=ResearchOperation.HISTORY,
        requested_url=None,
        redirects=[],
        settled=settled,
    )


async def research_tab(context: OperationContext, payload: TabInput) -> OperationResult:
    try:
        session = _session(context)
        if payload.action == "open":
            tab: ResearchTab | None = await session.open_tab()
        elif payload.action == "activate":
            assert payload.tab is not None  # The model validator required it.
            tab = session.activate(payload.tab)
        else:
            assert payload.tab is not None
            await session.close_tab(payload.tab)
            tab = session.tabs.get(session.active) if session.active else None
    except SessionError as error:
        return _failed(error.code)
    if payload.action == "activate" and tab is not None:
        # Activating is also the cheapest way to re-read where that tab is.
        return await _page_observation(
            context=context,
            session=session,
            tab=tab,
            sequence=payload.sequence,
            operation=ResearchOperation.TAB,
            requested_url=None,
            redirects=[],
            settled=True,
        )
    return await _tab_state_observation(
        context=context,
        session=session,
        tab=tab,
        sequence=payload.sequence,
        operation=ResearchOperation.TAB,
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
        output_model=ResearchObservation,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.OBSERVE_THEN_REPLAN,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=60.0,
        timeout_meaning=(
            "No observation was produced. The browser may have moved, so the step is never "
            "repeated blindly: the task re-observes the tab and the planner decides again."
        ),
        preconditions=(
            "an ACTIVE task grant authorised this operation and a single-use step "
            "authorization was consumed by a persisted execution attempt",
            "the session and tab exist in this worker generation",
        ),
        postconditions=postconditions,
        handler=handler,
        target=OperationTarget.RESEARCH_SESSION,
    )


_COMMON_POSTCONDITIONS = (
    "every redirect hop and the final document passed the destination policy",
    "no request other than GET or HEAD left the browser context",
    "a bounded observation whose content hash matches its text and links was produced",
)

OPERATIONS: tuple[BrowserOperation, ...] = (
    _operation(
        NAVIGATE,
        "Open one address the controller resolved from an observed search result, an observed "
        "link, or an address the user themselves typed. GET only; no clicks, typing, form "
        "submissions, uploads, downloads or non-GET requests. READ_ONLY means Lumi performs no "
        "intentional mutation; a GET can still have incidental effects on the remote server.",
        NavigateInput,
        research_navigate,
        (
            "the target ref resolved against the tab's current document epoch",
            *_COMMON_POSTCONDITIONS,
        ),
    ),
    _operation(
        OBSERVE,
        "Re-read one task-owned tab: bounded visible text, bounded semantic links, and where "
        "the tab actually is now. Issues no request of its own.",
        ObserveInput,
        research_observe,
        _COMMON_POSTCONDITIONS,
    ),
    _operation(
        SCROLL,
        "Send one Page Down or Page Up key press to a task-owned tab and read it again. No "
        "JavaScript, no coordinates, no other key.",
        ScrollInput,
        research_scroll,
        _COMMON_POSTCONDITIONS,
    ),
    _operation(
        HISTORY,
        "Move one task-owned tab back or forward in its own history and read it again.",
        HistoryInput,
        research_history,
        _COMMON_POSTCONDITIONS,
    ),
    _operation(
        TAB,
        "Open, activate or close a task-owned research tab, within the task's tab budget. "
        "Never touches a tab this session did not create.",
        TabInput,
        research_tab,
        ("only tabs this session created were opened, activated or closed",),
    ),
)

__all__ = [
    "HISTORY",
    "NAVIGATE",
    "OBSERVE",
    "OPERATIONS",
    "SCROLL",
    "TAB",
    "HistoryInput",
    "NavigateInput",
    "ObserveInput",
    "ScrollInput",
    "TabInput",
    "split_blocks",
]
