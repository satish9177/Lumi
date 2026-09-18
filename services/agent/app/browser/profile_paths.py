"""Where a persistent Chromium profile lives on disk, and who may read it.

    %LOCALAPPDATA%\\Lumi\\browser-profiles\\<profile-uuid>\\

Every part of that is load-bearing:

* **`%LOCALAPPDATA%`, never `%APPDATA%`.** A roaming profile is synchronised to
  a domain share. A directory holding live session cookies must not travel.
* **Outside the install directory.** An all-users install under `Program Files`
  is not writable, and an upgrade or uninstall would capture or destroy the
  profile. Outside the repository and `dist/` too -- `scripts/build-agent-
  runtime.mjs` fails the build if a `browser-profiles` directory is ever found
  inside the bundle, and `tests/test_profile_paths.py` asserts the resolved
  base is under `%LOCALAPPDATA%` and under none of those trees.
* **An opaque UUID for the directory name.** Neither the site nor the label
  appears anywhere in the path, so a directory listing -- by a backup tool, a
  sync client, a screen share or another person at the machine -- does not
  disclose which accounts the user holds. The site association is a database
  row, and only Lumi's database.
* **Derived, never transported.** Both trusted processes that need the path --
  the runtime (to delete a profile) and the worker (to open one) -- derive it
  from the profile id with this module. The path is not a field in any API
  response, any worker request, any log record, any diagnostic, or any model
  prompt, because there is no code that puts it in one.

**Windows permissions, stated honestly.** `%LOCALAPPDATA%` is already
user-scoped. On creation this module additionally replaces the directory's
inherited ACL with a single entry for the current user, so an account that
would otherwise inherit access from a parent directory does not get it. That is
the end of what it claims. Chromium encrypts `Cookies` and `Login Data` with a
DPAPI key bound to *the same user*, so **any process running as that user can
decrypt the profile**. Same-user malware is outside Lumi's security boundary
and no ACL changes that. The profile directory is secret material; treat it the
way you would treat the browser's own profile, because that is what it is.
"""

import logging
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("lumi.browser.profiles")

#: The fixed directory names under `%LOCALAPPDATA%`. Not configurable: a path
#: a caller could choose is a path a caller could point at a network share.
APP_DIRECTORY = "Lumi"
PROFILES_DIRECTORY = "browser-profiles"
#: The one environment variable that may move the base, for tests and for a
#: trusted packaged configuration that has to place it elsewhere. It is read
#: from the process environment, never from a request, a model or a page.
PROFILE_ROOT_VARIABLE = "LUMI_BROWSER_PROFILE_ROOT"
#: The exclusive-ownership file, inside the profile directory but not part of
#: Chromium's own state. See `profile_lock.py`.
LOCK_FILE_NAME = ".lumi-profile-lock"


class ProfilePathError(Exception):
    """The profile base could not be resolved. `code` is stable and loggable."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The profile directory could not be resolved ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class ProfilePaths:
    """The resolved base directory, and the only way to get a profile's path."""

    base: Path

    def directory(self, profile_id: uuid.UUID) -> Path:
        """`<base>/<uuid>`. The id is a `uuid.UUID`, so there is nothing to
        escape: no caller-supplied string ever becomes a path component."""
        return self.base / str(profile_id)

    def lock_file(self, profile_id: uuid.UUID) -> Path:
        return self.directory(profile_id) / LOCK_FILE_NAME

    def create(self, profile_id: uuid.UUID) -> Path:
        """Create the profile directory if it is absent, and restrict its ACL."""
        directory = self.directory(profile_id)
        existed = directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if not existed:
            restrict_to_current_user(directory)
        return directory

    def exists(self, profile_id: uuid.UUID) -> bool:
        return self.directory(profile_id).is_dir()


def resolve_profile_paths(environment: dict[str, str] | None = None) -> ProfilePaths:
    """`%LOCALAPPDATA%\\Lumi\\browser-profiles`, or the trusted override.

    Refuses rather than inventing a fallback: a profile written somewhere
    unexpected -- the current working directory, a temporary directory that a
    cleaner empties, a roaming share -- is worse than no profile at all.
    """
    source = os.environ if environment is None else environment
    override = (source.get(PROFILE_ROOT_VARIABLE) or "").strip()
    if override:
        base = Path(override)
        if not base.is_absolute():
            raise ProfilePathError("profile_root_not_absolute")
        return ProfilePaths(base=base)
    local = (source.get("LOCALAPPDATA") or "").strip()
    if not local:
        raise ProfilePathError("localappdata_not_set")
    root = Path(local)
    if not root.is_absolute():
        raise ProfilePathError("localappdata_not_absolute")
    return ProfilePaths(base=root / APP_DIRECTORY / PROFILES_DIRECTORY)


def restrict_to_current_user(directory: Path) -> bool:
    """Replace inherited access with a single entry for the current user.

    Windows only, and best-effort by design: `icacls` is invoked with a fixed
    argument vector and no shell, and a failure is logged and tolerated rather
    than aborting profile creation, because `%LOCALAPPDATA%` is already
    user-scoped and refusing to create a profile over an ACL edit would be a
    worse trade. Returns whether the restriction was applied, so a test can
    assert it on Windows without asserting it everywhere.

    What this does **not** do: protect the profile from other processes running
    as the same user. See the module docstring.
    """
    if os.name != "nt":
        return False
    account = os.environ.get("USERNAME")
    if not account:
        logger.warning("could not restrict the profile directory: no USERNAME")
        return False
    try:
        result = subprocess.run(
            [
                "icacls",
                str(directory),
                # Drop inherited entries, then grant exactly this user.
                "/inheritance:r",
                "/grant:r",
                f"{account}:(OI)(CI)F",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not restrict the profile directory", exc_info=False)
        return False
    if result.returncode != 0:
        # Never log the command output: it contains the path.
        logger.warning("icacls declined to restrict the profile directory")
        return False
    return True


__all__ = [
    "APP_DIRECTORY",
    "LOCK_FILE_NAME",
    "PROFILES_DIRECTORY",
    "PROFILE_ROOT_VARIABLE",
    "ProfilePathError",
    "ProfilePaths",
    "resolve_profile_paths",
    "restrict_to_current_user",
]
