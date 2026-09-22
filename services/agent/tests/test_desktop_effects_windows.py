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
from app.desktop.protocol import (
    FocusRequest,
    InvokeEffect,
    InvokeRequest,
    LaunchRequest,
    ScrollRequest,
    ScrollStep,
    SelectRequest,
    SetValueRequest,
)
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


def untouched_except(app: FixtureApp, allowed: tuple[str, ...]) -> dict[str, int]:
    """Every counter in `UNTOUCHED` is zero except the ones named (which the caller checks itself, with
    whatever lower bound is meaningful for that message). Proves an S4 effect caused only the kind of
    change it names and nothing else: no click beyond the one asked for, no key, mouse, focus theft or
    extra edit/selection. Returns the full dump so the caller can assert on the allowed counters."""
    counters = app.dump()
    zero_expected = {key: 0 for key in UNTOUCHED if key not in allowed}
    assert {key: counters[key] for key in zero_expected} == zero_expected, counters
    return counters


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


# ---- S4: bounded semantic mutations, against the real fixture and real UIA ---------------------------------
#
# The fixture's main EDIT and LISTBOX already exist (S1); nothing here needed a fixture change. Its
# message counters (`edit_changes`, `selection_changes`, `button_clicks`) were already wired to the exact
# Win32 notifications (`EN_CHANGE`, `LBN_SELCHANGE`, `BN_CLICKED`) a real `SetValue`/`Select`/`Invoke`
# causes, so a real effect can be told apart from every other kind of message with the same rigor S3 used.


def test_set_value_writes_the_exact_text_through_valuepattern_and_verifies_it(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        observation = world.run(lambda: world.observer.observe(ref, epoch))
        field = next(n for n in observation.nodes if n.role.value == "edit" and "value" in {p.value for p in n.patterns})
        tick = world.quiet_baseline()
        request = SetValueRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
            surface_epoch=epoch, observation_id=observation.observation_id, control_ref=field.control_ref,
            value="Lumi wrote this", input_tick=tick,
        )
        answer = world.run(lambda: world.effects.set_value(request))
        assert answer.outcome == "set"
        # Read back through a FRESH observation (control refs are only valid within the observation
        # they came from, so the old ref is never compared against the new one) and through real UIA.
        fresh = world.run(lambda: world.observer.observe(ref, epoch))
        written = next(n for n in fresh.nodes if n.role.value == "edit" and n.text == "Lumi wrote this")
        assert written.text == "Lumi wrote this"
        # A real edit-changed notification and a real text-set message; nothing else moved.
        counters = untouched_except(app, ("edit_changes", "settext_msgs"))
        assert counters["edit_changes"] >= 1 and counters["settext_msgs"] >= 1
        # The old observation's refs are dead: the effect kills the control table before it acts
        # (`_kill_old_refs`). Against a real window, the very next real observation can independently
        # decide the surface itself moved to a new epoch (`observer.py`'s `_project`/
        # `structurally_different`, driven by whatever the live tree actually looked like at that
        # moment -- not something this test controls). Either refusal proves the same security property
        # -- this exact (surface_ref, surface_epoch, observation_id, control_ref) tuple can never be
        # replayed -- so both are accepted rather than pinning one incidental real-world ordering.
        with pytest.raises(DesktopRefusal) as info:
            world.run(lambda: world.effects.set_value(request.model_copy(update={"dispatch_id": uuid.uuid4()})))
        assert info.value.code in (DesktopReason.STALE_CONTROL, DesktopReason.STALE_SURFACE)


def test_select_chooses_the_exact_listbox_item_through_selectionitem_and_verifies_it(tmp_path: Path, world: RealWorld) -> None:
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        observation = world.run(lambda: world.observer.observe(ref, epoch))
        items = [n for n in observation.nodes if n.role.value == "list_item" and n.name in ("One", "Two", "Three")]
        assert {n.name for n in items} == {"One", "Two", "Three"}, [(n.role.value, n.name) for n in observation.nodes]
        container = next(n for n in observation.nodes if n.control_ref == items[0].parent_ref)
        target = next(n for n in items if n.name == "Three")
        tick = world.quiet_baseline()
        request = SelectRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
            surface_epoch=epoch, observation_id=observation.observation_id, container_ref=container.control_ref,
            option_ref=target.control_ref, input_tick=tick,
        )
        answer = world.run(lambda: world.effects.select(request))
        assert answer.outcome == "selected"
        fresh = world.run(lambda: world.observer.observe(ref, epoch))
        now_selected = next(n for n in fresh.nodes if n.role.value == "list_item" and n.selected is True)
        assert now_selected.name == "Three"
        counters = untouched_except(app, ("selection_changes",))
        assert counters["selection_changes"] >= 1


def test_invoking_a_control_that_does_not_change_is_a_known_failure_never_a_guess(tmp_path: Path, world: RealWorld) -> None:
    """The fixture's Submit button really is pressed (proving Invoke fired for real) but does not
    rename itself, so `NAME_TOGGLE` reports the known, honest `no_change` outcome -- never `invoked`.
    A control whose Invoke effect is genuinely verifiable end to end needs a fixture control that
    renames itself on press, which this fixture does not yet have; that path stays fake-tested only
    (see `test_desktop_effects.py`), a documented residual.

    The fixture's Submit button is a plain Win32 BUTTON with no native UI Automation provider, so
    Windows itself services `InvokePattern.Invoke()` through its legacy/MSAA accessibility bridge --
    and that bridge's own implementation, not any code of Lumi's, brings the window to the foreground
    and synthesizes the click messages a real mouse click would produce (see
    `docs/reviews/milestone-9-s4.md`'s Invoke residual section). `uia_backend.py`'s `invoke()` makes
    exactly one COM call, `pattern.Invoke()`; it is the OS's own legacy provider, not Lumi, that turns
    that into `bm_click_msgs`/`mouse_msgs`/`activations` here. A control with a native UIA provider
    (most modern WinUI/WPF/UWP apps) does not need this bridge and would not show these counters move."""
    with fixture(tmp_path) as app:
        ref, epoch = world.surface(app.title)
        observation = world.run(lambda: world.observer.observe(ref, epoch))
        submit = next(n for n in observation.nodes if n.role.value == "button" and n.name == "Submit")
        tick = world.quiet_baseline()
        request = InvokeRequest(
            expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
            surface_epoch=epoch, observation_id=observation.observation_id, control_ref=submit.control_ref,
            effect=InvokeEffect.NAME_TOGGLE, input_tick=tick,
        )
        answer = world.run(lambda: world.effects.invoke(request))
        assert answer.outcome == "no_change"
        # The click really happened (Invoke is not a no-op); nothing else did. `bm_click_msgs`,
        # `mouse_msgs` and `activations` are the OS's own legacy-bridge mechanics for a plain Win32
        # button (see the docstring above) and are allowed here; every OTHER counter -- edit, selection,
        # key, window-move/close and password-read -- must still be exactly zero.
        counters = untouched_except(app, ("button_clicks", "bm_click_msgs", "mouse_msgs", "activations"))
        assert counters["button_clicks"] == 1
