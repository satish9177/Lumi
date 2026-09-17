"""`inspect_public_page`: open one approved public page and read it.

This is a *generic* operation, not a site adapter, and its whole input is one
URL. There is no selector, script, header, cookie, browser flag, click target
or wait condition anywhere in its schema, so a model -- or a page -- has
nothing to describe beyond "this address", and the address itself was fixed
when the user approved it.

Internally it composes a fixed sequence:

    check destination -> navigate (following redirects one validated hop at a
    time) -> wait for the visible text to settle -> check the final destination
    -> read bounded visible text and links -> build a hashed observation

Reading uses Playwright locators only; there is no page-scripting primitive in
this module. Page text is data. Nothing read here can change which operation
runs, where the browser goes next, or what Lumi is authorised to do: the only
thing returned is the observation, and the only consumer of that is the answer
step, which treats it as untrusted evidence.
"""

import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urldefrag, urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Page, Request
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
from app.domain.page_observation import (
    INSPECT_PUBLIC_PAGE,
    MAX_BLOCK_CHARS,
    MAX_BLOCKS,
    MAX_LINK_TEXT_CHARS,
    MAX_LINKS,
    MAX_REDIRECTS,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    MAX_URL_CHARS,
    PageLink,
    PageObservation,
    TextBlock,
    compute_content_hash,
)
from app.domain.public_url import UrlPolicyError

#: How long the visible text may keep changing before Lumi reads it anyway.
SETTLE_SECONDS = 8.0
SETTLE_POLL_MS = 300
#: Text unchanged for this many consecutive polls (about 1.5 s), with no page
#: script or API request still in flight, counts as settled.
SETTLED_AFTER_EQUAL_READS = 5
#: Requests whose completion can still change what the page shows.
_ACTIVITY_TYPES = frozenset({"document", "script", "xhr", "fetch"})
LOAD_WAIT_MS = 10_000
READ_TIMEOUT_MS = 5_000
LINK_CANDIDATES = 200
#: Read-once cap on raw text before splitting, so a huge page cannot exhaust memory.
MAX_RAW_TEXT_CHARS = 400_000


class InspectPageInput(BaseModel):
    """The whole vocabulary: one URL. Checked again against this worker's policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=MAX_URL_CHARS)


def _failed(code: str, **observation: Any) -> OperationResult:
    return OperationResult(
        status=OperationStatus.FAILED_BEFORE_EFFECT, observation=observation, error_code=code
    )


def _clean(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())


def split_blocks(raw: str) -> tuple[list[TextBlock], bool, int]:
    """Visible text -> numbered blocks within budget. Returns (blocks, truncated, total)."""
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


async def _links(page: Page, base_url: str) -> tuple[list[PageLink], int]:
    anchors = page.locator("a[href]")
    count = min(await anchors.count(), LINK_CANDIDATES)
    seen: set[str] = set()
    links: list[PageLink] = []
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
        seen.add(absolute)
        if len(links) < MAX_LINKS:
            links.append(
                PageLink(id=f"l{len(links) + 1}", text=label[:MAX_LINK_TEXT_CHARS], url=absolute)
            )
    return links, len(seen)


class _PageActivity:
    """Main-frame document commits (the document epoch) and requests in flight."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.count = 0
        self.in_flight: set[Request] = set()
        page.on("framenavigated", self._on_navigated)
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def _on_navigated(self, frame: Frame) -> None:
        if frame == self._page.main_frame:
            self.count += 1

    def _started(self, request: Request) -> None:
        if request.resource_type in _ACTIVITY_TYPES:
            self.in_flight.add(request)

    def _ended(self, request: Request) -> None:
        self.in_flight.discard(request)


async def _settle(page: Page, epochs: _PageActivity) -> bool:
    try:
        await page.wait_for_load_state("load", timeout=LOAD_WAIT_MS)
    except PlaywrightError:
        pass  # Slow subresources do not prevent reading what is visible.
    deadline = time.monotonic() + SETTLE_SECONDS
    previous: tuple[int, str] | None = None
    equal = 0
    while time.monotonic() < deadline:
        try:
            current = (epochs.count, await _visible_text(page))
        except PlaywrightError:
            current = (epochs.count, "\x00unreadable")
        if current == previous and not epochs.in_flight:
            equal += 1
            if equal >= SETTLED_AFTER_EQUAL_READS:
                return True
        else:
            equal = 0
        previous = current
        await page.wait_for_timeout(SETTLE_POLL_MS)
    return False


