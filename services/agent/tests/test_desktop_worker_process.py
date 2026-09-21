"""Milestone 9 S1: the desktop worker as a REAL supervised subprocess (Windows).

The supervisor spawns `python -m app.desktop.main` with a scrubbed environment; the runtime-side client
talks to it over authenticated loopback. These tests cover the process-level guarantees the in-process
ASGI tests cannot: the credential and generation across restarts, a crash, a *real* hung UI Automation
provider, and the parent watchdog.
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.browser.session import generate_worker_token
from app.config import AGENT_ROOT
from app.desktop.client import DesktopWorkerClient
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.managed import (
    WORKER_MODULE,
    ManagedDesktopWorker,
    _read_ready_line,
    _ready_port,
    worker_environment,
)
from app.desktop.protocol import WORKER_TOKEN_HEADER, ObserveRequest, SurfaceListRequest
from tests.desktop_harness import fixture, wait_for

pytestmark = [
    pytest.mark.desktop_uia,
    pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation"),
]


def make_worker(timeout: float = 5.0, roots: tuple[int, ...] = ()) -> ManagedDesktopWorker:
    # The pytest process plays the runtime and spawns the fixtures, so it must not be a trusted
    # root here: its children (the fixtures) would then be excluded as Lumi's own.
    return ManagedDesktopWorker(root_pids=roots, timeout_seconds=timeout, startup_timeout_seconds=90)


async def observe_retrying(client: DesktopWorkerClient, request: ObserveRequest):  # type: ignore[no-untyped-def]
    """One real read, retried as a *new* read if the window moved under it (`surface_changed`).

    That refusal is the designed answer to a tree that changed between the two passes, and the designed
    response is another read; observation has no effect to double. It was seen once, unreproduced, in a
    full-suite run (0/8 cold-start runs in isolation, 0/72 under CPU load), so the tests do what a caller
    would, and the review records it.
    """
    for attempt in range(3):
        try:
            return await client.observe(request)
        except DesktopRefusal as refusal:
            if refusal.code is not DesktopReason.SURFACE_CHANGED or attempt == 2:
                raise
    raise AssertionError("unreachable")


async def connect(worker: ManagedDesktopWorker) -> tuple[DesktopWorkerClient, object]:
    endpoint = await worker.endpoint()
    client = DesktopWorkerClient(base_url=endpoint.base_url, token=endpoint.token, timeout_seconds=endpoint.timeout_seconds + 3)
    return client, await client.identify()


def test_the_worker_environment_is_built_from_an_allowlist_not_copied() -> None:
    hostile = {
        "SystemRoot": "C:\\Windows", "TEMP": "C:\\Temp", "DATABASE_URL": "postgresql+asyncpg://secret",
        "LUMI_RUNTIME_TOKEN": "x" * 40, "OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-secret",
        "GEMINI_API_KEY": "secret", "LUMI_BROWSER_PROFILE_ROOT": "C:\\profiles", "PATH": "C:\\bin",
        "LUMI_BROWSER_TOKEN": "y" * 40, "PYTHONPATH": "C:\\evil", "PYTHONSTARTUP": "C:\\evil.py",
    }
    environment = worker_environment(
        token=SecretStr("t" * 32), parent_pid=1234, root_pids=(30, 10, 10), timeout_seconds=7, source=hostile
    )
    assert set(environment) == {
        "SystemRoot", "TEMP", "PYTHONUTF8", "PYTHONUNBUFFERED", "LUMI_DESKTOP_TOKEN",
        "LUMI_DESKTOP_PARENT_PID", "LUMI_DESKTOP_EXCLUDED_PIDS", "LUMI_DESKTOP_TIMEOUT_SECONDS",
        "LUMI_DESKTOP_TRUST_JOB", "LUMI_DESKTOP_REGISTERED_APPS",
    }
    assert environment["LUMI_DESKTOP_EXCLUDED_PIDS"] == "10,30" and environment["LUMI_DESKTOP_TRUST_JOB"] == "0"
    assert worker_environment(token=SecretStr("t" * 32), parent_pid=1, root_pids=(), timeout_seconds=7, trust_job=True, source={})["LUMI_DESKTOP_TRUST_JOB"] == "1"
    assert not any(secret in " ".join(environment.values()) for secret in ("secret", "postgresql", "evil"))


async def test_the_supervisor_spawns_only_the_worker_with_a_fixed_argument_vector() -> None:
    seen: list[list[str]] = []

    def spawn(arguments: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
        seen.append(arguments)
        raise OSError("not launched in this test")

    worker = ManagedDesktopWorker(root_pids=(), timeout_seconds=5, spawn=spawn)
    with pytest.raises(DesktopRefusal) as refused:
        await worker.endpoint()
    assert refused.value.code is DesktopReason.WORKER_UNAVAILABLE
    assert seen == [[sys.executable, "-m", WORKER_MODULE, "--host", "127.0.0.1", "--port", "0", "--log-level", "warning", "--ready-stdout"]]
    assert WORKER_MODULE == "app.desktop.main"


async def test_a_worker_that_never_reports_ready_is_killed_and_reported_unavailable() -> None:
    sleeper: list[subprocess.Popen[bytes]] = []

    def spawn(arguments: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
        sleeper.append(process)
        return process

    worker = ManagedDesktopWorker(root_pids=(), timeout_seconds=5, startup_timeout_seconds=1.0, spawn=spawn)
    with pytest.raises(DesktopRefusal) as refused:
        await worker.endpoint()
    assert refused.value.code is DesktopReason.WORKER_UNAVAILABLE
    assert sleeper[0].poll() is not None, "a worker that is not ready must be killed, not left running"


async def test_the_real_worker_serves_the_fixture_under_a_scrubbed_environment(tmp_path: Path) -> None:
    worker = make_worker()
    with fixture(tmp_path) as app:
        try:
            client, identity = await connect(worker)
            async with client:
                generation = identity.worker_generation  # type: ignore[attr-defined]
                listing = await client.list_surfaces(SurfaceListRequest(expected_worker_generation=generation))
                surface = next(s for s in listing.surfaces if s.window_title == app.title)
                observation = await observe_retrying(
                    client,
                    ObserveRequest(expected_worker_generation=generation, surface_ref=surface.surface_ref, surface_epoch=surface.surface_epoch),
                )
                assert observation.worker_generation == generation and observation.node_count > 5
                assert all(v == 0 for k, v in app.counters().items() if k != "getobject")
        finally:
            await worker.aclose()


async def test_the_credential_and_generation_are_enforced_by_the_real_process() -> None:
    worker = make_worker()
    try:
        endpoint = await worker.endpoint()
        async with httpx.AsyncClient(base_url=endpoint.base_url, timeout=10) as raw:
            body = {"expected_worker_generation": "00000000-0000-4000-8000-000000000000"}
            assert (await raw.get("/health")).status_code == 401
            assert (await raw.post("/v1/desktop/surfaces", json=body)).status_code == 401
            assert (await raw.get("/v1/desktop/execute")).status_code == 404
            good = {WORKER_TOKEN_HEADER: endpoint.token.get_secret_value()}
            assert (await raw.post("/v1/desktop/surfaces", json=body, headers=good)).status_code == 409
            assert (await raw.get("/health", headers={WORKER_TOKEN_HEADER: generate_worker_token().get_secret_value()})).status_code == 401
            assert (await raw.get("/health", headers=good)).status_code == 200
    finally:
        await worker.aclose()


async def test_a_crashed_worker_is_replaced_with_a_new_generation_and_a_new_credential() -> None:
    worker = make_worker()
    try:
        first_client, first_identity = await connect(worker)
        first = await worker.endpoint()
        async with first_client:
            pass
        worker_pid = worker.pid
        assert worker_pid is not None
        subprocess.run(["taskkill", "/F", "/PID", str(worker_pid)], capture_output=True, check=True)
        assert await asyncio.to_thread(wait_for, lambda: worker._process is not None and worker._process.poll() is not None, 10)  # noqa: SLF001

        second_client, second_identity = await connect(worker)
        second = await worker.endpoint()
        async with second_client:
            assert second_identity.worker_generation != first_identity.worker_generation  # type: ignore[attr-defined]
            assert second.token.get_secret_value() != first.token.get_secret_value()
        # The old generation and its credential are worthless against the new process.
        async with httpx.AsyncClient(base_url=second.base_url, timeout=10) as raw:
            old_credential = {WORKER_TOKEN_HEADER: first.token.get_secret_value()}
            assert (await raw.get("/health", headers=old_credential)).status_code == 401
            stale = {"expected_worker_generation": str(first_identity.worker_generation)}  # type: ignore[attr-defined]
            fresh = {WORKER_TOKEN_HEADER: second.token.get_secret_value()}
            assert (await raw.post("/v1/desktop/surfaces", json=stale, headers=fresh)).status_code == 409
    finally:
        await worker.aclose()


async def test_a_real_hung_provider_times_out_within_the_deadline_and_the_process_is_replaced(tmp_path: Path) -> None:
    worker = make_worker(timeout=2.0)
    with fixture(tmp_path, "--mode", "dynamic") as app:
        try:
            client, identity = await connect(worker)
            generation = identity.worker_generation  # type: ignore[attr-defined]
            async with client:
                listing = await client.list_surfaces(SurfaceListRequest(expected_worker_generation=generation))
                surface = next(s for s in listing.surfaces if s.window_title == app.title)
                # A hostile accessibility provider: the fixture's UI thread now blocks on every WM_GETOBJECT.
                app.hang(60)
                ticks = 0

                async def heartbeat() -> None:
                    nonlocal ticks
                    while True:
                        await asyncio.sleep(0.05)
                        ticks += 1

                beat = asyncio.create_task(heartbeat())
                started = time.monotonic()
                with pytest.raises(DesktopRefusal) as refused:
                    await client.observe(
                        ObserveRequest(expected_worker_generation=generation, surface_ref=surface.surface_ref, surface_epoch=surface.surface_epoch)
                    )
                elapsed = time.monotonic() - started
                beat.cancel()
                assert refused.value.code is DesktopReason.OBSERVATION_TIMEOUT
                assert elapsed < 8, f"the worker's own deadline (2s) must bound the call, took {elapsed:.1f}s"
                assert ticks > elapsed * 8, "the runtime event loop must stay responsive while a provider hangs"
                # The worker is poisoned and refuses everything else from this generation.
                with pytest.raises(DesktopRefusal) as poisoned:
                    await client.list_surfaces(SurfaceListRequest(expected_worker_generation=generation))
                assert poisoned.value.code is DesktopReason.OBSERVATION_TIMEOUT
            # The runtime kills and fences it; the next use is a fresh generation that works.
            await worker.fence()
            assert worker.pid is None
        finally:
            await worker.aclose()

    with fixture(tmp_path, title="Lumi Fixture After Hang") as healthy:
        worker = make_worker(timeout=5.0)
        try:
            client, identity = await connect(worker)
            async with client:
                listing = await client.list_surfaces(SurfaceListRequest(expected_worker_generation=identity.worker_generation))  # type: ignore[attr-defined]
                assert healthy.title in [s.window_title for s in listing.surfaces]
        finally:
            await worker.aclose()


async def test_the_worker_exits_when_the_runtime_that_owns_it_disappears() -> None:
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL)
    try:
        token = generate_worker_token()
        environment = worker_environment(token=token, parent_pid=owner.pid, root_pids=(), timeout_seconds=5)
        process = subprocess.Popen(
            [sys.executable, "-m", WORKER_MODULE, "--host", "127.0.0.1", "--port", "0", "--log-level", "warning", "--ready-stdout"],
            cwd=AGENT_ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            assert _ready_port(await asyncio.to_thread(_read_ready_line, process)) > 0
            assert process.poll() is None
            owner.kill()
            owner.wait(timeout=10)
            assert await asyncio.to_thread(wait_for, lambda: process.poll() is not None, 20), "the watchdog must end the worker"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdout:
                process.stdout.close()
    finally:
        if owner.poll() is None:
            owner.kill()
