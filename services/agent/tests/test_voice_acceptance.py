"""Acceptance (Milestone 5): natural voice -> durable task -> trusted click.

The built Electron app is launched with `LUMI_REALTIME_SCRIPTED=1`, which makes
main issue a `scripted` realtime credential (unpackaged builds only). The
renderer then talks to a deterministic in-process Realtime stand-in that speaks
the real data-channel protocol: committed audio turns, interim and completed
transcriptions, function calls, function outputs and spoken transcripts. A test
"speaks" through `window.__lumiRealtimeHarness.say(...)`.

Everything after that is the production path: RealtimeClient -> preload ->
main VoiceTaskController -> AgentTaskController -> authenticated runtime ->
PostgreSQL ledger -> isolated browser worker -> fixture site. The only approval
in these tests is a real click on the rendered "Approve and book" button.

Opt in with `LUMI_ELECTRON_E2E=1` after `npm run build`.
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
    _action_row,
    _counts,
    _no_csp_violations,
    _off_thread,
    _wait_for_no_processes,
    desktop,
    playwright,  # noqa: F401 - fixture
    site,  # noqa: F401 - fixture
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_ELECTRON_E2E") != "1",
        reason="set LUMI_ELECTRON_E2E=1 after `npm run build` to drive the real desktop app",
    ),
]

SCRIPTED = {"LUMI_REALTIME_SCRIPTED": "1"}
VOICE_SECONDS = 60.0


def _approvals_granted(database_url: str) -> int:
    async def run() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(
                    text("SELECT count(*) FROM approvals WHERE approved_at IS NOT NULL")
                )
                return int(value or 0)
        finally:
            await engine.dispose()

    result: int = _off_thread(asyncio.run, run())
    return result


class Voice:
    """The scripted realtime harness inside one running desktop."""

    def __init__(self, app: Desktop) -> None:
        self.page = app.page

    def ready(self) -> None:
        self.page.wait_for_function(
            "() => window.__lumiRealtimeHarness && window.__lumiRealtimeHarness.spoken().length > 0",
            timeout=VOICE_SECONDS * 1000,
        )

    def spoken(self) -> list[str]:
        lines: list[str] = self.page.evaluate("() => window.__lumiRealtimeHarness.spoken()")
        return lines

    def tool_calls(self) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = self.page.evaluate("() => window.__lumiRealtimeHarness.toolCalls()")
        return calls

    def say(self, utterance: str, expect: str, **options: Any) -> str:
        """Speak, then wait for Lumi's next line matching `expect` (a regex)."""
        before = len(self.spoken())
        self.page.evaluate(
            "([utterance, options]) => window.__lumiRealtimeHarness.say(utterance, options)",
            [utterance, options],
        )
        self.page.wait_for_function(
            "([before, pattern]) => window.__lumiRealtimeHarness.spoken().slice(before)"
            ".some((line) => new RegExp(pattern).test(line))",
            arg=[before, expect],
            timeout=BOOK_SECONDS * 1000,
        )
        reply = [line for line in self.spoken()[before:]][-1]
        return reply

    def replay_last_events(self) -> None:
        self.page.evaluate(
            "() => { const h = window.__lumiRealtimeHarness; h.replayLastTranscript(); h.replayLastToolCall() }"
        )

    def barge_in(self) -> None:
        self.page.evaluate("() => window.__lumiRealtimeHarness.bargeIn()")

    def reconnect(self) -> None:
        self.page.evaluate("() => window.__lumiRealtimeHarness.reconnect()")
        self.ready()


def _focused_region(app: Desktop) -> str | None:
    focused: str | None = app.page.evaluate("() => document.activeElement?.dataset?.testid ?? null")
    return focused


