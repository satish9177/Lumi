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

from app.desktop.observer import ElementUnavailable, RawProps, UiaElement
from app.desktop.protocol import CheckedState, DesktopPattern
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
    ) -> FakeProcess:
        process = FakeProcess(pid=pid, created=created, image=image, integrity=integrity, parent=parent)
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


class FakeElement:
    def __init__(self, node: Node, backend: "FakeBackend") -> None:
        self.node = node
        self._backend = backend

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


class FakeBackend:
    """A scripted `UiaBackend`: one tree per window handle."""

    def __init__(self) -> None:
        self.trees: dict[int, Node] = {}
        self.reads = 0
        self.on_read: Callable[[int], object] | None = None
        self.raise_on_root: Exception | None = None

    def root_for_window(self, hwnd: int) -> UiaElement:
        if self.raise_on_root is not None:
            raise self.raise_on_root
        tree = self.trees.get(hwnd)
        if tree is None:
            raise ElementUnavailable
        return FakeElement(tree, self)


def window_tree(title: str = "A window", *children: Node) -> Node:
    return Node(control_type="Window", name=title, children=list(children), patterns=frozenset({DesktopPattern.WINDOW}))
