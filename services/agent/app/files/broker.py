"""The synchronous half of the Milestone 10 file broker: roots, resolution and handle-verified reads.

Callers run these in a worker thread (`asyncio.to_thread`); nothing here touches the database. The
service layer owns authority (which root, which task, which permission); this module owns only the
question "is this really the file, under that root, that Lumi means?".

```text
inspect_root(path)                 a folder chosen in a native dialog -> canonical path + (volume, index)
verify_root(root)                  still that directory? (replaced / now a link / gone -> refused)
resolve(root, components)          lstat-walk from the root: every component exists, none is a reparse point
read_verified(...)                 open -> prove by HANDLE (final path, identity, links) -> bounded read -> re-prove
list_documents(root)               a bounded listing of supported documents, never following links
inspect_dropped(path)              one file main already holds as dropped: exactly that file, nothing around it
```
"""

import hashlib
import os
import stat
from collections import deque
from dataclasses import dataclass
from typing import Final

from app.files.handles import (
    FileIdentity,
    final_path_of_fd,
    identity_of,
    is_local_absolute,
    is_reparse_point,
    is_within,
    normcase,
    open_read,
)
from app.files.names import FileNameRefusal, display_relative, validate_component

#: What an M10 listing offers, by extension. The helper still sniffs the bytes: a name is not a type.
DOCUMENT_EXTENSIONS: Final = {".pdf": "pdf", ".docx": "docx", ".txt": "txt", ".md": "txt"}
_SKIPPED_DIRECTORIES: Final = frozenset(
    {"node_modules", ".git", ".svn", "$recycle.bin", "system volume information", "appdata", ".venv", "__pycache__"}
)
MAX_LISTED: Final = 200
MAX_VISITED: Final = 5_000
MAX_LIST_DEPTH: Final = 4
_READ_CHUNK: Final = 64 * 1024


class FileBrokerRefusal(ValueError):
    """A refused file operation. `code` is stable and never carries a path or a name."""

    def __init__(self, code: str) -> None:
        super().__init__(f"The file operation was refused ({code}).")
        self.code = code


@dataclass(frozen=True, slots=True)
class RootFacts:
    canonical_path: str
    volume: int
    index: int


@dataclass(frozen=True, slots=True)
class ListedFile:
    relative_path: str
    name: str
    size: int
    mtime_ns: int
    format: str


@dataclass(frozen=True, slots=True)
class VerifiedRead:
    data: bytes
    identity: FileIdentity
    sha256: str


def _refuse(code: str) -> FileBrokerRefusal:
    return FileBrokerRefusal(code)


def _is_drive_root(path: str) -> bool:
    stripped = normcase(path).rstrip("\\/")
    return len(stripped) <= 2


#: Known folders a root may never be, contain or sit inside (S1 review finding 1: resolved from the shell,
#: never from environment variables the runtime may not even have).
_INSIDE_FORBIDDEN: Final = {
    "Windows": "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",
    "ProgramFiles": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "ProgramFilesX86": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    "ProgramData": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    "RoamingAppData": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
}
#: Known folders a root may sit inside, but never be or contain (a home folder is too broad).
_ANCESTOR_FORBIDDEN: Final = {
    "Profile": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
    "UserProfiles": "{0762D272-C50A-4BB0-A382-697DCD729B80}",
}


