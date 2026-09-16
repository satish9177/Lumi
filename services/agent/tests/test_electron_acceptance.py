"""Acceptance: the real Lumi desktop app drives the durable booking ledger.

These tests launch the built Electron app (`npm run build` first), drive its
rendered controls over the Chrome DevTools Protocol, and count what happened in
PostgreSQL and on the fixture site. Nothing here calls the runtime API: every
step goes renderer -> preload -> Electron main -> authenticated runtime.

Opt in with `LUMI_ELECTRON_E2E=1` (they need a fresh desktop build).

Isolation: a temporary Electron profile (`LUMI_USER_DATA_DIR`), the `_test`
database (`LUMI_AGENT_DATABASE_URL`), and a fixture site started here, which
outlives every Electron/runtime/worker restart.
"""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import AGENT_ROOT
from tests.browser_harness import SiteControl, fixture_site
from tests.conftest import truncate_all

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_ELECTRON_E2E") != "1",
        reason="set LUMI_ELECTRON_E2E=1 after `npm run build` to drive the real desktop app",
    ),
]

REPO_ROOT = AGENT_ROOT.parents[1]
STARTUP_SECONDS = 120.0
BOOK_SECONDS = 150.0


def _off_thread(function: Any, *args: Any) -> Any:
    """Run blocking asyncio helpers away from sync Playwright's own event loop."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(function, *args).result()


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _electron_executable() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to locate Electron")
    path = subprocess.run(
        [node, "-p", "require('electron')"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    if not Path(path).is_file():
        pytest.fail(f"Electron binary not found at {path}")
    return path


def _processes(pattern: str) -> list[int]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and "
        f"$_.CommandLine -like '{pattern}' }} | Select-Object -ExpandProperty ProcessId | ConvertTo-Json"
    )
    output = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.strip()
    if not output:
        return []
    parsed = json.loads(output)
    return parsed if isinstance(parsed, list) else [parsed]


def _wait_for_no_processes(pattern: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while _processes(pattern):
        if time.monotonic() > deadline:
            pytest.fail(f"processes matching {pattern!r} outlived their owner: {_processes(pattern)}")
        time.sleep(0.5)


@dataclass
class Counts:
    tasks: int
    actions: int
    attempts: int
    consequential_dispatches: int
    submitted_dispatches: int
    lookup_dispatches: int
    fixture_submissions: int
    bookings: int
    submissions_by_reference: dict[str, int]


def _database_counts(database_url: str) -> dict[str, int]:
    async def run() -> dict[str, int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                return {
                    "tasks": await connection.scalar(text("SELECT count(*) FROM tasks")) or 0,
                    "actions": await connection.scalar(text("SELECT count(*) FROM actions")) or 0,
                    "attempts": await connection.scalar(text("SELECT count(*) FROM action_attempts")) or 0,
                    "consequential": await connection.scalar(
                        text("SELECT count(*) FROM browser_dispatches WHERE effect = 'CONSEQUENTIAL'")
                    ) or 0,
                    "submitted": await connection.scalar(
                        text("SELECT count(*) FROM browser_dispatches WHERE submitted")
                    ) or 0,
                    "lookups": await connection.scalar(
                        text(
                            "SELECT count(*) FROM browser_dispatches "
                            "WHERE operation = 'lookup_booking' AND effect = 'READ_ONLY' AND attempt_id IS NULL"
                        )
                    ) or 0,
                }
        finally:
            await engine.dispose()

    result: dict[str, int] = _off_thread(asyncio.run, run())
    return result


def _counts(database_url: str, control: SiteControl) -> Counts:
    database = _database_counts(database_url)
    state = control.state()
    return Counts(
        tasks=database["tasks"],
        actions=database["actions"],
        attempts=database["attempts"],
        consequential_dispatches=database["consequential"],
        submitted_dispatches=database["submitted"],
        lookup_dispatches=database["lookups"],
        fixture_submissions=state["submissions"],
        bookings=state["booking_count"],
        submissions_by_reference=state["submissions_by_reference"],
    )


def _action_row(database_url: str, action_id: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        text(
                            "SELECT a.status, count(t.id) AS attempts, "
                            "count(t.id) FILTER (WHERE t.finished_at IS NULL) AS unfinished "
                            "FROM actions a LEFT JOIN action_attempts t ON t.action_id = a.id "
                            "WHERE a.id = :id GROUP BY a.status"
                        ),
                        {"id": action_id},
                    )
                ).one()
                return dict(row._mapping)
        finally:
            await engine.dispose()

    row: dict[str, Any] = _off_thread(asyncio.run, run())
    return row


class Desktop:
    """One running Electron instance and its renderer page."""

    def __init__(self, process: subprocess.Popen[bytes], page: Any, console: list[str]) -> None:
        self.process = process
        self.page = page
        self.console = console

    def open_tasks(self) -> None:
        page = self.page
        if not page.get_by_test_id("agent-task-panel").count():
            if not page.get_by_role("button", name="Collapse to orb").count():
                page.get_by_role("button", name="Open Lumi").click()
            page.get_by_test_id("open-agent-tasks").click()
        page.get_by_test_id("agent-runtime-state").filter(has_text="Agent runtime connected").wait_for(
            timeout=STARTUP_SECONDS * 1000
        )

    def card(self) -> Any:
        return self.page.get_by_test_id("agent-booking-card")

    def wait_for_status(self, status: str, timeout: float = BOOK_SECONDS) -> Any:
        card = self.page.locator(f'[data-testid="agent-booking-card"][data-action-status="{status}"]')
        card.wait_for(timeout=timeout * 1000)
        return card

    def start_task(self, specialty: str = "Dermatology", day: str = "Saturday") -> str:
        page = self.page
        page.get_by_label("Specialty").fill(specialty)
        page.get_by_label("Day").select_option(day)
        page.get_by_role("button", name="Start booking task").click()
        page.locator('[data-event-type="task.created"]').wait_for(timeout=30_000)
        task_id = page.get_by_test_id("agent-task-id").text_content()
        assert isinstance(task_id, str) and len(task_id) == 36, task_id
        return task_id

    def prepare(self, slot_id: str) -> str:
        page = self.page
        page.get_by_role("button", name="Search appointments").click()
        slot = page.locator(f'li[data-slot-id="{slot_id}"]')
        slot.wait_for(timeout=BOOK_SECONDS * 1000)
        slot.get_by_role("button", name="Prepare booking").click()
        card = self.wait_for_status("WAITING_APPROVAL")
        action_id: str = card.get_attribute("data-action-id")
        return action_id

    def buttons(self) -> list[str]:
        return [label.strip() for label in self.card().get_by_role("button").all_inner_texts()]

    def timeline_sequences(self) -> list[int]:
        return [
            int(value)
            for value in self.page.locator('[data-testid="agent-timeline"] li').evaluate_all(
                "items => items.map(item => item.dataset.sequence)"
            )
        ]

    def hard_kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)


@contextmanager
def desktop(
    playwright: Any, *, profile: Path, database_url: str, site_origin: str, log_path: Path
) -> Iterator[Desktop]:
    port = _free_port()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ELECTRON_RENDERER_URL", "OPENAI_API_KEY", "DATABASE_URL", "ELECTRON_RUN_AS_NODE"}
        and not key.startswith("LUMI_")
    }
    environment.update(
        LUMI_USER_DATA_DIR=str(profile),
        LUMI_AGENT_DATABASE_URL=database_url,
        LUMI_APPOINTMENT_FIXTURE_ORIGIN=site_origin,
    )
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [_electron_executable(), str(REPO_ROOT), f"--remote-debugging-port={port}"],
            cwd=REPO_ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
        )
        browser = None
        try:
            deadline = time.monotonic() + 60
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
                        if candidate.url.endswith("/renderer/index.html"):
                            page = candidate
                if page is None:
                    assert time.monotonic() < deadline, "the Lumi renderer never loaded"
                    time.sleep(0.25)
            console: list[str] = []
            page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
            page.wait_for_load_state("domcontentloaded")
            yield Desktop(process, page, console)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 - the app may have been killed.
                    pass
            if process.poll() is None:
                process.kill()
                process.wait(timeout=30)


@pytest.fixture
def site(tmp_path: Path) -> Iterator[SiteControl]:
    with fixture_site(tmp_path / "site.log") as process:
        control = SiteControl(process.base_url)
        control.reset()
        yield control


@pytest.fixture
def playwright() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as instance:
        yield instance


def _no_csp_violations(console: list[str]) -> None:
    violations = [line for line in console if re.search(r"Content Security Policy|Refused to", line)]
    assert violations == [], violations


def test_normal_booking_through_the_desktop_app(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log",
    ) as app:
        app.open_tasks()
        app.start_task()
        action_id = app.prepare("slot-a-1830")
        reference = f"lumi-{action_id}"

        card = app.card()
        details = card.inner_text()
        assert "Dr A" in details and "₹800" in details
        assert app.buttons() == ["Reject", "Approve and book"]
        assert _counts(migrated_database_url, site).fixture_submissions == 0

        card.get_by_role("button", name="Approve and book").click()
        done = app.wait_for_status("SUCCEEDED")
        assert app.page.get_by_test_id("agent-booking-title").inner_text() == "Booking confirmed"
        assert "BK-0001" in done.inner_text()
        assert app.buttons() == []

        # Timeline is ordered, complete and free of duplicates.
        app.page.locator('[data-event-type="action.succeeded"]').wait_for(timeout=10_000)
        sequences = app.timeline_sequences()
        assert sequences == list(range(1, len(sequences) + 1))

        counts = _counts(migrated_database_url, site)
        assert counts == Counts(
            tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=1, lookup_dispatches=0,
            fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
        )
        print(f"NORMAL BOOKING COUNTS: {counts}")

        # Renderer reload restores the same task and timeline.
        app.page.reload()
        app.open_tasks()
        app.wait_for_status("SUCCEEDED")
        assert app.page.get_by_test_id("agent-action-id").first.text_content() == action_id
        assert app.timeline_sequences() == sequences

        # --- changed price: known failure, facts shown, new review required --
        app.page.get_by_role("button", name="Close task").click()
        app.start_task()
        changed_id = app.prepare("slot-b-1915")
        site.set_faults(price_overrides={"slot-b-1915": 1100})
        app.card().get_by_role("button", name="Approve and book").click()
        failed = app.wait_for_status("FAILED")
        text_now = failed.inner_text()
        assert "The appointment changed after approval" in text_now
        assert "₹950" in text_now and "₹1,100" in text_now
        assert "Nothing was booked." in text_now
        assert "Book now" not in app.buttons()
        failed.get_by_role("button", name="Review updated details").click()
        reviewed = app.wait_for_status("WAITING_APPROVAL")
        assert reviewed.get_attribute("data-action-id") != changed_id
        assert "₹1,100" in reviewed.inner_text()
        reviewed.get_by_role("button", name="Reject").click()
        app.wait_for_status("REJECTED")

        # --- missing slot: known failure, nothing booked ----------------------
        site.set_faults()
        app.page.get_by_role("button", name="Close task").click()
        app.start_task(specialty="Dentistry")
        app.prepare("slot-c-1000")
        site.set_faults(removed_slots=["slot-c-1000"])
        app.card().get_by_role("button", name="Approve and book").click()
        gone = app.wait_for_status("FAILED")
        assert "This appointment is no longer available" in gone.inner_text()

        after = _counts(migrated_database_url, site)
        assert after.bookings == 1 and after.fixture_submissions == 1
        assert after.submitted_dispatches == 1
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")
    _wait_for_no_processes("*app.browser.main*--ready-stdout*")


def test_lost_response_hard_restart_is_reconciled_not_repeated(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile = tmp_path / "profile"
    log_path = tmp_path / "electron.log"

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=log_path,
    ) as first:
        first.open_tasks()
        task_id = first.start_task()
        action_id = first.prepare("slot-a-1830")
        reference = f"lumi-{action_id}"
        # The site will create the booking and then never answer.
        site.set_faults(drop_response_for_reference=reference, drop_response_mode="hang")
        first.card().get_by_role("button", name="Approve and book").click()
        state = site.wait_for_bookings(1, timeout=BOOK_SECONDS)
        assert state["submissions_by_reference"] == {reference: 1}
        first.wait_for_status("EXECUTING", timeout=30)
        assert _action_row(migrated_database_url, action_id) == {
            "status": "EXECUTING", "attempts": 1, "unfinished": 1,
        }
        # Hard kill: no graceful shutdown hook runs anywhere.
        first.hard_kill()

    # The runtime notices its parent died; its job takes the worker and Chromium.
    _wait_for_no_processes("*app.server*")
    _wait_for_no_processes("*app.browser.main*--ready-stdout*")
    assert _action_row(migrated_database_url, action_id)["status"] == "EXECUTING"
    site.set_faults()

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=log_path,
    ) as second:
        second.open_tasks()
        unknown = second.wait_for_status("OUTCOME_UNKNOWN", timeout=60)
        assert second.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert unknown.get_attribute("data-action-id") == action_id
        assert second.page.get_by_test_id("agent-booking-title").inner_text() == "Booking status uncertain"
        body = unknown.inner_text()
        assert "may have accepted the booking" in body
        # No retry, approve or book control while the outcome is unknown.
        assert second.buttons() == ["Check existing booking"]
        second.page.locator('[data-event-type="action.outcome_unknown"]').wait_for(timeout=10_000)
        assert "Outcome uncertain after Lumi restarted" in second.page.get_by_test_id("agent-timeline").inner_text()

        before = _counts(migrated_database_url, site)
        assert (before.attempts, before.fixture_submissions, before.bookings) == (1, 1, 1)
        assert before.lookup_dispatches == 0

        unknown.get_by_role("button", name="Check existing booking").click()
        confirmed = second.wait_for_status("SUCCEEDED")
        assert second.page.get_by_test_id("agent-booking-title").inner_text() == "Booking confirmed"
        text_now = confirmed.inner_text()
        assert "found the existing booking BK-0001" in text_now
        assert "No second booking was made." in text_now
        second.page.locator('[data-event-type="action.reconciled"]').wait_for(timeout=10_000)
        sequences = second.timeline_sequences()
        assert sequences == list(range(1, len(sequences) + 1))

        counts = _counts(migrated_database_url, site)
        assert counts == Counts(
            tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=0, lookup_dispatches=1,
            fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
        )
        print(f"LOST RESPONSE COUNTS: {counts}")
        _no_csp_violations(second.console)
    _wait_for_no_processes("*app.server*")
