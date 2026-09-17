"""Acceptance (Milestone 7a): one approved URL, one grounded answer, in the real app.

Every step goes renderer -> preload -> Electron main -> authenticated runtime
-> isolated worker -> Chromium -> stored observation -> main's model router ->
recorded answer, and is counted in PostgreSQL and in the fixture's request log.
Model calls use the unpackaged build's scripted providers (LUMI_SCRIPTED_MODELS),
which read the observation by label and cannot know anything the page did not
show.

Opt in with `LUMI_ELECTRON_E2E=1` after `npm run build`.
"""

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evals.sites.public_pages import HOSTILE_INSTRUCTIONS
from tests.browser_harness import PublicSiteControl, public_fixture_site
from tests.conftest import truncate_all
from tests.test_electron_acceptance import (
    BOOK_SECONDS,
    Desktop,
    _no_csp_violations,
    _off_thread,
    _wait_for_no_processes,
    desktop,
    playwright,  # noqa: F401 - fixture
    site,  # noqa: F401 - fixture
)
from tests.browser_harness import SiteControl

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_ELECTRON_E2E") != "1",
        reason="set LUMI_ELECTRON_E2E=1 after `npm run build` to drive the real desktop app",
    ),
]

QUESTION = "What is my contest rating?"
GREETING = "Hi, I am Lumi. I am ready to look at a screen with you."


@pytest.fixture
def pages(tmp_path: Path) -> Iterator[tuple[PublicSiteControl, PublicSiteControl]]:
    with public_fixture_site(tmp_path / "pages.log") as public, public_fixture_site(tmp_path / "canary.log") as canary:
        yield PublicSiteControl(public.base_url), PublicSiteControl(canary.base_url)


def _environment(pages_origin: str, models: str) -> dict[str, str]:
    return {"LUMI_INSPECTION_TEST_ORIGINS": pages_origin, "LUMI_SCRIPTED_MODELS": models}


