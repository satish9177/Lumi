"""The task-owned public browser session, inside the isolated worker.

Milestone 7a created a fresh context per dispatch and threw it away. Research
needs one context that survives many steps -- otherwise "follow that link" has
no page to follow it from -- so this module owns exactly that, and nothing more:

* **One context per research task**, created with no storage state, no imported
  profile, service workers blocked, downloads refused and no permissions. It is
  unauthenticated because there is no code path that could make it anything
  else: nothing here reads, writes, copies or is handed a cookie jar, a
  `storageState`, a credential or a user profile directory.
* **Tabs with logical refs** (`t1`..`t5`), allocated by the worker. A ref is a
  slot in this session, not a native handle, and it dies with the session.
* **A document epoch per tab**, incremented on every main-frame commit. This is
  what makes a semantic ref stale: `l5` was issued for tab `t1` at epoch 4, and
  at epoch 5 the worker no longer has a table for it.
* **A ref table per (tab, epoch)**, mapping `l<n>` to the address the page
  offered. The model never receives these addresses; the controller resolves a
  ref against its own persisted copy, and the worker independently resolves it
  against this one. Both must agree or the step is refused.

The session lives only in this process's memory. That is deliberate and it is
the recovery story: a worker restart takes every context, tab and ref table
with it, the runtime's `research_sessions` row becomes STALE, and the task must
re-observe authoritative state before it may continue. There is no path by
which a ref issued by a dead worker resolves in a new one.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Frame, Page

from app.browser.network_guard import PublicNetworkGuard
from app.domain.public_url import PublicUrlPolicy
from app.domain.research import MAX_TAB_SLOTS

logger = logging.getLogger("lumi.browser.research")

TAB_REFS: tuple[str, ...] = tuple(f"t{index}" for index in range(1, MAX_TAB_SLOTS + 1))
#: How many past document epochs keep their ref table. Two is enough for
#: "observe, then act on what was observed" while bounding memory; anything
#: older is stale by construction.
RETAINED_EPOCHS = 2


class SessionError(Exception):
    """A refused session operation. `code` is stable and safe to report."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The research session refused the step ({code}).")
        self.code = code


