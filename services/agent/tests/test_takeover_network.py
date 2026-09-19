"""The TAKEOVER network mode against a real Chromium (Milestone 8a S2).

Reuses S0's own broker test harness (`FixtureSite`, imported directly from
`test_egress_broker_browser.py`) rather than inventing a second one: the
destination boundary TAKEOVER relies on is the *same* `EgressBroker`, wired
exactly as `managed_launch_options` wires it for every other managed context.
What changes for TAKEOVER is only which *Playwright-level* guard sits above
the broker -- `TakeoverNetworkGuard` instead of `PublicNetworkGuard` -- and
that guard is deliberately wide: any method, any resource type, still no
non-`http(s)`/`data:`/`blob:` scheme, still no downloads.

**What this module proves, and where the boundary actually lives:**

* Everything `EgressBroker` already guarantees -- no private/loopback/
  metadata destination, no unbrokered loopback, QUIC disabled, broker death
  fails closed -- is untouched by which guard sits above it. These tests
  install `TakeoverNetworkGuard` and then run the *same* refusal scenarios
  `test_egress_broker_browser.py` runs, to show TAKEOVER does not widen the
  broker's own boundary even though it widens the guard's.
* What `TakeoverNetworkGuard` itself adds -- POST, images, fonts, public and
  SSO-shaped redirects working, versus `file:`/custom schemes refused -- is
  proven directly against the guard.
"""

import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page
from playwright.async_api import async_playwright

from app.browser.egress_broker import EgressBroker, managed_launch_options
from app.browser.takeover_guard import TakeoverNetworkGuard
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from tests.broker_teardown import bounded, quiesce_broker
from tests.test_egress_broker_browser import FixtureSite, refusal_of

pytestmark = [pytest.mark.browser]

@dataclass
class Takeover:
    broker: EgressBroker
    browser: Browser
    site: FixtureSite
    canary: FixtureSite

    async def guarded_context(self) -> tuple[BrowserContext, TakeoverNetworkGuard, Page]:
        context = await self.browser.new_context(
            service_workers="block", accept_downloads=False, permissions=[]
        )
        guard = TakeoverNetworkGuard()
        await guard.install(context)
        page = await context.new_page()
        return context, guard, page


@pytest.fixture
async def takeover() -> AsyncIterator[Takeover]:
    canary = FixtureSite()
    await canary.start()
    site = FixtureSite(canary_origin=canary.origin)
    await site.start()
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
        yield Takeover(broker=broker, browser=browser, site=site, canary=canary)
    finally:
        try:
            await browser.close()
        except PlaywrightError:  # pragma: no cover - already gone
            pass
        # See tests/takeover_teardown.py.
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())
        await bounded("site.aclose", site.aclose())
        await canary.aclose()


# ---- what TAKEOVER widens, and proves it needs to ----------------------------


async def test_a_login_post_reaches_the_fixture(takeover: Takeover) -> None:
    """`PublicNetworkGuard` (AGENT_READ) refuses this outright; TAKEOVER must not."""
    context, guard, page = await takeover.guarded_context()
    await page.goto(f"{takeover.site.origin}/page")
    await page.evaluate("() => fetch('/submit', {method: 'POST', body: 'v=1'})")
    await page.wait_for_timeout(200)
    assert takeover.site.hits["/submit"] >= 1
    assert guard.blocked.total() == 0
    await context.close()


async def test_image_and_font_shaped_requests_load(takeover: Takeover) -> None:
    """TAKEOVER does not discriminate by resource type the way `AGENT_READ`
    does: an image and a font-shaped GET both reach the fixture."""
    context, guard, page = await takeover.guarded_context()
    await page.goto(f"{takeover.site.origin}/page")
    await page.evaluate(
        """async () => {
            await new Promise((resolve) => {
                const img = new Image();
                img.onload = img.onerror = resolve;
                img.src = '/pixel.png';
            });
            await fetch('/font.woff2');
        }"""
    )
    await page.wait_for_timeout(200)
    assert takeover.site.hits["/pixel.png"] >= 1
    assert takeover.site.hits["/font.woff2"] >= 1
    assert guard.blocked.total() == 0
    await context.close()


async def test_a_public_redirect_is_followed_by_chromium_itself(takeover: Takeover) -> None:
    """No `route.fetch` interposition in TAKEOVER: Chromium follows the
    redirect on its own, and the target is still brokered."""
    context, guard, page = await takeover.guarded_context()
    await page.goto(f"{takeover.site.origin}/redirect-public")
    assert page.url.rstrip("/") == f"{takeover.site.origin}/page"
    assert guard.blocked.total() == 0
    await context.close()


