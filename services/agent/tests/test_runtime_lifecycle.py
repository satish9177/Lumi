import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.parent_watchdog import parent_liveness, watch_liveness
from app.services.runtime import RuntimeAlreadyActiveError, runtime_ownership
from app.server import _ReadyServer
import uvicorn


async def test_watchdog_exits_when_open_parent_reference_reports_dead() -> None:
    exits: list[int] = []
    await watch_liveness(lambda: False, poll_seconds=0, exit_process=exits.append)
    assert exits == [0]


async def test_failed_server_startup_never_emits_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_startup(
        server: uvicorn.Server, sockets: list[object] | None = None
    ) -> None:
        server.started = False
        server.should_exit = True

    monkeypatch.setattr(uvicorn.Server, "startup", failed_startup)
    server = _ReadyServer(uvicorn.Config("app.main:create_app", factory=True, port=43123))
    with pytest.raises(RuntimeError, match="stopped before binding"):
        await server.startup()


@pytest.mark.skipif(os.name != "nt", reason="Windows process-handle regression")
def test_windows_liveness_probe_never_terminates_the_process() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with parent_liveness(child.pid) as is_alive:
            assert is_alive()
            assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)

    try:
        with parent_liveness(child.pid) as is_alive:
            assert not is_alive()
    except OSError:
        pass


def _windows_process_alive(pid: int) -> bool:
    try:
        with parent_liveness(pid) as is_alive:
            return is_alive()
    except OSError:
        return False


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object regression")
def test_runtime_job_kills_a_real_descendant_when_owner_is_hard_killed() -> None:
    helper = (
        "import subprocess,sys,time; "
        "from app.services.windows_job import configure_runtime_process_tree; "
        "configure_runtime_process_tree(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(child.pid,flush=True); time.sleep(60)"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", helper],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert owner.stdout is not None
    line = owner.stdout.readline().strip()
    if not line:
        assert owner.stderr is not None
        pytest.fail(f"job owner failed: {owner.stderr.read()}")
    descendant_pid = int(line)
    assert _windows_process_alive(descendant_pid)

    owner.kill()
    owner.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _windows_process_alive(descendant_pid):
        time.sleep(0.05)
    assert not _windows_process_alive(descendant_pid)


async def test_only_one_runtime_can_own_a_database(engine: AsyncEngine) -> None:
    async with runtime_ownership(engine):
        with pytest.raises(RuntimeAlreadyActiveError):
            async with runtime_ownership(engine):
                pytest.fail("a second runtime acquired ownership")
