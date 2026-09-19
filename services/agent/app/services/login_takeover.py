"""Manual login and human takeover (Milestone 8a S2).

This is the trusted side of the flow in `docs/plans/milestone-8.md` §8. It
owns exactly three operations and no others:

    startTakeover(profileId, expectedRevision)    -> a headed window, human-driven
    cancelTakeover(profileId, attemptId, rev)      -> ends it; no authentication claim
    confirmTakeover(profileId, attemptId, rev)     -> ends it; one deterministic check

**A login attempt is not an authorization.** It records a bounded interval in
which the user controlled the browser. It never funds a step, a grant or an
approval, and clicking "I'm signed in" is never, by itself, treated as proof
that a sign-in succeeded -- only the deterministic check that follows is.

**Zero model and provider involvement, structurally.** Nothing in this module
imports a planner, a provider, a model router or anything that could reach
one. `tests/test_login_takeover_has_no_model_surface.py` scans this module
(and the worker-side takeover code) for exactly that.

**Closing the window is the service's decision, not the worker's.** After
`confirm_takeover`'s deterministic check runs -- whatever it finds -- and
after `cancel_takeover` or the expiry sweep ends an attempt, this module
closes the profile's persistent context. A takeover that just ended has
nothing further to do with a visible browser window, and "no login browser
survives its owner process" reads more simply if this module is also the one
that shuts the window at the moment its own job is done.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.browser.errors import BrowserWorkerError
from app.domain.browser_profile import BrowserContextKind, BrowserProfile, ProfileRefusal
from app.domain.login_takeover import (
    DEFAULT_TAKEOVER_TTL_SECONDS,
    LoginAttempt,
    TakeoverRefusal,
    TakeoverSiteScope,
)
from app.repositories.login_attempts import LoginAttemptRepository
from app.services.browser_execution import BrowserExecutionService
from app.services.browser_profiles import BrowserProfileService

logger = logging.getLogger("lumi.login_takeover")

#: Reasons `confirm_takeover` may report alongside a profile that stayed
#: `NEEDS_LOGIN`. Never persisted -- transient response fields only.
REASON_NOT_ON_PROFILE_SITE = "login_not_on_profile_site"
REASON_CREDENTIAL_SURFACE_PRESENT = "login_credential_surface_present"
REASON_NO_PAGE = "login_no_page"


@dataclass(frozen=True, slots=True)
class TakeoverOutcome:
    """One takeover operation's full, reportable result.

    `refusal_reason` is set only by a *completed* confirmation that did not
    result in `AUTHENTICATED`; it explains why without ever naming a host or
    quoting a page.
    """

    attempt: LoginAttempt
    profile: BrowserProfile
    refusal_reason: str | None = None


class LoginTakeoverService:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        runtime_generation: uuid.UUID,
        browser: BrowserExecutionService,
        profiles: BrowserProfileService,
        ttl_seconds: int = DEFAULT_TAKEOVER_TTL_SECONDS,
    ) -> None:
        self._engine = engine
        self._runtime_generation = runtime_generation
        self._browser = browser
        self._profiles = profiles
        self._ttl = timedelta(seconds=ttl_seconds)

    # -- starting -------------------------------------------------------------

    async def start_takeover(
        self, profile_id: uuid.UUID, *, expected_revision: int
    ) -> TakeoverOutcome:
        """Open the profile headed, navigate its own site, and hand it over.

        Order, and why: the profile is opened (leased, headed) *before* the
        attempt row is created, so a refusal to open never leaves an orphaned
        `login_attempts` row behind. If the worker call that starts the
        takeover then fails, the profile is closed again and the refusal
        propagates -- there is no half-open takeover state.
        """
        profile = await self._profiles.get_profile(profile_id)
        if profile.is_deleted:
            raise ProfileRefusal("profile_deleted")
        if profile.revision != expected_revision:
            raise ProfileRefusal("stale_revision")

        async with self._engine.connect() as connection:
            existing = await LoginAttemptRepository(connection).find_open_for_profile(profile_id)
        if existing is not None:
            raise TakeoverRefusal("login_attempt_already_open")

        opened = await self._profiles.open_profile(
            profile_id, kind=BrowserContextKind.AUTHENTICATED_PROFILE, headed=True
        )
        try:
            status = await self._browser.start_takeover(
                profile_id=profile_id,
                worker_generation=opened.worker_generation,
                site=opened.profile.site,
            )
        except BaseException:
            await self._profiles.close_profile(profile_id)
            raise
        if status != "OPEN":
            await self._profiles.close_profile(profile_id)
            raise TakeoverRefusal(
                "login_navigation_failed" if status == "NAVIGATION_FAILED" else "login_profile_not_open"
            )

        attempt_id = uuid.uuid4()
        try:
            async with self._engine.begin() as connection:
                await LoginAttemptRepository(connection).create(
                    attempt_id=attempt_id,
                    profile_id=profile_id,
                    runtime_generation=self._runtime_generation,
                    profile_revision=opened.profile.revision,
                    ttl=self._ttl,
                )
                await LoginAttemptRepository(connection).set_worker_generation(
                    attempt_id=attempt_id, worker_generation=opened.worker_generation
                )
        except IntegrityError:
            # A concurrent start won the race for this profile's one open
            # attempt slot.
            await self._profiles.close_profile(profile_id)
            raise TakeoverRefusal("login_attempt_already_open") from None
        except BaseException:
            # Anything else durable-record-side (a lost database connection,
            # for instance) must not leave a headed window with no attempt
            # row anyone can address.
            await self._profiles.close_profile(profile_id)
            raise
        # Re-read: `set_worker_generation` returns the row with the field set.
        refreshed = await self._get_attempt(attempt_id)
        logger.info(
            "login takeover started",
            extra={"profile_id": str(profile_id), "attempt_id": str(attempt_id)},
        )
        return TakeoverOutcome(attempt=refreshed, profile=opened.profile)

    # -- cancelling -------------------------------------------------------------

    async def cancel_takeover(
        self, profile_id: uuid.UUID, attempt_id: uuid.UUID, *, expected_revision: int
    ) -> TakeoverOutcome:
        """End the takeover. Never a logout, never an authentication claim."""
        attempt = await self._get_attempt(attempt_id)
        if attempt.profile_id != profile_id:
            raise TakeoverRefusal("login_attempt_not_found")
        profile = await self._profiles.get_profile(profile_id)
        if profile.revision != expected_revision:
            raise ProfileRefusal("stale_revision")

        async with self._engine.begin() as connection:
            cancelled = await LoginAttemptRepository(connection).cancel(attempt_id)
        if cancelled is None:
            raise TakeoverRefusal("login_attempt_not_open")
        await self._profiles.close_profile(profile_id)
        logger.info(
            "login takeover cancelled",
            extra={"profile_id": str(profile_id), "attempt_id": str(attempt_id)},
        )
        refreshed_profile = await self._profiles.get_profile(profile_id)
        return TakeoverOutcome(attempt=cancelled, profile=refreshed_profile)

    # -- confirming -------------------------------------------------------------

    async def confirm_takeover(
        self, profile_id: uuid.UUID, attempt_id: uuid.UUID, *, expected_revision: int
    ) -> TakeoverOutcome:
        """End the takeover and run the one deterministic post-login check.

        Clicking "I'm signed in" is not, by itself, an authentication claim.
        It only starts this check, and this check is the only thing that may
        move the profile to `AUTHENTICATED`.
        """
        attempt = await self._get_attempt(attempt_id)
        if attempt.profile_id != profile_id:
            raise TakeoverRefusal("login_attempt_not_found")
        profile = await self._profiles.get_profile(profile_id)
        if profile.revision != expected_revision:
            raise ProfileRefusal("stale_revision")

        now = datetime.now(UTC)
        if attempt.is_expired(now):
            async with self._engine.begin() as connection:
                await LoginAttemptRepository(connection).expire_due(now=now)
            await self._profiles.close_profile(profile_id)
            raise TakeoverRefusal("login_attempt_expired")

        async with self._engine.begin() as connection:
            unconfirmed = await LoginAttemptRepository(connection).begin_confirm(attempt_id)
        if unconfirmed is None:
            raise TakeoverRefusal("login_attempt_not_open")

        # From here the attempt is UNCONFIRMED. If this process dies before
        # `complete()` below runs, the next restart's reconciliation marks it
        # INTERRUPTED and the profile is left exactly as it was -- never
        # optimistically authenticated.
        worker_generation = unconfirmed.worker_generation
        if worker_generation is None:  # pragma: no cover - defensive; always set at start.
            raise TakeoverRefusal("login_attempt_not_open")

        try:
            check = await self._browser.confirm_takeover(
                profile_id=profile_id, worker_generation=worker_generation, site=profile.site
            )
        except BaseException:
            # The attempt stays UNCONFIRMED -- never COMPLETED without a
            # verdict -- and a restart's reconciliation marks it INTERRUPTED.
            # Best-effort close so a broken worker call does not leave a
            # headed window nobody is driving.
            try:
                await self._profiles.close_profile(profile_id)
            except BrowserWorkerError:
                pass
            raise

        refusal_reason: str | None = None
        if check.status != "CHECKED" or check.scope == TakeoverSiteScope.NO_PAGE.value:
            # Either the worker could not run the check at all (the profile
            # was not open), or it ran and found no tracked page/tab left --
            # distinct from "found a page on the wrong site" below.
            refusal_reason = REASON_NO_PAGE
        elif check.scope != TakeoverSiteScope.IN_PROFILE_SITE.value:
            refusal_reason = REASON_NOT_ON_PROFILE_SITE
        elif check.credential_surface:
            refusal_reason = REASON_CREDENTIAL_SURFACE_PRESENT

        if refusal_reason is None:
            await self._profiles.mark_authenticated(
                profile_id, account_fingerprint=check.account_fingerprint
            )
        async with self._engine.begin() as connection:
            completed = await LoginAttemptRepository(connection).complete(attempt_id)

        await self._profiles.close_profile(profile_id)
        final_profile = await self._profiles.get_profile(profile_id)
        logger.info(
            "login takeover confirmed",
            extra={
                "profile_id": str(profile_id),
                "attempt_id": str(attempt_id),
                "authenticated": refusal_reason is None,
                "reason": refusal_reason,
            },
        )
        return TakeoverOutcome(
            attempt=completed if completed is not None else unconfirmed,
            profile=final_profile,
            refusal_reason=refusal_reason,
        )

    # -- background maintenance ------------------------------------------------

    async def reconcile_interrupted(self) -> int:
        """Startup only: any open attempt from a runtime generation that is
        gone becomes `INTERRUPTED`. The profile itself is left untouched --
        it was never optimistically marked `AUTHENTICATED` mid-takeover, so
        there is nothing to walk back."""
        async with self._engine.begin() as connection:
            interrupted = await LoginAttemptRepository(
                connection
            ).mark_interrupted_for_stale_generations(self._runtime_generation)
        if interrupted:
            logger.info(
                "marked interrupted login attempts from a prior generation",
                extra={"count": len(interrupted)},
            )
        return len(interrupted)

    async def sweep_expired(self) -> int:
        """Periodic: close the headed window for every takeover past its
        hard timeout, and settle the durable record. No assumption is made
        about whether the website session persisted."""
        now = datetime.now(UTC)
        async with self._engine.begin() as connection:
            expired = await LoginAttemptRepository(connection).expire_due(now=now)
        for attempt in expired:
            try:
                await self._profiles.close_profile(attempt.profile_id)
            except BrowserWorkerError:  # pragma: no cover - best-effort close.
                logger.warning(
                    "could not close an expired takeover's browser",
                    extra={"profile_id": str(attempt.profile_id)},
                )
        if expired:
            logger.info("expired login takeovers", extra={"count": len(expired)})
        return len(expired)

    # -- reads ------------------------------------------------------------------

    async def get_attempt(self, attempt_id: uuid.UUID) -> LoginAttempt:
        return await self._get_attempt(attempt_id)

    async def _get_attempt(self, attempt_id: uuid.UUID) -> LoginAttempt:
        async with self._engine.connect() as connection:
            attempt = await LoginAttemptRepository(connection).get(attempt_id)
        if attempt is None:
            raise TakeoverRefusal("login_attempt_not_found")
        return attempt


__all__ = [
    "REASON_CREDENTIAL_SURFACE_PRESENT",
    "REASON_NOT_ON_PROFILE_SITE",
    "REASON_NO_PAGE",
    "LoginTakeoverService",
    "TakeoverOutcome",
]
