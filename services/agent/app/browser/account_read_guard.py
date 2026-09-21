"""The network boundary around an authenticated agent read (Milestone 8a S3).

This is **not** `TakeoverNetworkGuard`. A takeover is a human signing in, so it
must allow a `POST`, a CAPTCHA image and an SSO redirect chain. An agent read
is Lumi looking at somebody's account, so it is the narrow mode: the S0 broker
stays underneath (this guard is a filter in front of the same brokered context,
and its own `route.fetch` goes through the broker) and on top of that:

* **`GET` and `HEAD` only.** No other method leaves the browser context: not a
  form submission, not a same-site `fetch(..., {method: "POST"})`, not `PUT`,
  `PATCH`, `DELETE` or `OPTIONS`. This does not make a `GET` free of
  server-side effects -- a site can count, log or "mark as read" on one -- it
  means Lumi issues no *intentional* mutation.
* **Top-level navigation stays inside the profile's site.** A main-document
  request whose registrable domain is not the profile's site is refused with
  `left_site_scope` **before it is contacted**, and a redirect out of the site
  is refused per hop, from the `Location` header, without following it. An
  authenticated link can itself be a capability, so it is never handed to
  anything else either.
* **Public subresources stay allowed.** Real sites use CDNs; a page Lumi cannot
  render is a page it cannot read. Subresource destinations are still checked
  for shape here and for address by the broker, so private, loopback,
  link-local and metadata destinations remain blocked exactly as S0 blocks
  them. Allowing a third-party subresource does not widen top-level scope.
* **No downloads, no WebSockets, no popups.** An attachment is refused, every
  WebSocket is closed without connecting, and a page opening a window has that
  window closed rather than adopted as a task tab. Service workers and
  permissions are already off at context level, exactly as S1 opens it.
* **Redirects are never followed by the browser.** Playwright does not call a
  route handler for redirect hops, so a browser-followed redirect would bypass
  every check above. The guard fetches with redirects disabled and decides each
  hop itself.

What it does not do: it is not where the connection is made (the broker is),
and it cannot stop a site from recording that Lumi looked.
"""

import asyncio
import logging
import re
from collections import Counter
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Page, Request, Route, WebSocketRoute
from playwright.async_api import Error as PlaywrightError

from app.browser import site_scope
from app.domain.public_url import PublicUrlPolicy, UrlPolicyError

logger = logging.getLogger("lumi.browser.account_read_guard")