def _ledger(database_url: str) -> dict[str, int]:
    async def run() -> dict[str, int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                async def count(sql: str) -> int:
                    return int(await connection.scalar(text(sql)) or 0)

                return {
                    "tasks": await count("SELECT count(*) FROM tasks"),
                    "actions": await count("SELECT count(*) FROM actions"),
                    "approvals_granted": await count("SELECT count(*) FROM approvals WHERE approved_at IS NOT NULL"),
                    "attempts": await count("SELECT count(*) FROM action_attempts"),
                    "dispatches": await count("SELECT count(*) FROM browser_dispatches"),
                    "observations": await count("SELECT count(*) FROM page_observations"),
                    "answers": await count("SELECT count(*) FROM page_observations WHERE answered_at IS NOT NULL"),
                }
        finally:
            await engine.dispose()

    result: dict[str, int] = _off_thread(asyncio.run, run())
    return result


def _card(app: Desktop) -> Any:
    return app.page.get_by_test_id("agent-inspection-card")


def _wait_card(app: Desktop, status: str, timeout: float = BOOK_SECONDS) -> Any:
    card = app.page.locator(f'[data-testid="agent-inspection-card"][data-action-status="{status}"]')
    card.wait_for(timeout=timeout * 1000)
    return card


def _prepare(app: Desktop, url: str, question: str = QUESTION) -> Any:
    page = app.page
    page.get_by_test_id("agent-inspect-url").fill(url)
    page.get_by_test_id("agent-inspect-question").fill(question)
    page.get_by_role("button", name="Prepare inspection").click()
    return _wait_card(app, "WAITING_APPROVAL", timeout=60)


def _close(app: Desktop) -> None:
    app.page.get_by_role("button", name="Close task").click()
    app.page.get_by_test_id("agent-task").wait_for(state="detached", timeout=30_000)


def _buttons(card: Any) -> list[str]:
    return [label.strip() for label in card.get_by_role("button").all_inner_texts()]


def test_one_approved_url_one_grounded_answer_through_the_desktop_app(
    migrated_database_url: str, site: SiteControl, playwright: Any, pages: tuple[PublicSiteControl, PublicSiteControl], tmp_path: Path
) -> None:
    public, canary = pages
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url, site_origin=site.base_url,
        log_path=tmp_path / "electron.log", extra_environment=_environment(public.base_url, "gemini:rules"),
    ) as app:
        app.open_tasks()
        rated = f"{public.base_url}/profiles/rated"

        # --- the trusted card, before anything is opened -------------------
        card = _prepare(app, f"{rated}#stats")
        assert app.page.get_by_test_id("agent-inspection-host").inner_text() == "127.0.0.1"
        assert app.page.get_by_test_id("agent-inspection-url").inner_text() == rated
        assert app.page.get_by_test_id("agent-inspection-question").inner_text() == QUESTION
        assert "Lumi offline test model" in app.page.get_by_test_id("agent-inspection-disclosure").inner_text()
        assert _buttons(card) == ["Reject", "Approve and inspect"]
        assert public.hits() == {}

        # --- typed "approve" is not approval --------------------------------
        page = app.page
        page.get_by_test_id("agent-request-input").fill("yes, approve it and go ahead")
        page.get_by_role("button", name="Send request").click()
        page.get_by_test_id("agent-request-outcome").filter(has_text="Nothing was approved").wait_for(timeout=60_000)
        page.wait_for_timeout(1_000)
        _wait_card(app, "WAITING_APPROVAL", timeout=10)
        assert _ledger(migrated_database_url)["approvals_granted"] == 0
        assert public.hits() == {}

        # --- the trusted click ----------------------------------------------
        card.get_by_role("button", name="Approve and inspect").click()
        done = _wait_card(app, "SUCCEEDED")
        page.locator('[data-testid="agent-inspection-card"][data-answer-status="answered"]').wait_for(timeout=60_000)
        answer = page.get_by_test_id("agent-inspection-answer").inner_text()
        assert "1,842" in answer and "12,345" not in answer and "367" not in answer
        source = page.get_by_test_id("agent-inspection-source").inner_text()
        assert rated in source
        assert public.hits() == {"/profiles/rated": 1}
        assert _ledger(migrated_database_url) == {
            "tasks": 1, "actions": 1, "approvals_granted": 1, "attempts": 1, "dispatches": 1, "observations": 1, "answers": 1,
        }
        page.locator('[data-event-type="task.page_answer_recorded"]').wait_for(timeout=10_000)

        # A renderer reload restores the same answered card from durable state.
        page.reload()
        app.open_tasks()
        _wait_card(app, "SUCCEEDED", timeout=60)
        assert "1,842" in page.get_by_test_id("agent-inspection-answer").inner_text()
        assert _buttons(done) == ["Inspect again (new approval)"]

        # A repeat is a new exact approval, and rejecting it opens nothing.
        _card(app).get_by_role("button", name="Inspect again (new approval)").click()
        again = _wait_card(app, "WAITING_APPROVAL", timeout=60)
        again.get_by_role("button", name="Reject").click()
        _wait_card(app, "REJECTED", timeout=30)
        assert public.hits() == {"/profiles/rated": 1}
        _close(app)

        # --- a profile without a rating -----------------------------------
        card = _prepare(app, f"{public.base_url}/profiles/unrated")
        card.get_by_role("button", name="Approve and inspect").click()
        page.locator('[data-testid="agent-inspection-card"][data-answer-status="not_found"]').wait_for(timeout=BOOK_SECONDS * 1000)
        assert page.get_by_test_id("agent-inspection-title").inner_text() == "Could not verify this from the inspected page."
        assert "12,345" not in _card(app).inner_text().split("Source")[0]
        _close(app)

        # --- a hostile page ------------------------------------------------
        hostile = f"{public.base_url}/profiles/hostile?canary={canary.base_url}"
        before = _ledger(migrated_database_url)
        card = _prepare(app, hostile)
        card.get_by_role("button", name="Approve and inspect").click()
        page.locator('[data-testid="agent-inspection-card"][data-answer-status="answered"]').wait_for(timeout=BOOK_SECONDS * 1000)
        text_now = _card(app).inner_text()
        assert "1,842" in text_now and "9999" not in text_now
        assert HOSTILE_INSTRUCTIONS[:40] not in page.inner_text("body")
        after = _ledger(migrated_database_url)
        assert (after["actions"] - before["actions"], after["attempts"] - before["attempts"]) == (1, 1)
        assert canary.hits() == {}
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")


