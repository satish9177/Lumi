"""Manual login and human takeover (Milestone 8a S2): the state machine.

Everything here is deterministic and needs no browser: it is the controller
half of S2, exercised with a fake worker exactly as `test_browser_profiles.py`
exercises S1's lease and deletion rules. The browser half -- headed Chromium,
the TAKEOVER network guard, the credential-surface detector, the account
fingerprint -- lives in `test_login_takeover_browser.py` behind the `browser`
marker.

The claims that matter, in the order they appear:

* a login attempt is not an authorization -- it funds nothing and clicking
  "I'm signed in" is not, by itself, an authentication claim;
* `NEEDS_LOGIN -> AUTHENTICATED` happens on exactly one conjunction: the
  worker's check reports `CHECKED`, `IN_PROFILE_SITE`, and no credential
  surface -- and on nothing else;
* cancelling, expiring or being interrupted never claims anything was signed
  in;
* every transition is a single-row compare-and-swap, so a duplicate confirm,
  a duplicate cancel and a confirm/cancel race each resolve to exactly one
  winner;
* `LoginTakeoverService` has no way to reach a planner, a provider or a model
  of any kind -- structurally, not by convention.
"""

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.errors import BrowserWorkerError, BrowserWorkerUnavailableError
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.domain.browser_profile import (
    BrowserVersions,
    ProfileRefusal,
    ProfileStatus,
)
from app.domain.login_takeover import LoginAttemptStatus, TakeoverRefusal
from app.repositories.browser import BrowserRepository
from app.repositories.login_attempts import LoginAttemptRepository
from app.services.browser_execution import TakeoverCheckOutcome, WorkerProfileOpen
from app.services.browser_profiles import BrowserProfileService
from app.services.login_takeover import (
    REASON_CREDENTIAL_SURFACE_PRESENT,
    REASON_NOT_ON_PROFILE_SITE,
    LoginTakeoverService,
)
from app.services.runtime import RuntimeGeneration, register_runtime_generation

TTL_SECONDS = 300


class FakeTakeoverBrowser:
    """Stands in for `BrowserExecutionService` for the whole S2 surface.

    Records every call it was asked to make; `confirm_takeover` returns a
    scripted `TakeoverCheckOutcome` per call, in order, which is how the
    service's branch on the check result is exercised without a browser.
    """

    def __init__(self) -> None:
        self.worker_generation = uuid.uuid4()
        self.build = "153.0.8010.12"
        self.opened: list[tuple[uuid.UUID, bool]] = []
        self.closed: list[uuid.UUID] = []
        self.started_takeovers: list[tuple[uuid.UUID, str]] = []
        self.confirmed_takeovers: list[tuple[uuid.UUID, str]] = []
        self.start_status = "OPEN"
        self.confirm_outcomes: list[TakeoverCheckOutcome] = []
        self.fail_confirm = False

    async def open_browser_profile(
        self, *, profile_id: uuid.UUID, recorded_chromium_build: str | None, headed: bool = False
    ) -> WorkerProfileOpen:
        self.opened.append((profile_id, headed))
        return WorkerProfileOpen(
            profile_id=profile_id,
            worker_generation=self.worker_generation,
            versions=BrowserVersions(
                chromium_build=self.build, playwright_version="1.63.0", app_version=""
            ),
            lock_held=True,
        )

    async def close_browser_profile(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID
    ) -> bool:
        self.closed.append(profile_id)
        return True

    async def start_takeover(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str
    ) -> str:
        self.started_takeovers.append((profile_id, site))
        return self.start_status

    async def confirm_takeover(
        self, *, profile_id: uuid.UUID, worker_generation: uuid.UUID, site: str
    ) -> TakeoverCheckOutcome:
        self.confirmed_takeovers.append((profile_id, site))
        if self.fail_confirm:
            raise BrowserWorkerUnavailableError("the worker refused the connection")
        return self.confirm_outcomes.pop(0)


def _checked(
    *, scope: str = "IN_PROFILE_SITE", credential_surface: bool = False, fingerprint: str | None = None
) -> TakeoverCheckOutcome:
    return TakeoverCheckOutcome(
        status="CHECKED", scope=scope, credential_surface=credential_surface,
        signals=("PASSWORD_FIELD",) if credential_surface else (),
        account_fingerprint=fingerprint,
    )


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    return tmp_path / "browser-profiles"


