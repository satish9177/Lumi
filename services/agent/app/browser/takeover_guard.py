"""The network mode installed on a profile's page during a human takeover.

`PublicNetworkGuard` (`network_guard.py`) is the M7a/M7b `AGENT_READ` mode: it
fetches every request itself, allows only `GET`/`HEAD`, allows only a handful
of resource types, and never lets Chromium follow a redirect. That mode
cannot be reused unchanged for a human signing in -- a login form is a
`POST`, a CAPTCHA is a picture, and an SSO chain is a browser-followed
redirect through an identity provider Lumi cannot enumerate in advance.

`TakeoverNetworkGuard` is the deliberately narrower `TAKEOVER` mode this
milestone adds. It does **not** fetch requests itself and does **not**
restrict method or resource type -- Chromium handles the human's traffic
natively, exactly as an ordinary browser would. What it still refuses,
before a request ever leaves the browser process:

* **Any scheme other than `http`, `https`, `data:` or `blob:`.** `data:` and
  `blob:` are inert, already-local resources (an inline icon, a canvas
  export) that never generate a network request at all; refusing them would
  break ordinary page rendering for no security benefit. Everything else --
  `ftp:`, a custom app-registered scheme, `chrome:`, `view-source:` -- is
  refused at the request-interception layer, which is what keeps "no
  external protocol launch" true even though method and resource type are
  wide open.

  **`file:` is a documented exception to that, and it is reactive rather
  than preventive.** Chromium loads a `file:` navigation through a dedicated
  local-file loader that never reaches the network service layer
  Playwright's `route()` intercepts -- confirmed empirically in
  `tests/test_takeover_network.py::test_a_file_url_is_unavailable`, which is
  also the tripwire for a future Playwright/Chromium version that changes
  this. There is no Playwright API that prevents the navigation before it
  commits. What this guard does instead: it is notified the instant the top
  frame commits (`page.on("framenavigated")`, registered for every page the
  context has or opens), and if the committed URL's scheme is not allowed it
  immediately navigates the page back to `about:blank`. **R:** the forbidden
  page's content is genuinely rendered for a short, real interval before
  that happens -- this closes exposure quickly, it does not prevent it.

Everything else TAKEOVER mode still enforces is enforced elsewhere and is not
this guard's job:

* **The destination.** `--proxy-server` and `--proxy-bypass-list=<-loopback>`
  route every `http(s)` request through the S0 egress broker, which still
  requires a public, globally-routable address (or an exactly-configured test
  origin) before it dials anything. TAKEOVER does not touch the broker's
  policy at all.
* **Downloads.** `accept_downloads=False` on the persistent context, set at
  open time exactly as S1 set it. This guard does not re-implement a
  content-disposition check, because it does not intercept responses at all.
* **Service workers, permissions.** Unchanged from S1: still blocked, still
  empty.

Popups are tracked, never adopted as a durable resource, and are not treated
specially here: the whole persistent context -- main tab and any popup alike
-- is closed when the takeover ends (confirmed, cancelled or expired), which
is what "a takeover popup dies when takeover ends" means in practice.
"""

import asyncio
from collections import Counter
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error as PlaywrightError, Frame, Page, Route

#: Schemes a human-driven page may legitimately generate traffic for.
#: `data:`/`blob:` do not reach the request-interception handler in practice
#: (they are resolved locally by Chromium, not fetched), and are listed
#: anyway so the rule reads as an allowlist rather than a guess about what
#: never arrives. `about:` is additionally allowed only for the reactive
#: navigation check, because a fresh tab's initial `about:blank` must not be
#: treated as a violation of itself.
ALLOWED_SCHEMES = frozenset({"http", "https", "data", "blob"})
_ALLOWED_NAVIGATION_SCHEMES = ALLOWED_SCHEMES | {"about"}


class TakeoverNetworkGuard:
    """Installed on a persistent profile's context for the life of a takeover."""

    def __init__(self) -> None:
        self.blocked: Counter[str] = Counter()
        self._popups: set[Page] = set()
        self._retreats: set[asyncio.Task[None]] = set()

    async def install(self, context: BrowserContext) -> None:
        await context.route("**/*", self._handle)
        context.on("page", self._track_popup)
        context.on("page", self._watch_navigation)
        for page in context.pages:
            self._watch_navigation(page)

    def _track_popup(self, popup: Page) -> None:
        # Not adopted into any resource table, and not closed here either:
        # the whole context (main tab and every popup) is closed when the
        # takeover ends. Tracked only so a future extension has somewhere to
        # hang a bounded "how many extra tabs" count if one is ever needed.
        self._popups.add(popup)

    def _watch_navigation(self, page: Page) -> None:
        page.on("framenavigated", self._on_navigated)

    def _on_navigated(self, frame: Frame) -> None:
        # Only the top frame: an embedded frame reaching a forbidden scheme
        # has no filesystem or protocol-launch capability of its own to
        # exercise, and closing the whole page over a same-origin subframe
        # would be a worse failure mode than the one this guards against.
        if frame.parent_frame is not None:
            return
        scheme = urlsplit(frame.url).scheme.lower()
        if scheme in _ALLOWED_NAVIGATION_SCHEMES:
            return
        self.blocked[f"scheme_blocked_reactive_{scheme or 'unknown'}"] += 1
        task = asyncio.ensure_future(self._retreat(frame.page))
        self._retreats.add(task)
        task.add_done_callback(self._retreats.discard)

    @staticmethod
    async def _retreat(page: Page) -> None:
        try:
            await page.goto("about:blank")
        except PlaywrightError:  # pragma: no cover - the page went away.
            pass

    async def _handle(self, route: Route) -> None:
        scheme = urlsplit(route.request.url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            self.blocked[f"scheme_blocked_{scheme or 'unknown'}"] += 1
            try:
                await route.abort("blockedbyclient")
            except PlaywrightError:  # pragma: no cover - the page went away.
                pass
            return
        try:
            await route.continue_()
        except PlaywrightError:  # pragma: no cover - the page went away.
            pass


__all__ = ["ALLOWED_SCHEMES", "TakeoverNetworkGuard"]