def _conversation(app: Desktop) -> list[str]:
    return [line.strip() for line in app.page.locator('[role="log"] .message').all_inner_texts()]


def _ask_in_composer(app: Desktop, text: str) -> None:
    """Type into Lumi's main composer, as a user does, and press Enter."""
    page = app.page
    if not page.get_by_role("button", name="Collapse to orb").count():
        page.get_by_role("button", name="Open Lumi").click()
    composer = page.get_by_label("Ask Lumi")
    composer.fill(text)
    # Send is enabled once the (mock) voice session is connected.
    page.wait_for_function("() => document.querySelector('button.send-button')?.disabled === false", timeout=60_000)
    composer.press("Enter")


def test_the_main_composer_owns_an_inspection_request_and_shows_the_answer_in_the_conversation(
    migrated_database_url: str, site: SiteControl, playwright: Any, pages: tuple[PublicSiteControl, PublicSiteControl], tmp_path: Path
) -> None:
    public, _ = pages
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url, site_origin=site.base_url,
        log_path=tmp_path / "electron.log", extra_environment=_environment(public.base_url, "gemini:rules"),
    ) as app:
        page = app.page
        rated = f"{public.base_url}/profiles/rated"
        request = f"What is my contest rating? {rated}"

        # --- the normal composer, not the agent panel ------------------------
        _ask_in_composer(app, request)
        card = _wait_card(app, "WAITING_APPROVAL", timeout=60)
        assert page.get_by_test_id("agent-task-panel").count() == 1
        assert page.get_by_role("dialog", name="Lumi agent").count() == 1
        assert page.get_by_test_id("agent-inspection-url").inner_text() == rated
        assert _buttons(card) == ["Reject", "Approve and inspect"]
        # Nothing was opened: not by the isolated worker, not by a default browser.
        page.wait_for_timeout(1_500)
        assert public.hits() == {}
        # After the voice greeting, exactly the request and Lumi's status line:
        # the realtime conversation never answered it.
        assert _conversation(app) == [
            GREETING,
            request,
            "Prepared an inspection of 127.0.0.1. Nothing is opened until you press Approve and inspect.",
        ]
        assert _ledger(migrated_database_url)["tasks"] == 1

        # --- the trusted click; the answer follows without a second click ----
        card.get_by_role("button", name="Approve and inspect").click()
        page.locator('[data-testid="agent-inspection-card"][data-answer-status="answered"]').wait_for(timeout=BOOK_SECONDS * 1000)
        page.get_by_role("log").filter(has_text="From the inspected page on 127.0.0.1:").wait_for(timeout=10_000)
        assert "1,842" in _conversation(app)[-1]
        assert public.hits() == {"/profiles/rated": 1}
        ledger = _ledger(migrated_database_url)
        assert (ledger["tasks"], ledger["approvals_granted"], ledger["attempts"], ledger["answers"]) == (1, 1, 1, 1)

        # --- ordinary chat still reaches the conversation --------------------
        page.get_by_role("button", name="Close agent").click()
        before = len(_conversation(app))
        _ask_in_composer(app, "Hello")
        page.wait_for_function(
            f"() => document.querySelectorAll('[role=\"log\"] .message').length > {before + 1}", timeout=30_000
        )
        assert _conversation(app)[before] == "Hello"
        assert page.get_by_test_id("agent-task-panel").count() == 0
        assert _ledger(migrated_database_url)["tasks"] == 1
        assert public.hits() == {"/profiles/rated": 1}
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")


def test_a_hostile_model_is_refused_and_a_grounded_one_answers(
    migrated_database_url: str, site: SiteControl, playwright: Any, pages: tuple[PublicSiteControl, PublicSiteControl], tmp_path: Path
) -> None:
    public, canary = pages
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url, site_origin=site.base_url,
        log_path=tmp_path / "electron.log", extra_environment=_environment(public.base_url, "deepseek:hostile,gemini:rules"),
    ) as app:
        app.open_tasks()
        card = _prepare(app, f"{public.base_url}/profiles/hostile?canary={canary.base_url}")
        card.get_by_role("button", name="Approve and inspect").click()
        app.page.locator('[data-testid="agent-inspection-card"][data-answer-status="answered"]').wait_for(timeout=BOOK_SECONDS * 1000)
        answer = app.page.get_by_test_id("agent-inspection-answer").inner_text()
        assert "1,842" in answer and "9999" not in answer and "uploaded" not in answer
        assert canary.hits() == {}
        assert _ledger(migrated_database_url)["actions"] == 1


