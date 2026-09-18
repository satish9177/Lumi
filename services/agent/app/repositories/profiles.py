"""SQL for persistent browser profiles. Callers own the transaction.

Three statements here are load-bearing rather than convenient, and all three
are about ownership:

* **`acquire_lease` is a compare-and-swap that also decides the contest.** The
  conditions under which a lease may be taken are written into the `UPDATE`,
  not checked by an earlier `SELECT`, so two runtime generations racing for one
  profile cannot both win: exactly one `UPDATE` matches a row, and the other
  gets zero rows back and refuses.

* **`release_lease` is generation-bound.** A generation can only clear its own
  lease. A late release from a process that has already been reclaimed from
  cannot unlock a profile somebody else now owns.

* **`mark_deleted` refuses while any lease is live.** Deletion is not allowed to
  win a race against an owner, because the directory removal that follows it is
  not reversible.

The row carries no path, no cookie and no token, so there is no statement here
that could return one.
"""

import uuid
from datetime import timedelta

from sqlalchemy import Row, and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.tables import browser_profiles
from app.domain.browser_profile import (
    LIVE_STATUSES,
    BrowserProfile,
    BrowserVersions,
    ProfileStatus,
)

_LIVE = tuple(status.value for status in LIVE_STATUSES)


def _profile(row: Row[tuple[object, ...]]) -> BrowserProfile:
    mapping = row._mapping
    origins = mapping["allowed_origins"]
    return BrowserProfile(
        id=mapping["id"],
        label=mapping["label"],
        site=mapping["site"],
        allowed_origins=tuple(origins) if isinstance(origins, list) else (),
        status=ProfileStatus(mapping["status"]),
        revision=mapping["revision"],
        chromium_build=mapping["chromium_build"],
        playwright_version=mapping["playwright_version"],
        app_version=mapping["app_version"],
        lease_runtime_generation=mapping["lease_runtime_generation"],
        lease_expires_at=mapping["lease_expires_at"],
        revoke_epoch=mapping["revoke_epoch"],
        account_fingerprint=mapping["account_fingerprint"],
        account_label_hash=mapping["account_label_hash"],
        last_login_completed_at=mapping["last_login_completed_at"],
        last_observed_at=mapping["last_observed_at"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
        deleted_at=mapping["deleted_at"],
    )


class BrowserProfileRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def create(
        self,
        *,
        profile_id: uuid.UUID,
        label: str,
        site: str,
        allowed_origins: tuple[str, ...],
    ) -> BrowserProfile:
        """Insert a `NEW` profile. The site binding is fixed from here on.

        The partial unique index on a live `site` is what refuses a second
        profile for a site that already has one; the caller turns the resulting
        integrity error into `profile_site_already_bound`.
        """
        result = await self._connection.execute(
            insert(browser_profiles)
            .values(
                id=profile_id,
                label=label,
                site=site,
                allowed_origins=list(allowed_origins),
                status=ProfileStatus.NEW.value,
                revision=1,
                revoke_epoch=0,
            )
            .returning(*browser_profiles.c)
        )
        return _profile(result.one())

    async def get(self, profile_id: uuid.UUID) -> BrowserProfile | None:
        result = await self._connection.execute(
            select(browser_profiles).where(browser_profiles.c.id == profile_id)
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def list_live(self) -> list[BrowserProfile]:
        """Every profile that has not been deleted, oldest first."""
        result = await self._connection.execute(
            select(browser_profiles)
            .where(browser_profiles.c.status.in_(_LIVE))
            .order_by(browser_profiles.c.created_at)
        )
        return [_profile(row) for row in result.all()]

    async def find_live_by_site(self, site: str) -> BrowserProfile | None:
        result = await self._connection.execute(
            select(browser_profiles).where(
                browser_profiles.c.site == site, browser_profiles.c.status.in_(_LIVE)
            )
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def acquire_lease(
        self,
        *,
        profile_id: uuid.UUID,
        runtime_generation: uuid.UUID,
        ttl: timedelta,
        expected_revision: int | None = None,
        reclaim: bool = False,
    ) -> BrowserProfile | None:
        """Take (or renew, or reclaim) the one-owner lease. `None` means refused.

        The lease may be taken when it is **free** (nobody holds it), **ours**
        (a renewal), or **expired**. A lease held by another generation and not
        yet expired is only reclaimable with `reclaim=True`, which the service
        passes solely after establishing that no process holds the OS-level
        handle on the profile directory. Both conditions, never one.
        """
        free = browser_profiles.c.lease_runtime_generation.is_(None)
        ours = browser_profiles.c.lease_runtime_generation == runtime_generation
        expired = browser_profiles.c.lease_expires_at <= func.now()
        claims = [free, ours, expired]
        if reclaim:
            claims.append(browser_profiles.c.lease_runtime_generation.isnot(None))
        conditions = [
            browser_profiles.c.id == profile_id,
            browser_profiles.c.status.in_(_LIVE),
            or_(*claims),
        ]
        if expected_revision is not None:
            conditions.append(browser_profiles.c.revision == expected_revision)
        result = await self._connection.execute(
            update(browser_profiles)
            .where(and_(*conditions))
            .values(
                lease_runtime_generation=runtime_generation,
                lease_expires_at=func.now() + ttl,
                revision=browser_profiles.c.revision + 1,
                updated_at=func.now(),
            )
            .returning(*browser_profiles.c)
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def release_lease(
        self, *, profile_id: uuid.UUID, runtime_generation: uuid.UUID
    ) -> BrowserProfile | None:
        """Drop our own lease. A lease we do not hold is not ours to drop."""
        result = await self._connection.execute(
            update(browser_profiles)
            .where(
                browser_profiles.c.id == profile_id,
                browser_profiles.c.lease_runtime_generation == runtime_generation,
            )
            .values(
                lease_runtime_generation=None,
                lease_expires_at=None,
                revision=browser_profiles.c.revision + 1,
                updated_at=func.now(),
            )
            .returning(*browser_profiles.c)
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def release_generation_leases(self, runtime_generation: uuid.UUID) -> int:
        """Clear every lease a generation holds. Startup reclamation.

        A runtime holds an exclusive advisory lock for its whole life, so a
        lease belonging to any *other* generation in this database describes a
        process that is gone. Clearing them at startup is what stops a crashed
        generation's lease outliving it for the length of its TTL.
        """
        result = await self._connection.execute(
            update(browser_profiles)
            .where(
                browser_profiles.c.lease_runtime_generation.isnot(None),
                browser_profiles.c.lease_runtime_generation != runtime_generation,
            )
            .values(
                lease_runtime_generation=None,
                lease_expires_at=None,
                revision=browser_profiles.c.revision + 1,
                updated_at=func.now(),
            )
        )
        return result.rowcount or 0

    async def record_open(
        self,
        *,
        profile_id: uuid.UUID,
        runtime_generation: uuid.UUID,
        versions: BrowserVersions,
    ) -> BrowserProfile | None:
        """Record the browser that just opened this profile, and its status.

        `NEW` becomes `NEEDS_LOGIN`: a directory now exists and nobody has
        signed in through it. **No other status transition happens here, and in
        particular nothing becomes `AUTHENTICATED`** -- a profile that opened is
        not a profile somebody is signed into, and S1 has no way to tell the
        difference because it never looks at a page.
        """
        status_now = func.coalesce(
            func.nullif(browser_profiles.c.status, ProfileStatus.NEW.value),
            ProfileStatus.NEEDS_LOGIN.value,
        )
        result = await self._connection.execute(
            update(browser_profiles)
            .where(
                browser_profiles.c.id == profile_id,
                browser_profiles.c.lease_runtime_generation == runtime_generation,
                browser_profiles.c.status.in_(_LIVE),
            )
            .values(
                status=status_now,
                chromium_build=versions.chromium_build,
                playwright_version=versions.playwright_version,
                app_version=versions.app_version,
                last_observed_at=func.now(),
                revision=browser_profiles.c.revision + 1,
                updated_at=func.now(),
            )
            .returning(*browser_profiles.c)
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def mark_deleted(
        self, *, profile_id: uuid.UUID, expected_revision: int | None = None
    ) -> BrowserProfile | None:
        """`DELETED`, terminal, and only while nobody holds a live lease.

        Refusing on a live lease is what stops a deletion racing an owner: the
        directory removal that follows cannot be undone, so the database says
        no rather than hoping the other process has finished.
        """
        conditions = [
            browser_profiles.c.id == profile_id,
            browser_profiles.c.status.in_(_LIVE),
            or_(
                browser_profiles.c.lease_runtime_generation.is_(None),
                browser_profiles.c.lease_expires_at <= func.now(),
            ),
        ]
        if expected_revision is not None:
            conditions.append(browser_profiles.c.revision == expected_revision)
        result = await self._connection.execute(
            update(browser_profiles)
            .where(and_(*conditions))
            .values(
                status=ProfileStatus.DELETED.value,
                lease_runtime_generation=None,
                lease_expires_at=None,
                deleted_at=func.now(),
                revision=browser_profiles.c.revision + 1,
                updated_at=func.now(),
            )
            .returning(*browser_profiles.c)
        )
        row = result.one_or_none()
        return _profile(row) if row is not None else None

    async def purge(self, profile_id: uuid.UUID) -> bool:
        """Remove a row outright. Tests and fixtures only; never a user path."""
        result = await self._connection.execute(
            delete(browser_profiles).where(browser_profiles.c.id == profile_id)
        )
        return bool(result.rowcount)


__all__ = ["BrowserProfileRepository"]
