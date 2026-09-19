"""Acceptance (Milestone 8a S2): manual login and human takeover, in the real app.

Every step goes renderer -> preload -> Electron main -> authenticated runtime
-> browser worker, exactly like the sibling acceptance modules. Opt in with
`LUMI_ELECTRON_E2E=1` after `npm run build`.

**Why this test never reaches an open, interactive takeover window.**
`ProfileSessionStore.start_takeover` always navigates the takeover tab to
`https://{profile.site}/` on the real wire path -- its `scheme` override is
test-infrastructure-only, and `POST /browser-profiles/{id}/takeover` (the
route the real UI calls) never supplies one. Every fixture site in this repo
speaks plain HTTP on a loopback IP (see `test_login_takeover_browser.py`'s own
docstring on why: an IP literal has no registrable domain, and a real
profile's `site` can never be an IP literal either, since `canonical_site()`
refuses one before a row exists). So there is no site this test can bind a
real desktop profile to that both (a) the S0 broker will dial and (b) will
answer with a real TLS handshake -- reaching that would need either a
self-signed-cert fixture plus a Chromium certificate-trust override, or a
narrow relaxation of `start_takeover`'s hardcoded scheme, and this milestone's
own review chose to add neither rather than touch that gating code for a
test's convenience.

What this module proves instead, against the real desktop app: the trusted
"Sign in manually" dialog renders exactly the app-authored words (never
website text), the click drives the real IPC/HTTP path end to end, a
navigation failure is refused cleanly with no half-open state (no
`login_attempts` row, ever -- `LoginTakeoverService.start_takeover` only
creates one *after* the worker reports `OPEN`), the profile is left at
`NEEDS_LOGIN` rather than any authenticated claim, zero tasks/actions/
dispatches/observations are ever created, and no `app.server` or Chromium
process outlives the run. `test_login_takeover_browser.py` and
`test_login_takeover_service_browser.py` (39 tests, real headed Chromium, one
of them also a real database) are what prove the positive path -- reaching
`AUTHENTICATED` -- and the credential-surface/site-scope state machine this
module cannot exercise end to end.
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

PROFILE_SITE = "127.0.0.1"
PROFILE_LABEL = "Acceptance test profile"


def _create_needs_login_profile(database_url: str) -> uuid.UUID:
    """A profile row, inserted directly -- the renderer deliberately has no
    `createBrowserProfile` IPC method, so this is trusted test setup, exactly
    as `test_login_takeover_service_browser.py` does it. Started at
    `NEEDS_LOGIN` rather than `NEW` to match the state the spec describes;
    in the real app the very first open does this same transition itself."""
    profile_id = uuid.uuid4()

    async def run() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await BrowserProfileRepository(connection).create(
                    profile_id=profile_id,
                    label=PROFILE_LABEL,
                    site=PROFILE_SITE,
                    allowed_origins=allowed_origins_for(PROFILE_SITE),
                )
                await connection.execute(
                    text("UPDATE browser_profiles SET status = 'NEEDS_LOGIN' WHERE id = :id"),
                    {"id": str(profile_id)},
                )
        finally:
            await engine.dispose()

    _off_thread(asyncio.run, run())
    return profile_id


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
                    "dispatches": await count("SELECT count(*) FROM browser_dispatches"),
                    "observations": await count("SELECT count(*) FROM page_observations"),
                    "login_attempts": await count("SELECT count(*) FROM login_attempts"),
                }
        finally:
            await engine.dispose()

    result: dict[str, int] = _off_thread(asyncio.run, run())
    return result


def _profile_status(database_url: str, profile_id: uuid.UUID) -> str:
    async def run() -> str:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                status = await connection.scalar(
                    text("SELECT status FROM browser_profiles WHERE id = :id"), {"id": str(profile_id)}
                )
                assert isinstance(status, str)
                return status
        finally:
            await engine.dispose()

    result: str = _off_thread(asyncio.run, run())
    return result


def _open_panel(app: Any) -> None:
    page = app.page
    if not page.get_by_test_id("browser-profile-panel").count():
        if not page.get_by_role("button", name="Collapse to orb").count():
            page.get_by_role("button", name="Open Lumi").click()
        page.get_by_test_id("open-browser-profiles").click()
    page.get_by_test_id("browser-profile-panel").wait_for(timeout=30_000)


def _card(app: Any, profile_id: uuid.UUID) -> Any:
    return app.page.locator(f'[data-testid="browser-profile-card"][data-profile-id="{profile_id}"]')


def test_manual_login_refusal_through_the_desktop_app(
    migrated_database_url: str, playwright: Any, tmp_path: Path
) -> None:
    _off_thread(truncate_all, migrated_database_url)
    _wait_for_no_processes("*app.server*")
    profile_id = _create_needs_login_profile(migrated_database_url)
    before = _ledger(migrated_database_url)

    with desktop(
        playwright,
        profile=tmp_path / "profile",
        database_url=migrated_database_url,
        site_origin="http://127.0.0.1:1",
        log_path=tmp_path / "electron.log",
    ) as app:
        _open_panel(app)
        card = _card(app, profile_id)
        card.wait_for(timeout=30_000)
        assert card.get_attribute("data-profile-status") == "NEEDS_LOGIN"
        assert card.get_by_test_id("browser-profile-status").inner_text() == "Needs sign-in"

        # --- the trusted dialog, app-authored words only --------------------
        card.get_by_role("button", name="Sign in manually").click()
        dialog = card.get_by_test_id("sign-in-confirm-dialog")
        dialog.wait_for(timeout=10_000)
        dialog_text = dialog.inner_text()
        for phrase in (
            "Lumi needs you to sign in yourself.",
            "see your password",
            "see your OTP",
            "solve CAPTCHA",
            "use your passkey",
            "send the login page to an AI model",
            "A separate Lumi browser window will open.",
        ):
            assert phrase in dialog_text, dialog_text

        # --- dismissing the dialog does nothing durable ---------------------
        dialog.get_by_role("button", name="Cancel").click()
        assert not card.get_by_test_id("sign-in-confirm-dialog").count()
        assert _ledger(migrated_database_url) == before

        # --- the trusted click: a real takeover attempt, cleanly refused ----
        card.get_by_role("button", name="Sign in manually").click()
        card.get_by_test_id("sign-in-confirm-dialog").get_by_role("button", name="Sign in manually").click()
        card.locator("p.workspace-note", has_text="Lumi could not open the sign-in page. Try again.").wait_for(
            timeout=30_000
        )

        # No half-open state: never a takeover banner, never a login_attempts row.
        assert not card.get_by_test_id("takeover-banner").count()
        assert card.get_attribute("data-profile-status") == "NEEDS_LOGIN"
        after = _ledger(migrated_database_url)
        assert after == before  # zero tasks/actions/dispatches/observations/login_attempts
        assert _profile_status(migrated_database_url, profile_id) == "NEEDS_LOGIN"

        _no_csp_violations(app.console)
    _wait_for_no_processes("*app.server*")