def _known_folder(guid: str) -> str | None:
    if os.name != "nt":  # pragma: no cover - the broker ships on Windows.
        return None
    import ctypes
    import uuid as _uuid
    from ctypes import wintypes

    class _Guid(ctypes.Structure):
        _fields_ = [("data", ctypes.c_byte * 16)]

    shell32 = ctypes.WinDLL("shell32")
    ole32 = ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_Guid), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    value = _Guid()
    ctypes.memmove(value.data, _uuid.UUID(guid).bytes_le, 16)
    out = ctypes.c_wchar_p()
    if shell32.SHGetKnownFolderPath(ctypes.byref(value), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return out.value
    finally:
        ole32.CoTaskMemFree(out)


_PROTECTED_CACHE: list[tuple[tuple[str, ...], tuple[str, ...]]] = []


def _protected() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(never inside, never at-or-above). Fails closed unless Windows names EVERY one of its folders: a
    folder that could not be resolved must not quietly stop being protected."""
    if os.name != "nt":  # pragma: no cover
        return (), ()
    if _PROTECTED_CACHE:
        return _PROTECTED_CACHE[0]
    inside: list[str] = []
    above: list[str] = []
    for guid in _INSIDE_FORBIDDEN.values():
        path = _known_folder(guid)
        if not path:
            raise _refuse("protected_folders_unknown")
        inside.append(normcase(path))
    for guid in _ANCESTOR_FORBIDDEN.values():
        path = _known_folder(guid)
        if not path:
            raise _refuse("protected_folders_unknown")
        above.append(normcase(path))
    result = (tuple(inside), tuple(above))
    _PROTECTED_CACHE.append(result)
    return result


ProtectedFolders = tuple[tuple[str, ...], tuple[str, ...]]


def inspect_root(path: str, *, forbidden: tuple[str, ...] = (), protected: ProtectedFolders | None = None) -> RootFacts:
    """A folder the person chose in a native dialog. Refuses what would be too broad or not a folder.

    `forbidden` names Lumi-owned trees (for example the download quarantine) that can never become an
    approved root, directly or as an ancestor/descendant.
    """
    if not is_local_absolute(path):
        raise _refuse("root_not_local")
    try:
        link_facts = os.lstat(path)
    except OSError:
        raise _refuse("root_unavailable") from None
    if is_reparse_point(link_facts):
        raise _refuse("root_is_reparse_point")
    if not stat.S_ISDIR(link_facts.st_mode):
        raise _refuse("root_not_directory")
    try:
        canonical = os.path.realpath(path, strict=True)
        facts = os.stat(canonical)
        canonical_link = os.lstat(canonical)
    except OSError:
        raise _refuse("root_unavailable") from None
    if not is_local_absolute(canonical) or is_reparse_point(canonical_link) or not stat.S_ISDIR(facts.st_mode):
        raise _refuse("root_is_reparse_point")
    if _is_drive_root(canonical):
        raise _refuse("root_too_broad")
    # `protected` exists for tests that must place roots under the system temp folder (itself inside
    # LocalAppData); production never passes it, so the shell's own known folders always apply.
    inside, above = protected if protected is not None else _protected()
    key = normcase(canonical)
    for tree in (*inside, *(normcase(item) for item in forbidden)):
        if key == tree or is_within(canonical, tree) or is_within(tree, canonical):
            raise _refuse("root_protected")
    for tree in above:
        if key == tree or is_within(tree, canonical):
            raise _refuse("root_protected")
    return RootFacts(canonical_path=canonical, volume=int(facts.st_dev), index=int(facts.st_ino))


def verify_root(canonical_path: str, *, volume: int, index: int) -> str:
    """The approved directory, still itself. A replaced or re-pointed root is not the approved root."""
    try:
        link_facts = os.lstat(canonical_path)
    except OSError:
        raise _refuse("root_unavailable") from None
    if is_reparse_point(link_facts) or not stat.S_ISDIR(link_facts.st_mode):
        raise _refuse("root_changed")
    if int(link_facts.st_dev) != volume or int(link_facts.st_ino) != index:
        raise _refuse("root_changed")
    return canonical_path


def resolve(root_canonical: str, components: tuple[str, ...]) -> str:
    """Walk down from the root with `lstat` (links are never followed), refusing any reparse point."""
    if not components:
        raise _refuse("file_missing")
    current = root_canonical
    for position, component in enumerate(components):
        current = os.path.join(current, component)
        try:
            facts = os.lstat(current)
        except FileNotFoundError:
            raise _refuse("file_missing") from None
        except OSError:
            raise _refuse("file_unavailable") from None
        if is_reparse_point(facts):
            raise _refuse("reparse_point")
        last = position == len(components) - 1
        if not last and not stat.S_ISDIR(facts.st_mode):
            raise _refuse("file_missing")
        if last and not stat.S_ISREG(facts.st_mode):
            raise _refuse("not_a_regular_file")
    return current


def read_verified(
    path: str,
    *,
    expected_final: str,
    max_bytes: int,
    root_canonical: str | None,
    expected: FileIdentity | None = None,
    expected_sha256: str | None = None,
) -> VerifiedRead:
    """Open, prove by handle, read at most `max_bytes`, prove again. Any doubt refuses the read.

    * The final path of the handle must be exactly the path Lumi resolved (normalised case). An 8.3
      short-name alias, a junction swapped in after `resolve`, or a link to elsewhere all differ.
    * For a root file it must also lie strictly inside the root's canonical path.
    * A file with more than one hard link is refused: its other names may be outside the root.
    * `expected` / `expected_sha256` bind a *version*: a different file, or the same file rewritten,
      under the same name is `file_changed`, never the approved document.
    """
    try:
        fd = open_read(path)
    except FileNotFoundError:
        raise _refuse("file_missing") from None
    except OSError:
        raise _refuse("file_unavailable") from None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _refuse("not_a_regular_file")
        identity = identity_of(before)
        final = final_path_of_fd(fd)
        if final is None:
            raise _refuse("final_path_unknown")
        if normcase(final) != normcase(expected_final):
            raise _refuse("path_mismatch")
        if root_canonical is not None and not is_within(final, root_canonical):
            raise _refuse("outside_root")
        if before.st_nlink > 1:
            raise _refuse("hardlinked_file")
        if expected is not None and not identity.same_version(expected):
            raise _refuse("file_changed")
        if identity.size > max_bytes:
            raise _refuse("file_too_large")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(_READ_CHUNK, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > max_bytes:
            raise _refuse("file_too_large")
        after = os.fstat(fd)
        if identity_of(after) != identity or total != identity.size:
            raise _refuse("file_changed")
        data = b"".join(chunks)
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise _refuse("file_changed")
        return VerifiedRead(data=data, identity=identity, sha256=digest)
    finally:
        os.close(fd)


def listable(components: tuple[str, ...]) -> bool:
    """The listing's own exclusions, applied to a name the renderer sends back (S1 review finding 2): a
    file the listing would never have shown cannot be added either."""
    if not components or len(components) > MAX_LIST_DEPTH:
        return False
    for directory in components[:-1]:
        if directory.startswith(".") or directory.casefold() in _SKIPPED_DIRECTORIES:
            return False
    name = components[-1]
    return not name.startswith(("~$", ".")) and document_format_for(name) is not None


def document_format_for(name: str) -> str | None:
    return DOCUMENT_EXTENSIONS.get(os.path.splitext(name)[1].casefold())


def list_documents(root_canonical: str) -> tuple[list[ListedFile], bool]:
    """Supported documents under the root, breadth first, bounded. Links and odd names are skipped.

    Returns the listing and whether a bound cut it short.
    """
    found: list[ListedFile] = []
    visited = 0
    truncated = False
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(root_canonical, ())])
    while queue:
        directory, prefix = queue.popleft()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError:
            continue
        for entry in entries:
            visited += 1
            if visited > MAX_VISITED or len(found) >= MAX_LISTED:
                truncated = True
                return found, truncated
            try:
                facts = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if is_reparse_point(facts):
                continue
            try:
                name = validate_component(entry.name)
            except FileNameRefusal:
                continue
            components = (*prefix, name)
            if stat.S_ISDIR(facts.st_mode):
                if len(components) < MAX_LIST_DEPTH and not name.startswith(".") and name.casefold() not in _SKIPPED_DIRECTORIES:
                    queue.append((entry.path, components))
                continue
            if not stat.S_ISREG(facts.st_mode):
                continue
            kind = document_format_for(name)
            if kind is None or name.startswith(("~$", ".")):
                continue
            found.append(
                ListedFile(
                    relative_path=display_relative(components),
                    name=name,
                    size=int(facts.st_size),
                    mtime_ns=int(facts.st_mtime_ns),
                    format=kind,
                )
            )
    return found, truncated


def inspect_dropped(path: str) -> str:
    """Exactly the one file main holds as dropped. It never becomes a root or authority for its folder."""
    if not is_local_absolute(path):
        raise _refuse("file_not_local")
    try:
        facts = os.lstat(path)
    except OSError:
        raise _refuse("file_missing") from None
    if is_reparse_point(facts):
        raise _refuse("reparse_point")
    if not stat.S_ISREG(facts.st_mode):
        raise _refuse("not_a_regular_file")
    try:
        canonical = os.path.realpath(path, strict=True)
    except OSError:
        raise _refuse("file_missing") from None
    if normcase(canonical) != normcase(path):
        raise _refuse("path_mismatch")
    return canonical
