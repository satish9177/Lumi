"""Process harness for browser tests.

Milestone 3 has three processes that must be started, killed and restarted
independently: the fixture site (which holds the authoritative booking state and
must *survive* a crash), the browser worker (which owns Chromium), and the Lumi
runtime. Everything here is about starting them on free ports with the right
credential, and killing them without a graceful shutdown.

The worker token is minted per test run and passed to the worker through its
environment. It never appears in a URL or on a command line, which is what makes
it reasonable for the runtime and the worker to trust each other on a machine
where any process can reach loopback.
"""

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
from pydantic import SecretStr

from app.browser.session import generate_worker_token
from app.config import AGENT_ROOT

STARTUP_TIMEOUT_SECONDS = 90.0


def free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def kill(process: subprocess.Popen[bytes]) -> None:
    """A hard kill. No signal handler, no lifespan shutdown, no cleanup hook."""
    if process.poll() is None:
        process.kill()
        process.wait(timeout=30)


@dataclass(frozen=True, slots=True)
class Process:
    process: subprocess.Popen[bytes]
    base_url: str
    log_path: Path

    def logs(self) -> str:
        return self.log_path.read_text(errors="replace")

    def kill(self) -> None:
        kill(self.process)


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    probe: str,
    log_path: Path,
    headers: dict[str, str] | None = None,
) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"process exited early:\n{log_path.read_text(errors='replace')}")
        try:
            if httpx.get(probe, timeout=2, headers=headers or {}).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    pytest.fail(f"process never became ready:\n{log_path.read_text(errors='replace')}")


def _spawn(arguments: list[str], environment: dict[str, str], log: IO[bytes]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, *arguments],
        cwd=AGENT_ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


# ---- the fixture site -------------------------------------------------------


@contextmanager
def fixture_site(log_path: Path, port: int | None = None) -> Iterator[Process]:
    chosen = port if port is not None else free_port()
    base_url = f"http://127.0.0.1:{chosen}"
    with log_path.open("wb") as log:
        process = _spawn(
            [
                "-m", "evals.sites.appointments.server",
                "--port", str(chosen), "--log-level", "warning",
            ],
            {**os.environ},
            log,
        )
        try:
            _wait_until_ready(process, f"{base_url}/", log_path)
            yield Process(process, base_url, log_path)
        finally:
            kill(process)