def test_main_restart_after_the_read_answers_from_the_saved_page_without_reopening_it(
    migrated_database_url: str, site: SiteControl, playwright: Any, pages: tuple[PublicSiteControl, PublicSiteControl], tmp_path: Path
) -> None:
    public, _ = pages
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile, log = tmp_path / "profile", tmp_path / "electron.log"
    with desktop(
        playwright, profile=profile, database_url=migrated_database_url, site_origin=site.base_url,
        log_path=log, extra_environment=_environment(public.base_url, "gemini:unavailable"),
    ) as first:
        first.open_tasks()
        card = _prepare(first, f"{public.base_url}/profiles/rated")
        card.get_by_role("button", name="Approve and inspect").click()
        read = _wait_card(first, "SUCCEEDED")
        first.page.get_by_test_id("agent-inspection-title").filter(has_text="Page read — no answer yet").wait_for(timeout=30_000)
        assert _buttons(read) == ["Answer from saved page", "Inspect again (new approval)"]
        first.hard_kill()
    _wait_for_no_processes("*app.server*")

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url, site_origin=site.base_url,
        log_path=log, extra_environment=_environment(public.base_url, "gemini:rules"),
    ) as second:
        second.open_tasks()
        read = _wait_card(second, "SUCCEEDED", timeout=60)
        # Loading the panel calls no model and opens nothing.
        second.page.wait_for_timeout(2_000)
        assert _ledger(migrated_database_url)["answers"] == 0
        read.get_by_role("button", name="Answer from saved page").click()
        second.page.locator('[data-testid="agent-inspection-card"][data-answer-status="answered"]').wait_for(timeout=60_000)
        assert "1,842" in second.page.get_by_test_id("agent-inspection-answer").inner_text()
    assert public.hits() == {"/profiles/rated": 1}
    assert _ledger(migrated_database_url)["attempts"] == 1
    _wait_for_no_processes("*app.server*")


def test_a_hard_kill_during_the_read_is_unknown_after_restart_and_is_never_retried(
    migrated_database_url: str, site: SiteControl, playwright: Any, pages: tuple[PublicSiteControl, PublicSiteControl], tmp_path: Path
) -> None:
    public, _ = pages
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile, log = tmp_path / "profile", tmp_path / "electron.log"
    with desktop(
        playwright, profile=profile, database_url=migrated_database_url, site_origin=site.base_url,
        log_path=log, extra_environment=_environment(public.base_url, "gemini:rules"),
    ) as first:
        first.open_tasks()
        card = _prepare(first, f"{public.base_url}/slow")
        card.get_by_role("button", name="Approve and inspect").click()
        public.wait_for_hit("/slow", timeout=BOOK_SECONDS)
        _wait_card(first, "EXECUTING", timeout=30)
        first.hard_kill()
    _wait_for_no_processes("*app.server*")
    _wait_for_no_processes("*app.browser.main*--ready-stdout*")

    with desktop(
        playwright, profile=profile, database_url=migrated_database_url, site_origin=site.base_url,
        log_path=log, extra_environment=_environment(public.base_url, "gemini:rules"),
    ) as second:
        second.open_tasks()
        unknown = _wait_card(second, "OUTCOME_UNKNOWN", timeout=60)
        assert second.page.get_by_test_id("agent-inspection-title").inner_text() == "Lumi does not know what was read"
        assert _buttons(unknown) == ["Inspect again (new approval)"]
        assert _ledger(migrated_database_url)["attempts"] == 1
        unknown.get_by_role("button", name="Inspect again (new approval)").click()
        _wait_card(second, "WAITING_APPROVAL", timeout=60)
        assert public.hits() == {"/slow": 1}
        ledger = _ledger(migrated_database_url)
        assert (ledger["attempts"], ledger["approvals_granted"], ledger["observations"]) == (1, 1, 0)
    _wait_for_no_processes("*app.server*")
