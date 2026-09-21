"""Milestone 9 S1: the runtime's desktop routes, persistence, retention and provider firewall.

Needs the shared test database. The disabled/unsupported/route-shape/validation tests are
platform-independent; the end-to-end tests drive the REAL isolated worker against the real
fixture window and are Windows-only.
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.managed import ManagedDesktopWorker
from app.main import create_app
from app.repositories.desktop import DesktopRepository
from app.services.desktop import RETAINED_OBSERVATIONS, RETAINED_SECONDS, DesktopService, desktop_exclusion_roots
from app.services.runtime import RuntimeGeneration
from tests.conftest import TEST_RUNTIME_TOKEN, running_app
from tests.desktop_harness import fixture

MARKER = "M9_S1_DESKTOP_PRIVATE_MARKER_71A"
SECRET = "M9_S1_PASSWORD_SECRET_77"


@pytest.fixture
def quiet_settings(settings: Settings) -> Settings:
    """No browser capability: these tests are about the desktop, and must not start Chromium."""
    return settings.model_copy(update={"public_inspection_hosts": "", "inspection_test_origins": ""})


@pytest.fixture
async def client(quiet_settings: Settings, engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with running_app(create_app(quiet_settings)) as client:
        yield client


def reason(response: httpx.Response) -> str:
    error = response.json()["error"]
    assert error["code"] == "desktop_refused"
    return str(error["reason"])


# ---- platform-independent -----------------------------------------------------------------------


async def test_the_capability_is_off_by_default_and_other_routes_are_unaffected(client: httpx.AsyncClient) -> None:
    body = {"worker_generation": str(uuid.uuid4()), "surface_ref": "s1", "surface_epoch": 1}
    for call in (client.get("/desktop/surfaces"), client.post("/desktop/observations", json=body)):
        response = await call
        assert response.status_code == 503 and reason(response) == "desktop_automation_disabled"
    assert (await client.get("/health")).status_code == 200


async def test_an_unsupported_platform_says_so_and_never_fakes_an_implementation(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> None:
    service = DesktopService(engine, runtime_generation=runtime_generation.id, worker=None, timeout_seconds=5, unsupported=True)
    for call in (service.list_surfaces(), service.observe(uuid.uuid4(), "s1", 1)):
        with pytest.raises(DesktopRefusal) as refused:
            await call
        assert refused.value.code is DesktopReason.UNSUPPORTED


async def test_the_runtime_requires_its_bearer_credential(quiet_settings: Settings, engine: AsyncEngine) -> None:
    async with running_app(create_app(quiet_settings)) as authorised:
        anonymous = httpx.AsyncClient(transport=authorised._transport, base_url="http://127.0.0.1")  # noqa: SLF001
        async with anonymous:
            assert (await anonymous.get("/desktop/surfaces")).status_code == 401
            body = {"worker_generation": str(uuid.uuid4()), "surface_ref": "s1", "surface_epoch": 1}
            assert (await anonymous.post("/desktop/observations", json=body)).status_code == 401


def test_the_runtime_exposes_exactly_two_desktop_routes_and_no_verbs(quiet_settings: Settings) -> None:
    paths = create_app(quiet_settings).openapi()["paths"]
    desktop = {path: sorted(methods) for path, methods in paths.items() if "desktop" in path}
    assert desktop == {"/desktop/surfaces": ["get"], "/desktop/observations": ["post"]}


@pytest.mark.parametrize(
    "body",
    [
        # Each half is required...
        {"surface_ref": "s1", "surface_epoch": 1},
        {"worker_generation": str(uuid.uuid4()), "surface_epoch": 1},
        {"worker_generation": str(uuid.uuid4()), "surface_ref": "s1"},
        {"worker_generation": "not-a-uuid", "surface_ref": "s1", "surface_epoch": 1},
        # ...and validated...
        {"worker_generation": str(uuid.uuid4()), "surface_ref": "s17", "surface_epoch": 1},
        {"worker_generation": str(uuid.uuid4()), "surface_ref": "s0", "surface_epoch": 1},
        {"worker_generation": str(uuid.uuid4()), "surface_ref": "u1", "surface_epoch": 1},
        {"worker_generation": str(uuid.uuid4()), "surface_ref": "s1", "surface_epoch": 0},
        # ...and nothing else is accepted.
        *[
            {"worker_generation": str(uuid.uuid4()), "surface_ref": "s1", "surface_epoch": 1, **extra}
            for extra in (
                {"hwnd": 5}, {"pid": 5}, {"process_path": "C:\\a.exe"}, {"selector": "//Button"}, {"x": 1, "y": 1},
                {"action": "click"}, {"script": "print(1)"}, {"property": "Name"},
                {"expected_worker_generation": str(uuid.uuid4())},
            )
        ],
    ],
)
async def test_an_observation_request_names_a_surface_and_nothing_else(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    assert (await client.post("/desktop/observations", json=body)).status_code == 422


def test_the_exclusion_roots_are_the_runtime_and_electron() -> None:
    assert desktop_exclusion_roots(100, None) == (100,)
    assert desktop_exclusion_roots(100, 50) == (50, 100)
    assert desktop_exclusion_roots(100, 100) == (100,)


async def test_the_production_wiring_passes_the_runtime_and_electron_pids_to_the_worker(
    quiet_settings: Settings, engine: AsyncEngine
) -> None:
    enabled = quiet_settings.model_copy(update={"desktop_observation": True, "runtime_parent_pid": os.getpid()})
    if os.name != "nt":
        pytest.skip("the managed worker is only created on Windows")
    app = create_app(enabled)
    async with app.router.lifespan_context(app):
        service: DesktopService = app.state.desktop_service
        worker = service._worker  # noqa: SLF001
        assert isinstance(worker, ManagedDesktopWorker)
        assert worker._root_pids == desktop_exclusion_roots(os.getpid(), os.getpid())  # noqa: SLF001
        assert worker.pid is None, "the worker must not start until a desktop request needs it"


# ---- persistence and retention ---------------------------------------------------------------------


async def _insert(engine: AsyncEngine, runtime: RuntimeGeneration, worker: uuid.UUID, count: int) -> list[uuid.UUID]:
    from datetime import UTC, datetime

    async with engine.begin() as connection:
        await DesktopRepository(connection).register_worker_generation(
            worker_generation=worker, runtime_generation=runtime.id, worker_started_at=datetime.now(UTC)
        )
    ids: list[uuid.UUID] = []
    for _ in range(count):
        observation_id = uuid.uuid4()
        ids.append(observation_id)
        # One transaction per observation, as in the service: `created_at` is the transaction time.
        async with engine.begin() as connection:
            await DesktopRepository(connection).insert_observation(
                observation_id=observation_id, worker_generation=worker, surface_ref="s1", surface_epoch=1,
                schema_version=1, classification="desktop_private", snapshot={"nodes": []}, snapshot_digest="a" * 64, truncated=False,
            )
        await asyncio.sleep(0.005)
    return ids


async def test_only_recent_observations_are_retained(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> None:
    ids = await _insert(engine, runtime_generation, uuid.uuid4(), RETAINED_OBSERVATIONS + 6)
    async with engine.begin() as connection:
        await DesktopRepository(connection).prune(keep=RETAINED_OBSERVATIONS, max_age_seconds=RETAINED_SECONDS)
    async with engine.connect() as connection:
        kept = {row[0] for row in await connection.execute(text("SELECT id FROM desktop_observations"))}
    assert kept == set(ids[-RETAINED_OBSERVATIONS:])


async def test_the_database_refuses_what_a_desktop_observation_may_not_be(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> None:
    from sqlalchemy.exc import IntegrityError

    worker = uuid.uuid4()
    await _insert(engine, runtime_generation, worker, 1)

    async def attempt(**changes: Any) -> None:
        values = {"id": uuid.uuid4(), "w": worker, "ref": "s1", "epoch": 1, "version": 1, "cls": "desktop_private",
                  "snap": "{}", "digest": "a" * 64, "trunc": False}
        values.update(changes)
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO desktop_observations (id, worker_generation, surface_ref, surface_epoch, schema_version, classification, snapshot, snapshot_digest, truncated) "
                     "VALUES (:id, :w, :ref, :epoch, :version, :cls, CAST(:snap AS jsonb), :digest, :trunc)"),
                values,
            )

    await attempt()
    for bad in ({"cls": "public"}, {"ref": "s17"}, {"ref": "x1"}, {"epoch": 0}, {"version": 0}, {"digest": "zz"}, {"snap": "[]"}, {"w": uuid.uuid4()}):
        with pytest.raises(IntegrityError):
            await attempt(**bad)


# ---- end to end through the real worker (Windows) ------------------------------------------------------


@pytest.fixture
async def desktop_client(quiet_settings: Settings, engine: AsyncEngine) -> AsyncIterator[tuple[httpx.AsyncClient, DesktopService]]:
    enabled = quiet_settings.model_copy(update={"desktop_observation": True, "desktop_observation_timeout_seconds": 3.0})
    app = create_app(enabled)
    async with running_app(app) as client:
        # The pytest process plays the runtime and spawns the fixtures, so it must not be a trusted
        # root or the fixtures would be excluded as Lumi's own children.
        service: DesktopService = app.state.desktop_service
        service._worker = ManagedDesktopWorker(root_pids=(), timeout_seconds=3.0, startup_timeout_seconds=90)  # noqa: SLF001
        try:
            yield client, service
        finally:
            assert service._worker is not None  # noqa: SLF001
            await service._worker.aclose()  # noqa: SLF001


async def _surface(client: httpx.AsyncClient, title: str) -> dict[str, Any]:
    """A listed surface, with the worker generation it was listed under (the request needs all three)."""
    response = await client.get("/desktop/surfaces")
    assert response.status_code == 200, response.text
    listing = response.json()
    surface = next(s for s in listing["surfaces"] if s["window_title"] == title)
    return {**surface, "worker_generation": listing["worker_generation"]}


async def _observe(client: httpx.AsyncClient, surface: dict[str, Any]) -> httpx.Response:
    """One read, retried as a new read if the window moved under it (`surface_changed`, the designed transient)."""
    response = await client.post(
        "/desktop/observations",
        json={
            "worker_generation": surface["worker_generation"],
            "surface_ref": surface["surface_ref"],
            "surface_epoch": surface["surface_epoch"],
        },
    )
    for _ in range(2):
        if response.status_code != 409 or response.json()["error"].get("reason") != "surface_changed":
            break
        response = await client.post(
            "/desktop/observations",
            json={
                "worker_generation": surface["worker_generation"],
                "surface_ref": surface["surface_ref"],
                "surface_epoch": surface["surface_epoch"],
            },
        )
    return response


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_observing_through_the_runtime_persists_only_the_safe_projection(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], engine: AsyncEngine, tmp_path: Path
) -> None:
    client, _ = desktop_client
    with fixture(tmp_path, "--marker", MARKER) as app:
        surface = await _surface(client, app.title)
        response = await _observe(client, surface)
        assert response.status_code == 200, response.text
        observation = response.json()
        assert observation["classification"] == "desktop_private" and observation["trust"] == "untrusted_environment"
        assert any(n["text"] == MARKER for n in observation["nodes"])
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT * FROM desktop_observations WHERE id = :i"), {"i": observation["observation_id"]})).one()
            generation = (await connection.execute(text("SELECT runtime_generation FROM desktop_worker_generations WHERE id = :w"), {"w": row.worker_generation})).one()
        assert row.classification == "desktop_private" and row.surface_ref == surface["surface_ref"] and row.schema_version == 1
        assert row.snapshot == observation and len(row.snapshot_digest) == 64 and row.truncated is False
        assert generation.runtime_generation is not None
        # Nothing native crosses into storage: not a handle, a pid, a path, a rectangle or an automation id.
        stored = json.dumps(row.snapshot)
        for planted in (str(app.pid), str(app.hwnd), "LumiFixture", "python.exe", "AutomationId", "ClassName"):
            assert planted not in stored, planted


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_a_credential_surface_is_refused_through_the_runtime_and_nothing_is_stored(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], engine: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = desktop_client
    with fixture(tmp_path, "--mode", "credential", "--marker", MARKER) as app:
        surface = await _surface(client, app.title)
        response = await _observe(client, surface)
        assert response.status_code == 403 and reason(response) == "credential_surface"
        assert SECRET not in response.text and MARKER not in response.text and SECRET not in caplog.text and MARKER not in caplog.text
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM desktop_observations")) == 0


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_a_stale_surface_is_refused_through_the_runtime(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], tmp_path: Path
) -> None:
    client, _ = desktop_client
    with fixture(tmp_path, title="Lumi Fixture Runtime Stale") as app:
        surface = await _surface(client, app.title)
    response = await _observe(client, surface)
    assert response.status_code == 409 and reason(response) == "stale_surface"
    unknown = {**surface, "surface_ref": "s16", "surface_epoch": 999}
    assert (await _observe(client, unknown)).status_code == 409


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_a_hung_provider_times_out_without_hanging_the_runtime_and_a_later_read_uses_a_fresh_worker(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], engine: AsyncEngine, tmp_path: Path
) -> None:
    client, service = desktop_client
    with fixture(tmp_path, title="Lumi Fixture Runtime Hang") as app:
        surface = await _surface(client, app.title)
        first_generation = (await client.get("/desktop/surfaces")).json()["worker_generation"]
        app.hang(60)
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        response = await _observe(client, surface)
        elapsed = time.monotonic() - started
        beat.cancel()
        assert response.status_code == 504 and reason(response) == "desktop_observation_timeout"
        assert elapsed < 12 and ticks > elapsed * 8, "the runtime must stay responsive while a provider hangs"
        assert service._worker is not None and service._worker.pid is None, "the hung worker generation must be killed and fenced"  # noqa: SLF001
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM desktop_observations")) == 0, "a timed-out read is never stored"
    with fixture(tmp_path, title="Lumi Fixture Runtime Healthy") as healthy:
        listing = (await client.get("/desktop/surfaces")).json()
        assert listing["worker_generation"] != first_generation, "a later read starts on a new worker generation"
        assert healthy.title in [s["window_title"] for s in listing["surfaces"]]
        assert (await _observe(client, await _surface(client, healthy.title))).status_code == 200


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_a_pair_from_a_replaced_worker_generation_is_refused_without_costing_the_new_worker_its_life(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], engine: AsyncEngine, tmp_path: Path
) -> None:
    """(surface_ref, surface_epoch) is unique within ONE worker generation. A new worker starts every slot
    again, so a pair issued by the old one must not be readable as whatever the new one put in that slot."""
    client, service = desktop_client
    with fixture(tmp_path, title="Lumi Fixture Generation") as app:
        first = await _surface(client, app.title)
        assert service._worker is not None  # noqa: SLF001
        await service._worker.fence()  # noqa: SLF001 - the worker is replaced (a crash, a timeout, a restart)
        second = await _surface(client, app.title)
        assert second["worker_generation"] != first["worker_generation"]
        worker_before = service._worker.pid  # noqa: SLF001
        stale = await _observe(client, first)
        assert stale.status_code == 409 and reason(stale) == "stale_worker_generation"
        assert service._worker.pid == worker_before, "a stale caller must not cost the healthy worker its life"  # noqa: SLF001
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT count(*) FROM desktop_observations")) == 0
        assert (await _observe(client, second)).status_code == 200


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_a_worker_that_dies_mid_call_is_reported_and_replaced(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], tmp_path: Path
) -> None:
    import subprocess

    client, service = desktop_client
    with fixture(tmp_path, title="Lumi Fixture Worker Dies") as app:
        surface = await _surface(client, app.title)
        app.hang(60)
        call = asyncio.create_task(_observe(client, surface))
        await asyncio.sleep(1.0)
        assert service._worker is not None and service._worker.pid is not None  # noqa: SLF001
        first_pid = service._worker.pid  # noqa: SLF001
        subprocess.run(["taskkill", "/F", "/PID", str(first_pid)], capture_output=True, check=True)
        response = await call
        assert response.status_code == 503 and reason(response) == "desktop_worker_unavailable"
    with fixture(tmp_path, title="Lumi Fixture After Death") as healthy:
        listing = (await client.get("/desktop/surfaces")).json()
        assert healthy.title in [s["window_title"] for s in listing["surfaces"]]
        assert service._worker.pid not in (None, first_pid)  # noqa: SLF001


# ---- retention, and a database error must not quote what it was storing -----------------------------------------


async def test_observations_expire_by_age_as_well_as_by_count(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> None:
    ids = await _insert(engine, runtime_generation, uuid.uuid4(), 3)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE desktop_observations SET created_at = now() - interval '2 days' WHERE id = :i"), {"i": ids[0]})
        await DesktopRepository(connection).prune(keep=RETAINED_OBSERVATIONS, max_age_seconds=RETAINED_SECONDS)
    async with engine.connect() as connection:
        kept = {row[0] for row in await connection.execute(text("SELECT id FROM desktop_observations"))}
    assert kept == set(ids[1:])


async def test_startup_sweeps_expired_desktop_text_even_when_the_capability_is_off(
    quiet_settings: Settings, engine: AsyncEngine, runtime_generation: RuntimeGeneration
) -> None:
    ids = await _insert(engine, runtime_generation, uuid.uuid4(), 1)
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE desktop_observations SET created_at = now() - interval '3 days'"))
    app = create_app(quiet_settings)
    async with app.router.lifespan_context(app):
        pass
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM desktop_observations WHERE id = :i"), {"i": ids[0]}) == 0


async def test_a_failed_insert_is_reported_without_quoting_the_observed_text(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from app.desktop.protocol import DesktopNode, DesktopObservation

    async def boom(self: DesktopRepository, **values: Any) -> None:
        raise SQLAlchemyError(f"INSERT failed [parameters: {values['snapshot']}] {MARKER}")

    monkeypatch.setattr(DesktopRepository, "insert_observation", boom)
    service = DesktopService(engine, runtime_generation=runtime_generation.id, worker=None, timeout_seconds=5)
    observation = DesktopObservation(
        observation_id=uuid.uuid4(), surface_ref="s1", surface_epoch=1, worker_generation=uuid.uuid4(),
        nodes=[DesktopNode(control_ref="u1", parent_ref=None, role="edit", name=MARKER, text=MARKER, enabled=True, visible=True,
                           focused=False, focusable=True, selected=None, checked=None, expanded=None, patterns=[])],
        node_count=1, depth=0, truncated=False, truncation=[], fingerprint="a" * 64,
    )
    with pytest.raises(DesktopRefusal) as caught:
        await service._persist(observation)  # noqa: SLF001
    assert caught.value.code is DesktopReason.BACKEND_FAILED and caught.value.__cause__ is None
    assert MARKER not in str(caught.value) and MARKER not in caplog.text


# ---- the provider / memory firewall --------------------------------------------------------------------------------


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation")
async def test_desktop_text_exists_only_in_desktop_storage(
    desktop_client: tuple[httpx.AsyncClient, DesktopService], engine: AsyncEngine, tmp_path: Path,
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str],
) -> None:
    """Plant a marker in a fixture control, observe it, then look for it everywhere else."""
    client, _ = desktop_client
    caplog.set_level("DEBUG")
    title = f"Private {MARKER} title"
    with fixture(tmp_path, "--marker", MARKER, title=title) as app:
        surface = await _surface(client, title)
        observation = (await _observe(client, surface)).json()
        assert any(n["text"] == MARKER for n in observation["nodes"]), "the marker is in the local observation"
        # Ordinary task traffic on the same runtime, before and after.
        task = await client.post("/tasks", json={"request": {"type": "note", "text": "hello"}})
        assert MARKER not in task.text
        assert app.title == title

    async with engine.connect() as connection:
        tables = [row[0] for row in await connection.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))]
        holders: dict[str, int] = {}
        for table in tables:
            count = await connection.scalar(text(f'SELECT count(*) FROM "{table}" AS t WHERE t::text LIKE :marker'), {"marker": f"%{MARKER}%"})
            if count:
                holders[table] = int(count)
    assert holders == {"desktop_observations": 1}, holders
    # Not in any log the runtime or the worker wrote, and not in what the worker printed.
    captured = capfd.readouterr()
    assert MARKER not in caplog.text and MARKER not in captured.out and MARKER not in captured.err
    assert title not in caplog.text and title not in captured.err


# ---- migration 0012 ------------------------------------------------------------------------------------------


def test_migration_0012_adds_exactly_the_desktop_tables_and_downgrades_cleanly(migrated_database_url: str) -> None:
    from tests.conftest import downgrade, migrate, truncate_all

    async def tables() -> dict[str, bool]:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                return {
                    name: bool(await connection.scalar(text(f"SELECT to_regclass('public.{name}') IS NOT NULL")))
                    for name in ("desktop_worker_generations", "desktop_observations", "form_drafts", "browser_worker_generations")
                }
        finally:
            await engine.dispose()

    async def columns() -> set[str]:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(migrated_database_url)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'desktop_observations'"))
                return {row[0] for row in rows}
        finally:
            await engine.dispose()

    truncate_all(migrated_database_url)
    try:
        assert asyncio.run(tables()) == {"desktop_worker_generations": True, "desktop_observations": True, "form_drafts": True, "browser_worker_generations": True}
        # Safe projection only: there is no column that could hold a handle, a pid, a path, a rectangle or a password.
        assert asyncio.run(columns()) == {
            "id", "worker_generation", "surface_ref", "surface_epoch", "schema_version", "classification",
            "snapshot", "snapshot_digest", "truncated", "created_at",
        }
        downgrade(migrated_database_url, "0011")
        assert asyncio.run(tables()) == {"desktop_worker_generations": False, "desktop_observations": False, "form_drafts": True, "browser_worker_generations": True}
    finally:
        migrate(migrated_database_url)
    assert asyncio.run(tables())["desktop_observations"] is True
