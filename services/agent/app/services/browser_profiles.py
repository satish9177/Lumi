"""Creating, opening, leasing and deleting persistent browser profiles.

This is the trusted side of Milestone 8a S1. It owns four operations and no
others:

    createBrowserProfile(site, label)      -> a NEW row bound to one eTLD+1
    listBrowserProfiles()                  -> ids and controller-authored metadata
    openBrowserProfile(profileId)          -> lease + a persistent context
    deleteBrowserProfile(profileId, rev)   -> local state removed, row DELETED

There is deliberately **no** `manageProfile(action, json)`, no operation that
re-points a profile at another site, and no operation that reads anything out
of a profile. What a profile contains is Chromium's; what it *is* is Lumi's.

**Opening is a two-lock protocol, and the order matters.**

1. The runtime takes the authoritative database lease with a compare-and-swap.
   Two generations racing here cannot both win.
2. The worker takes an exclusive OS handle inside the profile directory and
   only then launches Chromium. That is the layer that catches a second Lumi
   *installation* pointed at a different database, and a database lease that
   looks stale but is not.

If step 2 refuses, step 1 is undone. A half-held profile is never left behind.

**Stale-lease recovery is deterministic, and it fails closed.** A lease held by
another runtime generation describes a process that cannot be alive -- one
runtime holds a PostgreSQL advisory lock for its whole life -- but "cannot be
alive in *this* database" is not the same as "nothing is using the directory".
So a reclaim is attempted only when the OS-level handle is also free. If
something holds it, the answer is `profile_locked_by_another_process`, not a
guess that the other owner is probably dead. Opening one Chromium profile
directory twice corrupts it, and that is not a risk worth a heuristic.

**Deleting removes local state. It does not sign the user out.** No network
request of any kind is made by `delete_profile` -- no logout endpoint, no
session revocation, no "sign out everywhere". The card this will eventually
surface must say so:

    "This removes the sign-in data stored by Lumi on this computer.
     It does not sign you out on the website."

That sentence is true precisely because the deletion path has no HTTP client in
it, and a test asserts that deleting makes no outbound connection.

**Zero model and provider involvement.** Nothing in this module calls a
planner, a provider or a model of any kind. Profile lifecycle is deterministic
infrastructure, and it stays that way.
"""

import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.errors import BrowserWorkerError, BrowserWorkerRejectedError
from app.browser.profile_lock import ProfileLock, ProfileLockUnavailableError, profile_lock_is_free
from app.browser.profile_paths import ProfilePathError, ProfilePaths, resolve_profile_paths
from app.domain.browser_profile import (
    DEFAULT_LEASE_TTL_SECONDS,
    BrowserContextKind,
    BrowserProfile,
    BrowserVersions,
    ProfileRefusal,
    ProfileStatus,
    allowed_origins_for,
    canonical_label,
    canonical_site,
)
from app.repositories.authenticated import AuthenticatedRepository
from app.repositories.profiles import BrowserProfileRepository
from app.services.browser_execution import BrowserExecutionService, WorkerProfileOpen
from app.services.form_state import FormStateRegistry

logger = logging.getLogger("lumi.profiles")


@dataclass(frozen=True, slots=True)
class OpenProfile:
    """A profile this runtime currently holds open, and where it holds it."""

    profile: BrowserProfile
    worker_generation: uuid.UUID
    versions: BrowserVersions
    #: Whether this open used a visible (headed) Chromium window. Milestone
    #: 8a S2: a takeover needs headed=True; nothing else in S2 opens a
    #: profile at all. Tracked so a second open with the other mode is
    #: refused rather than silently handed the wrong window.
    headed: bool = False


