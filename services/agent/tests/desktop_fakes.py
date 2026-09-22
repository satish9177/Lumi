"""Deterministic fakes for the desktop domain tests: a scripted probe and a scripted UIA tree.

These let every rule (identity, exclusion, elevation, credentials, bounds, stability,
re-resolution) be tested exactly and on any platform. The real UI Automation backend is
exercised separately, against a real Win32 window, in `test_desktop_uia_windows.py`; a
fake alone would prove nothing about that.
"""

import itertools
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.desktop.observer import ElementUnavailable, RawProps, ScrollState, UiaElement, ValueState
from app.desktop.protocol import CheckedState, DesktopPattern, ScrollStep
from app.desktop.registry import RegisteredApp
from app.desktop.surfaces import (
    INTEGRITY_MEDIUM,
    ProcessIdentity,
    WindowFacts,
)


@dataclass
class FakeProcess:
    pid: int
    created: int
    image: str = "editor.exe"
    integrity: int | None = INTEGRITY_MEDIUM
    parent: int | None = None
    alive: bool = True
    path: str | None = None


@dataclass
class FakeWindow:
    hwnd: int
    pid: int
    title: str = "A window"
    visible: bool = True
    minimized: bool = False
    cloaked: bool = False
    owned: bool = False
    tool_window: bool = False
    app_window: bool = False
    hung: bool = False


class FakeProbe:
    """A scripted `SystemProbe`. Mutate `windows` and `processes` between calls to script change."""

    def __init__(self) -> None:
        self.windows: list[FakeWindow] = []
        self.processes: dict[int, FakeProcess] = {}
        self.own_integrity: int | None = INTEGRITY_MEDIUM
        self.destroyed: set[int] = set()
        #: Script the process snapshot: fail, or leave a live process out of it (a snapshot race).
        self.snapshot_error = False
        self.missing_from_snapshot: set[int] = set()
        #: Pids in the (fake) job object of the worker.
        self.job: set[int] = set()
        # The pytest process stands in for the worker itself.
        self.add_process(os.getpid(), created=1, image="python.exe")

    def add_process(
        self,
        pid: int,
        *,
        created: int = 100,
        image: str = "editor.exe",
        integrity: int | None = INTEGRITY_MEDIUM,
        parent: int | None = None,
        path: str | None = None,
    ) -> FakeProcess:
        process = FakeProcess(
            pid=pid, created=created, image=image, integrity=integrity, parent=parent, path=path
        )
        self.processes[pid] = process
        return process

    def add_window(self, hwnd: int, pid: int, title: str = "A window", **flags: bool) -> FakeWindow:
        window = FakeWindow(hwnd=hwnd, pid=pid, title=title, **flags)
        self.windows.append(window)
        return window

    # SystemProbe -----------------------------------------------------------------

    def enumerate_windows(self) -> Sequence[WindowFacts]:
        return [
            WindowFacts(
                hwnd=w.hwnd, pid=w.pid, visible=w.visible, minimized=w.minimized, cloaked=w.cloaked,
                owned=w.owned, tool_window=w.tool_window, app_window=w.app_window, hung=w.hung,
            )
            for w in self.windows
            if w.hwnd not in self.destroyed
        ]

    def window_pid(self, hwnd: int) -> int | None:
        for window in self.windows:
            if window.hwnd == hwnd and hwnd not in self.destroyed:
                return window.pid
        return None

    def window_title(self, hwnd: int) -> str:
        return next((w.title for w in self.windows if w.hwnd == hwnd), "")

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        process = self.processes.get(pid)
        return ProcessIdentity(pid=pid, created=process.created) if process and process.alive else None

    def process_image(self, pid: int) -> str | None:
        process = self.processes.get(pid)
        return process.image if process and process.alive else None

    def integrity_level(self, pid: int) -> int | None:
        process = self.processes.get(pid)
        return process.integrity if process and process.alive else None

    def own_integrity_level(self) -> int | None:
        return self.own_integrity

    def process_parents(self) -> dict[int, int]:
        if self.snapshot_error:
            raise OSError("scripted snapshot failure")
        return {
            p.pid: (p.parent or 0)
            for p in self.processes.values()
            if p.alive and p.pid not in self.missing_from_snapshot
        }

    def job_members(self) -> frozenset[int]:
        return frozenset(self.job)


