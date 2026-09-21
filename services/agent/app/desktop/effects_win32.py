"""The real `EffectPlatform`: the only place S3's native effects are performed.

Exactly four things happen here and nothing else may be added without changing the pinned list in
`tests/desktop_source_scan.py`:

* `GetForegroundWindow` (query)
* `GetLastInputInfo` (query: a tick that changes when the human touches the machine; the input itself
  is never seen, and this is not a hook)
* `QueryFullProcessImageNameW` (query, compared to a registered path and never returned to a caller)
* one `CreateProcess` of a registered executable, through `subprocess.Popen` with a list argument
  vector and `shell=False`, a scrubbed environment and no inherited handles

Focus is NOT here. It is UI Automation's `SetFocus` on the window root (`uia_backend.py`); a raw
`SetForegroundWindow` from this background process is refused by the Windows foreground lock, and the
tricks that defeat the lock (`AttachThreadInput`, a synthesized key or click) are exactly the input
authority Lumi does not have.

The launched application is started **outside** the runtime's kill-on-close job so it is an ordinary
application (not treated as Lumi's own, not killed with Lumi's tree).
"""

import ctypes
import ntpath
import os
import subprocess
from ctypes import wintypes
from typing import Any, Final

from app.desktop.registry import RegisteredApp

_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_CREATE_NEW_PROCESS_GROUP: Final = 0x00000200
_DETACHED_PROCESS: Final = 0x00000008
_CREATE_BREAKAWAY_FROM_JOB: Final = 0x01000000

#: The only variables a launched application inherits. No `LUMI_*`, no token, no database URL.
_LAUNCH_ENVIRONMENT: Final = (
    "SystemRoot", "SystemDrive", "windir", "PATH", "PATHEXT", "TEMP", "TMP", "USERNAME", "USERPROFILE",
    "USERDOMAIN", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)",
    "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "COMPUTERNAME", "HOMEDRIVE",
    "HOMEPATH", "LANG", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PUBLIC", "ALLUSERSPROFILE",
)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def launch_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    return {key: inherited[key] for key in _LAUNCH_ENVIRONMENT if key in inherited}


class WindowsEffectPlatform:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("desktop effects only run on Windows")
        user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32, self._kernel32 = user32, kernel32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
        user32.GetLastInputInfo.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    def foreground_hwnd(self) -> int | None:
        handle = self._user32.GetForegroundWindow()
        return int(handle) if handle else None

    def last_input_tick(self) -> int:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "input generation unavailable")
        return int(info.dwTime)

    def process_path(self, pid: int) -> str | None:
        handle = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            self._kernel32.CloseHandle(handle)

    def spawn(self, app: RegisteredApp) -> int:
        process = subprocess.Popen(  # noqa: S603 - a registered, validated executable; no shell.
            [app.executable, *app.args],
            shell=False,
            env=launch_environment(),
            cwd=ntpath.dirname(app.executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB,
        )
        return int(process.pid)
