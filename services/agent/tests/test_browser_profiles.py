"""Profile identity, the one-owner lease, site binding, and deletion.

Everything here is deterministic and needs no browser: it is the controller
half of Milestone 8a S1. The browser half -- persistence across a restart, the
OS-level handle, the Chromium version guard -- lives in
`test_browser_profile_session.py` behind the `browser` marker.

The claims that matter, in the order they appear:

* a profile is bound to one registrable domain, decided once, and **nothing can
  rebind it** -- not the service, not a direct `UPDATE`;
* two runtime generations cannot both hold one profile;
* a stale lease is reclaimable, a live one is not, and the difference is
  decided by the OS handle rather than guessed;
* deleting removes local state, marks the row `DELETED`, is idempotent, and
  **makes no network request of any kind** -- it is not a website logout.
"""

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.profile_lock import ProfileLock
from app.browser.profile_paths import PROFILE_ROOT_VARIABLE, resolve_profile_paths
from app.domain.browser_profile import (
    BrowserContextKind,
    BrowserVersions,
    ProfileRefusal,
    ProfileStatus,
    canonical_label,
    canonical_site,
)
from app.repositories.profiles import BrowserProfileRepository
from app.services.browser_execution import WorkerProfileOpen
from app.services.browser_profiles import BrowserProfileService
from app.services.runtime import RuntimeGeneration, register_runtime_generation
from tests.test_no_credential_extraction import PROFILE_FILES

TTL = timedelta(seconds=300)


class FakeBrowser:
    """Stands in for `BrowserExecutionService` without launching Chromium.

    It records what it was asked and can be made to refuse, which is how the
    controller-side lease and deletion rules are tested without a browser. The
    real worker path is exercised in `test_browser_profile_session.py`.
    """

    def __init__(self, *, refuse: str | None = None) -> None:
        self.refuse = refuse
        self.opened: list[uuid.UUID] = []
        self.closed: list[uuid.UUID] = []
        self.worker_generation = uuid.uuid4()
        self.build = "153.0.8010.12"

    async def open_browser_profile(
        self, *, profile_id: uuid.UUID, recorded_chromium_build: str | None
    ) -> WorkerProfileOpen:
        if self.refuse is not None:
            raise ProfileRefusal(self.refuse)
        self.opened.append(profile_id)
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


@pytest.fixture
def profile_root(tmp_path: Path) -> Path:
    """A temporary base, so the suite never writes into the real LOCALAPPDATA."""
    return tmp_path / "browser-profiles"


@pytest.fixture
def browser() -> FakeBrowser:
    return FakeBrowser()


@pytest.fixture
def profiles(
    engine: AsyncEngine,
    runtime_generation: RuntimeGeneration,
    browser: FakeBrowser,
    profile_root: Path,
) -> BrowserProfileService:
    return BrowserProfileService(
        engine,
        runtime_generation=runtime_generation.id,
        browser=browser,  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}),
    )


# ---- site binding ----------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("github.com", "github.com"),
        ("GitHub.com", "github.com"),
        ("https://github.com/lumi/repo", "github.com"),
        ("https://sub.github.com/", "github.com"),
        ("www.example.co.uk", "example.co.uk"),
        # PSL semantics, not string splitting: a Pages project is its own site.
        ("https://project.github.io/docs", "project.github.io"),
    ],
)
async def test_a_site_is_canonicalised_to_its_registrable_domain(
    profiles: BrowserProfileService, given: str, expected: str
) -> None:
    profile = await profiles.create_profile(site=given, label="Test profile")
    assert profile.site == expected
    assert profile.allowed_origins == (f"https://{expected}", f"https://www.{expected}")
    assert profile.status is ProfileStatus.NEW


@pytest.mark.parametrize(
    "site", ["com", "co.uk", "localhost", "127.0.0.1", "github.com:443", "", "not a host"]
)
async def test_a_site_with_no_registrable_domain_is_refused_before_a_row_exists(
    profiles: BrowserProfileService, site: str
) -> None:
    with pytest.raises(ProfileRefusal) as refusal:
        await profiles.create_profile(site=site, label="Test profile")
    assert refusal.value.code.startswith("site_")
    assert await profiles.list_profiles() == []


