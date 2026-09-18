"""The OS-level backstop for "one owner at a time" on a profile directory.

The authoritative lease is a `browser_profiles` row taken by a conditional
`UPDATE` (see `app/repositories/profiles.py`). This file holds the second,
independent boundary, and the two exist because they fail differently:

* The **database lease** is authoritative and survives a crash, but it is only
  as good as the database. Two Lumi installations pointed at *different*
  databases would both believe they held it.
* The **OS file handle** knows nothing about generations or expiry, but it is
  held by a live process on the machine. It is what makes "is that stale lease
  actually stale, or is somebody still using it?" answerable rather than
  guessable.

Together they give the recovery rule the architecture asks for:

    dead generation on the lease + nobody holds the handle  -> reclaim
    dead generation on the lease + somebody holds the handle -> refuse

The second line is the important one. A stale-looking row is **not** permission
to open a profile another process has open; opening one Chromium profile
directory twice corrupts it. So the answer when the handle is held is "no",
not "probably fine".

**Chromium's own `SingletonLock` is not used and is never deleted.** Its
failure modes are confusing, it may do something other than refuse, and
deleting it behind Chromium's back is exactly the kind of hand-editing of
browser internals this design avoids. Lumi holds its own file, with its own
name, that Chromium neither reads nor writes.

Implementation: a file opened for writing and locked with `msvcrt.locking` on
Windows (`LK_NBLCK`, one byte, non-blocking) or `fcntl.flock(LOCK_EX |
LOCK_NB)` elsewhere. Both are advisory in the sense that they only bind
processes that ask -- but the only processes that open this file are Lumi's,
which is the point. Windows releases the lock when the handle closes, including
when the process is killed, so a hard-killed worker does not leave the profile
permanently locked.
"""

import logging
import os
import uuid
from pathlib import Path
from types import TracebackType

from app.browser.profile_paths import ProfilePaths

logger = logging.getLogger("lumi.browser.profiles")

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl


class ProfileLockUnavailableError(Exception):
    """Another live process holds this profile. Never a reason to open anyway."""

    def __init__(self, profile_id: uuid.UUID) -> None:
        super().__init__("Another process holds this browser profile.")
        self.profile_id = profile_id
        self.code = "profile_locked_by_another_process"


def _try_lock(handle: int) -> bool:
    if os.name == "nt":  # pragma: no cover - platform branch
        try:
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:  # pragma: no cover - platform branch
        # Lumi ships on Windows, so the type stubs here are the Windows ones
        # and do not know `fcntl`. The branch is kept working rather than
        # deleted so the suite can run on a POSIX machine.
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
    except OSError:
        return False
    return True


def _unlock(handle: int) -> None:
    if os.name == "nt":  # pragma: no cover - platform branch
        try:
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    try:  # pragma: no cover - platform branch
        fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass


class ProfileLock:
    """An exclusive handle on one profile directory, held for as long as it is open.

    Use it as a context manager, or call `acquire()` / `release()`. Acquiring a
    lock that another process holds raises `ProfileLockUnavailableError`; it
    never blocks, never retries and never breaks the other holder's lock.
    """

    def __init__(self, path: Path, profile_id: uuid.UUID) -> None:
        self._path = path
        self._profile_id = profile_id
        self._handle: int | None = None

    @classmethod
    def for_profile(cls, paths: ProfilePaths, profile_id: uuid.UUID) -> "ProfileLock":
        return cls(paths.lock_file(profile_id), profile_id)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "ProfileLock":
        if self._handle is not None:
            return self
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT, never O_TRUNC: the file's *contents* mean nothing, and
        # truncating it would be a write another holder could observe.
        handle = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(handle):
            os.close(handle)
            raise ProfileLockUnavailableError(self._profile_id)
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        _unlock(handle)
        try:
            os.close(handle)
        except OSError:  # pragma: no cover - the handle is going away anyway.
            pass

    def __enter__(self) -> "ProfileLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def profile_lock_is_free(paths: ProfilePaths, profile_id: uuid.UUID) -> bool:
    """Is anybody holding this profile right now?

    Answered by taking the lock and immediately dropping it, which is the only
    honest way to ask: a file's existence says nothing, and a stale lease says
    less. The answer is a snapshot -- another process may take the lock the
    instant this returns -- so it is used to decide whether a *reclaim* may be
    attempted, never as the sole gate on opening a profile. The open path takes
    the lock and holds it.
    """
    lock = ProfileLock.for_profile(paths, profile_id)
    try:
        lock.acquire()
    except ProfileLockUnavailableError:
        return False
    except OSError:
        # The directory may not exist yet, which is not "somebody holds it".
        return True
    lock.release()
    return True


__all__ = [
    "ProfileLock",
    "ProfileLockUnavailableError",
    "profile_lock_is_free",
]
