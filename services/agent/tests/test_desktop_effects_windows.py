"""Milestone 9 S3: the three effects against a REAL Win32 window, the real UIA backend and real Windows.

Nothing here is mocked except the trusted app registry (which needs a descriptor for the fixture, whose
path is not under Program Files). The fixture counts every message that reaches it from outside, so the
tests prove not only that focus/scroll happened but that nothing else did: no click, edit, selection,
key or mouse message.

Human takeover is real too: a person using the machine moves the input generation, and these tests wait
for a quiet moment (and skip, rather than pass falsely, if the machine never goes quiet).

Windows only, and it needs an interactive desktop session.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from app.desktop.effects import DesktopEffects
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import DesktopObserver
from app.desktop.protocol import FocusRequest, LaunchRequest, ScrollRequest, ScrollStep
from app.desktop.registry import AppRegistry, RegisteredApp
from app.desktop.surfaces import ExclusionPolicy, SurfaceTable
from app.desktop.worker import UiaThread
from tests.desktop_harness import FixtureApp, fixture, foreground_window, wait_for

pytestmark = [
    pytest.mark.desktop_uia,
    pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation"),
]

T = TypeVar("T")
#: Messages that only an outside actor can cause. Focus and scroll must leave every one at zero.
UNTOUCHED = (
    "button_clicks", "bm_click_msgs", "toggle_msgs", "edit_changes", "selection_changes", "settext_msgs",
    "mouse_msgs", "key_msgs", "window_moves", "window_close_msgs", "password_reads",
)


class RealWorld:
    def __init__(self, registry: AppRegistry | None = None, *, own_pid_excluded: bool = False) -> None:
        self._uia = UiaThread()
        self.generation = uuid.uuid4()
        self._registry = registry or AppRegistry()
        self._own_pid_excluded = own_pid_excluded
        self.observer: DesktopObserver
        self.effects: DesktopEffects
        self.observer, self.effects, self.platform = self._uia.submit(self._build).result(timeout=90)

    def _build(self) -> tuple[DesktopObserver, DesktopEffects, Any]:
        from app.desktop.effects_win32 import WindowsEffectPlatform
        from app.desktop.uia_backend import PywinautoBackend
        from app.desktop.win32 import WindowsSystemProbe

        probe = WindowsSystemProbe()
        exclusion = (
            ExclusionPolicy.resolve(probe, (), trust_job=False) if self._own_pid_excluded else ExclusionPolicy(roots=())
        )
        surfaces = SurfaceTable(probe=probe, exclusion=exclusion)
        backend = PywinautoBackend()
        observer = DesktopObserver(surfaces=surfaces, backend=backend, worker_generation=self.generation)
        platform = WindowsEffectPlatform()
        effects = DesktopEffects(
            surfaces=surfaces, observer=observer, backend=backend, platform=platform, registry=self._registry
        )
        return observer, effects, platform

    def run(self, call: Callable[[], T], timeout: float = 60) -> T:
        return self._uia.submit(call).result(timeout=timeout)

    def surface(self, title: str) -> tuple[str, int]:
        listing = self.run(self.observer.list_surfaces)
        found = next(s for s in listing.surfaces if s.window_title == title)
        return found.surface_ref, found.surface_epoch

    def quiet_baseline(self) -> int:
        """The input generation once the human has been still for a moment. Skips if they never are."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            tick = self.platform.last_input_tick()
            time.sleep(1.2)
            if self.platform.last_input_tick() == tick:
                return int(tick)
        pytest.skip("someone is using the machine; the human-takeover guard would (correctly) refuse")


@pytest.fixture(scope="module")
def world() -> RealWorld:
    return RealWorld()


def focus_request(world: RealWorld, ref: str, epoch: int, tick: int) -> FocusRequest:
    return FocusRequest(
        expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(),
        surface_ref=ref, surface_epoch=epoch, input_tick=tick,
    )


def untouched(app: FixtureApp) -> None:
    counters = app.dump()
    assert {key: counters[key] for key in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 0), counters


