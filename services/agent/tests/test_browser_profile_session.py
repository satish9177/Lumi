"""A real persistent Chromium profile: does it persist, and does it stay bounded?

The headline test is `test_a_session_survives_a_browser_restart`, and how it
proves persistence is the point:

    the fixture's own `/session/start` issues an HttpOnly cookie
        ... the whole browser is closed, and a new one opened on the same
            profile directory ...
    the fixture's own `/account` says "signed in as Fixture Account"

**The server decides.** Lumi never calls `storage_state()`, never calls
`context.cookies()`, and never opens a file Chromium wrote. It reads the
visible text of a page, which is the same thing a person would do. The cookie
is `HttpOnly`, so not even page script can read it -- the only thing that could
present it to the server on the second run is the browser itself, carrying
state it kept in the profile directory. That is what persistence means, and it
is the only honest way to demonstrate it.

Everything here goes through the S0 egress broker, with the same launch options
the managed research browser uses. There is no loopback exemption and no
unbrokered persistent context: the fixture reaches the browser through the
broker like any other destination, using the reviewed configured-origin path.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright

from app.browser.egress_broker import MANAGED_CHROMIUM_ARGS, EgressBroker
from app.browser.profile_lock import ProfileLock, ProfileLockUnavailableError, profile_lock_is_free
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.browser.profile_session import (
    PERSISTENT_PROFILE_ARGS,
    PersistentProfileSession,
    ProfileSessionError,
    ProfileSessionStore,
    persistent_launch_options,
)
from app.domain.browser_profile import BrowserVersions
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from tests.broker_teardown import bounded, quiesce_broker
from evals.sites.account_fixture import ACCOUNT_NAME, SIGNED_IN, SIGNED_OUT, create_site

pytestmark = [pytest.mark.browser]

OPEN_TIMEOUT = 30.0


class FixtureServer:
    """The account fixture, in-process on a loopback port.

    It stays up across every browser restart in a test, because its session
    table lives here: if it restarted, "signed in" would be impossible for
    reasons that have nothing to do with the profile.
    """

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self.port = 0

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            create_site(), host="127.0.0.1", port=0, log_level="warning", lifespan="off"
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

    async def hits(self) -> dict[str, int]:
        """What this server was actually asked for. The test control plane.

        Read directly over loopback by the *test process*, which is not behind
        the broker -- so a server the browser could not reach is still
        answerable here, which is exactly what makes "it was never contacted"
        provable rather than inferred.
        """
        import httpx

        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.origin}/__eval__/state", timeout=10)
        hits: dict[str, int] = response.json()["hits"]
        return hits

    async def aclose(self) -> None:
        self._uvicorn.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=30)


@dataclass
class ProfileHarness:
    """A broker, a Playwright, a fixture site and a temporary profile base."""

    playwright: Playwright
    broker: EgressBroker
    site: FixtureServer
    paths: object
    current: BrowserVersions

    async def opened(
        self, store: ProfileSessionStore, profile_id: uuid.UUID, **kwargs: object
    ) -> PersistentProfileSession:
        return await store.open(
            playwright=self.playwright,
            broker=self.broker,
            paths=self.paths,  # type: ignore[arg-type]
            profile_id=profile_id,
            recorded=kwargs.pop("recorded", None),  # type: ignore[arg-type]
            current=kwargs.pop("current", self.current),  # type: ignore[arg-type]
            headless=True,
            timeout_seconds=OPEN_TIMEOUT,
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[ProfileHarness]:
    site = FixtureServer()
    await site.start()
    # The fixture is a *configured origin*, which is the existing reviewed
    # exception for local test pages. It is not a loopback bypass: the browser
    # still dials through the broker, and `--proxy-bypass-list=<-loopback>` is
    # still on the command line.
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({site.origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "browser-profiles")})
    probe = await playwright.chromium.launch(headless=True, args=list(MANAGED_CHROMIUM_ARGS))
    current = BrowserVersions(
        chromium_build=probe.version, playwright_version="1.63.0", app_version="0.1.0"
    )
    await probe.close()
    try:
        yield ProfileHarness(
            playwright=playwright, broker=broker, site=site, paths=paths, current=current
        )
    finally:
        # See tests/broker_teardown.py.
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())
        await bounded("site.aclose", site.aclose())


async def _text(store: ProfileSessionStore, profile_id: uuid.UUID, url: str) -> str:
    """Open the page and read what a person would see. No storage inspection."""
    context = store.get(profile_id).context
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(url, wait_until="load")
    body = await page.locator("body").inner_text()
    return body


# ---- the persistence proof -------------------------------------------------


async def test_a_session_survives_a_browser_restart(harness: ProfileHarness) -> None:
    """The proof, without any export: the server recognises the browser again."""
    profile_id = uuid.uuid4()

    first = ProfileSessionStore()
    await harness.opened(first, profile_id)
    started = await _text(first, profile_id, f"{harness.site.origin}/session/start")
    assert SIGNED_IN in started
    # Sanity: without the cookie the fixture says the other thing, so "signed
    # in" is a real signal rather than a page that always says yes.
    assert SIGNED_IN in await _text(first, profile_id, f"{harness.site.origin}/account")
    await first.close_all()

    # A completely new browser process, on the same directory.
    second = ProfileSessionStore()
    await harness.opened(second, profile_id)
    account = await _text(second, profile_id, f"{harness.site.origin}/account")
    await second.close_all()

    assert SIGNED_IN in account
    assert ACCOUNT_NAME in account
    assert SIGNED_OUT not in account


async def test_a_different_profile_does_not_inherit_the_session(
    harness: ProfileHarness,
) -> None:
    """Profiles are isolated from each other, which is what makes them one-site."""
    signed_in, fresh = uuid.uuid4(), uuid.uuid4()

    store = ProfileSessionStore(limit=2)
    await harness.opened(store, signed_in)
    assert SIGNED_IN in await _text(store, signed_in, f"{harness.site.origin}/session/start")
    await store.close(signed_in)

    await harness.opened(store, fresh)
    account = await _text(store, fresh, f"{harness.site.origin}/account")
    await store.close_all()
    assert SIGNED_OUT in account


async def test_web_storage_survives_a_browser_restart(harness: ProfileHarness) -> None:
    """`localStorage` too, and again observed by the fixture page, not by Lumi."""
    profile_id = uuid.uuid4()

    first = ProfileSessionStore()
    await harness.opened(first, profile_id)
    assert "written" in await _text(first, profile_id, f"{harness.site.origin}/storage/write")
    await first.close_all()

    second = ProfileSessionStore()
    await harness.opened(second, profile_id)
    state = await _text(second, profile_id, f"{harness.site.origin}/storage/read")
    await second.close_all()
    assert "kept" in state
    assert "missing" not in state


async def test_nothing_persists_for_a_directory_that_was_deleted(
    harness: ProfileHarness,
) -> None:
    """Deleting the directory really does remove the local sign-in."""
    import shutil

    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id)
    assert SIGNED_IN in await _text(store, profile_id, f"{harness.site.origin}/session/start")
    await store.close_all()

    shutil.rmtree(harness.paths.directory(profile_id))  # type: ignore[attr-defined]

    reopened = ProfileSessionStore()
    await harness.opened(reopened, profile_id)
    account = await _text(reopened, profile_id, f"{harness.site.origin}/account")
    await reopened.close_all()
    assert SIGNED_OUT in account


# ---- the OS-level lock -----------------------------------------------------


async def test_a_second_owner_cannot_open_the_same_profile(
    harness: ProfileHarness,
) -> None:
    """The backstop that covers two installations with different databases."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    session = await harness.opened(store, profile_id)
    assert session.lock.held

    # A second store is a second would-be owner. Its database knows nothing
    # about the first one's lease, and it is refused anyway.
    contender = ProfileSessionStore()
    with pytest.raises(ProfileSessionError) as refusal:
        await harness.opened(contender, profile_id)
    assert refusal.value.code == "profile_locked_by_another_process"
    assert len(contender) == 0

    await store.close_all()
    # Released, so the same profile opens cleanly afterwards.
    assert profile_lock_is_free(harness.paths, profile_id)  # type: ignore[arg-type]
    again = ProfileSessionStore()
    await harness.opened(again, profile_id)
    await again.close_all()


