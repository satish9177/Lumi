"""Acceptance: a task and its history survive the runtime going away."""

import os
import socket
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import AGENT_ROOT, Settings
from app.main import create_app
from tests.conftest import running_app

REQUEST = {"type": "appointment_search", "text": "Find a dermatologist appointment Saturday evening"}


async def test_task_survives_application_recreation(settings: Settings, engine: AsyncEngine) -> None:
    first_app = create_app(settings)
    async with running_app(first_app) as client:
        created = (await client.post("/tasks", json={"request": REQUEST})).json()
        cancelled = (await client.post(f"/tasks/{created['id']}/cancel")).json()
        history = (await client.get(f"/tasks/{created['id']}/events")).json()
    # Lifespan exit disposed the first app's engine and connection pool.
    assert first_app.state.engine.pool.checkedout() == 0

    async with running_app(create_app(settings)) as client:
        assert (await client.get(f"/tasks/{created['id']}")).json() == cancelled
        assert (await client.get(f"/tasks/{created['id']}/events")).json() == history


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _start_runtime(
    database_url: str, port: int, log: IO[bytes], token: str
) -> subprocess.Popen[bytes]:
    environment = {**os.environ, "DATABASE_URL": database_url, "LUMI_RUNTIME_TOKEN": token}
    return subprocess.Popen(
        [
            sys.executable, "-m", "app.server", "--port", str(port),
        ],
        cwd=AGENT_ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wait_healthy(
    process: subprocess.Popen[bytes], base_url: str, log_path: Path, token: str
) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"runtime exited early:\n{log_path.read_text(errors='replace')}")
        try:
            if httpx.get(f"{base_url}/health", timeout=1, headers=_headers(token)).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    pytest.fail(f"runtime never became healthy:\n{log_path.read_text(errors='replace')}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=30)


def test_task_survives_runtime_process_kill_and_restart(
    migrated_database_url: str, tmp_path: Path
) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    first_log = tmp_path / "first.log"
    first_token = secrets.token_urlsafe(32)
    with first_log.open("wb") as log:
        first = _start_runtime(migrated_database_url, port, log, first_token)
        try:
            _wait_healthy(first, base_url, first_log, first_token)
            created: dict[str, Any] = httpx.post(
                f"{base_url}/tasks", json={"request": REQUEST}, headers=_headers(first_token)
            ).json()
            before_task = httpx.get(
                f"{base_url}/tasks/{created['id']}", headers=_headers(first_token)
            ).json()
            before_events = httpx.get(
                f"{base_url}/tasks/{created['id']}/events", headers=_headers(first_token)
            ).json()
            assert before_task == created
        finally:
            # A hard kill, not a graceful shutdown: nothing may depend on cleanup.
            _stop(first)
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{base_url}/health", timeout=1)

    second_log = tmp_path / "second.log"
    second_token = secrets.token_urlsafe(32)
    with second_log.open("wb") as log:
        second = _start_runtime(migrated_database_url, port, log, second_token)
        try:
            _wait_healthy(second, base_url, second_log, second_token)
            assert second.pid != first.pid
            assert httpx.get(
                f"{base_url}/tasks/{created['id']}", headers=_headers(first_token)
            ).status_code == 401
            after_task = httpx.get(
                f"{base_url}/tasks/{created['id']}", headers=_headers(second_token)
            )
            after_events = httpx.get(
                f"{base_url}/tasks/{created['id']}/events", headers=_headers(second_token)
            )
        finally:
            _stop(second)

    assert after_task.status_code == 200
    assert after_task.json() == before_task
    assert after_task.json()["status"] == "CREATED"
    assert after_events.json() == before_events
    assert [e["event_type"] for e in after_events.json()["events"]] == ["task.created"]