# ---- a scripted UIA tree ------------------------------------------------------------

_RUNTIME_IDS = itertools.count(1000)


@dataclass
class Node:
    """One scripted element. Mutate its fields to script a change between reads."""

    control_type: str = "Pane"
    name: str = ""
    children: list["Node"] = field(default_factory=list)
    value: str | None = None
    is_password: bool = False
    enabled: bool = True
    offscreen: bool = False
    focused: bool = False
    focusable: bool = False
    checked: CheckedState | None = None
    selected: bool | None = None
    expanded: bool | None = None
    patterns: frozenset[DesktopPattern] = frozenset()
    automation_id: str = ""
    class_name: str = "Fake"
    runtime_id: tuple[int, ...] = field(default_factory=lambda: (next(_RUNTIME_IDS),))
    #: How many times anything but the control type and password flag was read.
    sensitive_reads: int = 0
    gone: bool = False
    #: ScrollPattern script. `scroll_percent is None` with `scrollable` means UIA reports no position.
    scrollable: bool = False
    scroll_percent: float | None = 0.0
    scroll_calls: list[ScrollStep] = field(default_factory=list)
    scroll_moves: bool = True
    # -- S4: ValuePattern, SelectionItem, Invoke scripts ---------------------------------------
    read_only: bool = False
    set_value_calls: list[str] = field(default_factory=list)
    #: When True, `set_value` silently fails to change `.value` (models an app that ignores the call).
    set_value_ignored: bool = False
    select_calls: int = 0
    #: When True, `select` silently fails to change `.selected` (models an app that ignores the call).
    select_ignored: bool = False
    invoke_calls: int = 0
    #: What `invoke()` changes `.name` to, if anything. `None` means invoking this control changes
    #: nothing observable, which is the `no_change` / known-failure path for `NAME_TOGGLE`.
    invoke_renames_to: str | None = None


class FakeElement:
    def __init__(self, node: Node, backend: "FakeBackend", hwnd: int | None = None) -> None:
        self.node = node
        self._backend = backend
        self.hwnd = hwnd

    def focus(self) -> None:
        if self.node.gone:
            raise ElementUnavailable
        if self.hwnd is None:
            raise AssertionError("focus is only ever called on a window root")
        self._backend.focus_calls.append(self.hwnd)
        if self._backend.on_focus is not None:
            self._backend.on_focus(self.hwnd)

    def props(self, level: str) -> RawProps:
        node = self.node
        if node.gone:
            raise ElementUnavailable
        self._backend.reads += 1
        if self._backend.on_read is not None:
            self._backend.on_read(self._backend.reads)
        if node.is_password:
            return RawProps(control_type=node.control_type, is_password=True)
        if level == "scan":
            name = node.name if node.control_type in ("Edit", "ComboBox") else ""
            return RawProps(control_type=node.control_type, is_password=False, name=name)
        node.sensitive_reads += 1
        base = {
            "control_type": node.control_type,
            "is_password": False,
            "name": node.name,
            "automation_id": node.automation_id,
            "class_name": node.class_name,
            "runtime_id": node.runtime_id,
            "enabled": node.enabled,
            "patterns": node.patterns,
        }
        if level == "structure":
            return RawProps(**base)  # type: ignore[arg-type]
        return RawProps(
            **base,  # type: ignore[arg-type]
            offscreen=node.offscreen,
            focused=node.focused,
            focusable=node.focusable,
            value=node.value,
            checked=node.checked,
            selected=node.selected,
            expanded=node.expanded,
        )

    def children(self) -> Sequence[UiaElement]:
        if self.node.gone:
            raise ElementUnavailable
        return [FakeElement(child, self._backend) for child in self.node.children]

    def scroll_state(self) -> ScrollState | None:
        if self.node.gone:
            raise ElementUnavailable
        if DesktopPattern.SCROLL not in self.node.patterns:
            return None
        return ScrollState(vertically_scrollable=self.node.scrollable, vertical_percent=self.node.scroll_percent)

    def scroll(self, step: ScrollStep) -> None:
        if self.node.gone:
            raise ElementUnavailable
        self.node.scroll_calls.append(step)
        if not self.node.scroll_moves or self.node.scroll_percent is None:
            return
        delta = {ScrollStep.SMALL_DOWN: 10.0, ScrollStep.SMALL_UP: -10.0,
                 ScrollStep.PAGE_DOWN: 40.0, ScrollStep.PAGE_UP: -40.0}[step]
        self.node.scroll_percent = max(0.0, min(100.0, self.node.scroll_percent + delta))

    # -- S4: ValuePattern, SelectionItem, Invoke ---------------------------------------------

    def value_state(self) -> ValueState | None:
        if self.node.gone:
            raise ElementUnavailable
        if DesktopPattern.VALUE not in self.node.patterns:
            return None
        return ValueState(read_only=self.node.read_only, value=self.node.value)

    def set_value(self, value: str) -> None:
        if self.node.gone:
            raise ElementUnavailable
        self.node.set_value_calls.append(value)
        if not self.node.set_value_ignored:
            self.node.value = value

    def select(self) -> None:
        if self.node.gone:
            raise ElementUnavailable
        self.node.select_calls += 1
        if not self.node.select_ignored:
            self.node.selected = True

    def invoke(self) -> None:
        if self.node.gone:
            raise ElementUnavailable
        self.node.invoke_calls += 1
        if self.node.invoke_renames_to is not None:
            self.node.name = self.node.invoke_renames_to


