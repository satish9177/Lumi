"""Atomic, no-overwrite placement of a quarantined file into an approved folder (Milestone 10 S2).

The one file mutation M10 performs. It is a *rename*, never a copy-then-delete, and it is expressed
relative to a **held handle on the destination directory**, not a path string:

1. open the destination directory (the approved root) with `FILE_FLAG_OPEN_REPARSE_POINT`, sharing
   read/write but **not delete**, so it cannot be renamed, removed or replaced while held; prove from the
   handle that it is not a reparse point, that its final path is the verified root, and that its
   (volume, file index) is the root's recorded identity;
2. open the quarantined payload with `DELETE` and read access, sharing **read only** (no writer can open it
   while held, and none may already hold it open), prove from its handle that it is the payload the manifest
   recorded (final path under the quarantine, identity, single link), and hand its bytes -- read through
   that same handle -- to the caller's `verify` (hash and signature), so what is checked is exactly what is
   renamed (S2 review finding 4);
3. `NtSetInformationFile(FileRenameInformation)` with `ReplaceIfExists = FALSE` and `RootDirectory` = the
   held directory handle: the destination is `<that directory>\\<validated name>`, it can never replace an
   existing file (`STATUS_OBJECT_NAME_COLLISION`), and a different volume fails outright
   (`STATUS_NOT_SAME_DEVICE`): cross-volume placement is refused, not emulated;
4. re-prove from the same payload handle where it now is and that it is still the same file.

A same-volume rename keeps the file's index and its alternate streams, so the Mark-of-the-Web written in
quarantine travels with it, and "is the file at the destination or still in quarantine" has exactly one
true answer -- the basis for authoritative reconciliation.
"""

import ctypes
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from app.files.handles import FileIdentity, identity_of, is_within, normcase
from app.files.names import validate_file_name

_GENERIC_READ: Final = 0x80000000
_DELETE: Final = 0x00010000
_SYNCHRONIZE: Final = 0x00100000
_FILE_READ_ATTRIBUTES: Final = 0x0080
_FILE_LIST_DIRECTORY: Final = 0x0001
_FILE_ADD_FILE: Final = 0x0002
_FILE_SHARE_READ: Final = 0x1
_FILE_SHARE_WRITE: Final = 0x2
_OPEN_EXISTING: Final = 3
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x400
_FILE_ATTRIBUTE_DIRECTORY: Final = 0x10
_FILE_RENAME_INFORMATION: Final = 10
_STATUS_OBJECT_NAME_COLLISION: Final = 0xC0000035
_STATUS_NOT_SAME_DEVICE: Final = 0xC00000D4
_INVALID_HANDLE: Final = ctypes.c_void_p(-1).value


class PlacementRefusal(ValueError):
    """`code` is stable and never carries a path. `effect_possible` is True only when the rename call
    itself was made and its result is not a definite refusal."""

    def __init__(self, code: str, *, effect_possible: bool = False) -> None:
        super().__init__(f"The file could not be placed ({code}).")
        self.code = code
        self.effect_possible = effect_possible


@dataclass(frozen=True, slots=True)
class Placed:
    identity: FileIdentity
    final_path: str


class _ByHandleInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        # FILETIME is two DWORDs (4-byte aligned); c_uint64 here would insert padding and shift every field.
        ("ftCreationTime", ctypes.c_uint32 * 2),
        ("ftLastAccessTime", ctypes.c_uint32 * 2),
        ("ftLastWriteTime", ctypes.c_uint32 * 2),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [("VolumeSerialNumber", ctypes.c_uint64), ("FileId", ctypes.c_ubyte * 16)]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p)]


def _api() -> tuple[Any, Any]:
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll: Any = ctypes.WinDLL("ntdll")
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleInformation)]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.ReadFile.restype = wintypes.BOOL
    ntdll.NtSetInformationFile.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p, wintypes.ULONG, ctypes.c_int
    ]
    ntdll.NtSetInformationFile.restype = ctypes.c_ulong
    return kernel32, ntdll


def _final_path(kernel32: Any, handle: int) -> str | None:
    buffer = ctypes.create_unicode_buffer(1024)
    length = kernel32.GetFinalPathNameByHandleW(handle, buffer, 1024, 0)
    if length == 0 or length >= 1024:
        return None
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        return None
    return value[4:] if value.startswith("\\\\?\\") else value


def _info(kernel32: Any, handle: int) -> _ByHandleInformation | None:
    info = _ByHandleInformation()
    return info if kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)) else None


_FILE_ID_INFO_CLASS: Final = 18


def _identity(kernel32: Any, handle: int, info: _ByHandleInformation) -> tuple[int, int, int]:
    """(volume, index, size) exactly as `os.stat` reports them: the 64-bit volume serial and the low 64
    bits of the 128-bit file id from `FILE_ID_INFO`, so handle facts and recorded facts compare equal."""
    ids = _FileIdInfo()
    if not kernel32.GetFileInformationByHandleEx(handle, _FILE_ID_INFO_CLASS, ctypes.byref(ids), ctypes.sizeof(ids)):
        raise PlacementRefusal("identity_unavailable")
    return (
        int(ids.VolumeSerialNumber),
        int.from_bytes(bytes(ids.FileId)[:8], "little"),
        (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow),
    )


