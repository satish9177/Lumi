"""The browser worker as a child process owned by the runtime.

The runtime launches the worker with a fixed argument vector, `shell` never
involved, and an environment built from an allowlist: a fresh worker credential,
the one reviewed site origin, and the handful of OS variables Chromium needs.
It never inherits `DATABASE_URL`, the runtime bearer credential, or any provider
key, because the environment is constructed rather than copied.

Being a child matters on Windows: `app.server` places itself in a kill-on-close
job, so the worker and its Chromium die with the runtime even on a hard kill.
The worker also watches the runtime's process handle as a second line.

Every (re)start mints a new credential. A worker that died is replaced on the
next use, a bounded number of times; nothing here retries a dispatch.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from app.browser.config import DEFAULT_SITE
from app.browser.errors import BrowserWorkerUnavailableError
from app.browser.main import READY_EVENT
from app.browser.session import generate_worker_token
from app.config import AGENT_ROOT

logger = logging.getLogger("lumi.browser.managed")

_INHERITED_KEYS = (
    "SystemRoot",
    "WINDIR",
    "SystemDrive",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONDONTWRITEBYTECODE",
)
_MAX_READY_LINE = 256


@dataclass(frozen=True, slots=True)
class WorkerEndpoint:
    base_url: str
    token: SecretStr
    timeout_seconds: float


def worker_environment(
    *, token: SecretStr, site_origin: str | None, headless: bool, parent_pid: int,
    public_hosts: str = "", inspection_test_origins: str = "",
    research_any_public_host: bool = False, research_hosts: str = "",
    research_test_origins: str = "", research_max_tabs: int = 5,
    profile_root: str = "", app_version: str = "", auth_test_origins: str = "",
    quarantine_root: str = "",
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {key: inherited[key] for key in _INHERITED_KEYS if key in inherited}
    environment.update(
        PYTHONUTF8="1",
        PYTHONUNBUFFERED="1",
        LUMI_BROWSER_TOKEN=token.get_secret_value(),
        LUMI_BROWSER_ALLOWED_ORIGINS=f"{DEFAULT_SITE}={site_origin}" if site_origin else "",
        LUMI_BROWSER_HEADLESS="true" if headless else "false",
        LUMI_BROWSER_PARENT_PID=str(parent_pid),
        LUMI_BROWSER_PUBLIC_HOSTS=public_hosts,
        LUMI_BROWSER_INSPECTION_TEST_ORIGINS=inspection_test_origins,
        LUMI_BROWSER_RESEARCH_ANY_PUBLIC_HOST="true" if research_any_public_host else "false",
        LUMI_BROWSER_RESEARCH_HOSTS=research_hosts,
        LUMI_BROWSER_RESEARCH_TEST_ORIGINS=research_test_origins,
        LUMI_BROWSER_RESEARCH_MAX_TABS=str(research_max_tabs),
        # A *base* directory for persistent profiles, not a profile path: the
        # worker appends the profile UUID itself. Empty means the worker
        # derives `%LOCALAPPDATA%\Lumi\browser-profiles` from the LOCALAPPDATA
        # it already inherits.
        LUMI_BROWSER_PROFILE_ROOT=profile_root,
        LUMI_BROWSER_AUTH_TEST_ORIGINS=auth_test_origins,
        # Milestone 10 S2: a *base* directory; the worker appends the transfer UUID itself.
        LUMI_BROWSER_QUARANTINE_ROOT=quarantine_root,
    )
    if app_version:
        environment["LUMI_BROWSER_APP_VERSION"] = app_version
    return environment


def _read_ready_line(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    line: bytes = process.stdout.readline(_MAX_READY_LINE + 1)
    return line


class ManagedBrowserWorker:
    def __init__(
        self,
        *,
        site_origin: str | None,
        headless: bool,
        timeout_seconds: float,
        public_hosts: str = "",
        inspection_test_origins: str = "",
        research_any_public_host: bool = False,
        research_hosts: str = "",
        research_test_origins: str = "",
        research_max_tabs: int = 5,
        profile_root: str = "",
        app_version: str = "",
        auth_test_origins: str = "",
        quarantine_root: str = "",
        startup_timeout_seconds: float = 60.0,
        maximum_starts: int = 4,
        spawn: Callable[[list[str], dict[str, str]], subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._site_origin = site_origin
        self._public_hosts = public_hosts
        self._inspection_test_origins = inspection_test_origins
        self._research_any_public_host = research_any_public_host
        self._research_hosts = research_hosts
        self._research_test_origins = research_test_origins
        self._research_max_tabs = research_max_tabs
        self._profile_root = profile_root
        self._app_version = app_version
        self._auth_test_origins = auth_test_origins
        self._quarantine_root = quarantine_root
        self._headless = headless
        self._timeout_seconds = timeout_seconds
        self._startup_timeout = startup_timeout_seconds
        self._maximum_starts = maximum_starts
        self._spawn = spawn or _spawn
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._endpoint: WorkerEndpoint | None = None
        self._starts = 0
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def endpoint(self) -> WorkerEndpoint:
        """The live worker, started (or replaced) if it is not running."""
        async with self._lock:
            if self._closed:
                raise BrowserWorkerUnavailableError("the runtime is shutting down")
            if self._process is not None and self._process.poll() is None and self._endpoint:
                return self._endpoint
            await self._terminate_locked()
            if self._starts >= self._maximum_starts:
                raise BrowserWorkerUnavailableError("the browser worker kept exiting")
            self._starts += 1
            return await self._start_locked()

    async def warm_up(self) -> None:
        try:
            await self.endpoint()
        except BrowserWorkerUnavailableError:
            logger.warning("browser worker did not start during runtime startup")

    async def _start_locked(self) -> WorkerEndpoint:
        token = generate_worker_token()
        arguments = [
            sys.executable, "-m", "app.browser.main",
            "--host", "127.0.0.1", "--port", "0", "--log-level", "warning", "--ready-stdout",
        ]
        environment = worker_environment(
            token=token,
            site_origin=self._site_origin,
            headless=self._headless,
            parent_pid=os.getpid(),
            public_hosts=self._public_hosts,
            inspection_test_origins=self._inspection_test_origins,
            research_any_public_host=self._research_any_public_host,
            research_hosts=self._research_hosts,
            research_test_origins=self._research_test_origins,
            research_max_tabs=self._research_max_tabs,
            profile_root=self._profile_root,
            app_version=self._app_version,
            auth_test_origins=self._auth_test_origins,
            quarantine_root=self._quarantine_root,
        )
        try:
            process = self._spawn(arguments, environment)
        except OSError:
            raise BrowserWorkerUnavailableError("the browser worker could not be launched") from None
        self._process = process
        try:
            line = await asyncio.wait_for(
                asyncio.to_thread(_read_ready_line, process), timeout=self._startup_timeout
            )
            port = _ready_port(line)
        except (TimeoutError, ValueError):
            await self._terminate_locked()
            raise BrowserWorkerUnavailableError("the browser worker did not become ready") from None
        if process.stdout is not None:
            process.stdout.close()
        self._endpoint = WorkerEndpoint(
            base_url=f"http://127.0.0.1:{port}", token=token, timeout_seconds=self._timeout_seconds
        )
        logger.info("browser worker started", extra={"worker_pid": process.pid})
        return self._endpoint

    async def _terminate_locked(self) -> None:
        process, self._process, self._endpoint = self._process, None, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 10)
            except subprocess.TimeoutExpired:
                process.kill()
                await asyncio.to_thread(process.wait, 10)
        if process.stdout is not None and not process.stdout.closed:
            # Unblocks a reader thread still waiting for a readiness line.
            threading.Thread(target=process.stdout.close, daemon=True).start()

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._terminate_locked()


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


def _spawn(arguments: list[str], environment: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        arguments,
        cwd=AGENT_ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        # Inherit stderr: the runtime's own stderr is already controlled by its
        # owner (ignored by Electron, captured to a file by tests).
        stderr=None,
        shell=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
