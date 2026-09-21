"""The real, passive `SystemProbe`: window enumeration and process identity via Win32.

Only queries. Enumerating windows reads their state (`IsWindowVisible`, `IsIconic`,
the DWM cloaked flag, extended styles); it never shows, restores, activates, moves
or closes one. Process handles are opened with `PROCESS_QUERY_LIMITED_INFORMATION`
and nothing stronger, so there is no way to terminate, inject into or read the memory
of a target through them.
"""

import ctypes
import os
from ctypes import wintypes
from typing import Any, Final

from app.desktop.protocol import clean_text
from app.desktop.surfaces import ProcessIdentity, WindowFacts

_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_STILL_ACTIVE: Final = 259
_TOKEN_QUERY: Final = 0x0008
_TOKEN_INTEGRITY_LEVEL: Final = 25
_TH32CS_SNAPPROCESS: Final = 0x00000002
_INVALID_HANDLE: Final = ctypes.c_void_p(-1).value
_GWL_EXSTYLE: Final = -20
_GW_OWNER: Final = 4
_WS_EX_TOOLWINDOW: Final = 0x00000080
_WS_EX_APPWINDOW: Final = 0x00040000
_DWMWA_CLOAKED: Final = 14
_MAX_TITLE_READ: Final = 512
_JOB_OBJECT_BASIC_PROCESS_ID_LIST: Final = 3
_ERROR_MORE_DATA: Final = 234
_MAX_JOB_PROCESSES: Final = 65_536


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _JobProcessList(ctypes.Structure):
    # JOBOBJECT_BASIC_PROCESS_ID_LIST with its flexible array member sized by the caller's buffer.
    _fields_ = [
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
    ]


class WindowsSystemProbe:
    """Implements `app.desktop.surfaces.SystemProbe` for the current desktop."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("the Windows desktop probe only runs on Windows")
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        advapi32: Any = ctypes.WinDLL("advapi32", use_last_error=True)
        dwmapi: Any = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._kernel32, self._user32, self._advapi32, self._dwmapi = kernel32, user32, advapi32, dwmapi

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL

        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        user32.IsHungAppWindow.restype = wintypes.BOOL
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        dwmapi.DwmGetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    # -- windows ----------------------------------------------------------------

    def enumerate_windows(self) -> list[WindowFacts]:
        found: list[WindowFacts] = []

        def visit(hwnd: int, _: int) -> bool:
            pid = self.window_pid(hwnd)
            if pid is not None:
                found.append(self._facts(hwnd, pid))
            return True

        callback = self._enum_proc(visit)
        self._user32.EnumWindows(callback, 0)
        return found

    def _facts(self, hwnd: int, pid: int) -> WindowFacts:
        user32 = self._user32
        style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)) & 0xFFFFFFFF
        cloaked = wintypes.DWORD(0)
        self._dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        return WindowFacts(
            hwnd=hwnd,
            pid=pid,
            visible=bool(user32.IsWindowVisible(hwnd)),
            minimized=bool(user32.IsIconic(hwnd)),
            cloaked=cloaked.value != 0,
            owned=bool(user32.GetWindow(hwnd, _GW_OWNER)),
            tool_window=bool(style & _WS_EX_TOOLWINDOW),
            app_window=bool(style & _WS_EX_APPWINDOW),
            hung=bool(user32.IsHungAppWindow(hwnd)),
        )

    def window_pid(self, hwnd: int) -> int | None:
        if not self._user32.IsWindow(hwnd):
            return None
        pid = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value) or None

    def window_title(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(_MAX_TITLE_READ)
        length = self._user32.GetWindowTextW(hwnd, buffer, _MAX_TITLE_READ)
        return clean_text(buffer.value[: max(length, 0)])

    # -- processes --------------------------------------------------------------

    def _open(self, pid: int) -> int | None:
        handle = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        return int(handle) if handle else None

    def _close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        handle = self._open(pid)
        if handle is None:
            return None
        try:
            code = wintypes.DWORD(0)
            if not self._kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return None
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not self._kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return ProcessIdentity(pid=pid, created=ticks)
        finally:
            self._close(handle)

    def process_image(self, pid: int) -> str | None:
        handle = self._open(pid)
        if handle is None:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            # The directory is dropped here: a path never leaves this function.
            return clean_text(buffer.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()) or None
        finally:
            self._close(handle)

    def integrity_level(self, pid: int) -> int | None:
        handle = self._open(pid)
        if handle is None:
            return None
        try:
            return self._token_integrity(handle)
        finally:
            self._close(handle)

    def own_integrity_level(self) -> int | None:
        return self._token_integrity(self._kernel32.GetCurrentProcess())

    def _token_integrity(self, process_handle: int) -> int | None:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(process_handle, _TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            self._advapi32.GetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(needed))
            if needed.value == 0 or needed.value > 4096:
                return None
            raw = ctypes.create_string_buffer(needed.value)
            if not self._advapi32.GetTokenInformation(
                token, _TOKEN_INTEGRITY_LEVEL, raw, needed, ctypes.byref(needed)
            ):
                return None
            label = ctypes.cast(raw, ctypes.POINTER(_SidAndAttributes)).contents
            if not label.Sid:
                return None
            count = self._advapi32.GetSidSubAuthorityCount(label.Sid).contents.value
            if count == 0:
                return None
            return int(self._advapi32.GetSidSubAuthority(label.Sid, count - 1).contents.value)
        finally:
            self._close(int(token.value or 0))

    def process_parents(self) -> dict[int, int]:
        snapshot = self._kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snapshot or int(snapshot) == _INVALID_HANDLE:
            raise OSError(ctypes.get_last_error(), "could not snapshot the process list")
        parents: dict[int, int] = {}
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(_ProcessEntry)
            more = self._kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                more = self._kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            self._close(int(snapshot))
        if not parents:
            raise OSError(0, "the process snapshot was empty")
        return parents

    def job_members(self) -> frozenset[int]:
        """The processes in the job this process belongs to; empty if it belongs to none."""
        capacity = 1024
        while capacity <= _MAX_JOB_PROCESSES:
            size = ctypes.sizeof(_JobProcessList) + capacity * ctypes.sizeof(ctypes.c_size_t)
            raw = ctypes.create_string_buffer(size)
            returned = wintypes.DWORD(0)
            if self._kernel32.QueryInformationJobObject(
                None, _JOB_OBJECT_BASIC_PROCESS_ID_LIST, raw, size, ctypes.byref(returned)
            ):
                header = _JobProcessList.from_buffer(raw)
                count = int(header.NumberOfProcessIdsInList)
                offset = ctypes.sizeof(_JobProcessList)
                pids = (ctypes.c_size_t * count).from_buffer(raw, offset)
                return frozenset(int(pid) for pid in pids)
            if ctypes.get_last_error() != _ERROR_MORE_DATA:
                return frozenset()
            capacity *= 4
        return frozenset()