async def test_only_one_live_profile_may_hold_a_site(
    profiles: BrowserProfileService,
) -> None:
    first = await profiles.create_profile(site="github.com", label="GitHub - Personal")
    with pytest.raises(ProfileRefusal) as refusal:
        await profiles.create_profile(site="https://www.github.com", label="GitHub - Work")
    assert refusal.value.code == "profile_site_already_bound"

    # Deleting frees the site again, which is what makes "delete this profile"
    # a complete action rather than a dead end.
    await profiles.delete_profile(first.id)
    replacement = await profiles.create_profile(site="github.com", label="GitHub - Work")
    assert replacement.id != first.id


async def test_a_profile_cannot_be_rebound_to_another_site(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    """There is no `changeProfileSite` API -- and the database refuses too.

    The service has no such operation, so this reaches past it with a direct
    `UPDATE`: the guarantee has to hold against a future code path nobody has
    written yet, not just against the ones that exist.
    """
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    statements = (
        "UPDATE browser_profiles SET site = 'example.com' WHERE id = :id",
        "UPDATE browser_profiles SET allowed_origins = '[\"https://evil.example\"]'::jsonb"
        " WHERE id = :id",
    )
    for statement in statements:
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(text(statement), {"id": profile.id})
    assert (await profiles.get_profile(profile.id)).site == "github.com"


async def test_a_deleted_profile_can_never_be_reopened(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    await profiles.delete_profile(profile.id)
    with pytest.raises(DBAPIError):
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE browser_profiles SET status = 'NEEDS_LOGIN' WHERE id = :id"),
                {"id": profile.id},
            )


async def test_a_label_is_bounded_sanitised_and_never_an_authority(
    profiles: BrowserProfileService,
) -> None:
    profile = await profiles.create_profile(site="github.com", label="  GitHub   -  Personal ")
    assert profile.label == "GitHub - Personal"
    # Whitespace a person pasted is normalised rather than refused: the label
    # is display text, and a stray newline is a typo, not an attack.
    assert canonical_label("line\nbreak") == "line break"
    assert canonical_label("tab\tstop") == "tab stop"
    # Everything that could make one name render as another, or be read as
    # structure by a later consumer, is refused outright.
    for bad, code in (
        ("", "label_length"),
        (" ", "label_length"),
        ("x" * 61, "label_length"),
        ("null\x00byte", "label_control_characters"),
        ("spoof‮yfilp", "label_control_characters"),
        ("<script>", "label_characters"),
        ('quote"it', "label_characters"),
        ("back\\slash", "label_characters"),
        ("path/../traversal\\x", "label_characters"),
    ):
        with pytest.raises(ProfileRefusal) as refusal:
            canonical_label(bad)
        assert refusal.value.code == code, bad


def test_canonical_site_and_label_are_pure_functions() -> None:
    """Neither touches the filesystem, the database or the network."""
    assert canonical_site(" HTTPS://WWW.GitHub.com/x?y#z ") == "github.com"
    assert canonical_label("Bank \u2013 joint") == "Bank \u2013 joint"


# ---- the lease -------------------------------------------------------------


async def test_the_first_owner_gets_the_lease_and_the_second_is_refused(
    engine: AsyncEngine, profiles: BrowserProfileService, browser: FakeBrowser
) -> None:
    """Two generations, one profile, and the database decides.

    The second generation's OS-level handle is free (no browser ever ran), so
    the only thing stopping a double open would be the lease -- which is
    exactly what is being tested. It is refused because the first generation's
    lease has not expired and the reclaim path requires an expired or absent
    one, not merely a different one.
    """
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    opened = await profiles.open_profile(
        profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
    )
    assert opened.profile.status is ProfileStatus.NEEDS_LOGIN
    assert browser.opened == [profile.id]

    other_generation = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        stolen = await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=other_generation.id, ttl=TTL
        )
    assert stolen is None, "a live lease was taken by a second generation"
    # The first owner still holds it, at the revision it was granted at.
    held = await profiles.get_profile(profile.id)
    assert held.lease_runtime_generation == opened.profile.lease_runtime_generation


async def test_a_released_lease_can_be_taken_again(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    await profiles.open_profile(profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE)
    await profiles.close_profile(profile.id)
    assert (await profiles.get_profile(profile.id)).lease_runtime_generation is None

    other = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        taken = await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=other.id, ttl=TTL
        )
    assert taken is not None
    assert taken.lease_runtime_generation == other.id