@pytest.fixture
async def browser(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> FakeTakeoverBrowser:
    """A fake worker whose generation is registered like a real one's.

    `login_attempts.worker_generation` is a real foreign key to
    `browser_worker_generations` -- true in production because
    `BrowserExecutionService._bind_worker` always registers a generation
    before `open_browser_profile` returns one. This fake stands in for that
    one side effect so the fixture matches what the fake claims to have done.
    """
    fake = FakeTakeoverBrowser()
    async with engine.begin() as connection:
        await BrowserRepository(connection).register_worker_generation(
            worker_generation=fake.worker_generation,
            runtime_generation=runtime_generation.id,
            worker_started_at=datetime.now(UTC),
        )
    return fake


@pytest.fixture
def profiles(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, browser: FakeTakeoverBrowser, profile_root: Path
) -> BrowserProfileService:
    return BrowserProfileService(
        engine,
        runtime_generation=runtime_generation.id,
        browser=browser,  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}),
    )


@pytest.fixture
def takeovers(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, browser: FakeTakeoverBrowser, profiles: BrowserProfileService
) -> LoginTakeoverService:
    return LoginTakeoverService(
        engine,
        runtime_generation=runtime_generation.id,
        browser=browser,  # type: ignore[arg-type]
        profiles=profiles,
        ttl_seconds=TTL_SECONDS,
    )


# ---- starting ---------------------------------------------------------------