class SiteControl:
    """The test control plane. Never used to book or to observe on Lumi's behalf."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def reset(self) -> None:
        httpx.post(f"{self.base_url}/__eval__/reset", timeout=10).raise_for_status()

    def set_faults(self, **faults: Any) -> None:
        httpx.post(f"{self.base_url}/__eval__/faults", json=faults, timeout=10).raise_for_status()

    def state(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/__eval__/state", timeout=10)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def wait_for_bookings(self, count: int, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the site has authoritatively created `count` bookings.

        This is what makes the lost-response test deterministic rather than a
        race: the kill happens only once the booking provably exists.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.state()
            if state["booking_count"] >= count:
                return state
            time.sleep(0.1)
        pytest.fail(f"the fixture never reached {count} booking(s): {self.state()}")


@contextmanager
def public_fixture_site(log_path: Path, port: int | None = None) -> Iterator[Process]:
    """The Milestone 7a public-page fixture (also used as a canary)."""
    chosen = port if port is not None else free_port()
    base_url = f"http://127.0.0.1:{chosen}"
    with log_path.open("wb") as log:
        process = _spawn(
            ["-m", "evals.sites.public_pages.server", "--port", str(chosen), "--log-level", "warning"],
            {**os.environ},
            log,
        )
        try:
            _wait_until_ready(process, f"{base_url}/__eval__/state", log_path)
            yield Process(process, base_url, log_path)
        finally:
            kill(process)


class PublicSiteControl:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def reset(self) -> None:
        httpx.post(f"{self.base_url}/__eval__/reset", timeout=10).raise_for_status()

    def hits(self) -> dict[str, int]:
        response = httpx.get(f"{self.base_url}/__eval__/state", timeout=10)
        response.raise_for_status()
        hits: dict[str, int] = response.json()["hits"]
        return hits

    def wait_for_hit(self, path: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.hits().get(path, 0) > 0:
                return
            time.sleep(0.1)
        pytest.fail(f"the fixture never saw a request for {path}: {self.hits()}")


@contextmanager
def account_fixture_site(log_path: Path, port: int | None = None) -> Iterator[Process]:
    """The Milestone 8a S1 session fixture.

    It stays up across worker and runtime restarts on purpose: its session
    table lives in the fixture process, so if the fixture restarted, "signed
    in" would be impossible for reasons that have nothing to do with the
    profile. Keeping it up makes the browser's own persistence the only
    explanation.
    """
    chosen = port if port is not None else free_port()
    base_url = f"http://127.0.0.1:{chosen}"
    with log_path.open("wb") as log:
        process = _spawn(
            ["-m", "evals.sites.account_fixture.server", "--port", str(chosen), "--log-level", "warning"],
            {**os.environ},
            log,
        )
        try:
            _wait_until_ready(process, f"{base_url}/__eval__/state", log_path)
            yield Process(process, base_url, log_path)
        finally:
            kill(process)


class AccountSiteControl:
    """The account fixture's test control plane. Never used on Lumi's behalf."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def reset(self) -> None:
        httpx.post(f"{self.base_url}/__eval__/reset", timeout=10).raise_for_status()

    def state(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/__eval__/state", timeout=10)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


# ---- the browser worker -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerProcess:
    process: Process
    token: SecretStr

    @property
    def base_url(self) -> str:
        return self.process.base_url

    def identity(self) -> dict[str, Any]:
        from app.browser.protocol import WORKER_TOKEN_HEADER

        response = httpx.get(
            f"{self.base_url}/health",
            headers={WORKER_TOKEN_HEADER: self.token.get_secret_value()},
            timeout=10,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def kill(self) -> None:
        self.process.kill()


@contextmanager
def browser_worker(
    log_path: Path,
    *,
    site_origin: str,
    token: SecretStr | None = None,
    port: int | None = None,
    operation_timeout_seconds: float = 0.0,
    public_hosts: str = "",
    inspection_test_origins: str = "",
    research_test_origins: str = "",
    research_hosts: str = "",
    research_any_public_host: bool = False,
    research_max_tabs: int = 5,
    profile_root: str = "",
) -> Iterator[WorkerProcess]:
    from app.browser.protocol import WORKER_TOKEN_HEADER

    credential = token if token is not None else generate_worker_token()
    chosen = port if port is not None else free_port()
    base_url = f"http://127.0.0.1:{chosen}"
    environment = {
        **os.environ,
        # The credential reaches the worker through its environment, not through
        # an argument vector that every other process on the machine can read.
        "LUMI_BROWSER_TOKEN": credential.get_secret_value(),
        "LUMI_BROWSER_ALLOWED_ORIGINS": f"appointment_fixture={site_origin}",
        "LUMI_BROWSER_HEADLESS": "true",
        "LUMI_BROWSER_OPERATION_TIMEOUT_SECONDS": str(operation_timeout_seconds),
        "LUMI_BROWSER_PUBLIC_HOSTS": public_hosts,
        "LUMI_BROWSER_INSPECTION_TEST_ORIGINS": inspection_test_origins,
        "LUMI_BROWSER_RESEARCH_TEST_ORIGINS": research_test_origins,
        "LUMI_BROWSER_RESEARCH_HOSTS": research_hosts,
        "LUMI_BROWSER_RESEARCH_ANY_PUBLIC_HOST": "true" if research_any_public_host else "false",
        "LUMI_BROWSER_RESEARCH_MAX_TABS": str(research_max_tabs),
        # A base directory, never a profile path. Tests point it at a temporary
        # directory so a suite never writes into the developer's real
        # %LOCALAPPDATA%\Lumirowser-profiles.
        "LUMI_BROWSER_PROFILE_ROOT": profile_root,
    }
    with log_path.open("wb") as log:
        process = _spawn(
            [
                "-m", "app.browser.main",
                "--port", str(chosen), "--log-level", "warning",
            ],
            environment,
            log,
        )
        try:
            _wait_until_ready(
                process,
                f"{base_url}/health",
                log_path,
                headers={WORKER_TOKEN_HEADER: credential.get_secret_value()},
            )
            yield WorkerProcess(Process(process, base_url, log_path), credential)
        finally:
            kill(process)


# ---- the Lumi runtime -------------------------------------------------------


class RuntimeHttp:
    """Authenticated calls to exactly one runtime origin.

    The bearer credential is attached per call and only after checking that the
    URL belongs to this runtime, so it can never leak to the worker or fixture.
    There is deliberately no global httpx patching and no bypass mode.
    """

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self._token = token

    def _checked(self, url: str) -> str:
        if not url.startswith(self.base_url + "/"):
            raise AssertionError(f"refusing to send the runtime credential to {url!r}")
        return url

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        return {**(extra or {}), "Authorization": f"Bearer {self._token}"}

    def get(self, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        return httpx.get(self._checked(url), headers=self._headers(headers), **kwargs)

    def post(self, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        return httpx.post(self._checked(url), headers=self._headers(headers), **kwargs)


def mint_runtime_token() -> str:
    return secrets.token_urlsafe(32)


def runtime_environment(database_url: str, token: str) -> dict[str, str]:
    environment = {**os.environ, "DATABASE_URL": database_url, "LUMI_RUNTIME_TOKEN": token}
    # Never inherit a desktop's managed-worker or parent configuration.
    for key in (
        "LUMI_BROWSER_SITE_ORIGIN",
        "LUMI_RUNTIME_PARENT_PID",
        "LUMI_RUNTIME_READY_FD",
        "LUMI_PUBLIC_INSPECTION_HOSTS",
        "LUMI_INSPECTION_TEST_ORIGINS",
        "LUMI_RESEARCH_ANY_PUBLIC_HOST",
        "LUMI_RESEARCH_HOSTS",
        "LUMI_RESEARCH_TEST_ORIGINS",
        "LUMI_RESEARCH_SEARCH_ENDPOINT",
    ):
        environment.pop(key, None)
    return environment


def runtime_arguments(port: int) -> list[str]:
    # The fixed entry point Electron uses, so tests exercise the same process
    # lock and Windows job, not a bare uvicorn launch that omits them.
    return ["-m", "app.server", "--port", str(port)]


@dataclass(frozen=True, slots=True)
class RuntimeProcess:
    process: Process
    http: RuntimeHttp

    @property
    def base_url(self) -> str:
        return self.process.base_url

    def logs(self) -> str:
        return self.process.logs()

    def kill(self) -> None:
        self.process.kill()


@contextmanager
def runtime(
    log_path: Path,
    *,
    database_url: str,
    port: int | None = None,
    worker_url: str | None = None,
    worker_token: SecretStr | None = None,
    worker_timeout_seconds: float = 120.0,
    token: str | None = None,
    extra_environment: dict[str, str] | None = None,
) -> Iterator[RuntimeProcess]:
    chosen = port if port is not None else free_port()
    base_url = f"http://127.0.0.1:{chosen}"
    credential = token if token is not None else mint_runtime_token()
    environment = runtime_environment(database_url, credential)
    if worker_url is not None and worker_token is not None:
        environment["BROWSER_WORKER_URL"] = worker_url
        environment["BROWSER_WORKER_TOKEN"] = worker_token.get_secret_value()
        environment["BROWSER_WORKER_TIMEOUT_SECONDS"] = str(worker_timeout_seconds)
    environment.update(extra_environment or {})
    with log_path.open("wb") as log:
        process = _spawn(runtime_arguments(chosen), environment, log)
        http = RuntimeHttp(base_url, credential)
        try:
            _wait_until_ready(
                process, f"{base_url}/health", log_path, headers=http._headers(None)
            )
            yield RuntimeProcess(Process(process, base_url, log_path), http)
        finally:
            kill(process)


# ---- driving the ledger over HTTP -------------------------------------------


def ok(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code in (200, 201), f"{response.status_code}: {response.text}"
    body: dict[str, Any] = response.json()
    return body


def booking_proposal(slot_id: str, doctor: str, time_iso: str, price: int) -> dict[str, Any]:
    return {
        "site": "appointment_fixture",
        "slot_id": slot_id,
        "doctor": doctor,
        "time": time_iso,
        "price": price,
        "currency": "INR",
    }


def drive_to_approved(
    http: RuntimeHttp, proposal: dict[str, Any], *, idempotency_key: str = "booking-001"
) -> dict[str, Any]:
    """Task -> proposal -> approval request -> approval. Stops short of executing."""
    base_url = http.base_url
    task = ok(
        http.post(
            f"{base_url}/tasks",
            json={"request": {"type": "appointment_booking", "text": "Book Saturday evening"}},
            timeout=30,
        )
    )
    action = ok(
        http.post(
            f"{base_url}/tasks/{task['id']}/actions",
            json={
                "idempotency_key": idempotency_key,
                "tool_name": "commit_booking",
                "risk_tier": "R2",
                "proposal": proposal,
            },
            timeout=30,
        )
    )
    action = ok(
        http.post(
            f"{base_url}/actions/{action['id']}/approval-request",
            json={"expected_revision": action["revision"]},
            timeout=30,
        )
    )
    action = ok(
        http.post(
            f"{base_url}/actions/{action['id']}/approve",
            json={"expected_revision": action["revision"]},
            timeout=30,
        )
    )
    assert action["status"] == "APPROVED"
    return action