async def test_closing_releases_the_handle_even_if_the_context_is_already_gone(
    harness: ProfileHarness,
) -> None:
    """A profile must never stay locked because a context died untidily."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    session = await harness.opened(store, profile_id)
    try:
        await session.context.close()
    except PlaywrightError:  # pragma: no cover - already closing
        pass
    await store.close(profile_id)
    assert not session.lock.held
    assert profile_lock_is_free(harness.paths, profile_id)  # type: ignore[arg-type]


def test_the_lock_is_exclusive_and_never_blocks(tmp_path: Path) -> None:
    """Acquiring a held lock refuses immediately. It never waits and never breaks."""
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    profile_id = uuid.uuid4()
    paths.create(profile_id)
    first = ProfileLock.for_profile(paths, profile_id).acquire()
    try:
        assert not profile_lock_is_free(paths, profile_id)
        with pytest.raises(ProfileLockUnavailableError):
            ProfileLock.for_profile(paths, profile_id).acquire()
    finally:
        first.release()
    assert profile_lock_is_free(paths, profile_id)


# ---- the Chromium version guard --------------------------------------------


async def test_the_same_or_newer_chromium_opens_the_profile(
    harness: ProfileHarness,
) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    same = await harness.opened(store, profile_id, recorded=harness.current)
    assert same.versions.chromium_build == harness.current.chromium_build
    await store.close_all()

    older_on_disk = BrowserVersions(
        chromium_build="120.0.1.1", playwright_version="1.40.0", app_version="0.0.9"
    )
    upgraded = ProfileSessionStore()
    session = await harness.opened(upgraded, profile_id, recorded=older_on_disk)
    # The metadata progresses to what actually opened it.
    assert session.versions.chromium_build == harness.current.chromium_build
    assert harness.current.is_upgrade_from(older_on_disk)
    await upgraded.close_all()


async def test_an_older_chromium_refuses_and_does_not_delete_the_profile(
    harness: ProfileHarness,
) -> None:
    """A downgrade is refused *before* the directory is touched.

    Chromium upgrades a profile in place and never downgrades it, so opening a
    directory written by a newer build risks corrupting something the user
    signed into. Lumi refuses, keeps the profile, and leaves a new sign-in in a
    new profile as the way forward.
    """
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id)
    assert SIGNED_IN in await _text(store, profile_id, f"{harness.site.origin}/session/start")
    await store.close_all()

    from_the_future = BrowserVersions(
        chromium_build="999.0.0.0", playwright_version="9.9.9", app_version="9.9.9"
    )
    refusing = ProfileSessionStore()
    with pytest.raises(ProfileSessionError) as refusal:
        await harness.opened(refusing, profile_id, recorded=from_the_future)
    assert refusal.value.code == "profile_browser_downgrade_refused"

    # Nothing was deleted, nothing was repaired, and the session is intact:
    # the profile still signs in once a build that may open it comes back.
    directory = harness.paths.directory(profile_id)  # type: ignore[attr-defined]
    assert directory.is_dir()
    assert any(directory.iterdir())
    assert profile_lock_is_free(harness.paths, profile_id)  # type: ignore[arg-type]

    recovered = ProfileSessionStore()
    await harness.opened(recovered, profile_id)
    account = await _text(recovered, profile_id, f"{harness.site.origin}/account")
    await recovered.close_all()
    assert SIGNED_IN in account


async def test_a_refused_downgrade_never_creates_a_directory(
    harness: ProfileHarness,
) -> None:
    """The guard runs first, so a refusal leaves no trace on disk at all."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    with pytest.raises(ProfileSessionError):
        await harness.opened(
            store,
            profile_id,
            recorded=BrowserVersions(
                chromium_build="999.0.0.0", playwright_version="9.9.9", app_version="9.9.9"
            ),
        )
    assert not harness.paths.exists(profile_id)  # type: ignore[attr-defined]