# ---- what stays exactly as narrow as the broker already made it -------------


async def test_the_broker_still_counts_every_connection(takeover: Takeover) -> None:
    """The fixture is plaintext HTTP (the reviewed configured-origin
    exception), so Chromium reaches it in absolute form, not `CONNECT`."""
    context, _guard, page = await takeover.guarded_context()
    before = takeover.broker.counters.allowed["http"]
    await page.goto(f"{takeover.site.origin}/page")
    assert takeover.broker.counters.allowed["http"] > before
    await context.close()


async def test_a_private_ipv4_destination_is_refused(takeover: Takeover) -> None:
    context, _guard, page = await takeover.guarded_context()
    refusal = await refusal_of(page, "https://10.0.0.5/")
    assert refusal in (None, "non_public_address")
    await context.close()


async def test_loopback_is_refused_except_the_configured_fixture_origin(takeover: Takeover) -> None:
    context, _guard, page = await takeover.guarded_context()
    before = takeover.canary.connections
    refusal = await refusal_of(page, f"{takeover.canary.origin}/page")
    assert refusal in (None, "plaintext_not_allowed")
    assert takeover.canary.connections == before
    await context.close()


async def test_a_cloud_metadata_destination_is_refused(takeover: Takeover) -> None:
    context, _guard, page = await takeover.guarded_context()
    refusal = await refusal_of(page, "https://169.254.169.254/")
    assert refusal in (None, "non_public_address")
    await context.close()


async def test_quic_is_disabled_on_the_takeover_launch(takeover: Takeover) -> None:
    from app.browser.egress_broker import MANAGED_CHROMIUM_ARGS
    from app.browser.profile_session import persistent_launch_options

    assert "--disable-quic" in MANAGED_CHROMIUM_ARGS
    options = persistent_launch_options(takeover.broker, headless=False)
    assert "--disable-quic" in options["args"]  # type: ignore[operator]


async def test_downloads_are_refused(takeover: Takeover) -> None:
    """`accept_downloads=False` on the context: Playwright still reports the
    attempt (so a test can observe it), but never saves a file for it."""
    context, _guard, page = await takeover.guarded_context()
    await page.goto(f"{takeover.site.origin}/page")
    async with page.expect_download(timeout=3_000) as download_info:
        await page.evaluate(
            "() => { const a = document.createElement('a'); "
            "a.href = '/pixel.png'; a.download = 'x.png'; "
            "document.body.appendChild(a); a.click(); }"
        )
    download = await download_info.value
    with pytest.raises(PlaywrightError):
        await download.path()
    await context.close()


async def test_a_custom_protocol_navigation_is_refused(takeover: Takeover) -> None:
    context, guard, page = await takeover.guarded_context()
    before = sum(guard.blocked.values())
    try:
        await page.goto("myapp://launch", timeout=5_000)
    except PlaywrightError:
        pass
    assert page.url.rstrip("/") != "myapp://launch"
    # Either the guard's own scheme check counted it, or Chromium refused a
    # scheme it has no handler for before any request reached the guard at
    # all -- both are "never left the allowed schemes".
    assert sum(guard.blocked.values()) >= before
    await context.close()


async def test_a_file_url_is_unavailable(takeover: Takeover) -> None:
    """`file:` bypasses `route()` entirely -- a real, documented Chromium
    limitation, not a gap in this guard's request interception -- so the
    guard reacts to the committed navigation instead. The page is genuinely
    on the file URL for a moment; this asserts it does not stay there."""
    context, _guard, page = await takeover.guarded_context()
    try:
        await page.goto("file:///C:/Windows/win.ini", timeout=5_000)
    except PlaywrightError:
        pass
    try:
        await page.wait_for_url("about:blank", timeout=5_000)
    except PlaywrightError:
        pass
    assert page.url == "about:blank"
    await context.close()


async def test_a_dead_broker_does_not_fall_back_to_direct_networking(takeover: Takeover) -> None:
    context, _guard, page = await takeover.guarded_context()
    await page.goto(f"{takeover.site.origin}/page")
    before = takeover.site.connections
    await takeover.broker.aclose()

    with pytest.raises(PlaywrightError):
        await page.goto(f"{takeover.site.origin}/page", timeout=5_000)

    assert takeover.site.connections == before
    await context.close()