def test_voice_request_to_trusted_click_books_exactly_once(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    # Page text that tries to act as an instruction is present throughout.
    site.set_faults(hostile_text=True)
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log", extra_environment=SCRIPTED,
    ) as app:
        app.open_tasks()
        voice = Voice(app)
        voice.ready()

        # 1. A natural request, with unstable interim text first.
        results = voice.say(
            "Find me a dermatologist Saturday evening under 1000.",
            r"I found 2 appointments",
            interim=["Find me", "Find me a derma", "Book it"],
        )
        assert "1. Dr A, Saturday 18:30, ₹800" in results
        assert "2. Dr B, Saturday 19:15, ₹950" in results
        # The same browser-observed results are in the trusted panel.
        app.page.locator('li[data-slot-id="slot-a-1830"]').wait_for(timeout=30_000)
        app.page.locator('li[data-slot-id="slot-b-1915"]').wait_for(timeout=30_000)
        criteria = app.page.get_by_test_id("agent-task-criteria").inner_text()
        assert "Dermatology · Saturday · 17:00–22:00 · up to ₹1,000" == criteria
        after_search = _counts(migrated_database_url, site)
        assert (after_search.tasks, after_search.actions, after_search.fixture_submissions) == (1, 0, 0)

        # A replayed transcript and tool call create nothing new.
        voice.replay_last_events()
        app.page.wait_for_timeout(1_500)
        assert _counts(migrated_database_url, site).tasks == 1

        # 2. Selection prepares the authoritative slot and surfaces the card.
        prepared = voice.say("Take the 6:30 one.", r"is ready but not booked")
        assert "Dr A on Saturday at 18:30 for ₹800" in prepared
        card = app.wait_for_status("WAITING_APPROVAL", timeout=60)
        action_id = card.get_attribute("data-action-id")
        assert action_id
        reference = f"lumi-{action_id}"
        details = card.inner_text()
        assert "Dr A" in details and "₹800" in details
        assert app.buttons() == ["Reject", "Approve and book"]
        app.page.wait_for_function(
            "() => document.activeElement?.dataset?.testid === 'agent-booking-region'", timeout=10_000
        )
        assert _focused_region(app) == "agent-booking-region"
        revision = card.get_attribute("data-action-revision")
        assert _counts(migrated_database_url, site).fixture_submissions == 0

        # 3. Spoken approval is not approval.
        for utterance in ("Book it.", "yes", "Go ahead and confirm"):
            reply = voice.say(utterance, r"cannot approve bookings by voice")
            assert "press Approve and book yourself" in reply
        voice.barge_in()
        app.page.wait_for_timeout(1_000)
        still = app.wait_for_status("WAITING_APPROVAL", timeout=10)
        assert still.get_attribute("data-action-revision") == revision
        assert _approvals_granted(migrated_database_url) == 0
        held = _counts(migrated_database_url, site)
        assert (held.tasks, held.actions, held.attempts, held.fixture_submissions, held.bookings) == (1, 1, 0, 0, 0)

        # A voice reconnect repeats nothing.
        voice.reconnect()
        app.page.wait_for_timeout(1_500)
        assert _counts(migrated_database_url, site) == held

        # 4. The trusted click.
        app.card().get_by_role("button", name="Approve and book").click()
        done = app.wait_for_status("SUCCEEDED")
        assert "BK-0001" in done.inner_text()
        assert _approvals_granted(migrated_database_url) == 1
        confirmed = voice.say("Did it go through?", r"confirmed")
        assert "BK-0001" in confirmed

        counts = _counts(migrated_database_url, site)
        assert counts == Counts(
            tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=1,
            lookup_dispatches=0, fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
        )
        print(f"VOICE NORMAL BOOKING COUNTS: {counts} approvals_granted=1")

        # Hostile page text never became speech or a tool call.
        spoken = "\n".join(voice.spoken())
        for fragment in ("IGNORE", "SYSTEM NOTICE", "9500"):
            assert fragment not in spoken
        assert all(call["name"].startswith("appointment_") for call in voice.tool_calls())

        sequences = app.timeline_sequences()
        assert sequences == list(range(1, len(sequences) + 1))
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")