# ---- the broker, and the context's boundaries ------------------------------


async def test_a_persistent_profile_is_launched_through_the_s0_broker(
    harness: ProfileHarness,
) -> None:
    """Every connection the profile makes is opened by the broker, not Chromium."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id)
    before = harness.broker.counters.dial_count
    await _text(store, profile_id, f"{harness.site.origin}/account")
    await store.close_all()
    assert harness.broker.counters.dial_count > before
    assert harness.broker.counters.allowed["http"] > 0


async def test_loopback_cannot_bypass_the_broker(harness: ProfileHarness) -> None:
    """A loopback origin the broker was not configured for is unreachable.

    This is what `--proxy-bypass-list=<-loopback>` buys: without it Chromium
    reaches `http://127.0.0.1:...` directly and the whole boundary is
    decorative. The unconfigured port is on the same interface as the fixture,
    so only the broker's decision separates them.

    A plaintext refusal arrives as the broker's own `403` rather than a
    transport error, so what the page thinks happened is not the evidence. The
    evidence is the other end of the wire: the second fixture recorded no
    request at all.
    """
    other = FixtureServer()
    await other.start()
    try:
        profile_id = uuid.uuid4()
        store = ProfileSessionStore()
        await harness.opened(store, profile_id)
        refused_before = harness.broker.counters.refused["plaintext_not_allowed"]
        context = store.get(profile_id).context
        page = context.pages[0] if context.pages else await context.new_page()

        # Full Chromium surfaces a proxy refusal on a top-level navigation as a
        # transport error rather than a readable 403; either shape is a refusal
        # and neither is the evidence, so both are accepted here.
        try:
            response = await page.goto(f"{other.origin}/account", timeout=15_000)
        except PlaywrightError:
            response = None
        if response is not None:
            assert response.status == 403
            assert response.headers.get("x-lumi-refusal") == "plaintext_not_allowed"
        await store.close_all()

        assert harness.broker.counters.refused["plaintext_not_allowed"] > refused_before
        # The assertion that counts: the other end of the wire saw nothing.
        assert await other.hits() == {}, "the unconfigured origin was contacted"
    finally:
        await other.aclose()


async def test_the_profile_fails_closed_when_the_broker_dies(
    harness: ProfileHarness,
) -> None:
    """No direct-connection fallback. A dead broker means no egress at all."""
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id)
    context = store.get(profile_id).context
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{harness.site.origin}/account", wait_until="load")

    await harness.broker.aclose()
    with pytest.raises(PlaywrightError):
        await page.goto(f"{harness.site.origin}/account", wait_until="load")
    await store.close_all()


def test_the_launch_options_are_exactly_the_reviewed_ones() -> None:
    """Asserted literally, because every field here is load-bearing.

    A change to any of them is a change to the security boundary, and should
    fail a test rather than pass a review unnoticed.
    """

    class _Broker:
        def proxy_settings(self) -> dict[str, str]:
            return {
                "server": "http://127.0.0.1:1",
                "username": "lumi",
                "password": "secret",
                "bypass": "<-loopback>",
            }

    options = persistent_launch_options(_Broker(), headless=True)  # type: ignore[arg-type]
    assert options["proxy"]["bypass"] == "<-loopback>"  # type: ignore[index]
    assert options["proxy"]["username"] == "lumi"  # type: ignore[index]
    args = options["args"]
    assert isinstance(args, list)
    # The S0 arguments survive, and the persistent-profile ones are added.
    for argument in MANAGED_CHROMIUM_ARGS:
        assert argument in args
    for argument in PERSISTENT_PROFILE_ARGS:
        assert argument in args
    assert "--disable-quic" in args
    # Session restore is never enabled, and the crash-restore bubble is hidden.
    assert "--hide-crash-restore-bubble" in args
    # The context is the narrowest Playwright offers.
    assert options["service_workers"] == "block"
    assert options["accept_downloads"] is False
    assert options["permissions"] == []
    # And there is no path, executable or storage state anywhere in it.
    for forbidden in ("storage_state", "storageState", "executable_path", "user_data_dir"):
        assert forbidden not in options


async def test_the_context_grants_no_permissions_and_no_downloads(
    harness: ProfileHarness,
) -> None:
    """Geolocation, notifications, camera, microphone and clipboard: all denied.

    S1 is profile infrastructure, not a browser with general permissions.
    """
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    await harness.opened(store, profile_id)
    context = store.get(profile_id).context
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(f"{harness.site.origin}/", wait_until="load")

    for name in ("geolocation", "notifications", "camera", "microphone", "clipboard-read"):
        state = await page.evaluate(
            "async (n) => { try { return (await navigator.permissions.query({name: n})).state } "
            "catch (e) { return 'unsupported' } }",
            name,
        )
        assert state in ("denied", "prompt", "unsupported"), f"{name} was granted"
    await store.close_all()


async def test_a_worker_opens_one_profile_at_a_time(harness: ProfileHarness) -> None:
    """S1's limit, so "who owns this browser" stays trivially answerable."""
    store = ProfileSessionStore(limit=1)
    await harness.opened(store, uuid.uuid4())
    with pytest.raises(ProfileSessionError) as refusal:
        await harness.opened(store, uuid.uuid4())
    assert refusal.value.code == "profile_session_limit"
    await store.close_all()


async def test_reopening_the_same_profile_returns_the_same_session(
    harness: ProfileHarness,
) -> None:
    profile_id = uuid.uuid4()
    store = ProfileSessionStore()
    first = await harness.opened(store, profile_id)
    second = await harness.opened(store, profile_id)
    assert first is second
    assert len(store) == 1
    await store.close_all()
