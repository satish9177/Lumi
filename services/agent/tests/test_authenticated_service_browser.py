"""Authenticated account reading (Milestone 8a S3): the whole pipeline, real.

`test_authenticated_read.py` proves the runtime's authority against a scripted
worker. `test_authenticated_browser.py` proves the browser mechanics with no
database. This module is the one place everything is real at once: the real
`AuthenticatedReadService` and `BrowserProfileService`, backed by a real
PostgreSQL, talking over the worker's real HTTP contract to a real worker app
driving a real Chromium through the synthetic account fixture.

**Why the worker is in-process.** Site scope is judged by the pinned Public
Suffix List, which rightly refuses a loopback IP as a registrable domain, so a
fixture on `127.0.0.1` needs `site_scope.in_site` replaced with a host
comparison (exactly as the S2 tests replace `_site_scope`). That replacement has
to live in the *worker's* process, so the worker app runs here, under uvicorn,
in this test's process. Nothing else about it is different: its own broker, its
own Chromium, its own token-authenticated HTTP contract.

It is the S3 acceptance:

    a signed-in account (a real session in a real persistent profile)
        -> a trusted card -> the trusted click -> bounded GET-only reads
        -> redaction inside the worker -> redacted evidence in PostgreSQL
        -> a grounded, cited answer

with the extra assertions that need everything real: that no planted identifier
or marker is persisted anywhere it should not be, and that a read genuinely
changed state on the "website" (`read_count`) after -- and only after -- the
permission was given.
"""

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser import site_scope
from app.browser.config import WorkerSettings
from app.browser.session import generate_worker_token
from app.browser.worker import create_worker_app
from app.domain.action_status import AttemptOutcome
from app.domain.authenticated import (
    AuthenticatedStepEnvelope,
    PauseReason,
    parse_authenticated_step,
)
from app.domain.browser_profile import BrowserContextKind, ProfileStatus, allowed_origins_for
from app.domain.login_takeover import hash_identity
from app.domain.research import GrantStatus, ResearchAnswer, ResearchEvidence
from app.repositories.profiles import BrowserProfileRepository
from app.services.actions import ActionService
from app.services.authenticated_read import AuthenticatedReadService
from app.services.browser_execution import BrowserExecutionService, BrowserWorkerConfig
from app.services.browser_profiles import BrowserProfileService
from app.services.runtime import RuntimeGeneration
from app.services.tasks import TaskService
from evals.sites.account_fixture import (
    ACCOUNT_ID,
    CONTROL_CLASS_SECRET,
    CONTROL_ID_SECRET,
    CONTROL_NAME_SECRET,
    CURRENT_VALUE_SECRET,
    FORM_FIELD_SECRET_MARKER,
    OPTION_VALUE_SECRET,
    PLANTED_CARD,
    PLANTED_EMAIL,
    PLANTED_LONG_ID,
    PLANTED_PHONE,
    PRIVATE_MARKER,
    PRIVATE_REPOSITORIES,
    create_site,
)
from tests.broker_teardown import quiesce_broker

pytestmark = [pytest.mark.browser]

OBJECTIVE = "Which of my repositories are private?"
FINGERPRINT = hash_identity(ACCOUNT_ID)
PLANTED = (PLANTED_EMAIL, PLANTED_PHONE, PLANTED_LONG_ID, PLANTED_CARD)


def _in_site_by_host(url: str, site: str) -> bool:
    return urlsplit(url).hostname == site


@pytest.fixture(autouse=True)
def _host_site_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_scope, "in_site", _in_site_by_host)


async def bounded(name: str, coroutine: Any, *, timeout: float = 30) -> Any:
    """Run one teardown step and abandon it, without waiting, if it hangs.

    `asyncio.wait_for` waits for the cancelled task to finish, which never
    happens when Playwright swallows the cancellation of a killed worker's
    parked navigation.
    """
    task = asyncio.ensure_future(coroutine)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        return None
    return task.result()


