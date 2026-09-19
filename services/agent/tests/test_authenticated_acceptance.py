"""Acceptance (Milestone 8a S3): authenticated account reading, in the real app.

Every step goes renderer -> preload -> Electron main -> authenticated runtime ->
browser worker, exactly like the sibling acceptance modules. Opt in with
`LUMI_ELECTRON_E2E=1` after `npm run build`.

**What this proves, and what it cannot.** The desktop worker judges site scope
with the pinned Public Suffix List, which refuses a loopback IP as a registrable
domain, and a real profile can never be bound to one (`canonical_site()` refuses
it). So there is no site this test can bind a desktop profile to that both the S0
broker will dial and the PSL will accept -- the same constraint S2's acceptance
records. The positive read (a page actually read and answered from) is therefore
proven at the level that *can* patch the site comparison:
`test_authenticated_service_browser.py` (real database, real worker, real
Chromium, the fixture question answered with citations).

What is proven here, against the real desktop app:

* the trusted disclosure card renders exactly the app-authored words, including
  the website-side-effect warning, the one provider and the no-failover promise,
  **before** anything is opened or sent;
* declining does nothing durable: no dispatch, no observation, no answer;
* the trusted Allow click drives the whole real path (grant, one worker read
  through the persistent profile, honest "could not verify" because the profile's
  blank tab holds no account page, an answer recorded for the approved provider
  only) and leaves no research row, no orphan browser and a released profile.
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.domain.browser_profile import allowed_origins_for
from app.domain.login_takeover import hash_identity
from app.repositories.profiles import BrowserProfileRepository
from tests.conftest import truncate_all
from tests.test_electron_acceptance import (
    _no_csp_violations,
    _off_thread,
    _wait_for_no_processes,
    desktop,
    playwright,  # noqa: F401 - fixture
)

pytestmark = [
    pytest.mark.browser,
    pytest.mark.hardkill,
    pytest.mark.skipif(
        os.environ.get("LUMI_ELECTRON_E2E") != "1",
        reason="set LUMI_ELECTRON_E2E=1 after `npm run build` to drive the real desktop app",
    ),
]

QUESTION = "Which of my repositories are private?"
LABEL = "Acceptance account"


def _run(coro_factory: Any) -> Any:
    return _off_thread(asyncio.run, coro_factory())


def _create_authenticated_profile(database_url: str) -> uuid.UUID:
    profile_id = uuid.uuid4()

    async def run() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                repository = BrowserProfileRepository(connection)
                await repository.create(
                    profile_id=profile_id, label=LABEL, site="127.0.0.1",
                    allowed_origins=allowed_origins_for("127.0.0.1"),
                )
                await repository.mark_authenticated(
                    profile_id=profile_id, account_fingerprint=hash_identity("fixture-account-1")
                )
        finally:
            await engine.dispose()

    _run(lambda: run())
    return profile_id


def _counts(database_url: str) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                async def count(sql: str) -> int:
                    return int(await connection.scalar(text(sql)) or 0)

                return {
                    "dispatches": await count("SELECT count(*) FROM browser_dispatches"),
                    "observations": await count("SELECT count(*) FROM authenticated_observations"),
                    "answers": await count("SELECT count(*) FROM authenticated_answers"),
                    "research": await count("SELECT count(*) FROM research_observations")
                    + await count("SELECT count(*) FROM research_answers"),
                    "grant": await connection.scalar(text("SELECT status FROM task_grants LIMIT 1")),
                    "provider": await connection.scalar(text("SELECT provider FROM authenticated_answers LIMIT 1")),
                    "effect": await connection.scalar(text("SELECT effect FROM browser_dispatches LIMIT 1")),
                    "leased": await count("SELECT count(*) FROM browser_profiles WHERE lease_runtime_generation IS NOT NULL"),
                }
        finally:
            await engine.dispose()

    result: dict[str, Any] = _run(lambda: run())
    return result


def _prepare(app: Any) -> Any:
    page = app.page
    app.open_tasks()
    form = page.get_by_test_id("agent-authenticated-form")
    form.wait_for(timeout=30_000)
    assert page.get_by_test_id("agent-authenticated-profile-select").inner_text().startswith(LABEL)
    form.get_by_test_id("agent-authenticated-question").fill(QUESTION)
    form.get_by_role("button", name="Prepare account reading").click()
    card = page.get_by_test_id("agent-authenticated-card")
    card.wait_for(timeout=60_000)
    return card


def test_the_disclosure_card_and_the_trusted_click_through_the_desktop_app(
    migrated_database_url: str, playwright: Any, tmp_path: Path
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    _create_authenticated_profile(migrated_database_url)
    environment = {
        "LUMI_SCRIPTED_MODELS": "gemini:rules",
        "LUMI_BROWSER_PROFILE_ROOT": str(tmp_path / "browser-profiles"),
    }

    with desktop(
        playwright, profile=tmp_path / "profile", database_url=migrated_database_url,
        site_origin="http://127.0.0.1:1", log_path=tmp_path / "electron.log", extra_environment=environment,
    ) as app:
        card = _prepare(app)
        assert card.get_attribute("data-grant-status") == "PENDING"

        # --- the disclosure, app-authored, before anything is opened ------------
        body = card.inner_text()
        for phrase in (
            "Lumi may:", "read pages on 127.0.0.1", "follow links within 127.0.0.1",
            "Lumi may not:", "sign in for you", "ask for your password or one-time code", "type into forms",
            "submit anything", "leave 127.0.0.1", "upload or download files", "buy, send, post or message anything",
            "Reading an account page may change website state, such as marking something as read, "
            'updating "last active", extending your session, or recording the visit.',
            "not invisible to the website",
            "up to 4,000 characters from account pages",
            "email addresses, phone numbers and long numbers hidden",
            "does not make it anonymous",
            "Lumi offline test model",
            "If that provider is unavailable, Lumi stops. It does not send the private page to another provider.",
        ):
            assert phrase in body, (phrase, body)
        assert card.get_by_test_id("agent-authenticated-profile").inner_text() == LABEL
        assert [b.inner_text() for b in card.get_by_role("button").all()] == ["Cancel", "Allow"]
        before = _counts(migrated_database_url)
        assert (before["dispatches"], before["observations"], before["answers"]) == (0, 0, 0)
        assert before["grant"] == "PENDING"

        # --- Cancel does nothing durable -----------------------------------------
        card.get_by_role("button", name="Cancel").click()
        app.page.locator('[data-testid="agent-authenticated-card"][data-grant-status="REVOKED"]').wait_for(timeout=30_000)
        after = _counts(migrated_database_url)
        assert (after["dispatches"], after["observations"], after["answers"]) == (0, 0, 0)
        app.page.get_by_role("button", name="Close task").click()

        # --- the trusted click: one real read, an honest answer ------------------
        card = _prepare(app)
        card.get_by_role("button", name="Allow").click()
        app.page.locator('[data-testid="agent-authenticated-card"][data-answer-status]').wait_for(timeout=180_000)
        done = _counts(migrated_database_url)
        assert done["dispatches"] >= 1 and done["effect"] == "ACCOUNT_READ"
        assert done["answers"] == 1 and done["provider"] == "scripted"
        assert done["research"] == 0
        assert done["grant"] in ("COMPLETED", "REVOKED")
        assert done["leased"] == 0  # The profile was released.
        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")
