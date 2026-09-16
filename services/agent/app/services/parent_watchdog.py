"""Fail closed when the trusted Electron parent disappears unexpectedly."""

from __future__ import annotations

import asyncio
import ctypes
import os
from ctypes import wintypes
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Protocol


class ParentLiveness(Protocol):
    def __call__(self) -> bool: ...


@contextmanager
def parent_liveness(parent_pid: int) -> Iterator[ParentLiveness]:
    """Open one stable parent reference, avoiding both PID reuse and killing it."""

    if os.name == "nt":
        kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        synchronize = 0x00100000
        wait_timeout = 0x00000102
        handle = kernel32.OpenProcess(synchronize, False, parent_pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "could not open Electron parent process")
        try:
            yield lambda: kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
        return

    # Signal 0 is a liveness probe on POSIX. It must never be used on Windows,
    # where CPython maps it to TerminateProcess.
    def posix_alive() -> bool:
        try:
            os.kill(parent_pid, 0)
        except OSError:
            return False
        return True

    yield posix_alive


async def watch_parent(
    parent_pid: int,
    *,
    poll_seconds: float = 1.0,
    open_liveness: Callable[[int], Any] = parent_liveness,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    with open_liveness(parent_pid) as is_alive:
        await watch_liveness(
            is_alive, poll_seconds=poll_seconds, exit_process=exit_process
        )


async def watch_liveness(
    is_alive: ParentLiveness,
    *,
    poll_seconds: float = 1.0,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    while True:
        if not is_alive():
            # The runtime's Windows job closes during process exit, killing
            # every descendant before any can become an orphan.
            exit_process(0)
            return
        await asyncio.sleep(poll_seconds)