async def inspect_public_page(context: OperationContext, payload: InspectPageInput) -> OperationResult:
    policy, guard, page = context.public_policy, context.network_guard, context.page
    if policy is None or guard is None or not policy.configured:
        return _failed("public_inspection_not_configured")
    try:
        requested = policy.check(payload.url)
    except UrlPolicyError as error:
        return _failed(error.code)

    epochs = _PageActivity(page)
    redirects: list[str] = []
    url = requested.url
    guard.following_redirects = True
    response = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            guard.pending_redirect = None
            guard.main_frame_block = None
            # Epochs count documents of the page being read, not redirect hops.
            epochs.count = 0
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightError:
                if guard.main_frame_block is not None:
                    return _failed(guard.main_frame_block, requested_url=requested.url)
                raise
            target = guard.pending_redirect
            if target is not None:
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
                continue
            break
        else:  # pragma: no cover - the loop always breaks or returns first.
            return _failed("too_many_redirects", requested_url=requested.url)
    finally:
        guard.following_redirects = False

    if response is None:
        return _failed("no_document", requested_url=requested.url)
    if response.status >= 400:
        return _failed("page_http_error", requested_url=requested.url, http_status=response.status)

    settled = await _settle(page, epochs)
    if guard.main_frame_block is not None or guard.pending_redirect is not None:
        # The page tried to move itself somewhere Lumi did not approve.
        return _failed("navigation_blocked", requested_url=requested.url)

    for _ in range(2):
        epoch_before = epochs.count
        final_url, _fragment = urldefrag(page.url)
        try:
            final = policy.check(final_url)
        except UrlPolicyError as error:
            return _failed(
                "final_destination_not_allowed", requested_url=requested.url, refusal=error.code
            )
        raw_text = await _visible_text(page)
        title = _clean(await page.title())[:MAX_TITLE_CHARS]
        links, total_links = await _links(page, final.url)
        if epochs.count == epoch_before:
            break
    else:
        return _failed("document_unstable", requested_url=requested.url)

    blocks, truncated, total_text = split_blocks(raw_text)
    try:
        observation = PageObservation(
            observation_id=context.observation_id,
            requested_url=requested.url,
            final_url=final.url,
            redirects=redirects,
            title=title,
            document_epoch=max(1, epochs.count),
            settled=settled,
            observed_at=datetime.now(UTC),
            blocks=blocks,
            links=links,
            truncated=truncated or total_links > len(links),
            total_text_chars=total_text,
            total_link_count=total_links,
            content_hash=compute_content_hash(
                final_url=final.url, title=title, blocks=blocks, links=links
            ),
        )
    except ValidationError:
        return _failed("observation_invalid", requested_url=requested.url)
    return OperationResult(status=OperationStatus.OK, observation=observation.model_dump(mode="json"))


OPERATIONS: tuple[BrowserOperation, ...] = (
    BrowserOperation(
        name=INSPECT_PUBLIC_PAGE,
        description=(
            "Open one approved public page in a fresh isolated context and read bounded "
            "visible text and links. No clicking, typing, sign-in, downloads or uploads."
        ),
        input_model=InspectPageInput,
        output_model=PageObservation,
        effect=Effect.READ_ONLY,
        retry=RetryPolicy.NEW_APPROVAL_REQUIRED,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=45.0,
        timeout_meaning=(
            "No observation was produced. The public page may have been requested; nothing "
            "consequential can follow from that. Never repeated automatically: a repeat is a "
            "new action with a new exact approval."
        ),
        preconditions=(
            "an approved inspect_public_page action was claimed by an execution attempt",
            "the URL passes this worker's own destination policy and resolves only to public addresses",
        ),
        postconditions=(
            "every redirect hop and the final document passed the destination policy",
            "a bounded observation whose content hash matches its text and links was produced",
        ),
        handler=inspect_public_page,
        target=OperationTarget.PUBLIC_PAGE,
    ),
)

__all__ = ["OPERATIONS", "InspectPageInput", "inspect_public_page", "split_blocks"]