async def test_an_expired_lease_is_reclaimable_without_help(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    """Expiry is evaluated by the statement, not by a sweeper."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    stale = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=stale.id, ttl=timedelta(seconds=-1)
        )
    opened = await profiles.open_profile(
        profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
    )
    assert opened.profile.lease_runtime_generation is not None
    assert opened.profile.lease_runtime_generation != stale.id


async def test_a_stale_lease_with_a_live_os_lock_is_refused_not_guessed(
    engine: AsyncEngine, profiles: BrowserProfileService, profile_root: Path
) -> None:
    """The fail-closed half of stale-lease recovery.

    The lease belongs to a generation that is gone, so the database alone would
    say "reclaim it". Something is holding the directory, so Lumi says no
    instead: opening one Chromium profile directory twice corrupts it, and a
    heuristic is not worth that.
    """
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    dead = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=dead.id, ttl=TTL
        )
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)})
    paths.create(profile.id)
    with ProfileLock.for_profile(paths, profile.id):
        with pytest.raises(ProfileRefusal) as refusal:
            await profiles.open_profile(
                profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
            )
    assert refusal.value.code == "profile_locked_by_another_process"
    # ...and the lease was not touched on the way out.
    assert (await profiles.get_profile(profile.id)).lease_runtime_generation == dead.id


async def test_a_stale_lease_with_a_free_os_lock_is_reclaimed(
    engine: AsyncEngine, profiles: BrowserProfileService, profile_root: Path
) -> None:
    """The other half: no process holds the directory, so the reclaim proceeds."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    dead = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=dead.id, ttl=TTL
        )
    opened = await profiles.open_profile(
        profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
    )
    assert opened.profile.lease_runtime_generation != dead.id


async def test_startup_clears_leases_left_by_generations_that_are_gone(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    """A crashed generation must not lock a profile out for its whole TTL."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    dead = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=dead.id, ttl=TTL
        )
    assert await profiles.release_stale_leases() == 1
    assert (await profiles.get_profile(profile.id)).lease_runtime_generation is None


async def test_a_worker_refusal_does_not_leave_a_lease_behind(
    engine: AsyncEngine,
    runtime_generation: RuntimeGeneration,
    profile_root: Path,
) -> None:
    """A half-held profile would be worse than a refused one."""
    refusing = FakeBrowser(refuse="profile_browser_downgrade_refused")
    service = BrowserProfileService(
        engine,
        runtime_generation=runtime_generation.id,
        browser=refusing,  # type: ignore[arg-type]
        paths=resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)}),
    )
    profile = await service.create_profile(site="github.com", label="GitHub")
    with pytest.raises(ProfileRefusal) as refusal:
        await service.open_profile(profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE)
    assert refusal.value.code == "profile_browser_downgrade_refused"
    after = await service.get_profile(profile.id)
    assert after.lease_runtime_generation is None
    # And the profile still exists, unchanged. A refused downgrade never
    # deletes or repairs anything.
    assert after.status is ProfileStatus.NEW


# ---- research / authenticated separation -----------------------------------


async def test_a_research_session_cannot_be_substituted_for_an_auth_profile(
    profiles: BrowserProfileService,
) -> None:
    """The two context kinds refuse each other rather than falling back."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    with pytest.raises(ProfileRefusal) as refusal:
        await profiles.open_profile(profile.id, kind=BrowserContextKind.RESEARCH_SESSION)
    assert refusal.value.code == "profile_kind_mismatch"
    assert (await profiles.get_profile(profile.id)).lease_runtime_generation is None


def test_the_research_service_has_no_route_to_a_persistent_profile() -> None:
    """M7b opens `/v1/sessions/open`, which creates no `user_data_dir`.

    Structural, not conventional: the research code has no import of the
    profile modules and no mention of a persistent context.
    """
    from pathlib import Path as _Path

    agent = _Path(__file__).resolve().parents[1]
    for module in ("app/services/research_tasks.py", "app/browser/research_session.py"):
        source = (agent / module).read_text(encoding="utf-8")
        assert "launch_persistent_context" not in source
        assert "user_data_dir" not in source
        assert "browser_profile" not in source
        assert "profile_paths" not in source


# ---- deletion --------------------------------------------------------------


async def test_deleting_removes_the_directory_and_marks_the_row(
    profiles: BrowserProfileService, profile_root: Path
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(profile_root)})
    directory = paths.create(profile.id)
    (directory / "Default").mkdir()
    (directory / "Default" / "something").write_bytes(b"chromium wrote this")
    assert directory.exists()

    deleted = await profiles.delete_profile(profile.id)
    assert deleted.status is ProfileStatus.DELETED
    assert deleted.deleted_at is not None
    assert deleted.lease_runtime_generation is None
    assert not directory.exists()
    # The row stays: later tables reference a profile with ondelete=RESTRICT,
    # and a deleted profile's history has to remain readable.
    assert (await profiles.get_profile(profile.id)).is_deleted
    assert await profiles.list_profiles() == []