async def test_starting_a_takeover_opens_the_profile_headed_and_records_an_attempt(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    outcome = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    assert outcome.attempt.status is LoginAttemptStatus.OPEN
    assert outcome.attempt.profile_id == profile.id
    assert browser.opened == [(profile.id, True)]
    assert browser.started_takeovers == [(profile.id, "github.com")]


async def test_starting_with_a_stale_revision_is_refused_and_opens_nothing(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    with pytest.raises(ProfileRefusal) as refusal:
        await takeovers.start_takeover(profile.id, expected_revision=profile.revision + 1)
    assert refusal.value.code == "stale_revision"
    assert browser.opened == []


async def test_a_second_takeover_on_an_already_open_profile_is_refused(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    first = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    with pytest.raises(TakeoverRefusal) as refusal:
        await takeovers.start_takeover(profile.id, expected_revision=first.profile.revision)
    assert refusal.value.code == "login_attempt_already_open"
    # Only one headed window was ever opened.
    assert browser.opened == [(profile.id, True)]


async def test_a_navigation_failure_closes_the_profile_and_creates_no_attempt(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser, engine: AsyncEngine
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    browser.start_status = "NAVIGATION_FAILED"
    with pytest.raises(TakeoverRefusal) as refusal:
        await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    assert refusal.value.code == "login_navigation_failed"
    assert browser.closed == [profile.id]
    async with engine.connect() as connection:
        assert await LoginAttemptRepository(connection).find_open_for_profile(profile.id) is None


# ---- confirming ---------------------------------------------------------------


async def test_confirming_a_clean_takeover_authenticates_the_profile(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked(fingerprint="a" * 64)]
    result = await takeovers.confirm_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.refusal_reason is None
    assert result.profile.status is ProfileStatus.AUTHENTICATED
    assert result.profile.account_fingerprint == "a" * 64
    assert result.profile.last_login_completed_at is not None
    assert result.attempt.status is LoginAttemptStatus.COMPLETED
    # The window is closed once the takeover's own job is done.
    assert browser.closed == [profile.id]


async def test_a_click_alone_is_not_authentication_a_remaining_credential_surface_refuses(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked(credential_surface=True)]
    result = await takeovers.confirm_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.refusal_reason == REASON_CREDENTIAL_SURFACE_PRESENT
    assert result.profile.status is ProfileStatus.NEEDS_LOGIN
    assert result.attempt.status is LoginAttemptStatus.COMPLETED


async def test_finishing_off_the_profiles_own_site_refuses_and_never_authenticates(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked(scope="OTHER_PUBLIC_SITE")]
    result = await takeovers.confirm_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.refusal_reason == REASON_NOT_ON_PROFILE_SITE
    assert result.profile.status is ProfileStatus.NEEDS_LOGIN


async def test_no_page_at_confirm_time_refuses(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked(scope="NO_PAGE")]
    result = await takeovers.confirm_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.refusal_reason == "login_no_page"
    assert result.profile.status is ProfileStatus.NEEDS_LOGIN


async def test_a_worker_failure_during_confirm_leaves_the_attempt_unconfirmed(
    profiles: BrowserProfileService,
    takeovers: LoginTakeoverService,
    browser: FakeTakeoverBrowser,
    engine: AsyncEngine,
) -> None:
    """Never `COMPLETED` without a verdict, and never an authentication claim."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.fail_confirm = True
    with pytest.raises(BrowserWorkerError):
        await takeovers.confirm_takeover(
            profile.id, started.attempt.id, expected_revision=started.profile.revision
        )
    async with engine.connect() as connection:
        attempt = await LoginAttemptRepository(connection).get(started.attempt.id)
    assert attempt is not None
    assert attempt.status is LoginAttemptStatus.UNCONFIRMED
    assert (await profiles.get_profile(profile.id)).status is ProfileStatus.NEEDS_LOGIN


# ---- cancelling, duplicates and races -----------------------------------------


async def test_cancelling_ends_the_takeover_with_no_authentication_claim(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    result = await takeovers.cancel_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert result.attempt.status is LoginAttemptStatus.CANCELLED
    assert result.profile.status is ProfileStatus.NEEDS_LOGIN
    assert browser.closed == [profile.id]
    # Cancellation is not logout: no confirm call was ever made.
    assert browser.confirmed_takeovers == []


async def test_a_duplicate_confirm_on_an_already_completed_attempt_is_refused(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked()]
    first = await takeovers.confirm_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    with pytest.raises(TakeoverRefusal) as refusal:
        await takeovers.confirm_takeover(
            profile.id, started.attempt.id, expected_revision=first.profile.revision
        )
    assert refusal.value.code == "login_attempt_not_open"
    # The worker's confirm endpoint was called exactly once for this attempt.
    assert len(browser.confirmed_takeovers) == 1


async def test_a_duplicate_cancel_is_refused(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    first = await takeovers.cancel_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    with pytest.raises(TakeoverRefusal) as refusal:
        await takeovers.cancel_takeover(
            profile.id, started.attempt.id, expected_revision=first.profile.revision
        )
    assert refusal.value.code == "login_attempt_not_open"


async def test_a_confirm_cancel_race_has_exactly_one_winner(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    """Both requests race for the same `OPEN` row; only one transition wins."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    browser.confirm_outcomes = [_checked()]

    # Cancel wins the race in this deterministic ordering.
    cancelled = await takeovers.cancel_takeover(
        profile.id, started.attempt.id, expected_revision=started.profile.revision
    )
    assert cancelled.attempt.status is LoginAttemptStatus.CANCELLED

    with pytest.raises(TakeoverRefusal) as refusal:
        await takeovers.confirm_takeover(
            profile.id, started.attempt.id, expected_revision=cancelled.profile.revision
        )
    assert refusal.value.code == "login_attempt_not_open"
    assert browser.confirmed_takeovers == []


# ---- timeout -------------------------------------------------------------------


async def test_confirming_after_the_hard_timeout_expires_the_attempt(
    engine: AsyncEngine, runtime_generation: RuntimeGeneration, browser: FakeTakeoverBrowser, profiles: BrowserProfileService
) -> None:
    short_takeovers = LoginTakeoverService(
        engine, runtime_generation=runtime_generation.id, browser=browser,  # type: ignore[arg-type]
        profiles=profiles, ttl_seconds=60,
    )
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await short_takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    # Force the attempt into the past without waiting a real minute.
    async with engine.begin() as connection:
        from sqlalchemy import text

        await connection.execute(
            text("UPDATE login_attempts SET started_at = now() - interval '2 hours', expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": started.attempt.id},
        )
    with pytest.raises(TakeoverRefusal) as refusal:
        await short_takeovers.confirm_takeover(
            profile.id, started.attempt.id, expected_revision=started.profile.revision
        )
    assert refusal.value.code == "login_attempt_expired"
    async with engine.connect() as connection:
        attempt = await LoginAttemptRepository(connection).get(started.attempt.id)
    assert attempt is not None and attempt.status is LoginAttemptStatus.EXPIRED
    assert (await profiles.get_profile(profile.id)).status is ProfileStatus.NEEDS_LOGIN
    assert browser.confirmed_takeovers == []


async def test_the_expiry_sweep_closes_the_window_and_settles_the_record(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser, engine: AsyncEngine
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    async with engine.begin() as connection:
        from sqlalchemy import text

        await connection.execute(
            text("UPDATE login_attempts SET started_at = now() - interval '2 hours', expires_at = now() - interval '1 hour' WHERE id = :id"),
            {"id": started.attempt.id},
        )
    swept = await takeovers.sweep_expired()
    assert swept == 1
    assert browser.closed == [profile.id]
    async with engine.connect() as connection:
        attempt = await LoginAttemptRepository(connection).get(started.attempt.id)
    assert attempt is not None and attempt.status is LoginAttemptStatus.EXPIRED
    assert (await profiles.get_profile(profile.id)).status is ProfileStatus.NEEDS_LOGIN


# ---- crash / restart reconciliation -------------------------------------------


async def test_an_attempt_from_a_stale_generation_is_marked_interrupted(
    engine: AsyncEngine, profiles: BrowserProfileService, browser: FakeTakeoverBrowser
) -> None:
    """A process that died with the takeover still open never claims success."""
    dead_generation = await register_runtime_generation(engine)
    dead_takeovers = LoginTakeoverService(
        engine, runtime_generation=dead_generation.id, browser=browser,  # type: ignore[arg-type]
        profiles=profiles, ttl_seconds=TTL_SECONDS,
    )
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await dead_takeovers.start_takeover(profile.id, expected_revision=profile.revision)

    live_generation = await register_runtime_generation(engine)
    live_takeovers = LoginTakeoverService(
        engine, runtime_generation=live_generation.id, browser=browser,  # type: ignore[arg-type]
        profiles=profiles, ttl_seconds=TTL_SECONDS,
    )
    interrupted = await live_takeovers.reconcile_interrupted()
    assert interrupted == 1
    async with engine.connect() as connection:
        attempt = await LoginAttemptRepository(connection).get(started.attempt.id)
    assert attempt is not None and attempt.status is LoginAttemptStatus.INTERRUPTED
    # The profile is left exactly as it was: never optimistically authenticated.
    assert (await profiles.get_profile(profile.id)).status is ProfileStatus.NEEDS_LOGIN


async def test_reconciliation_never_touches_an_attempt_from_the_current_generation(
    profiles: BrowserProfileService, takeovers: LoginTakeoverService, browser: FakeTakeoverBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    started = await takeovers.start_takeover(profile.id, expected_revision=profile.revision)
    assert await takeovers.reconcile_interrupted() == 0
    attempt = await takeovers.get_attempt(started.attempt.id)
    assert attempt.status is LoginAttemptStatus.OPEN


# ---- structural: no model surface ---------------------------------------------


_FORBIDDEN_MODEL_TOKENS = (
    "ModelRouter", "Planner", "planner", "provider", "openai", "gemini",
    "deepseek", "anthropic", "google_auth",
)
_STRIPPED = re.compile(r'(""".*?"""|\'\'\'.*?\'\'\'|#.*$)', re.DOTALL | re.MULTILINE)


def _source_without_prose(path: Path) -> str:
    """Docstrings and comments stripped, in `test_no_credential_extraction.py`'s
    own idiom, so a module may explain the rule it enforces without tripping it."""
    return _STRIPPED.sub("", path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "module",
    [
        "app/services/login_takeover.py",
        "app/repositories/login_attempts.py",
        "app/domain/login_takeover.py",
        "app/browser/takeover_guard.py",
        "app/browser/credential_signals.py",
    ],
)
def test_the_takeover_modules_import_nothing_model_shaped(module: str) -> None:
    """Zero planner calls and zero provider calls, proven structurally.

    `LoginTakeoverService` has no planner or provider dependency in its
    constructor at all -- there is nothing to inject and nothing to call.
    This is the source-level half of that guarantee, in the same spirit as
    `tests/test_no_credential_extraction.py`: it scans for the shape of a
    violation rather than trusting a comment that says one was avoided.
    """
    from app.config import AGENT_ROOT

    text = _source_without_prose(AGENT_ROOT / module)
    lowered = text.lower()
    for token in _FORBIDDEN_MODEL_TOKENS:
        assert token.lower() not in lowered, f"{module} references {token!r}"


def test_the_login_takeover_service_takes_no_planner_or_provider_dependency() -> None:
    import inspect

    parameters = inspect.signature(LoginTakeoverService.__init__).parameters
    assert set(parameters) == {"self", "engine", "runtime_generation", "browser", "profiles", "ttl_seconds"}
