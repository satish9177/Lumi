"""Manual login and human takeover (Milestone 8a S2): the real database, real secrets.

`test_login_takeover.py` proves the state machine with a scripted fake
worker. `test_login_takeover_browser.py` proves the browser mechanics with a
real Chromium and no database. This module is the one place both are real at
once: `LoginTakeoverService` and `BrowserProfileService`, backed by a real
Postgres, driving a real headed Chromium through a real synthetic login --
so the two claims that need *both* halves real can actually be checked:

* **the fixture's planted password and OTP never appear anywhere Lumi
  persists or returns** -- scanned for across every durable row this
  milestone could plausibly touch, not just structurally absent by
  inspection;
* **zero dispatches, zero observations, zero tasks, zero actions** are ever
  created by a takeover -- row counts before and after, not a trust that no
  code path exists.

**Why there is no separate worker *subprocess* here.** The worker's HTTP
contract has no "type into this field" endpoint, deliberately -- that is
exactly the generic-automation surface this milestone refuses to have. A
human (or a test standing in for one) can only drive the page by holding the
same `Page` object Lumi's own code would, which means being in the same
process as it. `RealTakeoverBrowser` below is the `BrowserExecutionService`-
shaped adapter that makes that possible while every database write still
goes through the real `LoginTakeoverService` and `BrowserProfileService`,
exactly as `app.main` wires them.

**Site-scope, again.** As in the sibling browser-test module, the fixture
runs on a bare loopback IP, which the pinned Public Suffix List correctly
refuses as a registrable domain. This module inserts the profile row
directly through `BrowserProfileRepository` (bypassing only
`canonical_site()`'s PSL step -- already covered exhaustively by
`test_public_suffix.py` and `test_browser_profiles.py`) and monkeypatches
`app.browser.profile_session._site_scope` to compare `host:port`, exactly as
the sibling module does and for the same reason.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser import profile_session as profile_session_module
from app.browser.egress_broker import EgressBroker
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.browser.profile_session import ProfileSessionStore
from app.domain.browser_profile import BrowserVersions, ProfileStatus, allowed_origins_for
from app.domain.login_takeover import LoginAttemptStatus, TakeoverSiteScope, hash_identity
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from tests.broker_teardown import bounded, quiesce_broker
from app.repositories.profiles import BrowserProfileRepository
from app.services.browser_execution import TakeoverCheckOutcome, WorkerProfileOpen
from app.services.browser_profiles import BrowserProfileService
from app.services.login_takeover import LoginTakeoverService
from app.services.runtime import RuntimeGeneration
from evals.sites.account_fixture import ACCOUNT_ID, FIXTURE_OTP, FIXTURE_PASSWORD, FIXTURE_USERNAME, create_site
from playwright.async_api import Page, Playwright, async_playwright

pytestmark = [pytest.mark.browser]

OPEN_TIMEOUT = 30.0


def _site_scope_by_host(page: object, site: str) -> TakeoverSiteScope:
    """This module has exactly one fixture origin, so comparing bare host is
    enough to distinguish "on it" from "not on it" -- and, unlike
    `host:port`, a bare loopback host still satisfies
    `browser_profiles.site`'s `ck_browser_profiles_site_format` constraint,
    which the module docstring's `host:port` sibling technique cannot."""
    if page is None or page.is_closed():  # type: ignore[attr-defined]
        return TakeoverSiteScope.NO_PAGE
    parsed = urlsplit(page.url)  # type: ignore[attr-defined]
    if not parsed.hostname:
        return TakeoverSiteScope.NO_PAGE
    return TakeoverSiteScope.IN_PROFILE_SITE if parsed.hostname == site else TakeoverSiteScope.OTHER_PUBLIC_SITE


@pytest.fixture(autouse=True)
def _host_site_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile_session_module, "_site_scope", _site_scope_by_host)


class FixtureServer:
    """The account fixture, in-process on a loopback port."""

    def __init__(self) -> None:
        self._task: "asyncio.Task[None] | None" = None
        self.port = 0

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(create_site(), host="127.0.0.1", port=0, log_level="warning", lifespan="off")
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
        return "127.0.0.1"

    async def aclose(self) -> None:
        self._uvicorn.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=30)


