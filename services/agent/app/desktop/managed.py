"""The desktop worker as a child process owned by the runtime.

Same shape as the managed browser worker, and for the same reasons: a fixed argument
vector with `shell` never involved, an environment built from an allowlist rather than
copied, a fresh credential for every start, readiness reported over a pipe only the
runtime holds, and a Windows kill-on-close job inherited from the runtime so the worker
dies with it even on a hard kill. The worker also watches the runtime's process handle.

What this environment deliberately lacks is the point: no `DATABASE_URL`, no runtime
bearer token, no provider key, no browser profile path, no cookies, no task history.

`fence` is the containment primitive. A hung accessibility provider can leave the
worker's UIA thread stuck forever, so on any timeout the runtime kills the whole
process immediately and the next call starts a new generation with a new credential.

This is the only module in the package allowed to start a process, and the only thing
it starts is the worker itself, with a fixed argument vector. `tests/desktop_source_scan.py`
pins both facts.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from app.browser.session import generate_worker_token
from app.config import AGENT_ROOT
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import READY_EVENT

logger = logging.getLogger("lumi.desktop.managed")

_INHERITED_KEYS = ("SystemRoot", "WINDIR", "SystemDrive", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE")
_MAX_READY_LINE = 256
WORKER_MODULE = "app.desktop.main"


@dataclass(frozen=True, slots=True)
class DesktopEndpoint:
    base_url: str
    token: SecretStr
    timeout_seconds: float


def worker_environment(
    *,
    token: SecretStr,
    parent_pid: int,
    root_pids: tuple[int, ...],
    timeout_seconds: float,
    trust_job: bool = False,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {key: inherited[key] for key in _INHERITED_KEYS if key in inherited}
    environment.update(
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        LUMI_DESKTOP_TOKEN=token.get_secret_value(),
        LUMI_DESKTOP_PARENT_PID=str(parent_pid),
        LUMI_DESKTOP_EXCLUDED_PIDS=",".join(str(pid) for pid in sorted(set(root_pids))),
        LUMI_DESKTOP_TIMEOUT_SECONDS=str(timeout_seconds),
        LUMI_DESKTOP_TRUST_JOB="1" if trust_job else "0",
    )
    return environment


def _read_ready_line(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    line: bytes = process.stdout.readline(_MAX_READY_LINE + 1)
    return line


def _ready_port(line: bytes) -> int:
    if not line.endswith(b"\n") or len(line) > _MAX_READY_LINE:
        raise ValueError("invalid readiness record")
    value = json.loads(line.decode("ascii"))
    if (
        not isinstance(value, dict)
        or set(value) != {"event", "port"}
        or value["event"] != READY_EVENT
        or not isinstance(value["port"], int)
        or isinstance(value["port"], bool)
        or not 1 <= value["port"] <= 65_535
    ):
        raise ValueError("invalid readiness record")
    port: int = value["port"]
    return port


class ManagedDesktopWorker:
    def __init__(
        self,
        *,
        root_pids: tuple[int, ...],
        timeout_seconds: float,
        startup_timeout_seconds: float = 90.0,
        maximum_failed_starts: int = 4,
        failure_cooldown_seconds: float = 60.0,
        trust_job: bool = False,
        spawn: Callable[[list[str], dict[str, str]], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._root_pids = root_pids
        self._timeout_seconds = timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._maximum_failed_starts = maximum_failed_starts
        self._cooldown = failure_cooldown_seconds
        self._trust_job = trust_job
        self._last_failure = 0.0
        self._spawn = spawn or _spawn
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._endpoint: DesktopEndpoint | None = None
        self._failed_starts = 0
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def endpoint(self) -> DesktopEndpoint:
        """The live worker, started (or replaced) if it is not running."""
        async with self._lock:
            if self._closed:
                raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE)
            if self._process is not None and self._process.poll() is None and self._endpoint:
                return self._endpoint
            await self._terminate_locked(grace=False)
            if self._failed_starts >= self._maximum_failed_starts:
                if time.monotonic() - self._last_failure < self._cooldown:
                    raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE)
                # A crash loop is stopped for a while, not for the life of the runtime.
                self._failed_starts = 0
            return await self._start_locked()

    def mark_healthy(self) -> None:
        """A handshake succeeded: the next crash is a new incident, not a crash loop."""
        self._failed_starts = 0

    async def fence(self) -> None:
        """Kill this generation now. Nothing it says afterwards can be believed."""
        async with self._lock:
            await self._terminate_locked(grace=False)

    async def _start_locked(self) -> DesktopEndpoint:
        token = generate_worker_token()
        arguments = [
            sys.executable, "-m", WORKER_MODULE,
            "--host", "127.0.0.1", "--port", "0", "--log-level", "warning", "--ready-stdout",
        ]
        environment = worker_environment(
            token=token,
            parent_pid=os.getpid(),
            root_pids=self._root_pids,
            timeout_seconds=self._timeout_seconds,
            trust_job=self._trust_job,
        )
        self._failed_starts += 1
        self._last_failure = time.monotonic()
        try:
            process = self._spawn(arguments, environment)
        except OSError:
            raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE) from None
        self._process = process
        try:
            line = await asyncio.wait_for(
                asyncio.to_thread(_read_ready_line, process), timeout=self._startup_timeout
            )
            port = _ready_port(line)
        except (TimeoutError, ValueError):
            await self._terminate_locked(grace=False)
            raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE) from None
        if process.stdout is not None:
            process.stdout.close()
        self._endpoint = DesktopEndpoint(
            base_url=f"http://127.0.0.1:{port}", token=token, timeout_seconds=self._timeout_seconds
        )
        logger.info("desktop worker started worker_pid=%s", process.pid)
        return self._endpoint

    async def _terminate_locked(self, *, grace: bool) -> None:
        process, self._process, self._endpoint = self._process, None, None
        if process is None:
            return
        if process.poll() is None:
            if grace:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()
            else:
                process.kill()
            await asyncio.to_thread(process.wait, 10)
        if process.stdout is not None and not process.stdout.closed:
            # Unblocks a reader thread still waiting for a readiness line.
            threading.Thread(target=process.stdout.close, daemon=True).start()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._terminate_locked(grace=True)


def _spawn(arguments: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        arguments,
        cwd=AGENT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
