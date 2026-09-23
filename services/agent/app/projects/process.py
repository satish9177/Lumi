"""Supervising ONE project run (Milestone 10 S3): fixed argv, scrubbed env, its own Job Object.

This is the only place in Lumi that starts a project's code. The shape is fixed:

* `argv = [<node.exe>, <npm-cli.js>, "run", <script>]` -- the executable and the npm entry point are the
  pinned files of the recipe (never looked up on PATH), and the script name is one the recipe registered.
  No shell: `shell=False`, no `cmd`, no PowerShell, no `.cmd` shim, and nothing typed into a terminal;
* `cwd` = the project root; `env` = exactly the dict the caller built (never merged with this process's);
* the child is created **suspended**, assigned to a fresh kill-on-close Job Object that no member may leave,
  and only then resumed -- so every process it ever starts is inside that job, and
  `TerminateJobObject` on it ends exactly this run and nothing else;
* stdout and stderr go to one pipe read into a bounded ring buffer (the log is untrusted text).

The job's only handle is held here, in the runtime. If the runtime dies, Windows closes it and the run
ends with it.
"""

from __future__ import annotations

import ctypes
import os
import re
import socket
import struct
import subprocess
import threading
from collections import deque
from collections.abc import Mapping
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Final

from app.services.windows_job import ProcessJob

_CREATE_SUSPENDED: Final = 0x00000004
_CREATE_NO_WINDOW: Final = 0x08000000
_CREATE_NEW_PROCESS_GROUP: Final = 0x00000200
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_STILL_ACTIVE: Final = 259
_AF_INET: Final = 2
_AF_INET6: Final = 23
_TCP_TABLE_OWNER_PID_LISTENER: Final = 3
_ERROR_INSUFFICIENT_BUFFER: Final = 122

LOG_MAX_LINES: Final = 200
LOG_MAX_LINE_CHARS: Final = 400
# eslint-style: C0 controls except tab, plus DEL and C1 -- a log line is shown as inert text only.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# ANSI escape sequences, stripped rather than rendered.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _kernel32() -> Any:
    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    return kernel32


def _ntdll() -> Any:
    ntdll: Any = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_ulong
    return ntdll


def _creation_time(kernel32: Any, handle: int) -> int | None:
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def process_is_alive(pid: int, creation_time: int) -> bool | None:
    """Is the process (pid, creation time) still running? None when Windows will not say.

    A PID alone proves nothing (it is reused); the pair identifies one process for its whole life."""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return False if error == 87 else None  # ERROR_INVALID_PARAMETER: no such process
    try:
        created = _creation_time(kernel32, int(handle))
        if created is None:
            return None
        if created != creation_time:
            return False  # the pid was reused by another process
        code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class BoundedLog:
    """The last `LOG_MAX_LINES` lines of a run's output, each bounded and stripped of control sequences."""

    def __init__(self, redact: tuple[tuple[str, str], ...] = ()) -> None:
        # Longest first, so a project inside the run folder (or the reverse) is replaced whole.
        self._redact = tuple(sorted(((re.compile(re.escape(prefix), re.IGNORECASE), label) for prefix, label in redact if prefix),
                                    key=lambda item: -len(item[0].pattern)))
        self._lines: deque[str] = deque(maxlen=LOG_MAX_LINES)
        self._lock = threading.Lock()
        self.total_lines = 0

    def add(self, raw: bytes) -> None:
        text = _ANSI.sub("", raw.decode("utf-8", errors="replace"))
        text = _CONTROL.sub("", text.rstrip("\r\n"))
        for pattern, label in self._redact:
            text = pattern.sub(label, text)
        text = text[:LOG_MAX_LINE_CHARS]
        with self._lock:
            self._lines.append(text)
            self.total_lines += 1

    def tail(self, count: int = 50) -> list[str]:
        with self._lock:
            return list(self._lines)[-max(0, min(count, LOG_MAX_LINES)):]


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    node_path: str
    npm_cli_path: str
    script: str
    cwd: str
    env: Mapping[str, str]
    #: Local path prefixes replaced in the log (the run folder, the project, Node.js) before display.
    redact: tuple[tuple[str, str], ...] = ()

    def argv(self) -> list[str]:
        return [self.node_path, self.npm_cli_path, "run", self.script]