class BrowserProfileService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        runtime_generation: uuid.UUID,
        browser: BrowserExecutionService,
        paths: ProfilePaths | None = None,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        forms: FormStateRegistry | None = None,
    ) -> None:
        self._engine = engine
        #: Milestone 8b S6. Which profiles hold a local draft: closing one would destroy it.
        self._forms = forms
        self._runtime_generation = runtime_generation
        self._browser = browser
        # Derived from this process's own environment, never from a request,
        # and resolved lazily: a machine with no `%LOCALAPPDATA%` must fail
        # *profile* operations, not refuse to start the whole runtime over a
        # capability nothing has used yet.
        self._resolved_paths = paths
        self._lease_ttl = timedelta(seconds=lease_ttl_seconds)
        self._open: dict[uuid.UUID, OpenProfile] = {}

    @property
    def _paths(self) -> ProfilePaths:
        if self._resolved_paths is None:
            try:
                self._resolved_paths = resolve_profile_paths()
            except ProfilePathError as error:
                raise ProfileRefusal(error.code) from None
        return self._resolved_paths

    # -- creation and listing ------------------------------------------------

    async def create_profile(self, *, site: str, label: str) -> BrowserProfile:
        """One profile, one registrable domain, decided once and never again.

        `site` is canonicalised through the pinned Public Suffix List before a
        row exists, so `https://Sub.GitHub.com/x` and `github.com.` both become
        `github.com`, and an address literal, a bare public suffix or a local
        name is refused here rather than becoming an unbindable profile.
        """
        canonical = canonical_site(site)
        name = canonical_label(label)
        profile_id = uuid.uuid4()
        try:
            async with self._engine.begin() as connection:
                created = await BrowserProfileRepository(connection).create(
                    profile_id=profile_id,
                    label=name,
                    site=canonical,
                    allowed_origins=allowed_origins_for(canonical),
                )
        except IntegrityError:
            raise ProfileRefusal("profile_site_already_bound") from None
        logger.info(
            "browser profile created",
            extra={"profile_id": str(created.id), "site": created.site},
        )
        return created

    async def list_profiles(self) -> list[BrowserProfile]:
        async with self._engine.connect() as connection:
            return await BrowserProfileRepository(connection).list_live()

    async def get_profile(self, profile_id: uuid.UUID) -> BrowserProfile:
        async with self._engine.connect() as connection:
            profile = await BrowserProfileRepository(connection).get(profile_id)
        if profile is None:
            raise ProfileRefusal("profile_not_found")
        return profile

    # -- opening and leasing -------------------------------------------------

    async def open_profile(
        self, profile_id: uuid.UUID, *, kind: BrowserContextKind, headed: bool = False
    ) -> OpenProfile:
        """Lease the profile, then have the worker open its persistent context.

        `kind` exists to make the research/authenticated separation structural.
        A caller holding a `RESEARCH_SESSION` intent cannot reach a persistent
        profile through this method, and the research path (`ResearchService`)
        has no way to reach one at all -- it opens `/v1/sessions/open`, which
        creates a disposable context with no `user_data_dir`.

        `headed` exists for Milestone 8a S2's manual-login takeover, which
        needs a visible window. A profile already open in the *other* mode is
        refused rather than silently handed back, because a caller that asked
        for a visible window must never be given a headless one it cannot see.
        """
        if kind is not BrowserContextKind.AUTHENTICATED_PROFILE:
            raise ProfileRefusal("profile_kind_mismatch")
        profile = await self.get_profile(profile_id)
        if profile.is_deleted:
            raise ProfileRefusal("profile_deleted")
        already = self._open.get(profile_id)
        if already is not None:
            if already.headed != headed:
                raise ProfileRefusal("profile_open_mode_mismatch")
            return already

        leased = await self._acquire(profile)
        try:
            opened = await self._open_on_worker(leased, headed=headed)
        except BaseException:
            # Never leave a lease behind for a context that did not open.
            await self._release(profile_id)
            raise
        async with self._engine.begin() as connection:
            recorded = await BrowserProfileRepository(connection).record_open(
                profile_id=profile_id,
                runtime_generation=self._runtime_generation,
                versions=opened.versions,
            )
        state = OpenProfile(
            profile=recorded if recorded is not None else leased,
            worker_generation=opened.worker_generation,
            versions=opened.versions,
            headed=headed,
        )
        self._open[profile_id] = state
        return state

    async def mark_authenticated(
        self, profile_id: uuid.UUID, *, account_fingerprint: str | None
    ) -> BrowserProfile:
        """Milestone 8a S2: record a completed, verified sign-in.

        Called only by `LoginTakeoverService`, only after its worker-side
        deterministic check found the browser on the profile's own site with
        no login surface remaining. This service performs no check of its
        own here -- it trusts the caller to have already run one.
        """
        async with self._engine.begin() as connection:
            updated = await BrowserProfileRepository(connection).mark_authenticated(
                profile_id=profile_id, account_fingerprint=account_fingerprint
            )
        if updated is None:
            raise ProfileRefusal("profile_deleted")
        return updated

    async def release_read_open(self, profile_id: uuid.UUID) -> bool:
        """Close a *headless* (agent-read) open of this profile, if there is one.

        Milestone 8a S3. A human takeover always wins: it is the trusted,
        user-initiated interval, and no agent read may coexist with it. Called
        by the takeover start before it opens the headed window, so a read that
        was left open cannot make the takeover refuse -- and cannot be running
        while the human signs in.
        """
        state = self._open.get(profile_id)
        if state is None or state.headed:
            return False
        return await self.close_profile(profile_id)

    async def close_profile(self, profile_id: uuid.UUID) -> bool:
        """Close the context and drop the lease. Idempotent.

        Refused (`form_is_dirty`) while the profile holds a local form draft: closing the
        browser destroys it, and that is only ever done by the reviewed discard.
        """
        if self._forms is not None:
            self._forms.assert_clean(profile_id)
        state = self._open.pop(profile_id, None)
        if self._forms is not None:
            self._forms.clear(profile_id)
        if state is not None:
            try:
                await self._browser.close_browser_profile(
                    profile_id=profile_id, worker_generation=state.worker_generation
                )
            except BrowserWorkerError:
                # The worker is gone, so the context and its OS handle are too.
                logger.info(
                    "the worker was unavailable while closing a profile",
                    extra={"profile_id": str(profile_id)},
                )
        released = await self._release(profile_id)
        return state is not None or released

    async def _acquire(self, profile: BrowserProfile) -> BrowserProfile:
        repository_ttl = self._lease_ttl
        held_by_other = (
            profile.lease_runtime_generation is not None
            and profile.lease_runtime_generation != self._runtime_generation
        )
        # A foreign lease may only be reclaimed when nothing holds the directory.
        # This is the whole of the stale-lease rule, and it fails closed.
        reclaim = False
        if held_by_other:
            if not profile_lock_is_free(self._paths, profile.id):
                raise ProfileRefusal("profile_locked_by_another_process")
            reclaim = True
        async with self._engine.begin() as connection:
            leased = await BrowserProfileRepository(connection).acquire_lease(
                profile_id=profile.id,
                runtime_generation=self._runtime_generation,
                ttl=repository_ttl,
                reclaim=reclaim,
            )
        if leased is None:
            raise ProfileRefusal("profile_lease_unavailable")
        return leased

    async def _release(self, profile_id: uuid.UUID) -> bool:
        async with self._engine.begin() as connection:
            released = await BrowserProfileRepository(connection).release_lease(
                profile_id=profile_id, runtime_generation=self._runtime_generation
            )
        return released is not None

    async def _open_on_worker(
        self, profile: BrowserProfile, *, headed: bool = False
    ) -> WorkerProfileOpen:
        try:
            return await self._browser.open_browser_profile(
                profile_id=profile.id,
                recorded_chromium_build=profile.chromium_build,
                headed=headed,
            )
        except BrowserWorkerRejectedError as error:
            # The worker's refusal codes are already stable and safe; surface
            # the specific ones the contract names rather than a generic error.
            for code in (
                "profile_browser_downgrade_refused",
                "profile_locked_by_another_process",
                "profile_session_limit",
                "profile_directory_unavailable",
                "profile_open_failed",
            ):
                if code in str(error):
                    raise ProfileRefusal(code) from None
            raise ProfileRefusal("profile_open_failed") from None

    # -- startup reclamation -------------------------------------------------

    async def release_stale_leases(self) -> int:
        """Clear leases belonging to generations that are gone. Startup only.

        A runtime holds an exclusive advisory lock for its whole life, so any
        lease in this database naming a different generation belongs to a
        process that no longer exists. Clearing them at startup means a crashed
        generation does not lock its profiles out for the length of a TTL --
        and the OS-level handle still refuses the open if something genuinely
        does hold the directory.
        """
        async with self._engine.begin() as connection:
            cleared = await BrowserProfileRepository(connection).release_generation_leases(
                self._runtime_generation
            )
        if cleared:
            logger.info("cleared stale profile leases", extra={"profiles": cleared})
        return cleared

    # -- deletion ------------------------------------------------------------

    async def delete_profile(
        self, profile_id: uuid.UUID, *, expected_revision: int | None = None
    ) -> BrowserProfile:
        """Remove the local profile directory and mark the row `DELETED`.

        Order, and why:

        1. **Close our own context** if this runtime has one open, so Chromium
           is not writing into a directory that is about to be removed.
        2. **Refuse if another owner holds a live lease** -- the database
           statement decides, not an earlier read.
        3. **Take the exclusive OS handle** so nothing opens the profile in the
           window between the row changing and the files going.
        4. **Remove the directory tree.**
        5. **Mark the row `DELETED`** and keep it: other tables will reference a
           profile with `ondelete=RESTRICT`, and a deleted profile's history has
           to stay readable.

        This is ordinary application deletion, not forensic erasure: the files
        are unlinked, and nothing here overwrites disk sectors, defeats a
        journalling filesystem, an SSD's wear levelling, a shadow copy or a
        backup. And it makes **no network request at all** -- it is a local
        deletion, never a website logout.
        """
        profile = await self.get_profile(profile_id)
        if profile.is_deleted:
            # Idempotent by contract: deleting a deleted profile is a no-op that
            # returns the same terminal row, not an error and not a second
            # directory removal.
            return profile
        await self.close_profile(profile_id)

        async with self._engine.begin() as connection:
            deleted = await BrowserProfileRepository(connection).mark_deleted(
                profile_id=profile_id, expected_revision=expected_revision
            )
        if deleted is None:
            raise ProfileRefusal("profile_delete_refused")
        # Milestone 8a S3. The epoch already moved (a deleted profile authorises
        # nothing). Close the grants so their state says so, and remove the
        # account-private evidence and answers that were read through it: with
        # the profile gone there is nothing left they could legitimately be
        # shown against, and they must not linger as orphaned private data.
        async with self._engine.begin() as connection:
            account = AuthenticatedRepository(connection)
            await account.revoke_open_grants_for_profile(profile_id)
            await account.delete_evidence_for_profile(profile_id)

        lock = ProfileLock.for_profile(self._paths, profile_id)
        try:
            lock.acquire()
        except ProfileLockUnavailableError:
            # The row is already DELETED and holds no lease, so nothing will
            # open it again; the directory is removed on the next attempt.
            logger.warning(
                "a browser profile directory is still held and was not removed",
                extra={"profile_id": str(profile_id)},
            )
            return deleted
        except OSError:
            return deleted
        self._remove_directory(profile_id, lock)
        logger.info("browser profile deleted", extra={"profile_id": str(profile_id)})
        return deleted

    def _remove_directory(self, profile_id: uuid.UUID, lock: ProfileLock) -> None:
        """Remove everything the browser wrote, in two phases.

        The exclusive handle lives *inside* the profile directory, and Windows
        will not unlink a file that is open. So Chromium's state goes first --
        every child except the lock file, while the lock is still held, which is
        what keeps a second installation from opening the profile mid-removal --
        and only then is the handle released and the now-empty directory taken
        with it.

        This is ordinary application deletion: files are unlinked. It is not
        forensic erasure, and nothing here defeats a journalling filesystem, an
        SSD's wear levelling, a shadow copy or a backup.
        """
        directory = self._paths.directory(profile_id)
        lock_file = self._paths.lock_file(profile_id)
        errors: list[str] = []

        def _failed(function: object, path: str, error: BaseException) -> None:
            # Codes only: a failure record must not carry the path either.
            errors.append(type(error).__name__)

        if directory.is_dir():
            for child in directory.iterdir():
                if child == lock_file:
                    continue
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, onexc=_failed)
                    else:
                        child.unlink()
                except OSError as error:
                    errors.append(type(error).__name__)
        lock.release()
        try:
            lock_file.unlink(missing_ok=True)
            if directory.is_dir():
                directory.rmdir()
        except OSError as error:
            errors.append(type(error).__name__)
        if directory.exists():
            # Never "probably fine": a partially removed profile that reported
            # success would leave session cookies on disk while the UI said
            # they were gone.
            logger.warning(
                "a browser profile directory could not be fully removed",
                extra={"profile_id": str(profile_id), "errors": sorted(set(errors))},
            )


__all__ = ["BrowserProfileService", "OpenProfile", "ProfileStatus"]
