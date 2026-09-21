"""Windows process-tree ownership for the runtime and every descendant."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any

_JOB_HANDLE: int | None = None
_PROCESS_LOCK_HANDLE: int | None = None
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def configure_runtime_process_tree() -> None:
    """Put this process in a kill-on-close job inherited by child processes."""

    global _JOB_HANDLE
    if os.name != "nt" or _JOB_HANDLE is not None:
        return
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "could not create runtime process job")
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, kernel32.GetCurrentProcess()
    )
    if not assigned:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise OSError(error, "could not own runtime process tree")
    # Intentionally retained until process termination. Windows then closes the
    # last job handle and terminates all descendants, including Chromium.
    _JOB_HANDLE = int(job)


def runtime_job_is_active() -> bool:
    """True when this process created the kill-on-close job that its descendants inherit."""

    return _JOB_HANDLE is not None


def acquire_runtime_process_lock() -> None:
    """Refuse a second local runtime before either process reaches recovery."""

    global _PROCESS_LOCK_HANDLE
    if os.name != "nt" or _PROCESS_LOCK_HANDLE is not None:
        return
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, "Local\\LumiAgentRuntime-v1")
    if not handle:
        raise OSError(ctypes.get_last_error(), "could not create runtime process lock")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("another local Lumi agent runtime is already active")
    _PROCESS_LOCK_HANDLE = int(handle)