async def test_deleting_is_idempotent(profiles: BrowserProfileService) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    first = await profiles.delete_profile(profile.id)
    second = await profiles.delete_profile(profile.id)
    assert first.status is second.status is ProfileStatus.DELETED
    assert first.revision == second.revision, "the second delete wrote nothing"


async def test_deleting_is_refused_while_another_owner_holds_a_live_lease(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    other = await register_runtime_generation(engine)
    async with engine.begin() as connection:
        await BrowserProfileRepository(connection).acquire_lease(
            profile_id=profile.id, runtime_generation=other.id, ttl=TTL
        )
    with pytest.raises(ProfileRefusal) as refusal:
        await profiles.delete_profile(profile.id)
    assert refusal.value.code == "profile_delete_refused"
    assert not (await profiles.get_profile(profile.id)).is_deleted


async def test_deleting_with_a_stale_expected_revision_writes_nothing(
    profiles: BrowserProfileService,
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    with pytest.raises(ProfileRefusal):
        await profiles.delete_profile(profile.id, expected_revision=profile.revision + 5)
    assert not (await profiles.get_profile(profile.id)).is_deleted


async def test_deleting_makes_no_network_request_at_all(
    profiles: BrowserProfileService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local deletion, never a website logout.

    The wording the trusted UI will carry -- *"This removes the sign-in data
    stored by Lumi on this computer. It does not sign you out on the website."*
    -- is true precisely because there is no HTTP client in this path. Every
    socket-opening entry point is replaced with one that fails the test.
    """
    import socket

    import httpx

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deleting a profile must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)

    profile = await profiles.create_profile(site="github.com", label="GitHub")
    deleted = await profiles.delete_profile(profile.id)
    assert deleted.status is ProfileStatus.DELETED


def test_the_deletion_path_holds_no_http_client_and_no_logout() -> None:
    """Source-level, so it stays true when somebody adds a "tidy up" step."""
    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "browser_profiles.py"
    ).read_text(encoding="utf-8")
    # Docstrings and comments removed: the module explains at length that it
    # performs no logout, and has to be able to say the word it forbids.
    import re as regex

    body = regex.sub(r"#[^\n]*", "", regex.sub(r'"""(?:.|\n)*?"""', "", source))
    for forbidden in ("httpx", "requests", "urlopen", "logout", "sign_out", "signout"):
        assert forbidden not in body, f"{forbidden} has no place in profile lifecycle code"


# ---- what a profile row may reveal -----------------------------------------


async def test_no_profile_path_appears_in_any_api_response(
    client: Any, profile_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path is derived in trusted processes and crosses no boundary."""
    monkeypatch.setenv(PROFILE_ROOT_VARIABLE, str(profile_root))
    created = await client.post(
        "/browser-profiles", json={"site": "github.com", "label": "GitHub - Personal"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    listed = await client.get("/browser-profiles")
    assert listed.status_code == 200

    for payload in (created.text, listed.text):
        lowered = payload.lower()
        for leak in (
            "localappdata",
            "browser-profiles",
            "userdatadir",
            "user_data_dir",
            "profilepath",
            "profile_path",
            "storagestate",
            "storage_state",
            "cookie",
            "appdata",
            "c:\\\\",
            "chrome.exe",
        ):
            assert leak not in lowered, f"{leak!r} reached an API response"
    # What *is* returned is the controller's own vocabulary.
    assert body["site"] == "github.com"
    assert body["label"] == "GitHub - Personal"
    assert body["status"] == "NEW"
    assert body["account_fingerprint"] is None
    assert body["account_label_hash"] is None
    assert body["revoke_epoch"] == 0
    assert body["leased"] is False


async def test_the_api_refuses_a_path_shaped_field(client: Any) -> None:
    """`extra="forbid"`, so a boundary widening is a test failure, not a
    silently ignored field."""
    for extra in (
        {"profilePath": "C:/tmp/x"},
        {"userDataDir": "C:/tmp/x"},
        {"storageState": "{}"},
        {"cookieFile": "C:/tmp/cookies"},
        {"browserExecutablePath": "C:/chrome.exe"},
    ):
        response = await client.post(
            "/browser-profiles", json={"site": "github.com", "label": "GitHub", **extra}
        )
        assert response.status_code == 422, extra


async def test_no_runtime_log_record_carries_a_profile_path(
    profiles: BrowserProfileService,
    profile_root: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Diagnostics are ids, sites and codes. A path is none of those.

    Every record the profile service emits during a full create/open/delete
    cycle is inspected -- the formatted message *and* every attribute the
    record carries, since `extra=` fields are what a structured log ships.
    """
    caplog.set_level("DEBUG")
    profile = await profiles.create_profile(site="github.com", label="GitHub - Personal")
    await profiles.open_profile(profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE)
    await profiles.delete_profile(profile.id)

    records = [record for record in caplog.records if record.name.startswith("lumi")]
    assert records, "nothing was logged, so this test proved nothing"
    leaks = (
        str(profile_root),
        str(profile_root).replace("\\", "/"),
        "browser-profiles",
        "LOCALAPPDATA",
        "user_data_dir",
        "userDataDir",
        "storage_state",
        # Chromium's own profile filenames, from the one list the source-level
        # guard uses, so the two cannot drift.
        *PROFILE_FILES,
    )
    for record in records:
        rendered = record.getMessage() + " " + repr(record.__dict__)
        for leak in leaks:
            assert leak not in rendered, f"{leak!r} reached {record.name}"
    # What *is* there: the opaque id, so a support question stays answerable.
    assert any(str(profile.id) in repr(record.__dict__) for record in records)


async def test_an_unknown_profile_is_refused_without_saying_whether_a_directory_exists(
    client: Any,
) -> None:
    response = await client.get(f"/browser-profiles/{uuid.uuid4()}")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "browser_profile_refused"
    assert body["error"]["reason"] == "profile_not_found"
    assert "browser-profiles" not in response.text.lower()


# ---- version metadata ------------------------------------------------------


async def test_opening_records_the_browser_that_opened_it(
    profiles: BrowserProfileService, browser: FakeBrowser
) -> None:
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    opened = await profiles.open_profile(
        profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
    )
    assert opened.profile.chromium_build == browser.build
    assert opened.profile.playwright_version == "1.63.0"
    assert opened.profile.last_observed_at is not None


async def test_opening_never_makes_a_profile_authenticated(
    profiles: BrowserProfileService,
) -> None:
    """S1 has no login flow, so nothing may claim anybody is signed in.

    A profile that opened is a profile with a browser attached. Whether anyone
    is signed in is decided in S2 by a fresh observation, and S1 does not
    observe anything at all.
    """
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    for _ in range(3):
        opened = await profiles.open_profile(
            profile.id, kind=BrowserContextKind.AUTHENTICATED_PROFILE
        )
        assert opened.profile.status is ProfileStatus.NEEDS_LOGIN
        await profiles.close_profile(profile.id)
    final = await profiles.get_profile(profile.id)
    assert final.status is ProfileStatus.NEEDS_LOGIN
    assert final.last_login_completed_at is None
    assert final.account_fingerprint is None


async def test_a_profile_row_can_never_hold_a_fabricated_fingerprint(
    engine: AsyncEngine, profiles: BrowserProfileService
) -> None:
    """Nullable, hash-shaped, and never defaulted to something that looks real."""
    profile = await profiles.create_profile(site="github.com", label="GitHub")
    assert profile.account_fingerprint is None
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE browser_profiles SET account_fingerprint = 'unknown' WHERE id = :id"
                ),
                {"id": profile.id},
            )