class RunProcess:
    """A started run: its root process, its job and its log. The runtime is the only holder of the job."""

    def __init__(self, popen: subprocess.Popen[bytes], job: ProcessJob, creation_time: int, redact: tuple[tuple[str, str], ...] = ()) -> None:
        self._popen = popen
        self._job = job
        self.pid = popen.pid
        self.creation_time = creation_time
        self.log = BoundedLog(redact)
        self._reader = threading.Thread(target=self._read, name=f"lumi-run-log-{popen.pid}", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stream = self._popen.stdout
        if stream is None:  # pragma: no cover - always piped
            return
        # A bounded read: a child that writes gigabytes without a newline cannot exhaust the runtime (finding 5).
        for line in iter(lambda: stream.readline(LOG_MAX_LINE_CHARS * 4), b""):
            self.log.add(line)
        stream.close()

    def exit_code(self) -> int | None:
        return self._popen.poll()

    def job_contains(self, pid: int) -> bool:
        return self._job.contains(pid)

    def active_processes(self) -> int:
        try:
            return self._job.active_processes()
        except OSError:
            return 0

    def stop(self) -> bool:
        """End every process in THIS run's job, and only that job. Never by image name or bare PID."""
        ended = self._job.terminate(1)
        try:
            self._popen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return ended

    def close(self) -> None:
        self._job.close()  # kill-on-close: anything still in the job ends here


class LaunchError(RuntimeError):
    """`code` is stable. `effect_possible` is True when the child may have run any code."""

    def __init__(self, code: str, *, effect_possible: bool) -> None:
        super().__init__(f"The project could not be started ({code}).")
        self.code = code
        self.effect_possible = effect_possible


def launch(spec: LaunchSpec, *, record_pid: Any) -> RunProcess:
    """Create the root process suspended, put it in its own job, let `record_pid(pid, creation_time)`
    make that durable, and only then resume it. Until the resume, no project code has run."""
    if os.name != "nt":  # pragma: no cover - M10 ships on Windows.
        raise LaunchError("unsupported_platform", effect_possible=False)
    job = ProcessJob()
    try:
        popen = subprocess.Popen(  # noqa: S603 - the one reviewed project spawn: fixed argv, no shell.
            spec.argv(),
            executable=spec.node_path,
            cwd=spec.cwd,
            env=dict(spec.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
        )
    except OSError:
        job.close()
        raise LaunchError("spawn_failed", effect_possible=False) from None
    handle = int(popen._handle)  # type: ignore[attr-defined]  # the process handle Popen owns
    kernel32 = _kernel32()
    try:
        job.assign(popen.pid)
        creation_time = _creation_time(kernel32, handle)
        if creation_time is None:
            raise OSError("no creation time")
    except OSError:
        # Still suspended: nothing of the project has run. End it and report a clean failure.
        popen.kill()
        popen.wait(timeout=10)
        job.close()
        raise LaunchError("job_assignment_failed", effect_possible=False) from None
    try:
        record_pid(popen.pid, creation_time)
    except BaseException:
        popen.kill()
        popen.wait(timeout=10)
        job.close()
        raise
    status = _ntdll().NtResumeProcess(handle)
    if status != 0:
        job.terminate(1)
        job.close()
        raise LaunchError("resume_failed", effect_possible=True)
    return RunProcess(popen, job, creation_time, spec.redact)


# ---- listeners -------------------------------------------------------------------------------------------


def _iphlpapi() -> Any:
    iphlpapi: Any = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG
    ]
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    return iphlpapi


def _table(family: int) -> bytes:
    iphlpapi = _iphlpapi()
    size = wintypes.DWORD(0)
    for _ in range(4):
        buffer = ctypes.create_string_buffer(max(size.value, 4))
        result = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if result == 0:
            return buffer.raw[: size.value]
        if result != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(result, "could not read the TCP listener table")
    raise OSError("the TCP listener table kept growing")


@dataclass(frozen=True, slots=True)
class Listener:
    address: str
    port: int
    pid: int


def listeners(port: int) -> list[Listener]:
    """Every TCP listener on `port` (IPv4 and IPv6) with its owning process id."""
    found: list[Listener] = []
    data = _table(_AF_INET)
    (count,) = struct.unpack_from("<I", data, 0)
    for index in range(count):
        _, local_addr, local_port, _, _, pid = struct.unpack_from("<IIIIII", data, 4 + index * 24)
        if socket.ntohs(local_port & 0xFFFF) == port:
            found.append(Listener(socket.inet_ntoa(struct.pack("<I", local_addr)), port, pid))
    data = _table(_AF_INET6)
    (count,) = struct.unpack_from("<I", data, 0)
    for index in range(count):
        offset = 4 + index * 56
        address = data[offset : offset + 16]
        (local_port,) = struct.unpack_from("<I", data, offset + 20)
        (pid,) = struct.unpack_from("<I", data, offset + 52)
        if socket.ntohs(local_port & 0xFFFF) == port:
            found.append(Listener(socket.inet_ntop(socket.AF_INET6, address), port, pid))
    return found
