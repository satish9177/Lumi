"""Windows process-tree ownership for the runtime and every descendant."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from typing import Any

_JOB_HANDLE: int | None = None
_PROCESS_LOCK_HANDLE: int | None = None
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# Lets a process that asks with CREATE_BREAKAWAY_FROM_JOB leave the job. Only the desktop worker's
# registered-application launch asks; nothing else in the tree passes that flag.
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
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
    information.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
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


# ---- Milestone 10: one job per supervised child --------------------------------------------------

_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    kernel32.IsProcessInJob.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class ProcessJob:
    """A Job Object owning exactly one supervised child tree (Milestone 10).

    Always kill-on-close, and never created with the flag that would let a member leave it: every
    process the child starts stays inside, so `terminate` ends exactly this tree and nothing else, and
    closing the last handle (including by the runtime dying) ends it too. The runtime holds the only
    handle; the job is unnamed, so no other process can open it.

    Optional limits: `memory_limit_bytes` (per process) and `max_processes` (1 forbids the child from
    starting any process at all).
    """

    def __init__(self, *, memory_limit_bytes: int | None = None, max_processes: int | None = None) -> None:
        if os.name != "nt":
            raise OSError("process jobs are Windows-only")
        self._kernel32 = _kernel32()
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "could not create a process job")
        information = _ExtendedLimitInformation()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        if memory_limit_bytes is not None:
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            information.ProcessMemoryLimit = memory_limit_bytes
        if max_processes is not None:
            flags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            information.BasicLimitInformation.ActiveProcessLimit = max_processes
        information.BasicLimitInformation.LimitFlags = flags
        if not self._kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(information), ctypes.sizeof(information)
        ):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(job)
            raise OSError(error, "could not configure a process job")
        self._handle: int | None = int(job)
        # Terminate and close are serialised: a Stop can never act on a handle value another job reused.
        self._lock = threading.Lock()

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise OSError("the process job is closed")
        return self._handle

    def assign(self, pid: int) -> None:
        process = self._kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "could not open the child process")
        try:
            if not self._kernel32.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "could not assign the child to its job")
        finally:
            self._kernel32.CloseHandle(process)

    def contains(self, pid: int) -> bool:
        """Is `pid` a member of THIS job? A PID alone is never ownership; membership is."""
        process = self._kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not process:
            return False
        try:
            result = wintypes.BOOL(False)
            if not self._kernel32.IsProcessInJob(process, self.handle, ctypes.byref(result)):
                return False
            return bool(result.value)
        finally:
            self._kernel32.CloseHandle(process)

    def active_processes(self) -> int:
        information = _BasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self.handle,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "could not query the process job")
        return int(information.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> bool:
        """End every process in this job, and only this job."""
        with self._lock:
            if self._handle is None:
                return False
            return bool(self._kernel32.TerminateJobObject(self._handle, exit_code))

    def close(self) -> None:
        """Closing the last handle kills anything still in the job (kill-on-close)."""
        with self._lock:
            if self._handle is not None:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


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
