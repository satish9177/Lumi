"""Acceptance (Milestone 6): compound requests, provider failover, Gemini Live.

Every scenario drives the built Electron app (`npm run build` first) and counts
what happened in PostgreSQL and on the fixture site. The calendar clock is
pinned to Wednesday 16 September 2026 (India time) with `LUMI_FIXED_NOW`,
which main honours only in unpackaged builds, so "Saturday" keeps meaning the
fixture's Saturday on any later date.

* A: a spoken compound request prepares exactly one booking; "book it" only
  surfaces the card; the trusted click books once. Run over the OpenAI-protocol
  scripted harness and over the Gemini Live path (scripted Vertex socket behind
  main's relay).
* B: a typed request whose first model provider fails completes on the
  fallback provider, on the same durable task, with no duplicate work; the task
  survives an Electron restart.
* C: a compound voice request, a lost response after the booking was created,
  a hard kill, OUTCOME_UNKNOWN, read-only reconciliation, zero duplicates.

Opt in with `LUMI_ELECTRON_E2E=1`.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.browser_harness import SiteControl
from tests.conftest import truncate_all
from tests.test_electron_acceptance import (
    BOOK_SECONDS,
    Counts,
    Desktop,
    _counts,
    _no_csp_violations,
    _off_thread,
    _wait_for_no_processes,
    desktop,
    playwright,  # noqa: F401 - fixture
    site,  # noqa: F401 - fixture
)
from tests.test_voice_acceptance import Voice, _approvals_granted

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_ELECTRON_E2E") != "1",
        reason="set LUMI_ELECTRON_E2E=1 after `npm run build` to drive the real desktop app",
    ),
]

CALENDAR = {"LUMI_FIXED_NOW": "2026-09-16T04:30:00Z", "LUMI_TIMEZONE": "Asia/Kolkata"}
COMPOUND = "Find me a dermatologist Saturday evening under ₹1000 and prepare the cheapest available option."


def _task_request(database_url: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(text("SELECT request FROM tasks ORDER BY created_at DESC LIMIT 1"))
                return dict(value or {})
        finally:
            await engine.dispose()

    result: dict[str, Any] = _off_thread(asyncio.run, run())
    return result


def _held(database_url: str, control: SiteControl) -> tuple[int, int, int, int, int]:
    counts = _counts(database_url, control)
    return (counts.tasks, counts.actions, counts.attempts, counts.fixture_submissions, counts.bookings)


def _ask(app: Desktop, request: str, expect: str) -> str:
    """Type a request into the trusted panel and wait for its outcome."""
    page = app.page
    page.get_by_test_id("agent-request-input").fill(request)
    page.get_by_role("button", name="Send request").click()
    outcome = page.get_by_test_id("agent-request-outcome").filter(has_text=expect)
    outcome.wait_for(timeout=BOOK_SECONDS * 1000)
    reply: str = outcome.inner_text()
    return reply


def _close_task(app: Desktop) -> None:
    app.page.get_by_role("button", name="Close task").click()
    app.page.get_by_test_id("agent-task").wait_for(state="detached", timeout=30_000)


def _compound_voice_booking(app: Desktop, database_url: str, control: SiteControl, *, provider: str) -> None:
    app.open_tasks()
    voice = Voice(app)
    voice.ready()

    prepared = voice.say(COMPOUND, r"is ready but not booked")
    assert "Dr A on Saturday at 18:30 for ₹800" in prepared
    card = app.wait_for_status("WAITING_APPROVAL", timeout=60)
    action_id = card.get_attribute("data-action-id")
    reference = f"lumi-{action_id}"
    [call] = [c for c in voice.tool_calls() if c["name"] == "appointment_plan"]
    assert call["arguments"]["choose"] == {"strategy": "cheapest"}
    assert call["arguments"]["search"]["when"] == {"kind": "weekday", "weekday": "Saturday"}
    request = _task_request(database_url)
    assert (request["date_from"], request["date_to"], request["day"]) == ("2026-09-19", "2026-09-19", "Saturday")
    assert (request["earliest_time"], request["max_price"], request["source"]) == ("17:00", 1000, "voice")
    shown = app.page.get_by_test_id("agent-task-criteria").inner_text()
    assert "Sat" in shown and "19" in shown and "17:00–22:00" in shown
    assert _held(database_url, control) == (1, 1, 0, 0, 0)
    assert _approvals_granted(database_url) == 0

    # "Book it" is not approval, on either provider.
    reply = voice.say("Book it.", r"cannot approve bookings by voice")
    assert "press Approve and book yourself" in reply
    app.page.wait_for_timeout(1_000)
    assert app.wait_for_status("WAITING_APPROVAL", timeout=10).get_attribute("data-action-id") == action_id
    assert _held(database_url, control) == (1, 1, 0, 0, 0)
    assert _approvals_granted(database_url) == 0

    app.card().get_by_role("button", name="Approve and book").click()
    done = app.wait_for_status("SUCCEEDED")
    assert "BK-0001" in done.inner_text()
    counts = _counts(database_url, control)
    assert counts == Counts(
        tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=1,
        lookup_dispatches=0, fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
    )
    assert _approvals_granted(database_url) == 1
    print(f"M6 SCENARIO A ({provider}) COUNTS: {counts} approvals_granted=1")

    spoken = "\n".join(voice.spoken())
    for fragment in ("IGNORE", "SYSTEM NOTICE", "9500"):
        assert fragment not in spoken
    sequences = app.timeline_sequences()
    assert sequences == list(range(1, len(sequences) + 1))
    _no_csp_violations(app.console)


def test_scenario_a_compound_voice_request_openai_protocol(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    site.set_faults(hostile_text=True)
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log",
        extra_environment={"LUMI_REALTIME_SCRIPTED": "1", **CALENDAR},
    ) as app:
        _compound_voice_booking(app, migrated_database_url, site, provider="openai-protocol")
    _wait_for_no_processes("*app.server*")


def test_scenario_a_compound_voice_request_over_gemini_live_relay(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    site.set_faults(hostile_text=True)
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log",
        extra_environment={"LUMI_REALTIME_SCRIPTED": "gemini", **CALENDAR},
    ) as app:
        # The renderer holds no Google credential or endpoint.
        leaked = app.page.evaluate(
            "() => [document.documentElement.outerHTML, JSON.stringify(Object.keys(window.lifeLens))].join(' ')"
        )
        assert "scripted-token" not in leaked and "aiplatform" not in leaked
        _compound_voice_booking(app, migrated_database_url, site, provider="gemini-relay")


def test_scenario_b_provider_failover_keeps_one_task_and_survives_restart(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile = tmp_path / "profile"
    environment = {"LUMI_SCRIPTED_MODELS": "deepseek:fail_once,gemini:rules", **CALENDAR}
    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log", extra_environment=environment,
    ) as app:
        app.open_tasks()
        found = _ask(app, "Find me a dermatologist Saturday evening under 1000", "Found 2 matching appointments")
        assert "Found 2" in found
        task_id = app.page.get_by_test_id("agent-task-id").text_content()
        assert _held(migrated_database_url, site) == (1, 0, 0, 0, 0)

        # Ordinary conversation creates nothing.
        _ask(app, "Hello Lumi, how are you?", "Lumi handles appointment and clinic questions")
        assert _held(migrated_database_url, site) == (1, 0, 0, 0, 0)

        prepared = _ask(app, "Show me the cheapest one and prepare it", "Nothing is booked until you press Approve and book")
        assert "Dr A" in prepared
        steps = app.page.locator('[data-testid="agent-plan"] li').evaluate_all(
            "items => items.map(item => `${item.dataset.step}:${item.dataset.status}`)"
        )
        assert steps == ["choose:done", "prepare:done"]
        card = app.wait_for_status("WAITING_APPROVAL", timeout=60)
        action_id = card.get_attribute("data-action-id")
        assert app.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert _held(migrated_database_url, site) == (1, 1, 0, 0, 0)
        assert _approvals_granted(migrated_database_url) == 0
        request = _task_request(migrated_database_url)
        assert request["source"] == "text" and request["request_id"].startswith("req_")

        # The failed provider and the fallback are both visible, redacted.
        app.page.get_by_test_id("agent-technical").locator("summary").click()
        diagnostics = app.page.get_by_test_id("agent-diagnostics")
        diagnostics.wait_for(timeout=10_000)
        lines = diagnostics.inner_text()
        assert "deepseek scripted-fail_once · unavailable" in lines
        assert "gemini scripted-rules · ok" in lines
        assert "Find me" not in lines and "cheapest" not in lines
        _no_csp_violations(app.console)

    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log", extra_environment=environment,
    ) as restarted:
        restarted.open_tasks()
        card = restarted.wait_for_status("WAITING_APPROVAL", timeout=60)
        assert restarted.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert card.get_attribute("data-action-id") == action_id
        assert _held(migrated_database_url, site) == (1, 1, 0, 0, 0)
        print(f"M6 SCENARIO B COUNTS: {_counts(migrated_database_url, site)} approvals_granted=0")
    _wait_for_no_processes("*app.server*")


def test_clinic_information_workflow_and_saved_preferences(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    site.set_faults(hostile_text=True)
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log",
        extra_environment={"LUMI_SCRIPTED_MODELS": "gemini:rules", **CALENDAR},
    ) as app:
        app.open_tasks()
        _ask(app, "What languages does Dr A speak?", "Read 1 doctor profile")
        profiles = app.page.get_by_test_id("agent-profiles")
        profiles.wait_for(timeout=30_000)
        assert "English, Telugu, Hindi" in profiles.inner_text()
        assert "IGNORE" not in profiles.inner_text()
        assert app.page.get_by_test_id("agent-task").get_attribute("data-task-kind") == "clinic_info"
        app.page.get_by_role("button", name="Look up again").click()
        app.page.locator('[data-event-type="task.info_lookup_completed"]').nth(1).wait_for(timeout=60_000)
        assert site.state()["profile_views"] == 2
        counts = _counts(migrated_database_url, site)
        assert (counts.tasks, counts.actions, counts.consequential_dispatches, counts.fixture_submissions) == (1, 0, 0, 0)

        # A remembered preference fills a gap; it is visible and removable.
        _ask(app, "Remember I prefer morning appointments", "Saved preference")
        app.page.get_by_test_id("agent-preferences").wait_for(timeout=10_000)
        _close_task(app)
        found = _ask(app, "Find me a dermatologist on Saturday", "No appointments matched")
        assert "Used your saved preferences" in found
        _close_task(app)
        explicit = _ask(app, "Find me a dermatologist Saturday evening", "Found 2 matching appointments")
        assert "saved preferences" not in explicit
        app.page.get_by_test_id("agent-preferences").get_by_role("button", name="Forget").click()
        app.page.get_by_test_id("agent-preferences").wait_for(state="detached", timeout=10_000)

        # An ambiguous relative date is asked back; nothing is created.
        before = _counts(migrated_database_url, site).tasks
        _close_task(app)
        _ask(app, "Find me a dermatologist next Saturday", "Which day did you mean")
        assert _counts(migrated_database_url, site).tasks == before
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")


def test_scenario_c_compound_voice_lost_response_is_reconciled_not_repeated(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile = tmp_path / "profile"
    log_path = tmp_path / "electron.log"
    environment = {"LUMI_REALTIME_SCRIPTED": "1", **CALENDAR}

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=log_path, extra_environment=environment,
    ) as first:
        first.open_tasks()
        voice = Voice(first)
        voice.ready()
        voice.say(COMPOUND, r"is ready but not booked")
        card = first.wait_for_status("WAITING_APPROVAL", timeout=60)
        task_id = first.page.get_by_test_id("agent-task-id").text_content()
        action_id = card.get_attribute("data-action-id")
        reference = f"lumi-{action_id}"
        site.set_faults(drop_response_for_reference=reference, drop_response_mode="hang")
        card.get_by_role("button", name="Approve and book").click()
        state = site.wait_for_bookings(1, timeout=BOOK_SECONDS)
        assert state["submissions_by_reference"] == {reference: 1}
        first.wait_for_status("EXECUTING", timeout=30)
        first.hard_kill()

    _wait_for_no_processes("*app.server*")
    _wait_for_no_processes("*app.browser.main*--ready-stdout*")
    site.set_faults()

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=log_path, extra_environment=environment,
    ) as second:
        second.open_tasks()
        unknown = second.wait_for_status("OUTCOME_UNKNOWN", timeout=60)
        assert second.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert unknown.get_attribute("data-action-id") == action_id
        voice = Voice(second)
        voice.ready()
        # A repeated compound request cannot start or book anything.
        reply = voice.say(COMPOUND, r"could not do that yet \(unresolved booking\)|do not know yet")
        assert "confirmed" not in reply
        voice.say("Book it.", r"do not know yet")
        before = _counts(migrated_database_url, site)
        assert (before.tasks, before.actions, before.attempts, before.fixture_submissions, before.bookings) == (1, 1, 1, 1, 1)
        assert before.lookup_dispatches == 0

        checked = voice.say("Please check it.", r"confirmed|do not know yet")
        assert "did not book again" in checked
        second.wait_for_status("SUCCEEDED")
        counts = _counts(migrated_database_url, site)
        assert counts == Counts(
            tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=0,
            lookup_dispatches=1, fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
        )
        print(f"M6 SCENARIO C COUNTS: {counts}")
        sequences = second.timeline_sequences()
        assert sequences == list(range(1, len(sequences) + 1))
        _no_csp_violations(second.console)
    _wait_for_no_processes("*app.server*")
