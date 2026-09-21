"""The persistent, Lumi-managed Chromium profile, inside the isolated worker.

This is the one place in Lumi that opens a browser with a `user_data_dir`. What
that buys, and what it deliberately costs:

* **Cookies, `localStorage`, `sessionStorage`, IndexedDB and site settings
  persist exactly as a browser's do** -- because they are a browser's. Chromium
  writes them, Chromium reads them, and no Lumi code path touches them. The
  alternative, a persisted Playwright `storage_state`, would require Lumi to
  pull session cookies into the worker's memory and then write them to a file
  of its own: a portable, replayable impersonation token that did not exist
  before. It is not implemented here and there is a source-level test
  (`tests/test_no_credential_extraction.py`) that fails if anybody adds it.
* **One context per profile, one profile per registrable domain.** The context
  is the profile; `launch_persistent_context` returns a `BrowserContext` whose
  browser process is dedicated to that directory.

Every persistent context is launched **through the S0 egress broker**, with the
same `proxy`, `--proxy-bypass-list=<-loopback>`, credential and QUIC-disabling
arguments the managed research browser uses (`managed_launch_options`). There
is no code path that launches an unbrokered persistent context, and no
loopback exemption: a fixture origin reaches the broker like everything else.

Its context options are the narrowest Playwright offers, and they are the
options S1 intends to keep:

| Option | Why |
|---|---|
| `service_workers="block"` | A service worker's fetches sit outside request routing, and Background Sync can queue a write for later. Kept blocked, exactly as M7a/M7b keep it. |
| `accept_downloads=False` | S1 is profile infrastructure, not a file manager. |
| `permissions=[]` | No geolocation, notifications, camera, microphone or clipboard. Nothing is granted and nothing asks. |
| `--hide-crash-restore-bubble`, `--disable-session-crashed-bubble` | After an unclean shutdown the profile opens with no tabs and no "Restore pages?" prompt. Session restore is never enabled, so Chromium has nothing to restore; these only remove the visible artefact a headed S2 window would otherwise show. |

**Version guard.** Chromium upgrades a profile directory in place and never
downgrades it. Before the directory is opened, the build recorded on the
profile row is compared with this worker's build. Newer recorded build ->
refused with `profile_browser_downgrade_refused`, **before any file is
touched**, and nothing is deleted. The user can create a new profile and sign
in again; Lumi will not gamble with a directory somebody signed into.
"""

import logging
import uuid
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
)

from app.browser import site_scope
from app.browser.authenticated_session import AuthenticatedReadSession
from app.browser.credential_signals import account_fingerprint, detect_credential_surface
from app.browser.egress_broker import EgressBroker, managed_launch_options
from app.browser.profile_lock import ProfileLock, ProfileLockUnavailableError
from app.browser.profile_paths import ProfilePaths
from app.browser.takeover_guard import TakeoverNetworkGuard
from app.domain.browser_profile import BrowserVersions
from app.domain.public_suffix import PublicSuffixError, registrable_domain
from app.domain.login_takeover import TakeoverSiteScope
from app.domain.research import MAX_REDIRECTS

logger = logging.getLogger("lumi.browser.profiles")

#: Arguments added on top of `MANAGED_CHROMIUM_ARGS` for a persistent profile.
#: Both concern what the browser shows after an unclean shutdown; neither
#: enables anything.
PERSISTENT_PROFILE_ARGS: tuple[str, ...] = (
    "--hide-crash-restore-bubble",
    "--disable-session-crashed-bubble",
    # First-run promos and default-browser prompts have no place in a profile
    # Lumi drives, headless or headed.
    "--no-first-run",
    "--no-default-browser-check",
)


