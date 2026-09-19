"""Manual login and human takeover (Milestone 8a S2), against a real Chromium.

Worker-level, exactly at `test_browser_profile_session.py`'s level: a real
persistent profile, a real S0 broker, and the synthetic account fixture --
now driven through its full login surface rather than only `/session/start`.

**A note on site-scope testing.** `_site_scope` compares the *registrable
domain* of the active page against the profile's bound site, through the
pinned Public Suffix List (`app.domain.public_suffix.registrable_domain`).
Every fixture in this repository runs on a bare loopback IP
(`http://127.0.0.1:<port>`), and an IP literal has no registrable domain by
construction -- `test_public_suffix.py` asserts exactly that refusal, and the
S0 broker's own `configured_origins` mechanism requires a literal loopback
IP, not a hostname, so there is no way to give a fixture a real domain name
without either weakening the broker's narrow local-fixture exception or
fighting the PSL's (correct) refusal of IP literals. Real profiles are always
bound to a real registrable domain in production; `canonical_site()` refuses
an IP before a row even exists.

So this module monkeypatches `app.browser.profile_session._site_scope` to
compare `host:port` instead of a PSL-derived registrable domain, which is the
same comparison with the same code shape, substituting only the identity
function two loopback fixtures can actually have. It does not touch
production code, and it does not change what is being proven: that the
*worker* correctly tracks one page, asks the *comparison* the right question,
and gates the credential-surface check and the account fingerprint on its
answer exactly as `app/browser/profile_session.py` says it does.
`test_public_suffix.py` already covers the PSL comparison itself, 46 ways.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Playwright, async_playwright

from app.browser import profile_session as profile_session_module
from app.browser.credential_signals import ACCOUNT_IDENTITY_ATTRIBUTE
from app.browser.egress_broker import EgressBroker
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.browser.profile_session import ProfileSessionStore
from app.domain.browser_profile import BrowserVersions
from app.domain.login_takeover import TakeoverSiteScope, hash_identity
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from tests.broker_teardown import bounded, quiesce_broker
from evals.sites.account_fixture import (
    ACCOUNT_ID,
    FIXTURE_OTP,
    FIXTURE_PASSWORD,
    FIXTURE_USERNAME,
    SECOND_ACCOUNT_ID,
    SIGNED_IN,
    create_site,
)

pytestmark = [pytest.mark.browser]

OPEN_TIMEOUT = 30.0


class FixtureServer:
    """The account fixture, in-process on a loopback port. See
    `test_browser_profile_session.py::FixtureServer`, which this mirrors."""

    def __init__(self, *, idp_origin: str | None = None) -> None:
        self._task: "asyncio.Task[None] | None" = None
        self.port = 0
        self._idp_origin = idp_origin

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            create_site(idp_origin=self._idp_origin), host="127.0.0.1", port=0,
            log_level="warning", lifespan="off",
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        self._task = asyncio.create_task(server.serve())
        for _ in range(600):
            if server.started and server.servers:
                self.port = server.servers[0].sockets[0].getsockname()[1]
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("the account fixture did not start")

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def site(self) -> str:
        """The `host:port` identity this module's monkeypatched
        `_site_scope` compares against -- see the module docstring."""
        return f"127.0.0.1:{self.port}"

    async def aclose(self) -> None:
        self._uvicorn.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=30)


def _site_scope_by_host_and_port(page: object, site: str) -> TakeoverSiteScope:
    if page is None or page.is_closed():  # type: ignore[attr-defined]
        return TakeoverSiteScope.NO_PAGE
    parsed = urlsplit(page.url)  # type: ignore[attr-defined]
    if not parsed.hostname:
        return TakeoverSiteScope.NO_PAGE
    current = f"{parsed.hostname}:{parsed.port}"
    return TakeoverSiteScope.IN_PROFILE_SITE if current == site else TakeoverSiteScope.OTHER_PUBLIC_SITE


@pytest.fixture(autouse=True)
def _host_port_site_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile_session_module, "_site_scope", _site_scope_by_host_and_port)


@dataclass
class Harness:
    playwright: Playwright
    broker: EgressBroker
    site: FixtureServer
    idp: FixtureServer
    paths: object
    current: BrowserVersions

    async def opened(self, store: ProfileSessionStore, profile_id: uuid.UUID, *, headed: bool) -> None:
        await store.open(
            playwright=self.playwright, broker=self.broker, paths=self.paths,  # type: ignore[arg-type]
            profile_id=profile_id, recorded=None, current=self.current,
            headless=not headed, timeout_seconds=OPEN_TIMEOUT,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    idp = FixtureServer()
    await idp.start()
    site = FixtureServer(idp_origin=idp.origin)
    await site.start()
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({site.origin, idp.origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "browser-profiles")})
    probe = await playwright.chromium.launch(headless=True)
    current = BrowserVersions(
        chromium_build=probe.version, playwright_version="1.63.0", app_version="0.1.0"
    )
    await probe.close()
    try:
        yield Harness(playwright=playwright, broker=broker, site=site, idp=idp, paths=paths, current=current)
    finally:
        # See tests/takeover_teardown.py: full Chromium's lingering idle
        # keep-alive connection to the proxy has been observed to hang
        # `playwright.stop()` on Windows if torn down while one is still
        # open. Quiesce first, and bound every step so a future regression
        # fails one test loudly instead of hanging the whole suite.
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())
        await bounded("site.aclose", site.aclose())
        await bounded("idp.aclose", idp.aclose())


# ---- password + OTP, the primary flow ----------------------------------------


async def test_password_and_otp_login_ends_authenticated_with_no_credential_surface(
    harness: Harness,
) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)

    outcome = await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    assert outcome == "OPEN"

    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None

    # Start-takeover lands on the site's root, exactly as a real site would
    # show an unauthenticated visitor; the human clicks through to sign in.
    await page.goto(f"{harness.site.origin}/login", wait_until="load")

    # The human types the fixture's synthetic credentials directly into the
    # headed page. Lumi application code never receives them.
    await page.locator("input[name='username']").fill(FIXTURE_USERNAME)
    await page.locator("input[name='password']").fill(FIXTURE_PASSWORD)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/login/otp")
    await page.locator("input[name='code']").fill(FIXTURE_OTP)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/account")

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.scope is TakeoverSiteScope.IN_PROFILE_SITE
    assert result.credential_surface is False
    assert result.signals == []
    assert result.account_fingerprint == hash_identity(ACCOUNT_ID)
    await store.close_all()


async def test_a_captcha_shaped_challenge_is_click_through_only(harness: Harness) -> None:
    """The human clicks through it; Lumi never inspects it and never fails
    to recognise the eventual signed-in state."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None

    await page.goto(f"{harness.site.origin}/login/challenge", wait_until="load")
    assert await page.locator(".challenge").count() == 1
    await page.locator("#challenge-continue").click()
    await page.wait_for_url("**/account")

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None and result.scope is TakeoverSiteScope.IN_PROFILE_SITE
    assert result.credential_surface is False
    await store.close_all()


