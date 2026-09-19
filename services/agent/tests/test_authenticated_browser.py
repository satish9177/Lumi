"""Authenticated account reading (Milestone 8a S3), against a real Chromium.

Worker-level, at exactly the level `test_login_takeover_browser.py` works: a
real persistent profile, a real S0 broker, and the synthetic account fixture --
now driven through the five `ACCOUNT_READ` operations instead of a human.

**Site-scope testing.** As in S2, every fixture runs on a bare loopback IP,
which has no registrable domain by construction, so `site_scope.in_site` -- the
one function every caller reaches through its module attribute -- is replaced
with a `host:port` comparison. Same code shape, same call sites, substituting
only the identity function two loopback fixtures can actually have. The PSL
comparison itself is covered 46 ways by `test_public_suffix.py`.

Sign-in happens *before* the read session exists, in the profile's own first
tab, exactly as a completed S2 takeover would have left it. From then on
nothing here sets, reads or copies a cookie: the server is asked what it sees.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.async_api import Playwright, async_playwright

from app.browser import site_scope
from app.browser.authenticated_session import AuthenticatedReadSession
from app.browser.egress_broker import EgressBroker
from app.browser.operations import authenticated as ops
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.browser.profile_session import ProfileSessionStore
from app.browser.protocol import OperationStatus
from app.browser.registry import OperationContext, OperationResult
from app.domain.authenticated import AuthenticatedObservation, WorkerReadResult
from app.domain.browser_profile import BrowserVersions
from app.domain.login_takeover import hash_identity
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from evals.sites.account_fixture import (
    ACCOUNT_ID,
    PLANTED_CARD,
    PLANTED_EMAIL,
    PLANTED_LONG_ID,
    PLANTED_PHONE,
    PRIVATE_MARKER,
    SECOND_ACCOUNT_ID,
    create_site,
)
from tests.broker_teardown import bounded, quiesce_broker

pytestmark = [pytest.mark.browser]

OPEN_TIMEOUT = 30.0
FINGERPRINT = hash_identity(ACCOUNT_ID)


class Fixture:
    """One account-fixture instance, in-process on a loopback port."""

    def __init__(
        self, *, external_origin: str | None = None, cdn_origin: str | None = None
    ) -> None:
        self._task: "asyncio.Task[None] | None" = None
        self.port = 0
        self._external = external_origin
        self._cdn = cdn_origin

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            create_site(external_origin=self._external, cdn_origin=self._cdn),
            host="127.0.0.1", port=0, log_level="warning", lifespan="off",
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
        return f"127.0.0.1:{self.port}"

    async def effects(self) -> dict[str, Any]:
        # Async on purpose: the fixture runs on this same event loop, so a
        # blocking client here would deadlock against the server it is asking.
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.origin}/__eval__/state")
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    async def reset(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            (await client.post(f"{self.origin}/__eval__/reset")).raise_for_status()

    async def aclose(self) -> None:
        self._uvicorn.should_exit = True
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=30)


def _in_site_by_host_and_port(url: str, site: str) -> bool:
    parsed = urlsplit(url)
    return bool(parsed.hostname) and f"{parsed.hostname}:{parsed.port}" == site


@pytest.fixture(autouse=True)
def _host_port_site_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_scope, "in_site", _in_site_by_host_and_port)


@dataclass
class Rig:
    playwright: Playwright
    broker: EgressBroker
    site: Fixture
    cdn: Fixture
    external: Fixture
    paths: object
    current: BrowserVersions
    store: ProfileSessionStore
    profile_id: uuid.UUID
    sequence: int = 0

    @property
    def test_origins(self) -> frozenset[str]:
        return frozenset({self.site.origin, self.cdn.origin, self.external.origin})

    async def read_session(self) -> AuthenticatedReadSession:
        return await self.store.read_session(
            self.profile_id, site=self.site.site, test_origins=self.test_origins
        )

    def context(self, read: AuthenticatedReadSession, tab: str = "t1") -> OperationContext:
        return OperationContext(
            page=read.tabs[tab].page,
            origin="",
            dispatch_id=uuid.uuid4(),
            observation_id=uuid.uuid4(),
            authenticated_session=read,
        )

    def common(self, **extra: Any) -> dict[str, Any]:
        self.sequence += 1
        return {
            "sequence": self.sequence,
            "site": self.site.site,
            "expected_account_fingerprint": FINGERPRINT,
            **extra,
        }

    async def sign_in(self, then: str = "/app") -> None:
        """Do what a completed S2 takeover left behind: a session in the profile."""
        session = self.store.get(self.profile_id)
        page = session.context.pages[0]
        await page.goto(f"{self.site.origin}/session/start", wait_until="load")
        if then:
            await page.goto(f"{self.site.origin}{then}", wait_until="load")

    async def observe(self, read: AuthenticatedReadSession, tab: str = "t1") -> OperationResult:
        return await ops.authenticated_observe(
            self.context(read, tab), ops.ObserveInput(**self.common(tab=tab))
        )

    async def follow(
        self, read: AuthenticatedReadSession, observation: AuthenticatedObservation, text: str,
        tab: str = "t1",
    ) -> OperationResult:
        ref = link_ref(observation, text)
        return await ops.authenticated_navigate(
            self.context(read, tab),
            ops.NavigateInput(
                **self.common(
                    tab=tab, target_ref=ref, expected_document_epoch=observation.document_epoch
                )
            ),
        )


def parsed(result: OperationResult) -> WorkerReadResult:
    assert result.status is OperationStatus.OK, (result.status, result.error_code)
    return WorkerReadResult.model_validate(result.observation["result"])


def observation_of(result: OperationResult) -> AuthenticatedObservation:
    observation = parsed(result).observation
    assert observation is not None, result.observation
    return observation


def link_ref(observation: AuthenticatedObservation, text: str) -> str:
    for link in observation.links:
        if text in link.text:
            return link.id
    raise AssertionError(f"no link labelled {text!r}: {[link.text for link in observation.links]}")


def joined(observation: AuthenticatedObservation) -> str:
    return "\n".join(block.text for block in observation.blocks)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    external = Fixture()
    await external.start()
    cdn = Fixture()
    await cdn.start()
    site = Fixture(external_origin=external.origin, cdn_origin=cdn.origin)
    await site.start()
    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({site.origin, cdn.origin, external.origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path / "browser-profiles")})
    probe = await playwright.chromium.launch(headless=True)
    current = BrowserVersions(
        chromium_build=probe.version, playwright_version="1.63.0", app_version="0.1.0"
    )
    await probe.close()
    store = ProfileSessionStore()
    profile_id = uuid.uuid4()
    await store.open(
        playwright=playwright, broker=broker, paths=paths,
        profile_id=profile_id, recorded=None, current=current, headless=True,
        timeout_seconds=OPEN_TIMEOUT,
    )
    try:
        yield Rig(
            playwright=playwright, broker=broker, site=site, cdn=cdn, external=external,
            paths=paths, current=current, store=store, profile_id=profile_id,
        )
    finally:
        await bounded("store.close_all", store.close_all())
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())
        for fixture in (site, cdn, external):
            await bounded("fixture.aclose", fixture.aclose())


# ---- reading, and what is projected -----------------------------------------------


async def test_an_account_page_is_read_as_a_redacted_bounded_observation(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    text = joined(home)
    assert "lumi-notes" in text and "Private" in text
    assert home.classification == "account_private"
    assert home.provenance == "untrusted_environment"
    assert home.tab == "t1" and home.document_epoch >= 1
    # No address anywhere in the observation: a host, never a URL.
    assert "http://" not in home.model_dump_json() and "https://" not in home.model_dump_json()
    assert home.host == "127.0.0.1"


async def test_billing_identifiers_are_redacted_before_they_leave_the_worker(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    billing = observation_of(await rig.follow(read, home, "Billing"))
    text = joined(billing)
    for planted in (PLANTED_EMAIL, PLANTED_PHONE, PLANTED_LONG_ID, PLANTED_CARD):
        assert planted not in text
        assert planted not in billing.model_dump_json()
    assert "⟦email:1⟧" in text
    assert "⟦phone:1⟧" in text
    assert "⟦digits:2345⟧" in text
    assert "⟦card:4242⟧" in text
    # Ordinary quantities and dates are still there.
    assert "Seats: 5" in text and "2026-09-20" in text
    assert billing.redactions == {"email": 1, "phone": 1, "digits": 1, "card": 1}


async def test_an_ordinary_number_survives_redaction(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    organization = observation_of(await rig.follow(read, home, "Organization"))
    assert "There are 17 private repositories." in joined(organization)
    assert PRIVATE_MARKER in joined(organization)


async def test_the_text_budget_bounds_what_is_returned(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = await ops.authenticated_navigate(
        rig.context(read),
        ops.NavigateInput(
            **rig.common(
                tab="t1",
                target_ref=link_ref(home, "Ledger"),
                expected_document_epoch=home.document_epoch,
                max_text_chars=500,
                max_blocks=5,
            )
        ),
    )
    ledger = observation_of(result)
    assert len(ledger.blocks) <= 5
    assert sum(len(block.text) for block in ledger.blocks) <= 500
    assert ledger.truncated is True


# ---- the gate: credential surface, then identity, before any text ---------------------


async def test_a_credential_surface_returns_signals_only(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = parsed(await rig.follow(read, home, "Reauthenticate"))
    assert result.observation is None and result.identity is None
    assert result.credential_surface is not None
    assert [signal.value for signal in result.credential_surface.signals] == ["PASSWORD_FIELD"]
    dumped = result.model_dump_json()
    assert "Confirm your password" not in dumped
    assert "title" not in dumped and "blocks" not in dumped and "links" not in dumped


async def test_an_expired_session_lands_on_a_login_surface_and_returns_signals_only(
    rig: Rig,
) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    # The browser keeps its cookie; the *server* stops recognising it, exactly
    # as a real session expiry looks. The fixture drops every session it issued.
    await rig.site.reset()
    result = parsed(await rig.follow(read, home, "Home"))
    assert result.credential_surface is not None
    assert result.observation is None
    assert "Repositories" not in result.model_dump_json()


async def test_an_account_switch_is_reported_before_any_text(rig: Rig) -> None:
    await rig.sign_in("/switch-account")
    read = await rig.read_session()
    await read.tabs["t1"].page.goto(f"{rig.site.origin}/app")
    result = parsed(await rig.observe(read))
    assert result.identity is not None and result.identity.kind == "account_changed"
    assert result.observation is None and result.credential_surface is None
    assert SECOND_ACCOUNT_ID not in result.model_dump_json()
    assert hash_identity(SECOND_ACCOUNT_ID) not in result.model_dump_json()


async def test_a_page_with_no_identity_signal_is_unknown_not_assumed(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = parsed(await rig.follow(read, home, "Unidentified"))
    assert result.identity is not None and result.identity.kind == "account_identity_unknown"
    assert result.observation is None


# ---- scope: same site, no redirects out, no navigation out ----------------------------


async def test_an_external_link_is_not_offered_as_a_ref(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    assert all(link.text != "External site" for link in home.links)
    assert home.total_link_count >= len(home.links)
    assert rig.external.origin.split("//")[1] not in home.model_dump_json()


async def test_a_redirect_out_of_the_site_is_refused_and_never_followed(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    before = (await rig.external.effects())["hits"].get("/landed", 0)
    result = await rig.follow(read, home, "Off-site redirect")
    assert result.error_code == "left_site_scope"
    assert (await rig.external.effects())["hits"].get("/landed", 0) == before


async def test_a_same_site_redirect_is_followed_after_validation(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    landed = observation_of(await rig.follow(read, home, "Redirect"))
    assert "There are 17 private repositories." in joined(landed)


async def test_a_script_that_moves_the_page_off_site_is_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = await rig.follow(read, home, "Scripted move")
    assert result.error_code == "left_site_scope" or (
        result.status is OperationStatus.OK and parsed(result).observation is not None
    )
    # Whether the script fired before or after the read, the destination was
    # never contacted.
    assert (await rig.external.effects())["hits"].get("/landed", 0) == 0
    follow_up = await rig.observe(read)
    assert follow_up.error_code in (None, "left_site_scope")
    assert (await rig.external.effects())["hits"].get("/landed", 0) == 0


async def test_a_third_party_public_subresource_loads(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    assert "cdn-loaded" in joined(home)
    assert (await rig.cdn.effects())["hits"].get("/cdn/app.js", 0) >= 1


# ---- methods, sockets, workers, downloads, popups --------------------------------------


async def test_get_and_head_work_and_every_other_method_is_blocked(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    inbox = observation_of(await rig.follow(read, home, "Inbox"))
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in joined(inbox)
    await asyncio.sleep(0.5)
    effects = (await rig.site.effects())
    assert effects["ping_head"] >= 1  # HEAD reached the site.
    assert effects["mutations"] == 0  # POST and PUT never did.
    assert read.guard.blocked_methods["POST"] >= 1
    assert read.guard.blocked_methods["PUT"] >= 1


async def test_websockets_and_service_workers_are_blocked(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    await rig.follow(read, home, "Inbox")
    await asyncio.sleep(0.5)
    effects = (await rig.site.effects())
    assert effects["ws_connects"] == 0
    assert effects["sw_fetches"] == 0
    assert read.guard.blocked["websocket_blocked"] >= 1


async def test_a_download_is_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = await rig.follow(read, home, "Export")
    assert result.error_code == "download_blocked"


async def test_a_popup_is_never_adopted(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    page = read.tabs["t1"].page
    await page.evaluate("() => window.open('/app/popup')")
    await page.wait_for_timeout(500)
    assert read.guard.blocked["popup_blocked"] >= 1
    assert len(read.context.pages) == 1
    assert read.open_tabs == ["t1"]


async def test_private_loopback_and_metadata_destinations_stay_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    page = read.tabs["t1"].page
    for url in ("https://10.0.0.5/", "https://169.254.169.254/", "http://127.0.0.1:1/"):
        outcome = await page.evaluate(
            "async (u) => { try { await fetch(u); return 'sent'; } catch (e) { return 'refused'; } }",
            url,
        )
        assert outcome == "refused", url


async def test_a_dead_broker_does_not_fall_back_to_direct_networking(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    before = (await rig.site.effects())["hits"].get("/app/organization", 0)
    # `aclose` alone waits for Chromium's idle keep-alive connections (Python
    # 3.12's `wait_closed`), so the relays are cancelled the way a dying broker
    # would drop them: the browser then has nothing but a dead proxy.
    closing = asyncio.create_task(rig.broker.aclose())
    await asyncio.sleep(0.5)
    for task in list(rig.broker._connections):
        task.cancel()
    await asyncio.wait_for(closing, 20)
    result = await rig.follow(read, home, "Organization")
    assert result.status is not OperationStatus.OK or parsed(result).observation is None
    assert (await rig.site.effects())["hits"].get("/app/organization", 0) == before


# ---- refs, epochs, tabs, reveal, history ------------------------------------------------


async def test_a_stale_link_ref_is_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    await rig.follow(read, home, "Organization")
    stale = await ops.authenticated_navigate(
        rig.context(read),
        ops.NavigateInput(
            **rig.common(
                tab="t1", target_ref=link_ref(home, "Billing"),
                expected_document_epoch=home.document_epoch,
            )
        ),
    )
    assert stale.error_code == "stale_document_epoch"
    assert stale.status is OperationStatus.FAILED_BEFORE_EFFECT


async def test_an_unknown_link_ref_is_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    result = await ops.authenticated_navigate(
        rig.context(read),
        ops.NavigateInput(
            **rig.common(tab="t1", target_ref="l99", expected_document_epoch=home.document_epoch)
        ),
    )
    assert result.error_code == "unknown_target_ref"


async def test_reveal_scrolls_a_block_or_a_link_into_view_without_a_key_press(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    ledger = observation_of(await rig.follow(read, home, "Ledger"))
    page = read.tabs["t1"].page
    keys: list[str] = []
    await page.expose_function("recordKey", lambda key: keys.append(key))
    await page.evaluate("() => document.addEventListener('keydown', (e) => recordKey(e.key))")
    last_block = ledger.blocks[-1]
    revealed = observation_of(await ops.authenticated_reveal(
        rig.context(read),
        ops.RevealInput(
            **rig.common(
                tab="t1", target_kind="block", target_ref=last_block.id,
                expected_document_epoch=ledger.document_epoch,
            )
        ),
    ))
    assert revealed.tab == "t1"
    assert await page.evaluate("() => window.scrollY") > 0
    assert keys == []
    with_link = observation_of(await ops.authenticated_reveal(
        rig.context(read),
        ops.RevealInput(
            **rig.common(
                tab="t1", target_kind="link", target_ref=ledger.links[0].id,
                expected_document_epoch=revealed.document_epoch,
            )
        ),
    ))
    assert with_link.document_epoch == revealed.document_epoch


async def test_reveal_with_a_stale_ref_is_refused(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    await rig.follow(read, home, "Ledger")
    result = await ops.authenticated_reveal(
        rig.context(read),
        ops.RevealInput(
            **rig.common(
                tab="t1", target_kind="block", target_ref="b1",
                expected_document_epoch=home.document_epoch,
            )
        ),
    )
    assert result.error_code == "stale_document_epoch"


async def test_history_back_returns_to_the_previous_document(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    home = observation_of(await rig.observe(read))
    await rig.follow(read, home, "Organization")
    back = observation_of(await ops.authenticated_history(
        rig.context(read), ops.HistoryInput(**rig.common(tab="t1", direction="back"))
    ))
    assert "Your repositories" in joined(back)


async def test_tabs_open_close_and_stay_within_their_budget(rig: Rig) -> None:
    await rig.sign_in("/app")
    read = await rig.read_session()
    opened = observation_of(await ops.authenticated_tab(
        rig.context(read), ops.TabInput(**rig.common(action="open"))
    ))
    assert opened.kind == "tab_state" and opened.open_tabs == ["t1", "t2"]
    third = observation_of(await ops.authenticated_tab(
        rig.context(read), ops.TabInput(**rig.common(action="open"))
    ))
    assert third.open_tabs == ["t1", "t2", "t3"]
    fourth = await ops.authenticated_tab(rig.context(read), ops.TabInput(**rig.common(action="open")))
    assert fourth.error_code == "tab_budget_exhausted"
    closed = observation_of(await ops.authenticated_tab(
        rig.context(read), ops.TabInput(**rig.common(action="close", tab="t3"))
    ))
    assert closed.open_tabs == ["t1", "t2"]


async def test_a_read_session_is_refused_while_a_takeover_holds_the_profile(rig: Rig) -> None:
    from app.browser.profile_session import ProfileSessionError

    await rig.store.close(rig.profile_id)
    await rig.store.open(
        playwright=rig.playwright, broker=rig.broker, paths=rig.paths,  # type: ignore[arg-type]
        profile_id=rig.profile_id, recorded=None, current=rig.current, headless=False,
        timeout_seconds=OPEN_TIMEOUT,
    )
    assert await rig.store.start_takeover(
        rig.profile_id, site=rig.site.site, timeout_seconds=OPEN_TIMEOUT, scheme="http"
    ) == "OPEN"
    with pytest.raises(ProfileSessionError) as refused:
        await rig.read_session()
    assert refused.value.code == "profile_takeover_active"


async def test_a_second_site_is_refused_for_one_profile(rig: Rig) -> None:
    from app.browser.profile_session import ProfileSessionError

    await rig.sign_in("/app")
    await rig.read_session()
    with pytest.raises(ProfileSessionError) as refused:
        await rig.store.read_session(
            rig.profile_id, site="127.0.0.1:1", test_origins=rig.test_origins
        )
    assert refused.value.code == "profile_site_mismatch"
