"""Milestone 10 S1: the file broker against a real NTFS directory tree.

Junctions are created with `_winapi.CreateJunction`, which needs no elevation, so the reparse-point
refusals are exercised for real rather than mocked. Symbolic links need a privilege this host may not
have; their rule is the same `FILE_ATTRIBUTE_REPARSE_POINT` check and is covered through junctions.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

from app.files.broker import (
    FileBrokerRefusal,
    inspect_dropped,
    inspect_root,
    list_documents,
    read_verified,
    resolve,
    verify_root,
)
from app.files.handles import FileIdentity

#: Tests place roots under the system temp folder (inside LocalAppData); the real set is tested below.
NONE: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the M10 file broker ships on Windows")


def _junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def _code(error: pytest.ExceptionInfo[FileBrokerRefusal]) -> str:
    return error.value.code


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    (root / "Jobs").mkdir(parents=True)
    outside.mkdir()
    (root / "resume.txt").write_text("synthetic resume", encoding="utf-8")
    (root / "Jobs" / "offer.txt").write_text("synthetic offer", encoding="utf-8")
    (outside / "secret.txt").write_text("outside the root", encoding="utf-8")
    return {"root": root, "outside": outside}


def test_a_root_is_bound_to_its_directory_identity(tree: dict[str, Path]) -> None:
    facts = inspect_root(str(tree["root"]), protected=NONE)
    assert Path(facts.canonical_path) == tree["root"].resolve()
    assert verify_root(facts.canonical_path, volume=facts.volume, index=facts.index) == facts.canonical_path


def test_a_replaced_root_directory_is_not_the_approved_root(tree: dict[str, Path]) -> None:
    facts = inspect_root(str(tree["root"]), protected=NONE)
    shutil.rmtree(tree["root"])
    tree["root"].mkdir()
    with pytest.raises(FileBrokerRefusal) as refused:
        verify_root(facts.canonical_path, volume=facts.volume, index=facts.index)
    assert _code(refused) == "root_changed"


def test_a_root_that_became_a_junction_is_refused(tree: dict[str, Path]) -> None:
    facts = inspect_root(str(tree["root"]), protected=NONE)
    shutil.rmtree(tree["root"])
    _junction(tree["root"], tree["outside"])
    with pytest.raises(FileBrokerRefusal) as refused:
        verify_root(facts.canonical_path, volume=facts.volume, index=facts.index)
    assert _code(refused) == "root_changed"


def test_a_junction_cannot_be_approved_as_a_root(tree: dict[str, Path]) -> None:
    link = tree["root"].parent / "link"
    _junction(link, tree["outside"])
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_root(str(link), protected=NONE)
    assert _code(refused) == "root_is_reparse_point"


@pytest.mark.parametrize("path", ["relative\\dir", "\\\\server\\share\\dir", "\\\\?\\C:\\Users"])
def test_a_root_must_be_a_local_absolute_path(path: str) -> None:
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_root(path)
    assert _code(refused) == "root_not_local"


def test_a_drive_root_is_too_broad(tree: dict[str, Path]) -> None:
    drive = os.path.splitdrive(str(tree["root"]))[0] + "\\"
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_root(drive, protected=NONE)
    assert _code(refused) in ("root_too_broad", "root_protected")


def test_a_lumi_owned_tree_can_never_be_a_root(tree: dict[str, Path]) -> None:
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_root(str(tree["root"]), forbidden=(str(tree["root"] / "Jobs"),), protected=NONE)
    assert _code(refused) == "root_protected"


def test_a_junction_anywhere_below_the_root_is_refused_on_resolve(tree: dict[str, Path]) -> None:
    _junction(tree["root"] / "escape", tree["outside"])
    with pytest.raises(FileBrokerRefusal) as refused:
        resolve(str(tree["root"].resolve()), ("escape", "secret.txt"))
    assert _code(refused) == "reparse_point"


def test_a_verified_read_returns_the_exact_bytes_and_identity(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    path = resolve(canonical, ("Jobs", "offer.txt"))
    read = read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical)
    assert read.data == b"synthetic offer"
    assert read.identity.size == len(read.data)


def test_a_junction_swapped_in_after_resolve_cannot_move_the_read_outside_the_root(tree: dict[str, Path]) -> None:
    """The TOCTOU the handle check exists for: resolve saw a directory, the read goes through a junction."""
    canonical = str(tree["root"].resolve())
    (tree["outside"] / "offer.txt").write_text("outside offer", encoding="utf-8")
    path = resolve(canonical, ("Jobs", "offer.txt"))
    shutil.rmtree(tree["root"] / "Jobs")
    _junction(tree["root"] / "Jobs", tree["outside"])
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical)
    assert _code(refused) in ("path_mismatch", "outside_root")


def test_the_same_name_holding_a_different_file_is_file_changed(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    path = resolve(canonical, ("resume.txt",))
    first = read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical)
    os.remove(path)
    Path(path).write_text("synthetic resume", encoding="utf-8")  # same name, same bytes, new file
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(
            path, expected_final=path, max_bytes=1024, root_canonical=canonical, expected=first.identity, expected_sha256=first.sha256
        )
    assert _code(refused) == "file_changed"


def test_the_same_file_rewritten_in_place_is_file_changed(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    path = resolve(canonical, ("resume.txt",))
    first = read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical)
    with open(path, "r+b") as handle:
        handle.write(b"S")
    stale = FileIdentity(volume=first.identity.volume, index=first.identity.index, size=first.identity.size, mtime_ns=first.identity.mtime_ns)
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical, expected=stale, expected_sha256=first.sha256)
    assert _code(refused) == "file_changed"


def test_a_hard_linked_file_is_refused(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    os.link(tree["outside"] / "secret.txt", tree["root"] / "linked.txt")
    path = resolve(canonical, ("linked.txt",))
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(path, expected_final=path, max_bytes=1024, root_canonical=canonical)
    assert _code(refused) == "hardlinked_file"


def test_a_file_over_the_limit_is_refused(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    path = resolve(canonical, ("resume.txt",))
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(path, expected_final=path, max_bytes=4, root_canonical=canonical)
    assert _code(refused) == "file_too_large"


def test_a_short_name_alias_does_not_match_the_resolved_path(tree: dict[str, Path]) -> None:
    canonical = str(tree["root"].resolve())
    path = resolve(canonical, ("resume.txt",))
    with pytest.raises(FileBrokerRefusal) as refused:
        read_verified(path, expected_final=os.path.join(canonical, "other.txt"), max_bytes=1024, root_canonical=canonical)
    assert _code(refused) == "path_mismatch"


def test_the_listing_never_follows_a_junction_and_lists_only_documents(tree: dict[str, Path]) -> None:
    _junction(tree["root"] / "escape", tree["outside"])
    (tree["root"] / "tool.exe").write_bytes(b"MZ")
    (tree["root"] / "cv.pdf").write_bytes(b"%PDF-1.4")
    files, truncated = list_documents(str(tree["root"].resolve()))
    names = sorted(item.relative_path for item in files)
    assert names == ["Jobs/offer.txt", "cv.pdf", "resume.txt"]
    assert not truncated


def test_a_dropped_file_is_exactly_that_file(tree: dict[str, Path]) -> None:
    canonical = inspect_dropped(str((tree["root"] / "resume.txt").resolve()))
    assert Path(canonical).name == "resume.txt"
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_dropped(str(tree["root"].resolve()))
    assert _code(refused) == "not_a_regular_file"


# ---- S1 review finding 1: the real protected folders, resolved from the shell, not the environment ------


@pytest.mark.parametrize("variable_free", [True, False])
def test_system_and_application_folders_can_never_be_roots(monkeypatch: pytest.MonkeyPatch, variable_free: bool) -> None:
    if variable_free:
        # Exactly the runtime's own environment: no ProgramFiles* variables at all.
        for name in list(os.environ):
            if name.upper() not in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA"):
                monkeypatch.delenv(name, raising=False)
    home = Path.home()
    for candidate in (r"C:\Program Files", r"C:\Program Files (x86)", r"C:\PROGRA~1", r"C:\Windows", r"C:\ProgramData",
                      r"C:\Users", str(home), str(home / "AppData"), str(home / "AppData" / "Roaming"),
                      str(home / "AppData" / "Local" / "Programs")):
        if not os.path.isdir(candidate):
            continue
        with pytest.raises(FileBrokerRefusal) as refused:
            inspect_root(candidate)
        assert refused.value.code in ("root_protected", "root_too_broad"), candidate


def test_a_documents_folder_inside_the_profile_is_still_allowed() -> None:
    documents = Path.home() / "Documents"
    if documents.is_dir() and not os.path.islink(documents):
        assert inspect_root(str(documents)).canonical_path


def test_a_temp_folder_inside_local_app_data_is_refused_by_default(tmp_path: Path) -> None:
    with pytest.raises(FileBrokerRefusal) as refused:
        inspect_root(str(tmp_path))
    assert refused.value.code == "root_protected"


def test_listable_mirrors_the_listing_exclusions() -> None:
    from app.files.broker import listable

    assert listable(("Jobs", "resume.pdf"))
    for refused in (("AppData", "x.txt"), (".git", "x.md"), ("node_modules", "a.txt"), ("~$cv.docx",), (".secret.txt",),
                    ("a", "b", "c", "d", "e.pdf"), ("notes.json",)):
        assert not listable(refused), refused
