"""Windows-only helpers: launch the fixture window, and run the real observer on a UIA thread.

The fixture is a separate process so its window, its process identity and its message
loop are real. Every helper here is test-side: it may send the fixture private commands
(relabel, recreate, dump counters, hang) which the production code can never do.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from app.config import AGENT_ROOT
from app.desktop.observer import DesktopObserver
from app.desktop.surfaces import ExclusionPolicy, SurfaceTable
from app.desktop.worker import UiaThread

T = TypeVar("T")

WM_APP = 0x8000
CMD_RELABEL = WM_APP + 1
CMD_RECREATE_BUTTON = WM_APP + 2
CMD_DUMP = WM_APP + 10
CMD_GET_SUBMIT_HWND = WM_APP + 11
CMD_HANG = WM_APP + 20

#: Counters that must all stay at zero: none of them can move unless something acted on the fixture.
EFFECT_COUNTERS = (
    "button_clicks", "bm_click_msgs", "toggle_msgs", "edit_changes", "selection_changes",
    "settext_msgs", "scroll_msgs", "mouse_msgs", "key_msgs", "focus_events", "activations",
    "window_moves", "window_close_msgs", "password_reads",
)


def _user32() -> Any:
    return ctypes.WinDLL("user32", use_last_error=True)


def foreground_window() -> int:
    return int(_user32().GetForegroundWindow() or 0)


@dataclass
class FixtureApp:
    process: "subprocess.Popen[bytes]"
    hwnd: int
    pid: int
    title: str
    state_file: Path

    def send(self, message: int, wparam: int = 0) -> int:
        user32 = _user32()
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        return int(user32.SendMessageW(self.hwnd, message, wparam, 0))

    def dump(self) -> dict[str, Any]:
        self.send(CMD_DUMP)
        state: dict[str, Any] = json.loads(self.state_file.read_text(encoding="utf-8"))
        return state

    def relabel(self) -> int:
        return self.send(CMD_RELABEL)

    def recreate_submit(self) -> int:
        return self.send(CMD_RECREATE_BUTTON)

    def hang(self, seconds: int) -> None:
        self.send(CMD_HANG, seconds)

    def counters(self) -> dict[str, int]:
        return {key: int(value) for key, value in self.dump().items() if key in EFFECT_COUNTERS or key == "getobject"}

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()


def _read_ready(process: "subprocess.Popen[bytes]", timeout: float) -> dict[str, Any]:
    result: list[bytes] = []
    assert process.stdout is not None
    reader = threading.Thread(target=lambda: result.append(process.stdout.readline()), daemon=True)  # type: ignore[union-attr]
    reader.start()
    reader.join(timeout)
    if not result or not result[0].strip():
        process.kill()
        raise RuntimeError("the desktop fixture did not become ready")
    record: dict[str, Any] = json.loads(result[0])
    assert record["event"] == "fixture-ready"
    return record


def start_fixture(tmp_path: Path, *arguments: str, title: str | None = None) -> FixtureApp:
    title = title or f"Lumi Fixture {uuid.uuid4().hex[:8]}"
    state_file = tmp_path / f"fixture-{uuid.uuid4().hex[:8]}.json"
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.desktop_fixture_app", "--title", title, "--state-file", str(state_file), *arguments],
        cwd=AGENT_ROOT,
        stdout=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    ready = _read_ready(process, timeout=30)
    return FixtureApp(process=process, hwnd=int(ready["hwnd"]), pid=int(ready["pid"]), title=title, state_file=state_file)


@contextmanager
def fixture(tmp_path: Path, *arguments: str, title: str | None = None) -> Iterator[FixtureApp]:
    app = start_fixture(tmp_path, *arguments, title=title)
    try:
        yield app
    finally:
        app.stop()


class RealObserver:
    """The real probe and the real pywinauto backend, on one dedicated MTA thread."""

    def __init__(self, excluded_pids: tuple[int, ...] = (), *, time_budget_seconds: float = 10.0) -> None:
        self._uia = UiaThread()
        self._excluded = excluded_pids
        self._budget = time_budget_seconds
        self.generation = uuid.uuid4()
        self.observer: DesktopObserver = self._uia.submit(self._build).result(timeout=90)

    def _build(self) -> DesktopObserver:
        from app.desktop.uia_backend import PywinautoBackend
        from app.desktop.win32 import WindowsSystemProbe

        probe = WindowsSystemProbe()
        roots = tuple(identity for identity in (probe.process_identity(pid) for pid in self._excluded) if identity)
        return DesktopObserver(
            surfaces=SurfaceTable(probe=probe, exclusion=ExclusionPolicy(roots=roots)),
            backend=PywinautoBackend(),
            worker_generation=self.generation,
            time_budget_seconds=self._budget,
        )

    def run(self, call: Callable[[DesktopObserver], T], timeout: float = 60) -> T:
        return self._uia.submit(lambda: call(self.observer)).result(timeout=timeout)

    def surface_for(self, title: str) -> Any:
        listing = self.run(lambda observer: observer.list_surfaces())
        for surface in listing.surfaces:
            if surface.window_title == title:
                return surface
        raise AssertionError(f"no surface titled {title!r} in {[s.window_title for s in listing.surfaces]}")

    def observe_title(self, title: str) -> Any:
        """One real read, retried as a new read if the window moved under it (`surface_changed`)."""
        from app.desktop.errors import DesktopReason, DesktopRefusal

        surface = self.surface_for(title)
        for attempt in range(3):
            try:
                return self.run(lambda observer: observer.observe(surface.surface_ref, surface.surface_epoch))
            except DesktopRefusal as refusal:
                if refusal.code is not DesktopReason.SURFACE_CHANGED or attempt == 2:
                    raise
        raise AssertionError("unreachable")


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def is_windows() -> bool:
    return os.name == "nt"
