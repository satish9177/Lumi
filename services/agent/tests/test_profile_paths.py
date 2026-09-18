"""Where a persistent profile is allowed to live, and what its path may reveal.

Three separate claims are tested here, and they fail in different ways:

* **Location.** The base is under `%LOCALAPPDATA%\\Lumi\\browser-profiles`, and
  under none of the repository, the install directory, `dist/` or the packaged
  runtime resources. A profile written into any of those is captured by an
  upgrade, destroyed by an uninstall, or committed.
* **Opacity.** The directory name is the profile UUID and nothing else. Neither
  the site nor the label appears anywhere in the path, so a directory listing
  does not disclose which accounts the user holds.
* **Derivation.** The path is computed from the id inside a trusted process. No
  caller supplies it, and no request or response carries it.

The tests deliberately do not depend on the developer's username: they assert
the *relationship* between the resolved base and `%LOCALAPPDATA%`, not a
literal string.
"""

import os
import uuid
from pathlib import Path

import pytest

from app.browser.profile_paths import (
    APP_DIRECTORY,
    LOCK_FILE_NAME,
    PROFILES_DIRECTORY,
    PROFILE_ROOT_VARIABLE,
    ProfilePathError,
    resolve_profile_paths,
)
from app.config import AGENT_ROOT

REPOSITORY_ROOT = AGENT_ROOT.parents[1]


def test_the_base_is_under_localappdata_and_named_by_lumi() -> None:
    """`%LOCALAPPDATA%\\Lumi\\browser-profiles`, exactly, with no username in
    the assertion -- the test states the structure, not one machine's paths."""
    local = Path("C:/Users/someone-else/AppData/Local")
    paths = resolve_profile_paths({"LOCALAPPDATA": str(local)})
    assert paths.base == local / APP_DIRECTORY / PROFILES_DIRECTORY
    assert paths.base.parts[-2:] == (APP_DIRECTORY, PROFILES_DIRECTORY)


def test_the_base_is_local_and_never_roaming() -> None:
    """A roaming profile is synchronised to a domain share. Session cookies
    must not travel, so `%APPDATA%` is never consulted."""
    environment = {
        "LOCALAPPDATA": "C:/Users/someone-else/AppData/Local",
        "APPDATA": "C:/Users/someone-else/AppData/Roaming",
    }
    base = str(resolve_profile_paths(environment).base).replace("\\", "/")
    assert "AppData/Local" in base
    assert "Roaming" not in base


@pytest.mark.skipif(os.name != "nt", reason="the resolved base is a Windows path")
def test_the_real_resolved_base_is_outside_every_build_tree() -> None:
    """The base this machine actually resolves is not inside anything the build
    touches: not the checkout, not `dist/`, not `release/`, not `out/`."""
    base = resolve_profile_paths().base.resolve()
    assert base.is_absolute()
    forbidden = [
        REPOSITORY_ROOT,
        AGENT_ROOT,
        REPOSITORY_ROOT / "dist",
        REPOSITORY_ROOT / "dist" / "agent-runtime",
        REPOSITORY_ROOT / "release",
        REPOSITORY_ROOT / "out",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")),
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
    ]
    for tree in forbidden:
        assert not base.is_relative_to(tree.resolve()), f"the profile base is inside {tree}"
    # And it really is under this user's LOCALAPPDATA.
    local = Path(os.environ["LOCALAPPDATA"]).resolve()
    assert base.is_relative_to(local)


def test_a_profile_directory_is_an_opaque_uuid(tmp_path: Path) -> None:
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    profile_id = uuid.uuid4()
    directory = paths.directory(profile_id)
    assert directory.parent == tmp_path
    assert directory.name == str(profile_id)
    # A UUID and nothing else: parsing it back is the strongest statement that
    # nothing was appended, prefixed or encoded into the name.
    assert uuid.UUID(directory.name) == profile_id


def test_no_site_or_label_reaches_the_path(tmp_path: Path) -> None:
    """The site association is a database row, never a directory name."""
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    profile_id = uuid.uuid4()
    path = str(paths.directory(profile_id)).lower()
    for leak in ("github", "example", "personal", "work", "account", "login", ".com", ".co.uk"):
        assert leak not in path


def test_the_lock_file_lives_inside_the_profile_and_is_not_chromiums(tmp_path: Path) -> None:
    """Lumi holds its own handle. Chromium's `SingletonLock` is never touched."""
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    profile_id = uuid.uuid4()
    lock = paths.lock_file(profile_id)
    assert lock.parent == paths.directory(profile_id)
    assert lock.name == LOCK_FILE_NAME
    assert "singleton" not in lock.name.lower()


def test_an_absent_or_relative_base_is_refused_rather_than_guessed() -> None:
    """A profile written somewhere unexpected is worse than no profile."""
    with pytest.raises(ProfilePathError) as missing:
        resolve_profile_paths({})
    assert missing.value.code == "localappdata_not_set"

    with pytest.raises(ProfilePathError) as relative:
        resolve_profile_paths({PROFILE_ROOT_VARIABLE: "browser-profiles"})
    assert relative.value.code == "profile_root_not_absolute"

    with pytest.raises(ProfilePathError) as relative_local:
        resolve_profile_paths({"LOCALAPPDATA": "AppData/Local"})
    assert relative_local.value.code == "localappdata_not_absolute"


def test_creating_a_profile_directory_is_idempotent(tmp_path: Path) -> None:
    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    profile_id = uuid.uuid4()
    assert not paths.exists(profile_id)
    first = paths.create(profile_id)
    second = paths.create(profile_id)
    assert first == second
    assert paths.exists(profile_id)


@pytest.mark.skipif(os.name != "nt", reason="ACL restriction is a Windows behaviour")
def test_a_new_profile_directory_stops_inheriting_access(tmp_path: Path) -> None:
    """Inherited entries are dropped and the current user is granted explicitly.

    This is *not* a claim of protection against other processes running as the
    same user: Chromium's credential encryption is DPAPI-bound to that user, so
    any such process can decrypt the profile. See `profile_paths.py`.
    """
    import subprocess

    paths = resolve_profile_paths({PROFILE_ROOT_VARIABLE: str(tmp_path)})
    directory = paths.create(uuid.uuid4())
    listing = subprocess.run(
        ["icacls", str(directory)],
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    assert listing.returncode == 0
    assert "(I)" not in listing.stdout, "the directory still inherits access"
    assert os.environ["USERNAME"].lower() in listing.stdout.lower()