class DocumentEpochs:
    """Main-frame document commits for one tab, and requests still in flight."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.epoch = 1
        self.in_flight: set[object] = set()
        page.on("framenavigated", self._navigated)
        page.on("request", self._started)
        page.on("requestfinished", self._ended)
        page.on("requestfailed", self._ended)

    def _navigated(self, frame: Frame) -> None:
        try:
            if frame == self._page.main_frame:
                self.epoch += 1
        except PlaywrightError:  # pragma: no cover - the page is going away.
            pass

    def _started(self, request: object) -> None:
        resource = getattr(request, "resource_type", "")
        if resource in ("document", "script", "xhr", "fetch"):
            self.in_flight.add(request)

    def _ended(self, request: object) -> None:
        self.in_flight.discard(request)


@dataclass(slots=True)
class ResearchTab:
    ref: str
    page: Page
    epochs: DocumentEpochs
    #: document epoch -> {link ref: address}. Only this table resolves a ref.
    link_tables: dict[int, dict[str, str]] = field(default_factory=dict)

    @property
    def epoch(self) -> int:
        return self.epochs.epoch

    def record_links(self, links: dict[str, str]) -> None:
        self.link_tables[self.epoch] = links
        for epoch in sorted(self.link_tables)[:-RETAINED_EPOCHS]:
            del self.link_tables[epoch]

    def resolve_link(self, *, epoch: int, ref: str) -> str:
        """The address `ref` meant, for the document that issued it.

        Refuses when the tab has moved on (`stale_document_epoch`) or when the
        ref was never issued for that document (`unknown_target_ref`).
        """
        if epoch != self.epoch:
            raise SessionError("stale_document_epoch")
        table = self.link_tables.get(epoch)
        if table is None:
            raise SessionError("stale_document_epoch")
        address = table.get(ref)
        if address is None:
            raise SessionError("unknown_target_ref")
        return address


class ResearchBrowserSession:
    """One public, unauthenticated context and its task-owned tabs."""

    def __init__(
        self,
        *,
        session_id: uuid.UUID,
        context: BrowserContext,
        guard: PublicNetworkGuard,
        policy: PublicUrlPolicy,
        max_tabs: int = MAX_TAB_SLOTS,
    ) -> None:
        self.id = session_id
        self.context = context
        self.guard = guard
        self.policy = policy
        self.max_tabs = min(max_tabs, MAX_TAB_SLOTS)
        self.created_at = time.monotonic()
        self.tabs: dict[str, ResearchTab] = {}
        self.active: str | None = None

    @classmethod
    async def open(
        cls,
        *,
        browser: Browser,
        session_id: uuid.UUID,
        policy: PublicUrlPolicy,
        max_tabs: int = MAX_TAB_SLOTS,
    ) -> "ResearchBrowserSession":
        context = await browser.new_context(
            service_workers="block", accept_downloads=False, permissions=[]
        )
        guard = PublicNetworkGuard(policy)
        await guard.install(context)
        session = cls(
            session_id=session_id,
            context=context,
            guard=guard,
            policy=policy,
            max_tabs=max_tabs,
        )
        await session.open_tab()
        return session

    async def open_tab(self) -> ResearchTab:
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
        tab = ResearchTab(ref=ref, page=page, epochs=DocumentEpochs(page))
        self.tabs[ref] = tab
        self.active = ref
        return tab

    def tab(self, ref: str) -> ResearchTab:
        tab = self.tabs.get(ref)
        if tab is None:
            raise SessionError("unknown_tab")
        return tab

    def activate(self, ref: str) -> ResearchTab:
        tab = self.tab(ref)
        self.active = ref
        return tab

    async def close_tab(self, ref: str) -> None:
        tab = self.tab(ref)
        if len(self.tabs) == 1:
            # Closing the last tab would leave the session with nothing to
            # observe, which is indistinguishable from a broken session.
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

    async def close(self) -> None:
        try:
            await self.context.close()
        except PlaywrightError:  # pragma: no cover - the browser may be gone.
            pass
        self.tabs.clear()
        self.active = None


class SessionStore:
    """The worker's live research sessions, by the id the runtime persisted.

    In-memory on purpose: see the module docstring. A session id the runtime
    believes in but this process has never seen is an error, never an implicit
    "open a fresh one" -- that would silently hand a task a browser with no
    history, and every ref it holds would resolve against the wrong document.
    """

    def __init__(self, *, limit: int = 1) -> None:
        self._sessions: dict[uuid.UUID, ResearchBrowserSession] = {}
        self._limit = limit

    def get(self, session_id: uuid.UUID) -> ResearchBrowserSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("unknown_session")
        return session

    async def open(
        self,
        *,
        browser: Browser,
        session_id: uuid.UUID,
        policy: PublicUrlPolicy,
        max_tabs: int = MAX_TAB_SLOTS,
    ) -> ResearchBrowserSession:
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        if len(self._sessions) >= self._limit:
            raise SessionError("session_limit")
        session = await ResearchBrowserSession.open(
            browser=browser, session_id=session_id, policy=policy, max_tabs=max_tabs
        )
        self._sessions[session_id] = session
        logger.info("research session opened", extra={"session_id": str(session_id)})
        return session

    async def close(self, session_id: uuid.UUID) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        logger.info("research session closed", extra={"session_id": str(session_id)})
        return True

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.close(session_id)

    def __len__(self) -> int:
        return len(self._sessions)


__all__ = [
    "RETAINED_EPOCHS",
    "TAB_REFS",
    "DocumentEpochs",
    "ResearchBrowserSession",
    "ResearchTab",
    "SessionError",
    "SessionStore",
]