class InProcessServer:
    """A FastAPI app under uvicorn, on this test's own event loop."""

    def __init__(self, app: Any, *, lifespan: Literal["on", "off"] = "off") -> None:
        import uvicorn

        self._app = app
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", lifespan=lifespan
        )
        self._server = uvicorn.Server(self._config)
        self._task: "asyncio.Task[None] | None" = None
        self.port = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(1_200):
            if self._server.started and self._server.servers:
                self.port = self._server.servers[0].sockets[0].getsockname()[1]
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("the server did not start")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def kill(self) -> None:
        """Drop every connection at once, the way a dying worker would.

        Does not wait for the shutdown to finish: a dead worker does not tidy
        up after itself, and `aclose` cleans up what is left afterwards.
        """
        self._server.force_exit = True
        self._server.should_exit = True
        for connection in list(self._server.server_state.connections):
            connection.transport.abort()
        await asyncio.sleep(0.5)

    async def aclose(self) -> None:
        # The worker's own shutdown waits on Chromium's idle keep-alive
        # connections to its broker (Python 3.12's `wait_closed`), which is the
        # Windows hang `tests/broker_teardown.py` documents. Quiesce first.
        state = getattr(self._app, "state", None)
        profiles = getattr(state, "profiles", None)
        broker = getattr(state, "broker", None)
        if profiles is not None:
            await bounded("worker profiles", profiles.close_all())
        if broker is not None:
            await quiesce_broker(broker)
        self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=40)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover - teardown noise.
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)


class Site:
    def __init__(self, server: InProcessServer) -> None:
        self.server = server

    async def effects(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.server.base_url}/__eval__/state")
        body: dict[str, Any] = response.json()
        return body

    async def reset(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{self.server.base_url}/__eval__/reset")


@dataclass
class World:
    engine: AsyncEngine
    site: Site
    worker: InProcessServer
    worker_app: Any
    service: AuthenticatedReadService
    profiles: BrowserProfileService
    tasks: TaskService
    actions: ActionService
    profile_id: uuid.UUID
    calls: int = 0

    def page(self) -> Any:
        return self.worker_app.state.profiles.get(self.profile_id).context.pages[0]

    async def goto(self, path: str) -> None:
        await self.page().goto(f"{self.site.server.base_url}{path}", wait_until="load")

    async def task(self) -> uuid.UUID:
        task = await self.tasks.create_task({
            "type": "authenticated_read", "classification": "account_private", "text": OBJECTIVE,
            "objective": OBJECTIVE, "source": "text", "profile_id": str(self.profile_id),
        })
        return task.id

    async def granted(self, recipient: str = "gemini") -> tuple[uuid.UUID, Any]:
        task_id = await self.task()
        view = await self.service.prepare(task_id, recipient=recipient)  # type: ignore[arg-type]
        assert view.grant is not None
        confirmed = await self.service.confirm(task_id, grant_id=view.grant.id, expected_revision=view.grant.revision)
        assert confirmed.grant is not None and confirmed.grant.status is GrantStatus.ACTIVE
        return task_id, confirmed.grant

    def envelope(self, step: dict[str, Any]) -> AuthenticatedStepEnvelope:
        self.calls += 1
        return AuthenticatedStepEnvelope(
            request_id=f"req-{self.calls:08d}", step=parse_authenticated_step(step), planner_calls=self.calls
        )

    async def step(self, task_id: uuid.UUID, step: dict[str, Any]) -> Any:
        return await self.service.execute_step(task_id, self.envelope(step))

    async def observe(self, task_id: uuid.UUID) -> Any:
        return await self.step(task_id, {"operation": "observe", "tab": "t1"})

    async def follow(self, task_id: uuid.UUID, label: str) -> Any:
        view = await self.service.describe(task_id)
        observation = view.observations[-1].observation
        ref = next(link.id for link in observation.links if label in link.text)
        return await self.step(task_id, {
            "operation": "navigate", "tab": observation.tab,
            "target": {"kind": "link", "observation": observation.ref, "ref": ref},
        })

    async def sql(self, sql: str) -> Any:
        async with self.engine.connect() as connection:
            return await connection.scalar(text(sql))


@pytest.fixture
async def world(
    engine: AsyncEngine, action_service: ActionService, task_service: TaskService,
    runtime_generation: RuntimeGeneration, tmp_path: Path,
) -> AsyncIterator[World]:
    cdn = InProcessServer(create_site())
    await cdn.start()
    site_server = InProcessServer(create_site(cdn_origin=f"http://127.0.0.1:{cdn.port}"))
    await site_server.start()
    token = generate_worker_token()
    settings = WorkerSettings(
        token=SecretStr(token.get_secret_value()), headless=True,
        auth_test_origins=f"{site_server.base_url},{cdn.base_url}",
        profile_root=str(tmp_path / "browser-profiles"),
    )
    worker_app = create_worker_app(settings)
    worker = InProcessServer(worker_app, lifespan="on")
    await worker.start()
    source = BrowserWorkerConfig(base_url=worker.base_url, token=token, timeout_seconds=60.0)
    browser = BrowserExecutionService(
        engine, actions=action_service, runtime_generation=runtime_generation.id, worker=source
    )
    profiles = BrowserProfileService(
        engine, runtime_generation=runtime_generation.id, browser=browser, paths=settings.profile_paths,
    )
    service = AuthenticatedReadService(
        engine, tasks=task_service, actions=action_service, runtime_generation=runtime_generation.id,
        worker=source, profiles=profiles, grant_ttl_seconds=600, step_ttl_seconds=120,
    )
    profile_id = uuid.uuid4()
    async with engine.begin() as connection:
        repository = BrowserProfileRepository(connection)
        await repository.create(
            profile_id=profile_id, label="Fixture - Personal", site="127.0.0.1",
            allowed_origins=allowed_origins_for("127.0.0.1"),
        )
    # A completed sign-in, as S2 leaves it: a session in the profile's own tab,
    # then the profile recorded as AUTHENTICATED with the account's fingerprint.
    await profiles.open_profile(profile_id, kind=BrowserContextKind.AUTHENTICATED_PROFILE, headed=False)
    live = World(
        engine=engine, site=Site(site_server), worker=worker, worker_app=worker_app, service=service,
        profiles=profiles, tasks=task_service, actions=action_service, profile_id=profile_id,
    )
    await live.goto("/session/start")
    await live.goto("/app")
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).mark_authenticated(
            profile_id=profile_id, account_fingerprint=FINGERPRINT
        )
    try:
        yield live
    finally:
        try:
            await bounded("profiles.close", profiles.close_profile(profile_id))
        except Exception:  # noqa: BLE001 - teardown must continue.
            pass
        for server in (worker, site_server, cdn):
            try:
                await bounded("server.aclose", server.aclose())
            except Exception:  # noqa: BLE001 - teardown must continue.
                pass
        # A worker killed mid-request leaves its handler parked in Playwright;
        # cancel what is left so the loop can close.
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for leftover in leftovers:
            leftover.cancel()
        if leftovers:
            await bounded("leftover tasks", asyncio.gather(*leftovers, return_exceptions=True), timeout=15)


