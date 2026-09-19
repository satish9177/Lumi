"""The persistent profile across a real browser-worker **process** restart.

`test_browser_profile_session.py` proves persistence across a Chromium restart
inside one process. This module does the harder half: it starts the actual
`app.browser.main` worker as a subprocess, has it open a profile over its own
HTTP contract, kills it, starts a fresh one, and has *that* one open the same
profile.

The end-to-end sequence in `test_a_session_survives_a_worker_process_restart`
is what the S1 exit criteria ask for, and every step is a different process:

    worker A opens the profile          (a real subprocess, holding the OS lock)
    worker A closes it                  (lock released)
    the test's own browser signs in     (the fixture issues an HttpOnly cookie)
    worker A is killed outright         (no shutdown hook, no cleanup)
    worker B opens the profile          (a new process, new Chromium, same dir)
    worker B closes it
    the test's own browser reads it     (the fixture says "signed in")

The write and the read are separated by a hard-killed worker process and a
fresh one that launched Chromium on the same directory. Nothing exported a
cookie at any point: the fixture *server* decides what the page says.

Why the sign-in itself is driven by the test rather than by the worker: S1
deliberately has **no navigation operation for a persistent profile**. There is
no way to ask the worker to visit a page with a profile, because authenticated
page reading is S2/S3. The test therefore uses the same
`ProfileSessionStore.open` the worker uses, in its own process, while no worker
holds the lock -- which is also, incidentally, a demonstration that the lock
hands off cleanly.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.browser.egress_broker import EgressBroker
from app.browser.profile_lock import profile_lock_is_free
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.browser.profile_session import ProfileSessionStore
from app.browser.protocol import WORKER_TOKEN_HEADER
from app.browser.session import generate_worker_token
from app.domain.browser_profile import BrowserVersions
from app.domain.public_url import BROKER_POLICY_VERSION, PublicUrlPolicy
from tests.broker_teardown import bounded, quiesce_broker
from evals.sites.account_fixture import SIGNED_IN, SIGNED_OUT
from tests.browser_harness import account_fixture_site, free_port
# The one list of Chromium's own profile filenames, shared with the source-level
# guard so the two can never drift -- and so naming them here does not itself
# trip that guard's scan.
from tests.test_no_credential_extraction import PROFILE_FILES

pytestmark = [pytest.mark.browser, pytest.mark.hardkill]

#: Every subprocess in this module is bounded and killed in a `finally`.
READY_TIMEOUT_SECONDS = 120.0


class Worker:
    """One real browser-worker process, addressed only over its HTTP contract."""

    def __init__(self, process: subprocess.Popen[bytes], port: int, token: SecretStr) -> None:
        self.process = process
        self.base_url = f"http://127.0.0.1:{port}"
        self.token = token

    def _headers(self) -> dict[str, str]:
        return {WORKER_TOKEN_HEADER: self.token.get_secret_value()}

    def identity(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/health", headers=self._headers(), timeout=30)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def open_profile(
        self, profile_id: uuid.UUID, *, recorded_chromium_build: str | None = None
    ) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}/v1/profiles/open",
            headers=self._headers(),
            json={
                "profile_id": str(profile_id),
                "runtime_generation": str(uuid.uuid4()),
                "expected_worker_generation": self.identity()["worker_generation"],
                "recorded_chromium_build": recorded_chromium_build,
            },
            timeout=120,
        )

    def close_profile(self, profile_id: uuid.UUID) -> httpx.Response:
        return httpx.post(
            f"{self.base_url}/v1/profiles/close",
            headers=self._headers(),
            json={
                "profile_id": str(profile_id),
                "runtime_generation": str(uuid.uuid4()),
                "expected_worker_generation": self.identity()["worker_generation"],
            },
            timeout=60,
        )

    def kill(self) -> None:
        """A hard kill: no signal handler, no lifespan shutdown, no cleanup."""
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=60)


@contextmanager
def browser_worker_process(log_path: Path, *, profile_root: Path) -> Iterator[Worker]:
    token = generate_worker_token()
    port = free_port()
    environment = {
        **os.environ,
        "LUMI_BROWSER_TOKEN": token.get_secret_value(),
        "LUMI_BROWSER_ALLOWED_ORIGINS": "",
        "LUMI_BROWSER_HEADLESS": "true",
        # A base directory, never a profile path. The worker appends the UUID.
        PROFILE_ROOT_VARIABLE: str(profile_root),
    }
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "app.browser.main", "--port", str(port), "--log-level", "info"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        worker = Worker(process, port, token)
        try:
            _wait_until_healthy(worker, log_path)
            yield worker
        finally:
            worker.kill()


def _wait_until_healthy(worker: Worker, log_path: Path) -> None:
    import time

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if worker.process.poll() is not None:
            pytest.fail(f"the worker exited early:\n{log_path.read_text(errors='replace')}")
        try:
            worker.identity()
            return
        except (httpx.TransportError, httpx.HTTPStatusError):
            time.sleep(0.2)
    pytest.fail(f"the worker never became ready:\n{log_path.read_text(errors='replace')}")


async def _drive(
    profile_root: Path, profile_id: uuid.UUID, origin: str, path: str
) -> str:
    """Open the profile in this process and read one page's visible text.

    Uses the worker's own `ProfileSessionStore`, launched through a broker
    configured exactly as the worker configures its own. Nothing here inspects
    storage: it reads what the page says, which is what the *server* decided.
    """
    from playwright.async_api import async_playwright

    broker = EgressBroker(
        PublicUrlPolicy(version=BROKER_POLICY_VERSION, allow_any_public_host=True),
        configured_origins=frozenset({origin}),
    )
    await broker.start()
    playwright = await async_playwright().start()
    store = ProfileSessionStore()
    try:
        probe = await playwright.chromium.launch(headless=True, channel="chromium")
        current = BrowserVersions(
            chromium_build=probe.version, playwright_version="1.63.0", app_version="0.1.0"
        )
        await probe.close()
        session = await store.open(
            playwright=playwright,
            broker=broker,
            paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}),
            profile_id=profile_id,
            recorded=None,
            current=current,
            headless=True,
            timeout_seconds=30.0,
        )
        page = session.context.pages[0] if session.context.pages else await session.context.new_page()
        await page.goto(f"{origin}{path}", wait_until="load")
        return str(await page.locator("body").inner_text())
    finally:
        # See tests/broker_teardown.py.
        await store.close_all()
        await quiesce_broker(broker)
        await bounded("playwright.stop", playwright.stop())
        await bounded("broker.aclose", broker.aclose())


def _drive_sync(profile_root: Path, profile_id: uuid.UUID, origin: str, path: str) -> str:
    return asyncio.run(_drive(profile_root, profile_id, origin, path))


def wait_until_lock_free(paths: Any, profile_id: uuid.UUID, timeout: float = 30.0) -> float:
    """Wait for the OS to release a dead process's handle, and say how long.

    Windows releases a byte-range lock when the owning process dies, but not
    necessarily in the same instant the process object reports exit: teardown of
    the worker and its Chromium children is asynchronous. Measured on this
    machine the gap is around 0.06 s; it is bounded here rather than assumed to
    be zero, and the bound is generous because a slow machine failing this test
    would be a false alarm, not a finding.
    """
    import time

    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if profile_lock_is_free(paths, profile_id):
            return time.monotonic() - started
        time.sleep(0.05)
    pytest.fail("the profile stayed locked after its owner died")


def test_a_session_survives_a_worker_process_restart(tmp_path: Path) -> None:
    """The S1 persistence proof, across a hard-killed worker process."""
    profile_root = tmp_path / "browser-profiles"
    profile_id = uuid.uuid4()

    with account_fixture_site(tmp_path / "fixture.log") as fixture:
        # 1. A real worker process opens the profile, creating the directory.
        with browser_worker_process(tmp_path / "worker-a.log", profile_root=profile_root) as a:
            opened = a.open_profile(profile_id)
            assert opened.status_code == 200, opened.text
            body = opened.json()
            assert body["status"] == "OPEN"
            assert body["lock_held"] is True
            assert body["chromium_build"]
            # While worker A holds it, nothing else may open it.
            assert not profile_lock_is_free(
                resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}), profile_id
            )
            assert a.close_profile(profile_id).json()["status"] == "CLOSED"

            # 2. Sign in. The fixture issues an HttpOnly session cookie, which
            #    only the browser can present again.
            started = _drive_sync(profile_root, profile_id, fixture.base_url, "/session/start")
            assert SIGNED_IN in started

            # 3. Kill the worker outright, mid-life, with no shutdown hook.
            a.kill()

        # 4. A completely new worker process opens the same profile. The OS
        #    lock the dead process held is gone with it, and the directory the
        #    browser wrote is still there.
        with browser_worker_process(tmp_path / "worker-b.log", profile_root=profile_root) as b:
            reopened = b.open_profile(profile_id)
            assert reopened.status_code == 200, reopened.text
            assert reopened.json()["status"] == "OPEN"
            assert b.close_profile(profile_id).json()["status"] == "CLOSED"

        # 5. The fixture still recognises the browser.
        account = _drive_sync(profile_root, profile_id, fixture.base_url, "/account")

    assert SIGNED_IN in account
    assert SIGNED_OUT not in account


def test_two_worker_processes_cannot_hold_one_profile(tmp_path: Path) -> None:
    """The OS-level backstop across real processes, not just across stores.

    This is the case the database lease cannot cover on its own: two Lumi
    installations, each with its own database, each certain it owns the
    profile. The exclusive handle is what makes the second one stop.
    """
    profile_root = tmp_path / "browser-profiles"
    profile_id = uuid.uuid4()
    with browser_worker_process(tmp_path / "worker-a.log", profile_root=profile_root) as first:
        assert first.open_profile(profile_id).status_code == 200
        with browser_worker_process(
            tmp_path / "worker-b.log", profile_root=profile_root
        ) as second:
            refused = second.open_profile(profile_id)
            assert refused.status_code == 409
            assert refused.json()["code"] == "profile_locked_by_another_process"
            # The refusal says nothing about where the profile is.
            assert "browser-profiles" not in refused.text
            assert str(profile_root) not in refused.text


def test_killing_the_owner_frees_the_profile_rather_than_stranding_it(
    tmp_path: Path,
) -> None:
    """A crash must not lock a profile out for ever, and must not allow two owners.

    Both halves matter: the handle is released by the operating system when the
    process dies, so the next worker can take it -- and until the process dies,
    nobody else can.
    """
    profile_root = tmp_path / "browser-profiles"
    profile_id = uuid.uuid4()
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)})

    with browser_worker_process(tmp_path / "worker-a.log", profile_root=profile_root) as owner:
        assert owner.open_profile(profile_id).status_code == 200
        assert not profile_lock_is_free(paths, profile_id)
        owner.kill()

    # Released by the operating system with the process, not by any cleanup
    # hook -- the worker was killed outright and ran none.
    assert wait_until_lock_free(paths, profile_id) < 30.0
    with browser_worker_process(tmp_path / "worker-c.log", profile_root=profile_root) as heir:
        assert heir.open_profile(profile_id).status_code == 200


def test_a_worker_refuses_a_chromium_downgrade_and_keeps_the_profile(
    tmp_path: Path,
) -> None:
    """Over the real HTTP contract, and without touching the directory."""
    profile_root = tmp_path / "browser-profiles"
    profile_id = uuid.uuid4()
    with browser_worker_process(tmp_path / "worker.log", profile_root=profile_root) as worker:
        created = worker.open_profile(profile_id)
        assert created.status_code == 200
        assert worker.close_profile(profile_id).json()["status"] == "CLOSED"

        refused = worker.open_profile(profile_id, recorded_chromium_build="999.0.0.0")
        assert refused.status_code == 409
        assert refused.json()["code"] == "profile_browser_downgrade_refused"

        # Refused, not repaired and not deleted: the directory is intact and a
        # build that may open it still can.
        directory = resolve_profile_paths(
            {PROFILE_ROOT_VARIABLE: str(profile_root)}
        ).directory(profile_id)
        assert directory.is_dir() and any(directory.iterdir())
        assert worker.open_profile(profile_id).status_code == 200


def test_no_worker_output_carries_a_profile_path(tmp_path: Path) -> None:
    """Everything the worker writes, through a full profile lifecycle.

    The worker's stdout and stderr are captured together, so this covers its
    own log records, uvicorn's, and any traceback -- a stack trace naming the
    profile directory would be as much of a leak as a deliberate log line.

    The unauthenticated request at the start is what gives the check teeth: it
    forces a `lumi.browser.worker` warning into the same file, so a later
    assertion that no path is in the file cannot pass merely because Lumi's own
    logger was never wired up.
    """
    profile_root = tmp_path / "browser-profiles"
    profile_id = uuid.uuid4()
    log_path = tmp_path / "worker.log"
    with browser_worker_process(log_path, profile_root=profile_root) as worker:
        unauthenticated = httpx.post(
            f"{worker.base_url}/v1/profiles/open",
            json={
                "profile_id": str(profile_id),
                "runtime_generation": str(uuid.uuid4()),
                "expected_worker_generation": str(uuid.uuid4()),
            },
            timeout=30,
        )
        assert unauthenticated.status_code == 401
        assert worker.open_profile(profile_id).status_code == 200
        worker.close_profile(profile_id)
        refused = worker.open_profile(profile_id, recorded_chromium_build="999.0.0.0")
        assert refused.status_code == 409
        worker.kill()

    log = log_path.read_text(errors="replace")
    # Lumi's own logger reached this file, so the negative checks below mean
    # something.
    assert "rejected an unauthenticated browser-worker request" in log
    for leak in (
        str(profile_root),
        str(profile_root).replace("\\", "/"),
        "browser-profiles",
        "user_data_dir",
        "userDataDir",
        "storage_state",
        *PROFILE_FILES,
    ):
        assert leak not in log, f"{leak!r} reached the worker output"