#: A rendered account page can need images and fonts, and an image is a plain
#: `GET` the broker vets like any other. Everything else (media, event streams,
#: beacons, pings, manifests, WebSockets) is refused.
ALLOWED_RESOURCE_TYPES = frozenset(
    {"document", "stylesheet", "script", "xhr", "fetch", "image", "font"}
)
ALLOWED_METHODS = frozenset({"GET", "HEAD"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DOCUMENT_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
MAX_RESPONSE_BYTES = 5_000_000
REQUEST_TIMEOUT_SECONDS = 20.0
REDIRECT_PLACEHOLDER = "<!doctype html><title></title>"


class AccountReadNetworkGuard:
    def __init__(
        self,
        *,
        site: str,
        test_origins: frozenset[str] = frozenset(),
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._site = site
        #: Shape only. Which *host* a top-level document may be on is the site
        #: check; which *address* a socket may reach is the broker.
        self._policy = PublicUrlPolicy(
            allow_any_public_host=True, test_origins=test_origins, version="account-read-v1"
        )
        self._timeout_ms = request_timeout_seconds * 1_000
        self._pages: set[Page] = set()
        self._expected = 0
        self._popups: set[asyncio.Task[None]] = set()
        #: Set while the operation itself is following main-document redirects.
        self.following_redirects = False
        #: A same-site redirect target of the main document, validated here,
        #: for the operation to navigate to as a new, checked request.
        self.pending_redirect: str | None = None
        #: Why the main document's last navigation was refused, if it was.
        self.main_frame_block: str | None = None
        self.blocked: Counter[str] = Counter()
        #: Refused request methods, e.g. {"POST": 2}. Names only, never URLs.
        self.blocked_methods: Counter[str] = Counter()
        # Milestone 8b S6. The freeze is one boolean and one counter, both touched
        # only from the event loop with no `await` between reading and writing
        # them, which is what makes "atomically refuse new work" true.
        self._frozen = False
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._installed_context: BrowserContext | None = None

    async def install(self, context: BrowserContext) -> None:
        await context.route("**/*", self._handle)
        await context.route_web_socket(re.compile(r".*"), self._refuse_socket)
        context.on("page", self._close_popup)
        self._installed_context = context
        for page in context.pages:
            self._pages.add(page)

    async def uninstall(self) -> None:
        """Remove this guard's routes. Used only when a human takes the page over
        (Milestone 8b S6 handover), after another guard already answers every
        request, so there is no instant at which a request is unhandled."""
        context, self._installed_context = self._installed_context, None
        if context is None:
            return
        try:
            await context.unroute("**/*", self._handle)
        except PlaywrightError:  # pragma: no cover - the context is going away.
            pass
        # The WebSocket route has no removal call and is deliberately left: it
        # only ever *closes* a socket, so after a handover a live-chat socket on
        # the page stays refused, which is a compatibility cost, not a risk.

    # ---- the freeze (Milestone 8b S6) ---------------------------------------------

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def in_flight(self) -> int:
        """Requests currently inside this guard's network path (past the freeze
        check, not yet finished). Counted from the moment they enter."""
        return self._in_flight

    async def freeze(self, *, settle_timeout_seconds: float) -> bool:
        """Refuse every request that has not yet entered, and wait for the rest.

        The flag is set first and synchronously, so a request that has not yet
        passed the check at the top of `_handle` can never enter afterwards.
        Returns whether `in_flight` reached zero within the bound. **It stays
        frozen either way**: a caller that could not settle decides whether to
        `thaw()`, this never quietly re-opens.
        """
        self._frozen = True
        if self._in_flight == 0:
            return True
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=settle_timeout_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        return self._in_flight == 0

    def thaw(self) -> None:
        self._frozen = False

    def allows_top_level(self, url: str) -> bool:
        """Whether `url` may be a top-level document: well-formed, and in site.

        The same two checks `_handle` applies to a main-document request, so an
        observation never offers a link the guard would then refuse.
        """
        try:
            checked = self._policy.check(url)
        except UrlPolicyError:
            return False
        return site_scope.in_site(checked.url, self._site)

    def track(self, page: Page) -> None:
        self._pages.add(page)

    def untrack(self, page: Page) -> None:
        self._pages.discard(page)

    def expect_page(self) -> None:
        self._expected += 1

    def stop_expecting(self) -> None:
        self._expected = max(0, self._expected - 1)

    # ---- handlers -------------------------------------------------------------

    async def _refuse_socket(self, socket: WebSocketRoute) -> None:
        self.blocked["websocket_blocked"] += 1
        await socket.close(code=1008, reason="blocked")

    def _close_popup(self, popup: Page) -> None:
        if popup in self._pages:
            return
        if self._expected > 0:
            self._pages.add(popup)
            return
        self.blocked["popup_blocked"] += 1
        task = asyncio.ensure_future(self._close(popup))
        self._popups.add(task)
        task.add_done_callback(self._popups.discard)

    @staticmethod
    async def _close(popup: Page) -> None:
        try:
            await popup.close()
        except PlaywrightError:
            pass

    def _is_main_document(self, request: Request) -> bool:
        if not self._pages or not request.is_navigation_request():
            return False
        try:
            frame = request.frame
            return any(frame == page.main_frame for page in self._pages)
        except PlaywrightError:
            return False

    async def _refuse(self, route: Route, code: str, *, main_document: bool) -> None:
        self.blocked[code] += 1
        if main_document:
            self.main_frame_block = code
        try:
            await route.abort("blockedbyclient")
        except PlaywrightError:
            pass

    async def _handle(self, route: Route) -> None:
        # Milestone 8b S6. The very first thing, before the request is inspected,
        # fetched, resolved, proxied or followed: a frozen guard refuses. There
        # is no `await` between this check and the increment below, so a request
        # is either refused here or counted as in flight -- never neither.
        if self._frozen:
            await self._refuse(
                route, "frozen", main_document=self._is_main_document(route.request)
            )
            return
        self._in_flight += 1
        self._idle.clear()
        try:
            await self._handle_open(route)
        finally:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._idle.set()

    async def _handle_open(self, route: Route) -> None:
        request = route.request
        main_document = self._is_main_document(request)
        if request.resource_type not in ALLOWED_RESOURCE_TYPES:
            await self._refuse(route, "resource_type_blocked", main_document=main_document)
            return
        method = request.method.upper()
        if method not in ALLOWED_METHODS:
            self.blocked_methods[method if method.isalpha() and len(method) <= 16 else "OTHER"] += 1
            await self._refuse(route, "method_blocked", main_document=main_document)
            return
        try:
            checked = self._policy.check(request.url)
        except UrlPolicyError as error:
            await self._refuse(route, error.code, main_document=main_document)
            return
        if main_document and not site_scope.in_site(checked.url, self._site):
            # Refused before a byte leaves the machine: the destination is
            # never contacted, so nothing about the visit reaches it.
            await self._refuse(route, "left_site_scope", main_document=True)
            return

        try:
            response = await route.fetch(max_redirects=0, timeout=self._timeout_ms)
        except PlaywrightError:
            await self._refuse(route, "fetch_failed", main_document=main_document)
            return

        if response.status in REDIRECT_STATUSES:
            location = response.headers.get("location")
            await response.dispose()
            if main_document and location and self.following_redirects:
                target = urljoin(request.url, location)
                try:
                    hop = self._policy.check(target)
                except UrlPolicyError as error:
                    await self._refuse(route, error.code, main_document=True)
                    return
                if not site_scope.in_site(hop.url, self._site):
                    # An out-of-site hop is never followed and never contacted.
                    await self._refuse(route, "left_site_scope", main_document=True)
                    return
                self.pending_redirect = hop.url
                self.blocked["redirect_deferred"] += 1
                try:
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=REDIRECT_PLACEHOLDER,
                        headers={"content-security-policy": "default-src 'none'"},
                    )
                except PlaywrightError:
                    pass
                return
            await self._refuse(route, "redirect_not_followed", main_document=main_document)
            return

        length = response.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            await response.dispose()
            await self._refuse(route, "response_too_large", main_document=main_document)
            return

        if request.resource_type == "document":
            disposition = response.headers.get("content-disposition", "").strip().lower()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if disposition.startswith("attachment"):
                await response.dispose()
                await self._refuse(route, "download_blocked", main_document=main_document)
                return
            if content_type not in DOCUMENT_CONTENT_TYPES:
                await response.dispose()
                await self._refuse(route, "unsupported_content_type", main_document=main_document)
                return

        try:
            await route.fulfill(response=response)
        except PlaywrightError:
            pass


__all__ = ["ALLOWED_METHODS", "ALLOWED_RESOURCE_TYPES", "AccountReadNetworkGuard"]
