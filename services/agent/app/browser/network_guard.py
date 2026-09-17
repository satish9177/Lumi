"""The network boundary around one public-page inspection.

Every request the inspection's browser context makes -- the page, its frames,
scripts, stylesheets and API calls, and anything a popup tries -- passes through
`PublicNetworkGuard._handle` before it leaves the machine. The guard is narrow
and fail-closed:

* **Destination.** The URL must pass the same `PublicUrlPolicy` the runtime
  applied when the user approved it, and its host must resolve only to public
  addresses. The guard fetches the response itself, so no request is sent to a
  destination that failed either check.
* **Redirects are never followed by the browser.** Playwright does not call a
  route handler for redirect hops -- verified against Chromium in this
  repository -- so a browser-followed redirect would bypass every check here.
  The guard fetches with redirects disabled. A redirect of the main document is
  handed back to the operation, which validates the target and navigates to it
  as a new, checked request; any other redirect is dropped.
* **Only what reading needs.** Documents, scripts, stylesheets and fetch/XHR.
  Images, media, fonts, beacons, event streams, manifests and WebSockets are
  refused, which also removes most passive exfiltration channels a page has.
  Documents must be HTML or plain text and must not be attachments.

What it does not do, stated plainly: it is not an egress proxy. Resolution
happens here and again inside Playwright's driver, so a hostile DNS server can
still race the two (rebinding). That is why M7a combines this guard with a
trusted host allowlist rather than offering arbitrary hosts; the connection-time
egress broker is Milestone 7b work.
"""

import asyncio
import logging
import re
from collections import Counter
from urllib.parse import urljoin

from playwright.async_api import BrowserContext, Page, Request, Route, WebSocketRoute
from playwright.async_api import Error as PlaywrightError

from app.domain.public_url import (
    PublicUrlPolicy,
    Resolver,
    UrlPolicyError,
    ensure_public_resolution,
    system_resolver,
)

logger = logging.getLogger("lumi.browser.network_guard")

ALLOWED_RESOURCE_TYPES = frozenset({"document", "stylesheet", "script", "xhr", "fetch"})
DOCUMENT_METHODS = frozenset({"GET", "HEAD"})
SUBRESOURCE_METHODS = frozenset({"GET", "HEAD", "POST"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DOCUMENT_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
MAX_RESPONSE_BYTES = 5_000_000
REQUEST_TIMEOUT_SECONDS = 20.0
REDIRECT_PLACEHOLDER = "<!doctype html><title></title>"


class PublicNetworkGuard:
    def __init__(
        self,
        policy: PublicUrlPolicy,
        *,
        resolver: Resolver = system_resolver,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._policy = policy
        self._resolver = resolver
        self._timeout_ms = request_timeout_seconds * 1_000
        self._resolutions: dict[str, UrlPolicyError | None] = {}
        self._page: Page | None = None
        self._popups: set[asyncio.Task[None]] = set()
        #: Set while the operation itself is following main-document redirects.
        self.following_redirects = False
        #: A validated-later redirect target of the main document, for the operation.
        self.pending_redirect: str | None = None
        #: Why the main document's last navigation was refused, if it was.
        self.main_frame_block: str | None = None
        self.blocked: Counter[str] = Counter()

    async def install(self, context: BrowserContext, page: Page) -> None:
        self._page = page
        await context.route("**/*", self._handle)
        await context.route_web_socket(re.compile(r".*"), self._refuse_socket)
        context.on("page", self._close_popup)

    # ---- handlers -------------------------------------------------------------

    async def _refuse_socket(self, socket: WebSocketRoute) -> None:
        self.blocked["websocket_blocked"] += 1
        # Closing without connecting: the server is never contacted.
        await socket.close(code=1008, reason="blocked")

    def _close_popup(self, popup: Page) -> None:
        if popup is self._page:
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
        if self._page is None or not request.is_navigation_request():
            return False
        try:
            return request.frame == self._page.main_frame
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

    async def _checked_destination(self, url: str) -> None:
        checked = self._policy.check(url)
        if checked.host in self._resolutions:
            cached = self._resolutions[checked.host]
            if cached is not None:
                raise cached
            return
        try:
            await ensure_public_resolution(checked, self._resolver)
        except UrlPolicyError as error:
            self._resolutions[checked.host] = error
            raise
        self._resolutions[checked.host] = None

    async def _handle(self, route: Route) -> None:
        request = route.request
        main_document = self._is_main_document(request)
        resource_type = request.resource_type
        if resource_type not in ALLOWED_RESOURCE_TYPES:
            await self._refuse(route, "resource_type_blocked", main_document=main_document)
            return
        methods = DOCUMENT_METHODS if resource_type == "document" else SUBRESOURCE_METHODS
        if request.method not in methods:
            await self._refuse(route, "method_blocked", main_document=main_document)
            return
        try:
            await self._checked_destination(request.url)
        except UrlPolicyError as error:
            await self._refuse(route, error.code, main_document=main_document)
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
                # Not followed here: the operation validates and navigates.
                # An inert placeholder commits instead of an aborted error
                # page, whose late commit would interrupt that next navigation.
                self.pending_redirect = urljoin(request.url, location)
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

        if resource_type == "document":
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
            # The page went away while the response was in flight.
            pass