async def test_sso_redirect_through_a_second_origin_ends_back_in_scope(harness: Harness) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    outcome = await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    assert outcome == "OPEN"
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None

    await page.goto(f"{harness.site.origin}/login/sso", wait_until="load")
    # The chain visited the idp origin and landed back on the account origin.
    assert urlsplit(page.url).port == harness.site.port

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.scope is TakeoverSiteScope.IN_PROFILE_SITE
    assert result.credential_surface is False
    await store.close_all()


# ---- incomplete / off-site ----------------------------------------------------


async def test_confirming_while_still_on_the_login_page_reports_a_credential_surface(
    harness: Harness,
) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None
    await page.goto(f"{harness.site.origin}/login", wait_until="load")

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.scope is TakeoverSiteScope.IN_PROFILE_SITE
    assert result.credential_surface is True
    assert "PASSWORD_FIELD" in result.signals
    assert result.account_fingerprint is None
    await store.close_all()


async def test_finishing_on_the_idp_origin_is_reported_as_off_site(harness: Harness) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None

    await page.goto(
        f"{harness.site.origin}/login/offsite?to={harness.idp.origin}/", wait_until="load"
    )
    assert urlsplit(page.url).port == harness.idp.port

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.scope is TakeoverSiteScope.OTHER_PUBLIC_SITE
    await store.close_all()


async def test_closing_the_tracked_tab_reports_no_page(harness: Harness) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None
    await page.close()

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.scope is TakeoverSiteScope.NO_PAGE
    await store.close_all()


# ---- account identity ----------------------------------------------------------


async def test_switching_accounts_changes_the_fingerprint(harness: Harness) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    assert page is not None

    await page.goto(f"{harness.site.origin}/switch-account", wait_until="load")
    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.account_fingerprint == hash_identity(SECOND_ACCOUNT_ID)
    assert result.account_fingerprint != hash_identity(ACCOUNT_ID)
    await store.close_all()


async def test_an_existing_valid_session_is_rediscovered_without_re_entering_credentials(
    harness: Harness,
) -> None:
    """The human need not type anything if the profile already carries a
    valid cookie -- the deterministic check is about current state, not
    about how the browser got there."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)

    # A previous, already-completed sign-in (simulated the way a real one
    # would leave the profile: a valid cookie in the same directory). The
    # bootstrap tab is reused, not closed: closing a headed persistent
    # context's only remaining tab leaves Chromium in a transitional state
    # (a real desktop browser exits when its last tab closes) and a
    # subsequent `new_page()` can fail outright -- reproduced directly, not
    # theorised. `start_takeover` will pick this same tab back up.
    session = store.get(profile_id)
    bootstrap = session.context.pages[0] if session.context.pages else await session.context.new_page()
    await bootstrap.goto(f"{harness.site.origin}/session/start", wait_until="load")

    outcome = await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    assert outcome == "OPEN"
    takeover_page = store.get(profile_id).takeover_page
    assert takeover_page is not None
    # The human clicks through to the account page -- no form, because the
    # cookie from the prior session is still valid.
    await takeover_page.goto(f"{harness.site.origin}/account", wait_until="load")
    assert SIGNED_IN in await takeover_page.locator("body").inner_text()

    result = await store.confirm_takeover(profile_id, site=harness.site.site)
    assert result is not None
    assert result.credential_surface is False
    assert result.scope is TakeoverSiteScope.IN_PROFILE_SITE
    await store.close_all()


# ---- popups --------------------------------------------------------------------


async def test_a_popup_is_never_read_and_dies_with_the_context(harness: Harness) -> None:
    """A login popup is tracked (so it is not silently closed as an
    unexpected page) but never adopted as a durable resource and never has
    its DOM read -- it simply closes with the rest of the context."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id, headed=True)
    await store.start_takeover(profile_id, site=harness.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http")
    session = store.get(profile_id)
    page = session.takeover_page
    guard = session.takeover_guard
    assert page is not None and guard is not None

    async with session.context.expect_page() as popup_info:
        await page.evaluate("() => window.open('/login', 'popup')")
    popup = await popup_info.value
    await popup.wait_for_load_state()

    # Tracked (so the guard did not close it as an unrecognised page)...
    assert popup in guard._popups
    assert not popup.is_closed()

    # ...but not a durable resource of any kind: there is no table, no ref
    # and no API that could name this popup, and closing the whole context
    # takes it with it.
    await store.close(profile_id)
    assert popup.is_closed()
