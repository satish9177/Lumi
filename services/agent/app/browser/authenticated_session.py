"""The read session of one open persistent profile, inside the isolated worker.

An authenticated read runs in the browser context S1 already opens for a
profile -- the one that carries the user's session -- and this module adds the
bookkeeping a read needs on top of it, and nothing else:

* **Tabs with logical refs** (`t1`..`t3`), allocated by the worker. The
  profile's own first tab is `t1`. A ref is a slot in this session, not a
  native handle, and dies with it.
* **A document epoch per tab**, incremented on every main-frame commit, so a
  ref issued for one document is stale the moment the tab shows another.
* **A target table per (tab, epoch)** that lives **only here**. `l5` maps to the
  address the page offered; `b3` maps to the raw text of the block. Neither
  table is ever returned, persisted or logged: the runtime and the model hold
  refs, redacted labels and hosts. A private link can itself be a capability,
  so its address stays in this process's memory and dies with the epoch.

It is deliberately a different class from `ResearchBrowserSession`. A research
session id can never stand in for one of these, and a profile can never stand
in for a research session: the dispatch that reaches this module named a
profile, and the worker refused every other kind of id before getting here.
"""

import logging
import uuid
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from app.browser.account_read_guard import AccountReadNetworkGuard
from app.browser.research_session import RETAINED_EPOCHS, DocumentEpochs, SessionError
from app.domain.authenticated import MAX_AUTH_TABS

logger = logging.getLogger("lumi.browser.authenticated")

TAB_REFS: tuple[str, ...] = tuple(f"t{index}" for index in range(1, MAX_AUTH_TABS + 1))


@dataclass(frozen=True, slots=True)
class LinkEntry:
    """A link the page offered. Held only in the worker, only for one epoch."""

    href: str
    index: int


@dataclass(slots=True)
class AuthenticatedTab:
    ref: str
    page: Page
    epochs: DocumentEpochs
    #: document epoch -> {link ref: entry}
    links: dict[int, dict[str, LinkEntry]] = field(default_factory=dict)
    #: document epoch -> {block ref: the block's raw text}
    blocks: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def epoch(self) -> int:
        return self.epochs.epoch

    def record(self, *, links: dict[str, LinkEntry], blocks: dict[str, str]) -> None:
        self.links[self.epoch] = links
        self.blocks[self.epoch] = blocks
        for table in (self.links, self.blocks):
            for epoch in sorted(table)[:-RETAINED_EPOCHS]:
                del table[epoch]

    def resolve_link(self, *, epoch: int, ref: str) -> LinkEntry:
        """The entry `ref` meant, for the document that issued it. No guessing."""
        if epoch != self.epoch:
            raise SessionError("stale_document_epoch")
        table = self.links.get(epoch)
        if table is None:
            raise SessionError("stale_document_epoch")
        entry = table.get(ref)
        if entry is None:
            raise SessionError("unknown_target_ref")
        return entry

    def resolve_block(self, *, epoch: int, ref: str) -> str:
        if epoch != self.epoch:
            raise SessionError("stale_document_epoch")
        table = self.blocks.get(epoch)
        if table is None:
            raise SessionError("stale_document_epoch")
        raw = table.get(ref)
        if raw is None:
            raise SessionError("unknown_target_ref")
        return raw


class AuthenticatedReadSession:
    """One profile's read context, its guard and its task-owned tabs."""

    def __init__(
        self,
        *,
        profile_id: uuid.UUID,
        site: str,
        context: BrowserContext,
        guard: AccountReadNetworkGuard,
        max_tabs: int = MAX_AUTH_TABS,
    ) -> None:
        self.profile_id = profile_id
        self.site = site
        self.context = context
        self.guard = guard
        self.max_tabs = min(max_tabs, MAX_AUTH_TABS)
        self.tabs: dict[str, AuthenticatedTab] = {}
        self.active: str | None = None

    @classmethod
    async def open(
        cls,
        *,
        profile_id: uuid.UUID,
        site: str,
        context: BrowserContext,
        test_origins: frozenset[str] = frozenset(),
        max_tabs: int = MAX_AUTH_TABS,
    ) -> "AuthenticatedReadSession":
        guard = AccountReadNetworkGuard(site=site, test_origins=test_origins)
        await guard.install(context)
        session = cls(
            profile_id=profile_id, site=site, context=context, guard=guard, max_tabs=max_tabs
        )
        pages = context.pages
        if pages:
            # The profile's own first tab is `t1`; any further tab the browser
            # restored is closed rather than adopted.
            first, *extra = pages
            for page in extra:
                try:
                    await page.close()
                except PlaywrightError:  # pragma: no cover - already gone.
                    pass
            session._adopt(first)
        else:
            await session.open_tab()
        return session

    def _adopt(self, page: Page) -> AuthenticatedTab:
        ref = TAB_REFS[0]
        self.guard.track(page)
        tab = AuthenticatedTab(ref=ref, page=page, epochs=DocumentEpochs(page))
        self.tabs[ref] = tab
        self.active = ref
        return tab

    async def open_tab(self) -> AuthenticatedTab:
        if len(self.tabs) >= self.max_tabs:
            raise SessionError("tab_budget_exhausted")
        ref = next((candidate for candidate in TAB_REFS if candidate not in self.tabs), None)
        if ref is None:  # pragma: no cover - the budget check already refused.
            raise SessionError("tab_budget_exhausted")
        self.guard.expect_page()
        try:
            page = await self.context.new_page()
            self.guard.track(page)
        finally:
            self.guard.stop_expecting()
        tab = AuthenticatedTab(ref=ref, page=page, epochs=DocumentEpochs(page))
        self.tabs[ref] = tab
        self.active = ref
        return tab

    def tab(self, ref: str) -> AuthenticatedTab:
        tab = self.tabs.get(ref)
        if tab is None:
            raise SessionError("unknown_tab")
        return tab

    def activate(self, ref: str) -> AuthenticatedTab:
        tab = self.tab(ref)
        self.active = ref
        return tab

    async def close_tab(self, ref: str) -> None:
        tab = self.tab(ref)
        if len(self.tabs) == 1:
            raise SessionError("last_tab")
        self.guard.untrack(tab.page)
        del self.tabs[ref]
        try:
            await tab.page.close()
        except PlaywrightError:  # pragma: no cover - already gone.
            pass
        if self.active == ref:
            self.active = next(iter(self.tabs), None)

    @property
    def open_tabs(self) -> list[str]:
        return [ref for ref in TAB_REFS if ref in self.tabs]


__all__ = [
    "TAB_REFS",
    "AuthenticatedReadSession",
    "AuthenticatedTab",
    "LinkEntry",
]