class FakeBackend:
    """A scripted `UiaBackend`: one tree per window handle."""

    def __init__(self) -> None:
        self.trees: dict[int, Node] = {}
        self.reads = 0
        self.on_read: Callable[[int], object] | None = None
        self.raise_on_root: Exception | None = None
        self.focus_calls: list[int] = []
        self.on_focus: Callable[[int], object] | None = None

    def root_for_window(self, hwnd: int) -> UiaElement:
        if self.raise_on_root is not None:
            raise self.raise_on_root
        tree = self.trees.get(hwnd)
        if tree is None:
            raise ElementUnavailable
        return FakeElement(tree, self, hwnd)


def window_tree(title: str = "A window", *children: Node) -> Node:
    return Node(control_type="Window", name=title, children=list(children), patterns=frozenset({DesktopPattern.WINDOW}))


# ---- a scripted effect platform ---------------------------------------------------


class FakePlatform:
    """A scripted `EffectPlatform` over a `FakeProbe`.

    Records every effect it is asked for, so a test can prove that a refused request performed none.
    """

    def __init__(self, probe: FakeProbe, backend: FakeBackend, *, worker_pid: int) -> None:
        self.probe = probe
        self.backend = backend
        self.worker_pid = worker_pid
        self.foreground: int | None = None
        self.input_tick = 1000
        #: When False the OS refuses to change the foreground (the foreground lock).
        self.allow_foreground = True
        self.foreground_calls: list[int] = []
        self.spawned: list[tuple[str, tuple[str, ...]]] = []
        self.next_pid = 9000
        self.spawn_error: Exception | None = None
        #: Whether a spawned application's window appears (a slow start is `False`).
        self.spawn_shows_window = True
        self.spawn_created = 500
        self.on_foreground: Callable[[int], object] | None = None
        backend.on_focus = self._on_focus

    def foreground_hwnd(self) -> int | None:
        return self.foreground

    def last_input_tick(self) -> int:
        return self.input_tick

    def _on_focus(self, hwnd: int) -> None:
        self.foreground_calls.append(hwnd)
        if self.on_foreground is not None:
            self.on_foreground(hwnd)
        if self.allow_foreground:
            self.foreground = hwnd

    def process_path(self, pid: int) -> str | None:
        process = self.probe.processes.get(pid)
        return process.path if process and process.alive else None

    def spawn(self, app: "RegisteredApp") -> int:
        self.spawned.append((app.executable, app.args))
        if self.spawn_error is not None:
            raise self.spawn_error
        pid = self.next_pid
        self.next_pid += 1
        self.probe.add_process(
            pid, created=self.spawn_created, image=app.image, parent=self.worker_pid, path=app.executable
        )
        if self.spawn_shows_window:
            hwnd = 50_000 + pid
            self.probe.add_window(hwnd, pid, title=f"{app.label} window")
            self.backend.trees[hwnd] = window_tree(f"{app.label} window", Node(control_type="Edit", name="Body"))
        return pid