# ---- the S3 acceptance -----------------------------------------------------------------------


async def test_which_of_my_repositories_are_private_is_answered_with_citations(world: World) -> None:
    before = await world.site.effects()
    task_id, grant = await world.granted()
    # Nothing was read before the permission: the trusted click is the first read.
    assert before["read_count"] == 0 and await world.sql("SELECT count(*) FROM browser_dispatches") == 0

    first = await world.observe(task_id)
    assert first.outcome is AttemptOutcome.SUCCEEDED and first.observation is not None
    text_blocks = [block.text for block in first.observation.observation.blocks]
    joined = "\n".join(text_blocks)
    for private in PRIVATE_REPOSITORIES:
        assert f"{private} - Private" in joined
    assert "cdn-loaded" in joined  # A third-party public subresource loaded.

    evidence = [
        ResearchEvidence(observation="o1", block=f"b{index + 1}", quote=value)
        for index, value in enumerate(text_blocks)
        if value.endswith("- Private")
    ]
    assert {item.quote.split(" - ")[0] for item in evidence} == set(PRIVATE_REPOSITORIES)
    view = await world.service.record_answer(
        task_id,
        answer=ResearchAnswer(
            status="answered", stop_reason="goal_reached",
            answer="Private: " + ", ".join(item.quote.split(" - ")[0] for item in evidence), evidence=evidence,
        ),
        provider="gemini", model="gemini-2.5-flash", planner_calls=1,
    )
    assert view.answer is not None and view.answer.classification == "account_private"
    assert view.grant is not None and view.grant.status is GrantStatus.COMPLETED
    # The dispatch was an account read on the profile, never a research session.
    assert await world.sql("SELECT effect FROM browser_dispatches LIMIT 1") == "ACCOUNT_READ"
    assert await world.sql("SELECT count(*) FROM research_sessions") == 0
    assert grant.id is not None