def test_voice_refinement_withdraws_a_stale_prepared_booking(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin=site.base_url, log_path=tmp_path / "electron.log", extra_environment=SCRIPTED,
    ) as app:
        app.open_tasks()
        voice = Voice(app)
        voice.ready()
        voice.say("Find me a dermatologist Saturday evening under 1000.", r"I found 2 appointments")
        voice.say("Take the 7:15 one.", r"Dr B on Saturday at 19:15 for ₹950 is ready")
        stale = app.wait_for_status("WAITING_APPROVAL", timeout=60)
        stale_id = stale.get_attribute("data-action-id")

        refined = voice.say("Actually under 900.", r"I found 1 appointment")
        assert "no longer fits, so it was withdrawn" in refined
        withdrawn = app.wait_for_status("REJECTED", timeout=30)
        assert withdrawn.get_attribute("data-action-id") == stale_id
        assert "Book now" not in app.buttons() and "Approve and book" not in app.buttons()
        app.page.locator('[data-event-type="task.criteria_updated"]').wait_for(timeout=10_000)
        assert "up to ₹900" in app.page.get_by_test_id("agent-task-criteria").inner_text()

        # "Book it" now has nothing to show: the withdrawn booking cannot be approved.
        voice.say("Book it.", r"nothing prepared")
        voice.say("Take the 6:30 one.", r"Dr A on Saturday at 18:30 for ₹800 is ready")
        fresh = app.wait_for_status("WAITING_APPROVAL", timeout=60)
        assert fresh.get_attribute("data-action-id") != stale_id
        assert _action_row(migrated_database_url, str(stale_id))["status"] == "REJECTED"

        # Cancelling by voice rejects the open booking; nothing was ever submitted.
        voice.say("Cancel this task.", r"cancelled the appointment task")
        app.wait_for_status("REJECTED", timeout=30)
        app.page.locator('[data-event-type="task.cancelled"]').wait_for(timeout=10_000)
        assert "CANCELLED" in app.page.locator(".agent-task-panel .eyebrow").first.inner_text()
        counts = _counts(migrated_database_url, site)
        assert (counts.tasks, counts.actions, counts.attempts, counts.fixture_submissions, counts.bookings) == (1, 2, 0, 0, 0)
        assert _approvals_granted(migrated_database_url) == 0
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")


def test_voice_initiated_lost_response_is_reconciled_not_repeated(
    migrated_database_url: str, site: SiteControl, playwright: Any, tmp_path: Path  # noqa: F811
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile = tmp_path / "profile"
    log_path = tmp_path / "electron.log"

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url,
        site_origin=site.base_url, log_path=log_path, extra_environment=SCRIPTED,
    ) as first:
        first.open_tasks()
        voice = Voice(first)
        voice.ready()
        voice.say("Find me a dermatologist Saturday evening under 1000.", r"I found 2 appointments")
        voice.say("Take the 6:30 one.", r"is ready but not booked")
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
        site_origin=site.base_url, log_path=log_path, extra_environment=SCRIPTED,
    ) as second:
        second.open_tasks()
        unknown = second.wait_for_status("OUTCOME_UNKNOWN", timeout=60)
        assert second.page.get_by_test_id("agent-task-id").text_content() == task_id
        assert unknown.get_attribute("data-action-id") == action_id
        assert second.buttons() == ["Check existing booking"]
        voice = Voice(second)
        voice.ready()

        # Voice neither claims an outcome nor lets "book it" repeat anything.
        status = voice.say("What happened with my booking? What's the status?", r"do not know yet")
        assert "will not book again" in status
        for utterance in ("Book it.", "yes", "Take the 6:30 one.", "Cancel this task."):
            reply = voice.say(utterance, r"do not know yet|could not do that yet \(unresolved booking\)")
            assert "confirmed" not in reply
        before = _counts(migrated_database_url, site)
        assert (before.tasks, before.actions, before.attempts, before.fixture_submissions, before.bookings) == (1, 1, 1, 1, 1)
        assert before.lookup_dispatches == 0
        assert _action_row(migrated_database_url, str(action_id))["status"] == "OUTCOME_UNKNOWN"

        # "check it" runs the existing read-only reconciliation.
        checked = voice.say("Please check it.", r"confirmed|do not know yet")
        assert "I found it on the clinic site and did not book again" in checked
        confirmed = second.wait_for_status("SUCCEEDED")
        assert "found the existing booking BK-0001" in confirmed.inner_text()
        second.page.locator('[data-event-type="action.reconciled"]').wait_for(timeout=10_000)

        counts = _counts(migrated_database_url, site)
        assert counts == Counts(
            tasks=1, actions=1, attempts=1, consequential_dispatches=1, submitted_dispatches=0,
            lookup_dispatches=1, fixture_submissions=1, bookings=1, submissions_by_reference={reference: 1},
        )
        print(f"VOICE LOST RESPONSE COUNTS: {counts}")
        sequences = second.timeline_sequences()
        assert sequences == list(range(1, len(sequences) + 1))
        _no_csp_violations(second.console)
    _wait_for_no_processes("*app.server*")
