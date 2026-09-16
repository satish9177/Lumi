"""Acceptance (Milestone 6, Scenario D): the packaged Lumi application.

Launches `release/<version>/win-unpacked/Lumi.exe` (or `LUMI_PACKAGED_EXE`)
with an isolated profile and a scrubbed environment: no repository `.env`, no
`LUMI_*` development flags, no manually started Python server, no developer
shell. The only configuration is the profile's `agent-runtime.json`, which
points at the `_test` database and asks for the bundled demo clinic site.

Expected: Electron opens; the bundled Python runtime migrates the schema and
starts; its runtime-owned browser worker (bundled Chromium) drives the bundled
demo site; a typed request is answered by deterministic rules (no model keys
are configured); the trusted click books once; the task survives an Electron
restart; every child process exits with the app.

Opt in with `LUMI_PACKAGED_E2E=1` after `npm run package:dir`.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import truncate_all
from tests.test_electron_acceptance import (
    BOOK_SECONDS,
    REPO_ROOT,
    Desktop,
    _database_counts,
    _free_port,
    _off_thread,
    _processes,
    _wait_for_no_processes,
    playwright,  # noqa: F401 - fixture
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_PACKAGED_E2E") != "1",
        reason="set LUMI_PACKAGED_E2E=1 after `npm run package:dir`",
    ),
]

SCRUBBED_PREFIXES = ("LUMI_", "LIFELENS_", "OPENAI", "DEEPSEEK", "GOOGLE_", "ELECTRON_", "DATABASE_URL", "TEST_DATABASE_URL", "PLAYWRIGHT_", "VIRTUAL_ENV", "UV_")


def _executable() -> Path:
    configured = os.environ.get("LUMI_PACKAGED_EXE")
    if configured:
        return Path(configured)
    version: str = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    return REPO_ROOT / "release" / version / "win-unpacked" / "Lumi.exe"


@contextmanager
def packaged(playwright: Any, profile: Path, log_path: Path) -> Iterator[Desktop]:  # noqa: F811
    port = _free_port()
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(SCRUBBED_PREFIXES)}
    # Start outside the checkout, so nothing can resolve relative to it.
    working_directory = profile.parent
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [str(_executable()), f"--user-data-dir={profile}", f"--remote-debugging-port={port}"],
            cwd=working_directory, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        browser = None
        try:
            deadline = time.monotonic() + 90
            # Attach only once the renderer target exists: attaching to a
            # packaged Electron before its window is created intermittently
            # left Playwright without the page (see docs/reviews/milestone-6.md).
            while True:
                assert process.poll() is None, log_path.read_text(errors="replace")
                assert time.monotonic() < deadline, "the packaged renderer never appeared"
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as reply:
                        targets = json.load(reply)
                    if any(target.get("type") == "page" and str(target.get("url", "")).endswith("/renderer/index.html") for target in targets):
                        break
                except OSError:
                    pass
                time.sleep(0.5)
            while True:
                assert process.poll() is None, log_path.read_text(errors="replace")
                try:
                    browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:  # noqa: BLE001 - DevTools is not listening yet.
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.5)
            page = None
            while page is None:
                for context in browser.contexts:
                    for candidate in context.pages:
                        if candidate.url.startswith("file://") and candidate.url.endswith("/renderer/index.html"):
                            page = candidate
                if page is None:
                    assert time.monotonic() < deadline, (
                        "the packaged renderer never loaded: "
                        f"{[candidate.url for context in browser.contexts for candidate in context.pages]} "
                        f"alive={process.poll() is None}"
                    )
                    time.sleep(0.25)
            console: list[str] = []
            page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
            page.wait_for_load_state("domcontentloaded")
            yield Desktop(process, page, console)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


def _ask(app: Desktop, request: str, expect: str) -> str:
    app.page.get_by_test_id("agent-request-input").fill(request)
    app.page.get_by_role("button", name="Send request").click()
    outcome = app.page.get_by_test_id("agent-request-outcome").filter(has_text=expect)
    outcome.wait_for(timeout=BOOK_SECONDS * 1000)
    reply: str = outcome.inner_text()
    return reply


def _quit_gracefully(app: Desktop) -> None:
    """Close the window the way a user would, then wait for the app to exit."""
    app.page.evaluate("() => window.close()")
    try:
        app.process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        app.process.kill()
        app.process.wait(timeout=30)


def test_scenario_d_packaged_app_runs_the_bundled_runtime(
    migrated_database_url: str, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    exe = _executable()
    if not exe.is_file():
        pytest.fail(f"packaged app not found at {exe}; run `npm run package:dir`")
    resources = exe.parent / "resources" / "agent-runtime"
    assert (resources / "python" / "python.exe").is_file()
    assert not list(resources.rglob(".env")), "a .env file was bundled"
    manifest = json.loads((resources / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["browsers"] == "chromium"

    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    test_started = time.time()
    profile = tmp_path / "packaged-profile"
    log_path = tmp_path / "packaged.log"

    # Without configuration the app still opens and says what is missing.
    with packaged(playwright, profile, log_path) as unconfigured:
        unconfigured.page.get_by_role("button", name="Open Lumi").click()
        unconfigured.page.get_by_test_id("open-agent-tasks").click()
        unconfigured.page.get_by_test_id("agent-runtime-state").filter(has_text="needs setup").wait_for(timeout=30_000)
        assert (profile / "agent-runtime.example.json").is_file()
        _quit_gracefully(unconfigured)
    assert not _processes("*app.server*")

    (profile / "agent-runtime.json").write_text(
        json.dumps({"databaseUrl": migrated_database_url, "clinicSite": "demo"}), encoding="utf-8"
    )
    with packaged(playwright, profile, log_path) as app:
        started = time.monotonic()
        app.open_tasks()  # waits for "Agent runtime connected"
        startup_seconds = time.monotonic() - started
        servers = _processes("*app.server*")
        assert servers, "the bundled runtime is not running"
        found = _ask(app, "Find me a dermatologist Saturday evening under 1000", "Found 2 matching appointments")
        assert "Found 2" in found
        _ask(app, "Show me the cheapest one and prepare it", "Nothing is booked until you press Approve and book")
        card = app.wait_for_status("WAITING_APPROVAL", timeout=90)
        assert "Dr A" in card.inner_text()
        assert _database_counts(migrated_database_url)["attempts"] == 0
        card.get_by_role("button", name="Approve and book").click()
        done = app.wait_for_status("SUCCEEDED")
        assert "BK-0001" in done.inner_text()
        task_id = app.page.get_by_test_id("agent-task-id").text_content()
        counts = _database_counts(migrated_database_url)
        assert (counts["tasks"], counts["actions"], counts["attempts"], counts["consequential"], counts["submitted"]) == (1, 1, 1, 1, 1)
        print(f"PACKAGED COUNTS: {counts} startup_seconds={startup_seconds:.1f}")
        _quit_gracefully(app)

    # Every child of the packaged app exits with it.
    _wait_for_no_processes("*app.server*")
    _wait_for_no_processes("*app.browser.main*")

    with packaged(playwright, profile, log_path) as restarted:
        restarted.open_tasks()
        restarted.wait_for_status("SUCCEEDED", timeout=60)
        assert restarted.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert _database_counts(migrated_database_url)["attempts"] == 1
        _quit_gracefully(restarted)
    _wait_for_no_processes("*app.server*")
    # The installed runtime wrote nothing beside its own code.
    assert not [path for path in resources.rglob("*.pyc") if path.stat().st_mtime > test_started]
    shutil.rmtree(profile, ignore_errors=True)