class RealTakeoverBrowser:
    """The `BrowserExecutionService`-shaped adapter, backed by a real,
    test-owned `ProfileSessionStore` instead of a worker subprocess.

    Every method here does exactly what the real service does, minus the
    HTTP hop: `open_browser_profile` opens the real persistent context,
    `start_takeover`/`confirm_takeover` call the real worker-side methods
    directly. `LoginTakeoverService` and `BrowserProfileService` cannot tell
    the difference -- which is the point: this proves *their* database logic
    against a real browser, not a scripted one.
    """

    def __init__(
        self, *, playwright: Playwright, broker: EgressBroker, paths: object,
        current: BrowserVersions, account_origin: str,
    ) -> None:
        self._playwright = playwright
        self._broker = broker
        self._paths = paths
        self._current = current
        #: The fixture's real `host:port`. `profile.site` (what
        #: `LoginTakeoverService` passes as `site=`) is the bare host only,
        #: to satisfy `browser_profiles.site`'s format constraint -- see the
        #: module docstring. Navigation needs the real port, so this adapter
        #: keeps it separately rather than deriving it from `site`.
        self._account_origin = account_origin
        self.store = ProfileSessionStore()
        self.worker_generation = uuid.uuid4()

    async def open_browser_profile(
        self, *, profile_id: uuid.UUID, recorded_chromium_build: str | None, headed: bool = False
    ) -> WorkerProfileOpen:
        await self.store.open(
            playwright=self._playwright, broker=self._broker, paths=self._paths,  # type: ignore[arg-type]
            profile_id=profile_id, recorded=None, current=self._current,
            headless=not headed, timeout_seconds=OPEN_TIMEOUT,
        )
        return WorkerProfileOpen(
            profile_id=profile_id, worker_generation=self.worker_generation, versions=self._current, lock_held=True,
        )

    async def close_browser_profile(self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID) -> bool:
        return await self.store.close(profile_id)

    async def start_takeover(self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str) -> str:
        # Navigates to the fixture's real `host:port`, not the bare-host
        # `site` the caller passed -- see `__init__`'s note.
        navigation_site = self._account_origin.removeprefix("http://")
        return await self.store.start_takeover(
            profile_id, site=navigation_site, timeout_seconds=OPEN_TIMEOUT, scheme="http"
        )

    async def confirm_takeover(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str
    ) -> TakeoverCheckOutcome:
        result = await self.store.confirm_takeover(profile_id, site=site)
        if result is None:
            return TakeoverCheckOutcome(
                status="PROFILE_NOT_OPEN", scope="NO_PAGE", credential_surface=False, signals=(), account_fingerprint=None
            )
        return TakeoverCheckOutcome(
            status="CHECKED", scope=result.scope.value, credential_surface=result.credential_surface,
            signals=tuple(result.signals), account_fingerprint=result.account_fingerprint,
        )

    def page(self, profile_id: uuid.UUID) -> Page | None:
        return self.store.get(profile_id).takeover_page


@pytest.fixture
async def account() -> AsyncIterator[FixtureServer]:
    server = FixtureServer()
    await server.start()
    try:
        yield server
    finally:
        await server.aclose()


@pytest.fixture
async def browser(
    account: FixtureServer, tmp_path: Path, engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> AsyncIterator[RealTakeoverBrowser]:
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({account.origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "browser-profiles")})
    probe = await playwright.chromium.launch(headless=True)
    current = BrowserVersions(chromium_build=probe.version, playwright_version="1.63.0", app_version="0.1.0")
    await probe.close()
    adapter = RealTakeoverBrowser(
        playwright=playwright, broker=broker, paths=paths, current=current, account_origin=account.origin
    )
    # `login_attempts.worker_generation` is a real foreign key to
    # `browser_worker_generations` -- true in production because
    # `BrowserExecutionService._bind_worker` always registers a generation
    # first. This adapter has no HTTP handshake to do that, so it does the
    # one piece of bookkeeping directly.
    from datetime import UTC, datetime

    from app.repositories.browser import BrowserRepository

    async with engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=adapter.worker_generation,
            runtime_generation=runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
    try:
        yield adapter
    finally:
        # See tests/takeover_teardown.py.
        await adapter.store.close_all()
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())


