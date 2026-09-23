"""Handle-level file facts (Milestone 10 S1).

Everything the broker concludes about a file it concludes from an **open handle**, not from a path
string: the path is only how the handle was obtained. After `open`, the handle tells us

* which file it really is (`volume`, `index`: Windows' volume serial number and 64-bit file index,
  surfaced by `os.fstat` as `st_dev` / `st_ino`), its size, modification time and link count;
* where it really is (`GetFinalPathNameByHandleW`, normalised, DOS volume name), so a junction,
  symbolic link or mount point swapped in anywhere on the path after our checks cannot move the
  read outside the approved root without the final-path check noticing.

`reparse_point` is read with `lstat` (it does not follow links) from `st_file_attributes`.

The residual is the classic one and is documented, not hidden: Lumi runs as the user, so this is
handle-verified access, not an operating-system sandbox, and a same-user process can still change a
file *after* the verified read. The read itself is either of the verified file or refused.
"""

import ctypes
import os
import stat
from dataclasses import dataclass
from typing import Any, Final

_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
_O_BINARY: Final = getattr(os, "O_BINARY", 0)
_O_NOINHERIT: Final = getattr(os, "O_NOINHERIT", 0)


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """What makes a file *that* file. A different file under the same name differs in `index`."""

    volume: int
    index: int
    size: int
    mtime_ns: int

    def same_object(self, other: "FileIdentity") -> bool:
        return self.volume == other.volume and self.index == other.index

    def same_version(self, other: "FileIdentity") -> bool:
        return self.same_object(other) and self.size == other.size and self.mtime_ns == other.mtime_ns


def identity_of(result: os.stat_result) -> FileIdentity:
    return FileIdentity(
        volume=int(result.st_dev), index=int(result.st_ino), size=int(result.st_size), mtime_ns=int(result.st_mtime_ns)
    )


def is_reparse_point(result: os.stat_result) -> bool:
    """A junction, symbolic link, mount point or other reparse point (never followed by `lstat`)."""
    attributes = getattr(result, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(result.st_mode)


def open_read(path: str) -> int:
    """A read-only, non-inheritable descriptor. The caller proves what it opened before trusting it."""
    return os.open(path, os.O_RDONLY | _O_BINARY | _O_NOINHERIT)


if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _FILE_NAME_NORMALIZED: Final = 0x0
    _VOLUME_NAME_DOS: Final = 0x0

    def final_path_of_fd(fd: int) -> str | None:
        """The normalised DOS path of what `fd` really is, or None when it cannot be told."""
        handle = msvcrt.get_osfhandle(fd)
        size = 1024
        for _ in range(3):
            buffer = ctypes.create_unicode_buffer(size)
            length = _kernel32.GetFinalPathNameByHandleW(
                wintypes.HANDLE(handle), buffer, size, _FILE_NAME_NORMALIZED | _VOLUME_NAME_DOS
            )
            if length == 0:
                return None
            if length < size:
                return _strip_extended_prefix(buffer.value)
            size = length + 1
        return None

else:  # pragma: no cover - Lumi's file broker ships on Windows; this keeps pure tests portable.

    def final_path_of_fd(fd: int) -> str | None:
        link = f"/proc/self/fd/{fd}"
        try:
            return os.path.realpath(link)
        except OSError:
            return None


def _strip_extended_prefix(value: str) -> str | None:
    """`\\\\?\\C:\\x` -> `C:\\x`. A UNC or volume-GUID final path is not something M10 supports."""
    if value.startswith("\\\\?\\UNC\\"):
        return None
    if value.startswith("\\\\?\\"):
        remainder = value[4:]
        if len(remainder) >= 2 and remainder[1] == ":":
            return remainder
        return None
    return value


def normcase(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_within(candidate: str, root: str) -> bool:
    """Segment-aware containment on already-final paths (never a raw string-prefix test)."""
    child = normcase(candidate)
    parent = normcase(root).rstrip("\\/")
    if child == parent:
        return False
    return child.startswith(parent + os.sep)


def is_local_absolute(path: str) -> bool:
    """A local drive path (`C:\\...`). UNC, device and relative paths are not."""
    if not isinstance(path, str) or "\x00" in path:
        return False
    if path.startswith(("\\\\", "//")):
        return False
    if os.name == "nt":
        return len(path) >= 3 and path[1] == ":" and path[2] in "\\/" and path[0].isalpha()
    return os.path.isabs(path)  # pragma: no cover - non-Windows development only.