class ProfileSessionError(Exception):
    """A refused profile-session operation. `code` is stable and safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The profile session was refused ({code}).")
        self.code = code


def persistent_launch_options(
    broker: EgressBroker, *, headless: bool
) -> dict[str, object]:
    """Exactly how a persistent profile is launched. Asserted by a test.

    It is `managed_launch_options` -- the broker proxy, its credential, the
    loopback bypass and the QUIC/background-networking arguments -- plus the
    persistent-profile arguments and the closed context options. Building it
    from the S0 function rather than beside it is what keeps a future change to
    the broker's launch contract from silently missing this path.
    """
    managed = managed_launch_options(broker, headless=headless)
    args = [*(managed.get("args") or []), *PERSISTENT_PROFILE_ARGS]  # type: ignore[misc]
    return {
        **managed,
        "args": args,
        "service_workers": "block",
        "accept_downloads": False,
        "permissions": [],
    }


@dataclass(slots=True)
class PersistentProfileSession:
    """One open persistent profile: its context, and the handle that guards it."""

    profile_id: uuid.UUID
    context: BrowserContext
    lock: ProfileLock
    versions: BrowserVersions
    #: Whether this open used a visible window. Milestone 8a S2.
    headless: bool = True
    #: Set only while a takeover is open for this profile (Milestone 8a S2).
    #: The single tracked tab a takeover navigated, and the guard installed
    #: on the context for the duration. Neither is ever read for its text.
    takeover_page: Page | None = field(default=None, repr=False)
    takeover_guard: TakeoverNetworkGuard | None = field(default=None, repr=False)
    #: Milestone 8a S3. The agent's read session (its guard, tabs and target
    #: tables) on this profile's context, created lazily by the first
    #: authenticated dispatch. Mutually exclusive with a takeover: a takeover is
    #: headed and human-driven, a read is headless and Lumi-driven, and the two
    #: are never opened on one context.
    read_session: AuthenticatedReadSession | None = field(default=None, repr=False)

    async def close(self) -> None:
        try:
            await self.context.close()
        except PlaywrightError:  # pragma: no cover - the browser may be gone.
            pass
        finally:
            # The lock is released last and unconditionally: a context that
            # failed to close cleanly must not leave the profile locked.
            self.lock.release()


@dataclass(frozen=True, slots=True)
class TakeoverCheckResult:
    """The whole metadata surface a completed takeover may report.

    Deliberately this small: a closed scope enum (never a host string), a
    credential-surface verdict and its signal names, and an account
    fingerprint hash or `None`. Nothing else -- no text, no title, no URL.
    """

    scope: "TakeoverSiteScope"
    credential_surface: bool
    signals: list[str]
    account_fingerprint: str | None


def _site_scope(page: Page | None, site: str) -> "TakeoverSiteScope":
    """The closed enum for where a takeover ended. Never returns a host."""
    if page is None or page.is_closed():
        return TakeoverSiteScope.NO_PAGE
    try:
        host = urlsplit(page.url).hostname
    except (PlaywrightError, ValueError):  # pragma: no cover - defensive.
        return TakeoverSiteScope.NO_PAGE
    if not host:
        return TakeoverSiteScope.NO_PAGE
    try:
        domain = registrable_domain(host)
    except PublicSuffixError:
        return TakeoverSiteScope.OTHER_PUBLIC_SITE
    return (
        TakeoverSiteScope.IN_PROFILE_SITE
        if domain == site
        else TakeoverSiteScope.OTHER_PUBLIC_SITE
    )


def worker_versions(browser: Browser, *, playwright_version: str, app_version: str) -> BrowserVersions:
    """What this worker would write onto a profile it opened."""
    return BrowserVersions(
        chromium_build=browser.version,
        playwright_version=playwright_version,
        app_version=app_version,
    )


class ProfileSessionStore:
    """The persistent profiles this worker process currently has open.

    In memory, like `SessionStore`, and for the same reason: a worker restart
    takes every open context with it, so a profile the runtime believes is open
    in a dead worker is simply not open. The runtime re-derives that from the
    worker generation rather than from anything stored here.

    `limit` is 1 in S1. One authenticated profile at a time is all the slice
    needs, and it keeps the "who owns this browser" question trivially
    answerable while the lease design is new.
    """

    def __init__(self, *, limit: int = 1) -> None:
        self._sessions: dict[uuid.UUID, PersistentProfileSession] = {}
        self._limit = limit
        #: Milestone 8b S6. The in-site page a read session was on when form
        #: preparation began, captured *inside the worker* so the address never
        #: crosses any boundary. It outlives the headless context that held it (the
        #: profile is reopened headed) and dies with this worker process.
        self._destinations: dict[uuid.UUID, str] = {}

    def get(self, profile_id: uuid.UUID) -> PersistentProfileSession:
        session = self._sessions.get(profile_id)
        if session is None:
            raise ProfileSessionError("unknown_profile_session")
        return session

    def is_open(self, profile_id: uuid.UUID) -> bool:
        return profile_id in self._sessions

    async def open(
        self,
        *,
        playwright: Playwright,
        broker: EgressBroker,
        paths: ProfilePaths,
        profile_id: uuid.UUID,
        recorded: BrowserVersions | None,
        current: BrowserVersions,
        headless: bool,
        timeout_seconds: float,
    ) -> PersistentProfileSession:
        """Open (or return) the persistent context for one profile.

        Order matters and is the security-relevant part:

        1. **Version guard first**, so a refused downgrade never touches disk.
        2. **Directory created and its ACL restricted**, before anything writes.
        3. **Exclusive OS handle taken**, so a second owner -- including a second
           Lumi installation with its own database -- is refused rather than
           corrupting the directory.
        4. **Only then** is Chromium launched, through the broker.

        A failure at any step releases what the earlier steps took.
        """
        existing = self._sessions.get(profile_id)
        if existing is not None:
            if existing.headless != headless:
                # A caller that asked for a visible window must never be
                # handed back a headless one it cannot see, or vice versa.
                raise ProfileSessionError("profile_open_mode_mismatch")
            return existing
        if len(self._sessions) >= self._limit:
            raise ProfileSessionError("profile_session_limit")
        if current.is_downgrade_from(recorded):
            # Never opened, never repaired, never deleted.
            raise ProfileSessionError("profile_browser_downgrade_refused")

        directory = paths.create(profile_id)
        lock = ProfileLock.for_profile(paths, profile_id)
        try:
            lock.acquire()
        except ProfileLockUnavailableError:
            raise ProfileSessionError("profile_locked_by_another_process") from None
        except OSError:
            raise ProfileSessionError("profile_directory_unavailable") from None
        try:
            context = await playwright.chromium.launch_persistent_context(
                str(directory),
                **persistent_launch_options(broker, headless=headless),  # type: ignore[arg-type]
            )
        except BaseException:
            lock.release()
            raise
        context.set_default_timeout(timeout_seconds * 1_000)
        context.set_default_navigation_timeout(timeout_seconds * 1_000)
        session = PersistentProfileSession(
            profile_id=profile_id,
            context=context,
            lock=lock,
            versions=current,
            headless=headless,
        )
        self._sessions[profile_id] = session
        # The id and the codes, never the path: this record is a diagnostic and
        # diagnostics do not carry the location of a profile directory.
        logger.info(
            "persistent profile opened",
            extra={
                "profile_id": str(profile_id),
                "chromium_build": current.chromium_build,
                "headless": headless,
            },
        )
        return session

    async def close(self, profile_id: uuid.UUID, *, force: bool = False) -> bool:
        session = self._sessions.get(profile_id)
        if session is None:
            return False
        read = session.read_session
        if not force and read is not None and read.dirty and not read.handed_over:
            # Milestone 8b S6. Closing the context destroys a local draft. It is
            # never a side effect of ordinary read cleanup: only the reviewed
            # discard (which destroys the page while frozen) clears this.
            raise ProfileSessionError("form_is_dirty")
        self._sessions.pop(profile_id, None)
        await session.close()
        logger.info("persistent profile closed", extra={"profile_id": str(profile_id)})
        return True

    async def close_all(self) -> None:
        for profile_id in list(self._sessions):
            await self.close(profile_id, force=True)

    def __len__(self) -> int:
        return len(self._sessions)

    # -- authenticated reading (Milestone 8a S3) -----------------------------

    async def read_session(
        self,
        profile_id: uuid.UUID,
        *,
        site: str,
        test_origins: frozenset[str] = frozenset(),
    ) -> AuthenticatedReadSession:
        """The profile's read session, created on first use and pinned to `site`.

        Refused while a takeover holds the profile, and refused for a second
        site: a profile is bound to one, and a dispatch that names another is
        a confusion, not a request.
        """
        session = self._sessions.get(profile_id)
        if session is None:
            raise ProfileSessionError("unknown_profile_session")
        if session.takeover_guard is not None or session.takeover_page is not None:
            raise ProfileSessionError("profile_takeover_active")
        if session.read_session is None:
            session.read_session = await AuthenticatedReadSession.open(
                profile_id=profile_id,
                site=site,
                context=session.context,
                test_origins=test_origins,
            )
        elif session.read_session.site != site:
            raise ProfileSessionError("profile_site_mismatch")
        return session.read_session

    # -- form preparation mode (Milestone 8b S6) -----------------------------

    async def capture_preparation_destination(self, profile_id: uuid.UUID, *, site: str) -> str:
        """Remember the in-site page the read session is on, in worker memory only.

        Returns `CAPTURED`, `NO_DESTINATION` (nothing readable to return to) or
        `PROFILE_NOT_OPEN`. The address is not returned: it stays here and is used
        only by `restore_preparation`. A page that is not an in-site http(s)
        document, or that shows a credential surface, is not remembered.
        """
        session = self._sessions.get(profile_id)
        if session is None:
            return "PROFILE_NOT_OPEN"
        read = session.read_session
        if read is not None and (read.dirty or read.handed_over):
            raise ProfileSessionError("form_is_dirty")
        page = None
        if read is not None and read.active is not None and read.active in read.tabs:
            page = read.tabs[read.active].page
        if page is not None and not page.is_closed():
            url, _ = urldefrag(page.url)
            in_scope = url.startswith(("http://", "https://")) and site_scope.in_site(url, site)
            if in_scope and not await detect_credential_surface(page):
                self._destinations[profile_id] = url
                return "CAPTURED"
        return "CAPTURED" if profile_id in self._destinations else "NO_DESTINATION"

    async def restore_preparation(
        self,
        profile_id: uuid.UUID,
        *,
        site: str,
        test_origins: frozenset[str] = frozenset(),
        timeout_seconds: float = 30.0,
    ) -> str:
        """Navigate the headed profile back to the captured page, internally.

        The destination is worker-internal and was validated when it was captured
        and is validated again here: it must still be an in-site http(s) document,
        every redirect hop is judged by the read guard, and nothing outside the
        profile's site is contacted. On success the session is marked as being in
        preparation mode -- the only session a local draft may ever be made in.
        """
        session = self._sessions.get(profile_id)
        if session is None:
            return "PROFILE_NOT_OPEN"
        if session.headless:
            return "NOT_HEADED"
        url = self._destinations.get(profile_id)
        if url is None:
            return "NO_DESTINATION"
        read = await self.read_session(profile_id, site=site, test_origins=test_origins)
        if read.dirty or read.handed_over:
            raise ProfileSessionError("form_is_dirty")
        guard = read.guard
        if not guard.allows_top_level(url):
            return "LEFT_SITE_SCOPE"
        tab = read.tabs[read.active] if read.active in read.tabs else next(iter(read.tabs.values()))
        read.activate(tab.ref)
        guard.following_redirects = True
        try:
            for hop in range(MAX_REDIRECTS + 1):
                guard.pending_redirect = None
                guard.main_frame_block = None
                try:
                    await tab.page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_seconds * 1_000
                    )
                except PlaywrightError:
                    block = guard.main_frame_block
                    guard.main_frame_block = None
                    return "LEFT_SITE_SCOPE" if block == "left_site_scope" else "NAVIGATION_FAILED"
                block = guard.main_frame_block
                guard.main_frame_block = None
                if block is not None:
                    return "LEFT_SITE_SCOPE" if block == "left_site_scope" else "NAVIGATION_FAILED"
                target = guard.pending_redirect
                if target is None:
                    break
                if hop >= MAX_REDIRECTS:
                    return "NAVIGATION_FAILED"
                url = target
            else:  # pragma: no cover - the loop always breaks or returns first.
                return "NAVIGATION_FAILED"
        finally:
            guard.following_redirects = False
        try:
            await tab.page.wait_for_load_state("load", timeout=10_000)
        except PlaywrightError:
            pass
        read.preparation_mode = True
        return "RESTORED"

    # -- manual login takeover (Milestone 8a S2) -----------------------------

    async def start_takeover(
        self, profile_id: uuid.UUID, *, site: str, timeout_seconds: float, scheme: str = "https"
    ) -> str:
        """Navigate the profile's one tracked tab to its own site, human-driven.

        Installs `TakeoverNetworkGuard` -- wide method/resource-type traffic,
        still brokered, still no downloads -- and nothing else: no observation
        is taken, no planner is invoked, nothing here reads the page.

        `scheme` exists only for this module's own test suite, which drives a
        plaintext local fixture rather than a real HTTPS site. It is not a
        parameter the wire protocol exposes -- `TakeoverStartRequest` carries
        no scheme or URL field at all -- and the worker route that calls this
        always takes the default.
        """
        session = self._sessions.get(profile_id)
        if session is None:
            return "PROFILE_NOT_OPEN"
        if session.headless:
            raise ProfileSessionError("profile_not_headed")
        if session.read_session is not None and (
            session.read_session.dirty or session.read_session.handed_over
        ):
            raise ProfileSessionError("form_is_dirty")
        if session.read_session is not None:
            raise ProfileSessionError("profile_read_session_active")
        pages = session.context.pages
        page = pages[0] if pages else await session.context.new_page()
        guard = TakeoverNetworkGuard()
        await guard.install(session.context)
        session.takeover_guard = guard
        session.takeover_page = page
        try:
            await page.goto(
                f"{scheme}://{site}/", wait_until="load", timeout=timeout_seconds * 1_000
            )
        except PlaywrightError:
            return "NAVIGATION_FAILED"
        return "OPEN"

    async def confirm_takeover(
        self, profile_id: uuid.UUID, *, site: str
    ) -> "TakeoverCheckResult | None":
        """The one deterministic check run when a takeover ends.

        Returns `None` if the profile is not open in this worker at all.
        Otherwise: a closed site-scope enum, a credential-surface verdict and
        its signals, and an account fingerprint hash or `None`. No text, no
        title, no URL crosses out of this function.
        """
        session = self._sessions.get(profile_id)
        if session is None:
            return None
        page = session.takeover_page
        scope = _site_scope(page, site)
        credential_surface = False
        signals: list[str] = []
        fingerprint: str | None = None
        if page is not None and not page.is_closed():
            detected = await detect_credential_surface(page)
            signals = [signal.value for signal in detected]
            credential_surface = len(signals) > 0
            if scope == TakeoverSiteScope.IN_PROFILE_SITE and not credential_surface:
                fingerprint = await account_fingerprint(page)
        return TakeoverCheckResult(
            scope=scope,
            credential_surface=credential_surface,
            signals=signals,
            account_fingerprint=fingerprint,
        )


__all__ = [
    "PERSISTENT_PROFILE_ARGS",
    "PersistentProfileSession",
    "ProfileSessionError",
    "ProfileSessionStore",
    "TakeoverCheckResult",
    "persistent_launch_options",
    "worker_versions",
]