async def test_a_read_changes_state_on_the_website_only_after_the_permission(world: World) -> None:
    assert (await world.site.effects())["read_count"] == 0
    task_id, _ = await world.granted()
    assert (await world.site.effects())["read_count"] == 0
    home = await world.observe(task_id)
    assert home.observation is not None
    notifications = await world.follow(task_id, "Notifications")
    assert notifications.outcome is AttemptOutcome.SUCCEEDED
    # The honest claim: a GET changed server state, and Lumi did not suppress it.
    assert (await world.site.effects())["read_count"] == 1


async def test_no_identifier_is_persisted_anywhere_it_should_not_be(world: World) -> None:
    task_id, _ = await world.granted()
    await world.observe(task_id)
    billing = await world.follow(task_id, "Billing")
    assert billing.observation is not None
    body = "\n".join(block.text for block in billing.observation.observation.blocks)
    assert "⟦email:1⟧" in body and "⟦phone:1⟧" in body and "⟦card:4242⟧" in body and "⟦digits:2345⟧" in body
    assert "Seats: 5" in body and "2026-09-20" in body
    organization = await world.follow(task_id, "Organization")
    assert organization.observation is not None
    assert "There are 17 private repositories." in "\n".join(b.text for b in organization.observation.observation.blocks)

    for table in (
        "authenticated_observations", "action_attempts", "actions", "task_events", "browser_dispatches",
        "step_authorizations", "task_grants", "tasks", "browser_profiles",
    ):
        dumped = await world.sql(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
        for planted in PLANTED:
            assert planted not in dumped, (table, planted)
        assert not re.search(r"https?://", dumped) or table in ("browser_profiles", "task_grants")
    # The marker is real account text: it is stored in the private evidence table
    # and nowhere else.
    evidence = await world.sql(f"SELECT count(*) FROM authenticated_observations WHERE projection::text LIKE '%{PRIVATE_MARKER}%'")
    assert evidence == 1
    for table in ("research_observations", "research_answers", "task_events", "actions", "browser_dispatches", "action_attempts"):
        found = await world.sql(f"SELECT coalesce(string_agg(t::text, ' | '), '') FROM {table} t WHERE t::text LIKE '%{PRIVATE_MARKER}%'")
        assert found == "", (table, found[:600])


# ---- Milestone 8b S4: the form inventory is local evidence and nothing else ----------------------


async def test_the_form_inventory_is_stored_locally_and_reaches_no_other_surface(world: World) -> None:
    """S4 observes form structure only; and what it observes stays in one table."""
    from app.api.authenticated_schemas import AuthenticatedResponse

    await world.goto("/app/apply")
    task_id, _ = await world.granted()
    step = await world.observe(task_id)
    assert step.outcome is AttemptOutcome.SUCCEEDED and step.observation is not None
    observation = step.observation.observation
    assert observation.schema_version == 2 and observation.form_epoch >= 1
    assert any(e.accessible_name == "Full legal name" for e in observation.inventory.elements)

    # Persisted, explicitly versioned, in the account-private evidence table.
    assert await world.sql("SELECT schema_version FROM authenticated_observations") == 2
    assert await world.sql("SELECT form_epoch FROM authenticated_observations") == observation.form_epoch
    assert await world.sql(
        f"SELECT count(*) FROM authenticated_observations WHERE element_inventory::text LIKE '%{FORM_FIELD_SECRET_MARKER}%'"
    ) == 1
    assert await world.sql(
        "SELECT count(*) FROM authenticated_observations WHERE element_inventory::text LIKE '%Full legal name%'"
    ) == 1

    # The marker is in no other table -- not the public research tables, not the
    # ledger, not the events or dispatch results (diagnostics), not the profile.
    for table in (
        "research_observations", "research_answers", "research_sessions", "task_events", "actions",
        "action_attempts", "browser_dispatches", "step_authorizations", "task_grants", "tasks",
        "browser_profiles", "authenticated_answers", "task_events",
    ):
        found = await world.sql(
            f"SELECT coalesce(string_agg(t::text, ' | '), '') FROM {table} t WHERE t::text LIKE '%{FORM_FIELD_SECRET_MARKER}%'"
        )
        assert found == "", (table, found[:400])
    # Diagnostics carry counts and the epoch, never a name.
    attempts = await world.sql("SELECT coalesce(string_agg(result::text, ' '), '') FROM action_attempts")
    assert '"form_epoch"' in attempts and '"element_count"' in attempts
    assert "Full legal name" not in attempts and "Country" not in attempts

    # What the runtime hands to anything that talks to a provider is the S3 shape:
    # text and links. The inventory is not in it, so no provider prompt can carry it.
    view = await world.service.describe(task_id)
    wire = AuthenticatedResponse.from_view(view).model_dump_json()
    assert FORM_FIELD_SECRET_MARKER not in wire and "inventory" not in wire and "form_epoch" not in wire

    # Values, option values and DOM identity are in no table at all.
    for planted in (CURRENT_VALUE_SECRET, OPTION_VALUE_SECRET, CONTROL_ID_SECRET, CONTROL_NAME_SECRET, CONTROL_CLASS_SECRET):
        for table in ("authenticated_observations", "task_events", "browser_dispatches", "action_attempts", "actions"):
            found = await world.sql(
                f"SELECT count(*) FROM {table} t WHERE t::text LIKE '%{planted}%'"
            )
            assert found == 0, (table, planted)
    # Nothing was changed on the website by looking at its form.
    state = await world.site.effects()
    assert state["submissions"] == 0 and state["mutations"] == 0


# ---- pauses, against a real browser and a real ledger --------------------------------------------


async def test_session_expiry_pauses_for_login_and_stores_no_text(world: World) -> None:
    task_id, _ = await world.granted()
    await world.observe(task_id)
    await world.site.reset()  # The server stops recognising the browser's cookie.
    result = await world.follow(task_id, "Organization")
    assert result.pause_reason is PauseReason.LOGIN_REQUIRED and result.observation is None
    profile = await world.profiles.get_profile(world.profile_id)
    assert profile.status is ProfileStatus.NEEDS_LOGIN and profile.revoke_epoch == 0
    assert await world.sql("SELECT count(*) FROM authenticated_observations") == 1  # Only the first, pre-expiry read.
    view = await world.service.describe(task_id)
    assert view.pause_reason is PauseReason.LOGIN_REQUIRED
    dumped = await world.sql("SELECT coalesce(string_agg(result::text, ' '), '') FROM action_attempts")
    assert "Sign in" not in dumped and "Password" not in dumped  # the signal enum is allowed; page text is not


async def test_a_different_account_pauses_and_voids_the_grant(world: World) -> None:
    task_id, _ = await world.granted()
    # The browser now belongs to somebody else. (Done before the first read: once
    # the read guard is installed it refuses the redirect chain this needs.)
    await world.goto("/switch-account")
    await world.goto("/app")
    result = await world.observe(task_id)
    assert result.pause_reason is PauseReason.ACCOUNT_CHANGED and result.observation is None
    profile = await world.profiles.get_profile(world.profile_id)
    assert profile.revoke_epoch == 1 and profile.status is ProfileStatus.NEEDS_LOGIN
    assert profile.account_fingerprint is None
    view = await world.service.describe(task_id)
    assert view.grant is not None and view.grant.status is GrantStatus.REVOKED
    # Nothing from the second account was ever stored.
    assert await world.sql("SELECT count(*) FROM authenticated_observations") == 0


async def test_an_unidentifiable_page_pauses_and_sends_no_text(world: World) -> None:
    task_id, _ = await world.granted()
    home = await world.observe(task_id)
    assert home.observation is not None
    result = await world.follow(task_id, "Unidentified")
    assert result.pause_reason is PauseReason.ACCOUNT_IDENTITY_UNKNOWN and result.observation is None
    assert await world.sql("SELECT count(*) FROM authenticated_observations") == 1


async def test_a_credential_prompt_inside_the_account_pauses_for_login(world: World) -> None:
    task_id, _ = await world.granted()
    await world.observe(task_id)
    result = await world.follow(task_id, "Reauthenticate")
    assert result.pause_reason is PauseReason.LOGIN_REQUIRED
    assert await world.sql("SELECT count(*) FROM authenticated_observations WHERE title ILIKE '%password%'") == 0


# ---- a lost read: unknown, never retried -------------------------------------------------------------


async def test_a_worker_lost_mid_read_is_outcome_unknown_and_nothing_retries(world: World) -> None:
    task_id, _ = await world.granted()
    await world.observe(task_id)
    dispatches_before = await world.sql("SELECT count(*) FROM browser_dispatches")

    await _lost_body(world, task_id, dispatches_before)


async def _lost_body(world: World, task_id: uuid.UUID, dispatches_before: Any) -> None:
    slow = asyncio.create_task(world.follow(task_id, "Slow report"))
    await asyncio.sleep(3)  # The page is being served slowly; the worker is mid-navigation.
    await world.worker.kill()
    result = await asyncio.wait_for(slow, timeout=90)

    assert result.outcome is AttemptOutcome.OUTCOME_UNKNOWN
    assert await world.sql("SELECT count(*) FROM browser_dispatches") == dispatches_before + 1
    assert await world.sql("SELECT status FROM browser_dispatches ORDER BY started_at DESC LIMIT 1") == "OUTCOME_UNKNOWN"
    view = await world.service.describe(task_id)
    assert view.unresolved_step is True
    # Not retried, and only a fresh look may follow.
    assert await world.sql("SELECT count(*) FROM browser_dispatches") == dispatches_before + 1


# ---- the worker's own refusals, over its real HTTP contract ----------------------------------------------


async def test_the_worker_refuses_to_mix_research_sessions_and_profiles(world: World) -> None:
    worker = world.worker
    identity = None
    async with httpx.AsyncClient(timeout=30, base_url=worker.base_url) as client:
        token = world.worker_app.state.settings.token.get_secret_value()
        headers = {"x-lumi-worker-token": token}
        health = (await client.get("/health", headers=headers)).json()
        identity = health["worker_generation"]

        def request(**extra: Any) -> dict[str, Any]:
            return {
                "dispatch_id": str(uuid.uuid4()), "runtime_generation": str(uuid.uuid4()),
                "expected_worker_generation": identity, "action_id": str(uuid.uuid4()),
                "attempt_id": str(uuid.uuid4()), **extra,
            }

        common = {"sequence": 1, "site": "127.0.0.1", "expected_account_fingerprint": FINGERPRINT, "tab": "t1"}
        # A research session id where a profile is expected.
        mixed = await client.post("/v1/dispatch", headers=headers, json=request(
            operation="authenticated_observe", site="authenticated_read",
            session_id=str(uuid.uuid4()), profile_id=str(world.profile_id), input=common))
        assert mixed.status_code == 409 and mixed.json()["code"] == "session_kind_mismatch"
        # A profile id where a research session is expected.
        research = await client.post("/v1/dispatch", headers=headers, json=request(
            operation="research_observe", site="public_research",
            profile_id=str(world.profile_id), session_id=str(uuid.uuid4()), input={"sequence": 1, "tab": "t1"}))
        assert research.status_code == 409 and research.json()["code"] == "session_kind_mismatch"
        # No profile at all.
        missing = await client.post("/v1/dispatch", headers=headers, json=request(
            operation="authenticated_observe", site="authenticated_read", input=common))
        assert missing.status_code == 400 and missing.json()["code"] == "profile_required"
        # A profile this worker never opened.
        unknown = await client.post("/v1/dispatch", headers=headers, json=request(
            operation="authenticated_observe", site="authenticated_read",
            profile_id=str(uuid.uuid4()), input=common))
        assert unknown.status_code == 409 and unknown.json()["code"] == "unknown_profile_session"
        # An authenticated operation addressed to a reviewed site name.
        wrong_site = await client.post("/v1/dispatch", headers=headers, json=request(
            operation="authenticated_observe", site="public_research",
            profile_id=str(world.profile_id), input=common))
        assert wrong_site.status_code == 403
        # Fields no authenticated operation has.
        for extra in ({"url": "https://evil.example"}, {"selector": "a"}, {"provider": "openai"}):
            refused = await client.post("/v1/dispatch", headers=headers, json=request(
                operation="authenticated_observe", site="authenticated_read",
                profile_id=str(world.profile_id), input={**common, **extra}))
            assert refused.status_code == 422


async def test_no_authenticated_operation_can_take_a_screenshot_or_a_key_press() -> None:
    from pathlib import Path as P

    import app.browser.operations.authenticated as module

    source = P(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("screenshot", "keyboard", ".press(", ".type(", ".fill(", ".click(", "evaluate(", "set_input_files", "expect_download"):
        assert forbidden not in source, forbidden