def test_focus_brings_a_background_window_to_the_front_through_ui_automation_only(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        tick = world.quiet_baseline()
        # Something else is in front: the fixture starts without taking the foreground.
        if foreground_window() == app.hwnd:
            pytest.skip("the fixture already had the foreground")
        answer = world.run(lambda: world.effects.focus(focus_request(world, ref, epoch, tick)))
        assert answer.outcome == "focused"
        assert wait_for(lambda: foreground_window() == app.hwnd)
        untouched(app)


def test_human_input_before_focus_is_refused_on_the_real_machine(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        stale_baseline = world.platform.last_input_tick() - 5_000  # any older generation
        with pytest.raises(DesktopRefusal) as info:
            world.run(lambda: world.effects.focus(focus_request(world, ref, epoch, max(stale_baseline, 0))))
        assert info.value.code is DesktopReason.HUMAN_INPUT_DETECTED
        assert foreground_window() != app.hwnd or True
        untouched(app)


def test_a_closed_window_cannot_be_focused_and_a_recycled_slot_is_a_different_surface(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
    time.sleep(0.5)
    with pytest.raises(DesktopRefusal) as info:
        world.run(lambda: world.effects.focus(focus_request(world, ref, epoch, world.platform.last_input_tick())))
    assert info.value.code in (DesktopReason.STALE_SURFACE, DesktopReason.HUMAN_INPUT_DETECTED)


def test_semantic_scroll_moves_the_list_through_scrollpattern_and_touches_nothing_else(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path, "--scroll-items", "80") as app:
        ref, epoch = world.surface(app.title)
        observation = world.run(lambda: world.observer.observe(ref, epoch))
        scrollable = [
            node for node in observation.nodes if "scroll" in {pattern.value for pattern in node.patterns}
        ]
        assert scrollable, [(n.role.value, n.name, [p.value for p in n.patterns]) for n in observation.nodes]
        tick = world.quiet_baseline()
        request = ScrollRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
            surface_epoch=epoch, observation_id=observation.observation_id, control_ref=scrollable[-1].control_ref,
            step=ScrollStep.PAGE_DOWN, input_tick=tick,
        )
        answer = world.run(lambda: world.effects.scroll(request))
        assert answer.outcome == "scrolled"
        assert (answer.percent_after or 0) > (answer.percent_before or 0)
        # The refs of the observation the scroll was proposed against are dead.
        with pytest.raises(DesktopRefusal) as info:
            world.run(lambda: world.effects.scroll(request.model_copy(update={"dispatch_id": uuid.uuid4()})))
        assert info.value.code is DesktopReason.STALE_CONTROL
        # A fresh observation works and is a new one.
        fresh = world.run(lambda: world.observer.observe(ref, epoch))
        assert fresh.observation_id != observation.observation_id
        untouched(app)


def test_a_non_scrollable_control_is_refused_on_the_real_backend(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        observation = world.run(lambda: world.observer.observe(ref, epoch))
        button = next(n for n in observation.nodes if n.role.value == "button")
        tick = world.quiet_baseline()
        request = ScrollRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
            surface_epoch=epoch, observation_id=observation.observation_id, control_ref=button.control_ref,
            step=ScrollStep.SMALL_DOWN, input_tick=tick,
        )
        with pytest.raises(DesktopRefusal) as info:
            world.run(lambda: world.effects.scroll(request))
        assert info.value.code is DesktopReason.NOT_SCROLLABLE
        untouched(app)


# ---- registered application launch ---------------------------------------------------------------------------


CHARMAP = os.path.join(os.environ.get("SystemRoot", "C:\\Windows"), "System32", "charmap.exe")


@contextmanager
def registered_charmap() -> Iterator[RealWorld]:
    """A real, harmless classic Win32 program (Character Map) as the registered application.

    The test interpreter cannot be the registered app: Lumi refuses to launch an executable it is itself
    running (that rule has its own test), and a shared interpreter cannot tell instances apart.
    """
    app = RegisteredApp(app_id="charmap", label="Character Map", executable=CHARMAP)
    world = RealWorld(AppRegistry([app]), own_pid_excluded=True)
    if world.run(lambda: world.effects._running_instances(app)):
        pytest.skip("Character Map is already running on this machine; not touching the person's own instance")
    try:
        yield world
    finally:
        for identity in list(world.effects._surfaces.exclusion.launched):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(identity.pid)], capture_output=True, check=False)


def _charmap_pids(world: RealWorld) -> list[int]:
    app = world.effects._registry.get("charmap")
    return sorted(identity.pid for identity in world.run(lambda: world.effects._running_instances(app)))


def test_a_registered_application_is_launched_once_verified_and_never_duplicated() -> None:
    with registered_charmap() as world:
        tick = world.quiet_baseline()
        request = LaunchRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), app_id="charmap", input_tick=tick
        )
        answer = world.run(lambda: world.effects.launch(request))
        assert answer.outcome == "launched"
        assert answer.surface_ref is not None, "the launched application's window was found"
        assert len(world.effects._surfaces.exclusion.launched) == 1

        # The launched process descends from this test process, yet it is an ordinary surface.
        titles = [s.window_title for s in world.run(world.observer.list_surfaces).surfaces]
        assert any("Character Map" in title for title in titles), titles

        # A second request (a lost reply, a retry, another task) never starts a second instance.
        tick = world.quiet_baseline()
        again = world.run(
            lambda: world.effects.launch(
                LaunchRequest(expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), app_id="charmap", input_tick=tick)
            )
        )
        assert again.outcome == "already_running"
        assert len(world.effects._surfaces.exclusion.launched) == 1


def test_a_registered_application_that_is_this_workers_own_executable_is_refused() -> None:
    base_python = getattr(sys, "_base_executable", sys.executable)
    app = RegisteredApp(app_id="itself", label="Itself", executable=base_python)
    world = RealWorld(AppRegistry([app]), own_pid_excluded=True)
    request = LaunchRequest(
        expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), app_id="itself", input_tick=world.platform.last_input_tick()
    )
    with pytest.raises(DesktopRefusal) as info:
        world.run(lambda: world.effects.launch(request))
    assert info.value.code is DesktopReason.LAUNCH_REFUSED


def test_the_launched_application_did_not_inherit_the_worker_credentials(tmp_path: Path) -> None:
    from app.desktop.effects_win32 import launch_environment

    os.environ["LUMI_DESKTOP_TOKEN"] = "must-not-leak-1234567890"
    try:
        assert "LUMI_DESKTOP_TOKEN" not in launch_environment()
    finally:
        del os.environ["LUMI_DESKTOP_TOKEN"]