@pytest.fixture
def profiles(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, browser: RealTakeoverBrowser, tmp_path: Path
) -> BrowserProfileService:
    return BrowserProfileService(
        engine, runtime_generation=runtime_generation.id, browser=browser,  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "unused")}),
    )


@pytest.fixture
def takeovers(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, browser: RealTakeoverBrowser, profiles: BrowserProfileService
) -> LoginTakeoverService:
    return LoginTakeoverService(
        engine, runtime_generation=runtime_generation.id, browser=browser, profiles=profiles,  # type: ignore[arg-type]
    )


@pytest.fixture
async def profile_id(engine: AsyncEngine, account: FixtureServer) -> uuid.UUID:
    """A profile row bound to the fixture's `host:port`, inserted directly --
    see the module docstring for why `canonical_site()` cannot be used."""
    new_id = uuid.uuid4()
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).create(
            profile_id=new_id, label="Fixture Account", site=account.site,
            allowed_origins=allowed_origins_for(account.site),
        )
    return new_id


async def _row_counts(engine: AsyncEngine) -> dict[str, int]:
    tables = ("tasks", "actions", "browser_dispatches", "page_observations", "research_observations")
    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in tables:
            counts[table] = int(await connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
    return counts


async def _all_row_text(engine: AsyncEngine) -> str:
    """Every row of every table this milestone writes, as one string to
    scan. A second, independent check beside reading the code."""
    tables = ("login_attempts", "browser_profiles", "runtime_generations", "browser_worker_generations")
    parts: list[str] = []
    async with engine.connect() as connection:
        for table in tables:
            result = await connection.execute(text(f"SELECT * FROM {table}"))
            for row in result.all():
                parts.append(str(tuple(row)))
    return "\n".join(parts)


async def test_a_real_login_leaves_no_planted_secret_in_the_database(
    profile_id: uuid.UUID, profiles: BrowserProfileService, takeovers: LoginTakeoverService,
    browser: RealTakeoverBrowser, account: FixtureServer, engine: AsyncEngine,
) -> None:
    profile = await profiles.get_profile(profile_id)
    started = await takeovers.start_takeover(profile_id, expected_revision=profile.revision)
    assert started.attempt.status is LoginAttemptStatus.OPEN

    page = browser.page(profile_id)
    assert page is not None
    await page.goto(f"{account.origin}/login", wait_until="load")
    await page.locator("input[name='username']").fill(FIXTURE_USERNAME)
    await page.locator("input[name='password']").fill(FIXTURE_PASSWORD)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/login/otp")
    await page.locator("input[name='code']").fill(FIXTURE_OTP)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/account")

    result = await takeovers.confirm_takeover(
        profile_id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.refusal_reason is None
    assert result.profile.status is ProfileStatus.AUTHENTICATED
    assert result.profile.account_fingerprint == hash_identity(ACCOUNT_ID)

    haystack = await _all_row_text(engine)
    assert FIXTURE_PASSWORD not in haystack
    assert FIXTURE_OTP not in haystack
    assert ACCOUNT_ID not in haystack  # the raw identity; only its hash may appear
    # And the response objects this test already holds -- the runtime-shaped
    # values `LoginTakeoverService` returned -- carry none of it either.
    assert FIXTURE_PASSWORD not in repr(result)
    assert FIXTURE_OTP not in repr(result)


async def test_a_takeover_creates_no_task_action_dispatch_or_observation(
    profile_id: uuid.UUID, profiles: BrowserProfileService, takeovers: LoginTakeoverService,
    browser: RealTakeoverBrowser, account: FixtureServer, engine: AsyncEngine,
) -> None:
    before = await _row_counts(engine)

    profile = await profiles.get_profile(profile_id)
    started = await takeovers.start_takeover(profile_id, expected_revision=profile.revision)
    page = browser.page(profile_id)
    assert page is not None
    await page.goto(f"{account.origin}/login", wait_until="load")
    await page.locator("input[name='username']").fill(FIXTURE_USERNAME)
    await page.locator("input[name='password']").fill(FIXTURE_PASSWORD)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/login/otp")
    await page.locator("input[name='code']").fill(FIXTURE_OTP)
    await page.locator("button[type='submit']").click()
    await page.wait_for_url("**/account")
    await takeovers.confirm_takeover(profile_id, started.attempt.id, expected_revision=started.profile.revision)

    after = await _row_counts(engine)
    assert after == before