def _read_all(kernel32: Any, handle: int, limit: int) -> bytes:
    """Up to `limit` bytes from the start of a freshly opened handle (its position is 0)."""
    from ctypes import wintypes

    chunks: list[bytes] = []
    total = 0
    buffer = ctypes.create_string_buffer(1 << 20)
    while total < limit:
        read = wintypes.DWORD(0)
        want = min(len(buffer), limit - total)
        if not kernel32.ReadFile(handle, buffer, want, ctypes.byref(read), None):
            raise PlacementRefusal("quarantine_changed")
        if read.value == 0:
            break
        chunks.append(buffer.raw[: read.value])
        total += read.value
    return b"".join(chunks)


def _open(kernel32: Any, path: str, access: int, share: int, flags: int) -> int:
    handle = kernel32.CreateFileW(path, access, share, None, _OPEN_EXISTING, flags, None)
    if not handle or handle == _INVALID_HANDLE:
        raise PlacementRefusal("open_failed")
    return int(handle)


def place(
    *,
    payload_path: str,
    quarantine_directory: str,
    payload_identity: FileIdentity,
    root_canonical: str,
    root_volume: int,
    root_index: int,
    file_name: str,
    verify: Callable[[bytes], None],
) -> Placed:
    """Rename the payload into the root as `file_name`, atomically and without replacing anything."""
    if os.name != "nt":  # pragma: no cover - the broker ships on Windows.
        raise PlacementRefusal("unsupported_platform")
    name = validate_file_name(file_name)
    kernel32, ntdll = _api()
    directory = _open(
        kernel32,
        root_canonical,
        _FILE_LIST_DIRECTORY | _FILE_ADD_FILE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # never FILE_SHARE_DELETE: the folder cannot be swapped out
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        info = _info(kernel32, directory)
        if info is None or info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT or not info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise PlacementRefusal("root_changed")
        volume, index, _ = _identity(kernel32, directory, info)
        final_root = _final_path(kernel32, directory)
        if (volume, index) != (root_volume & 0xFFFFFFFFFFFFFFFF, root_index) or final_root is None or normcase(final_root) != normcase(root_canonical):
            raise PlacementRefusal("root_changed")
        source = _open(
            kernel32,
            payload_path,
            _GENERIC_READ | _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            _FILE_SHARE_READ,  # never write or delete: the bytes verified below cannot change before the rename
            _FILE_FLAG_OPEN_REPARSE_POINT,
        )
        try:
            payload = _info(kernel32, source)
            before_path = _final_path(kernel32, source)
            if payload is None or before_path is None or not is_within(before_path, quarantine_directory):
                raise PlacementRefusal("quarantine_changed")
            if payload.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT or payload.nNumberOfLinks != 1:
                raise PlacementRefusal("quarantine_changed")
            p_volume, p_index, p_size = _identity(kernel32, source, payload)
            if (p_index, p_size) != (payload_identity.index, payload_identity.size) or p_volume != (payload_identity.volume & 0xFFFFFFFFFFFFFFFF):
                raise PlacementRefusal("quarantine_changed")
            if p_volume != volume:
                raise PlacementRefusal("cross_volume_refused")
            # The content check happens HERE, through the held handle: raising refuses before any rename.
            verify(_read_all(kernel32, source, p_size + 1))
            encoded = name.encode("utf-16-le")
            # FILE_RENAME_INFORMATION (x64): BOOLEAN ReplaceIfExists; [pad]; HANDLE RootDirectory;
            # ULONG FileNameLength; WCHAR FileName[].
            size = 20 + len(encoded) + 2
            buffer = ctypes.create_string_buffer(size)
            ctypes.memset(buffer, 0, size)
            ctypes.c_ubyte.from_buffer(buffer, 0).value = 0  # ReplaceIfExists = FALSE
            ctypes.c_void_p.from_buffer(buffer, 8).value = directory
            ctypes.c_uint32.from_buffer(buffer, 16).value = len(encoded)
            ctypes.memmove(ctypes.addressof(buffer) + 20, encoded, len(encoded))
            status_block = _IoStatusBlock()
            # From here the rename may have happened; only a definite status says otherwise.
            status = ntdll.NtSetInformationFile(source, ctypes.byref(status_block), buffer, size, _FILE_RENAME_INFORMATION)
            if status == _STATUS_OBJECT_NAME_COLLISION:
                raise PlacementRefusal("destination_exists")
            if status == _STATUS_NOT_SAME_DEVICE:
                raise PlacementRefusal("cross_volume_refused")
            if status != 0:
                raise PlacementRefusal("rename_failed", effect_possible=(status & 0xC0000000) != 0xC0000000)
            after = _info(kernel32, source)
            after_path = _final_path(kernel32, source)
            expected = os.path.join(root_canonical, name)
            if after is None or after_path is None or normcase(after_path) != normcase(expected):
                raise PlacementRefusal("placement_unverified", effect_possible=True)
            a_volume, a_index, a_size = _identity(kernel32, source, after)
            if (a_volume, a_index, a_size) != (p_volume, p_index, p_size):
                raise PlacementRefusal("placement_unverified", effect_possible=True)
        finally:
            kernel32.CloseHandle(source)
    finally:
        kernel32.CloseHandle(directory)
    placed = identity_of(os.stat(expected, follow_symlinks=False))
    return Placed(identity=placed, final_path=expected)
