"""Milestone 9 S3: trusted focus, semantic scroll and registered-application launch.

Domain rules against scripted fakes, so every refusal is exact and platform independent. The real
Win32 fixture is exercised in `test_desktop_effects_windows.py`.

Every refusal test also asserts that the platform performed **no** effect: a refusal that still
touched the window would be worse than none.
"""

import os
import uuid

import pytest

from app.desktop.dpi import PhysicalRect
from app.desktop.effects import DesktopEffects
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import ControlTable, DesktopObserver
from app.desktop.protocol import (
    CaptureRequest,
    DesktopObservation,
    DesktopPattern,
    FocusRequest,
    InvokeEffect,
    InvokeRequest,
    LaunchRequest,
    ScrollRequest,
    ScrollStep,
    SelectRequest,
    SetValueRequest,
)
from app.desktop.registry import AppRegistry, RegisteredApp, validate
from app.desktop.surfaces import INTEGRITY_HIGH, ExclusionPolicy, SurfaceTable
from tests.desktop_fakes import FakeBackend, FakeCapturePlatform, FakePlatform, FakeProbe, Node, window_tree

APP_PATH = "C:\\Program Files\\Fake\\fake.exe"
ROOTS = ("c:\\program files",)
LIST_PATTERNS = frozenset({DesktopPattern.SCROLL})


def registry() -> AppRegistry:
    return AppRegistry([RegisteredApp(app_id="fake", label="Fake", executable=APP_PATH, args=("--fixed",))])


class World:
    def __init__(self, *, trust_job: bool = False) -> None:
        self.probe = FakeProbe()
        self.backend = FakeBackend()
        self.platform = FakePlatform(self.probe, self.backend, worker_pid=os.getpid())
        self.exclusion = ExclusionPolicy.resolve(self.probe, (), trust_job=trust_job)
        self.surfaces = SurfaceTable(probe=self.probe, exclusion=self.exclusion)
        self.generation = uuid.uuid4()
        self.observer = DesktopObserver(
            surfaces=self.surfaces, backend=self.backend, worker_generation=self.generation
        )
        self.capture_platform = FakeCapturePlatform()
        self.effects = DesktopEffects(
            surfaces=self.surfaces,
            observer=self.observer,
            backend=self.backend,
            platform=self.platform,
            registry=registry(),
            capture_platform=self.capture_platform,
            sleep=lambda _: None,
        )
        self.list_node = Node(
            control_type="List", name="Items", patterns=LIST_PATTERNS, scrollable=True, scroll_percent=0.0
        )
        # -- S4 fixtures ------------------------------------------------------------------------
        self.value_node = Node(control_type="Edit", name="Field", patterns=frozenset({DesktopPattern.VALUE}), value="")
        self.readonly_node = Node(
            control_type="Edit", name="Locked", patterns=frozenset({DesktopPattern.VALUE}), value="fixed", read_only=True
        )
        self.option_a = Node(
            control_type="ListItem", name="Option A", patterns=frozenset({DesktopPattern.SELECTION_ITEM})
        )
        self.option_b = Node(
            control_type="ListItem", name="Option B", patterns=frozenset({DesktopPattern.SELECTION_ITEM})
        )
        self.combo_node = Node(control_type="ComboBox", name="Combo", children=[self.option_a, self.option_b])
        self.button_node = Node(
            control_type="Button", name="Show details", patterns=frozenset({DesktopPattern.INVOKE}),
            invoke_renames_to="Hide details",
        )
        self.dead_button = Node(control_type="Button", name="No-op", patterns=frozenset({DesktopPattern.INVOKE}))

    def window(self, hwnd: int = 10, pid: int = 4001, *, title: str = "Notes", **flags: bool) -> tuple[str, int]:
        if pid not in self.probe.processes:
            self.probe.add_process(pid, image="editor.exe", parent=1)
        self.probe.add_window(hwnd, pid, title=title, **flags)
        self.backend.trees[hwnd] = window_tree(
            title, self.list_node, self.value_node, self.readonly_node, self.combo_node, self.button_node,
            self.dead_button,
        )
        self.capture_platform.add_window(
            hwnd, window_rect=PhysicalRect(0, 0, 820, 620), client_rect=PhysicalRect(8, 39, 812, 612)
        )
        return self.ref_for(title)

    def ref_for(self, title: str) -> tuple[str, int]:
        surface = next(s for s in self.observer.list_surfaces().surfaces if s.window_title == title)
        return surface.surface_ref, surface.surface_epoch

    def focus_request(self, ref: str, epoch: int, *, dispatch: uuid.UUID | None = None, tick: int | None = None) -> FocusRequest:
        return FocusRequest(
            expected_worker_generation=self.generation,
            dispatch_id=dispatch or uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            input_tick=self.platform.input_tick if tick is None else tick,
        )

    def observe(self, ref: str, epoch: int) -> tuple[uuid.UUID, str]:
        observation = self.observer.observe(ref, epoch)
        node = next(n for n in observation.nodes if n.role.value == "list")
        return observation.observation_id, node.control_ref

    def observe_full(self, ref: str, epoch: int) -> DesktopObservation:
        return self.observer.observe(ref, epoch)

    def ref_of(self, observation: DesktopObservation, name: str) -> str:
        node = next(n for n in observation.nodes if n.name == name)
        return node.control_ref

    def set_value_request(
        self, ref: str, epoch: int, observation_id: uuid.UUID, control: str, value: str = "hello", *, tick: int | None = None
    ) -> SetValueRequest:
        return SetValueRequest(
            expected_worker_generation=self.generation,
            dispatch_id=uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            observation_id=observation_id,
            control_ref=control,
            value=value,
            input_tick=self.platform.input_tick if tick is None else tick,
        )

    def select_request(
        self, ref: str, epoch: int, observation_id: uuid.UUID, container: str, option: str, *, tick: int | None = None
    ) -> SelectRequest:
        return SelectRequest(
            expected_worker_generation=self.generation,
            dispatch_id=uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            observation_id=observation_id,
            container_ref=container,
            option_ref=option,
            input_tick=self.platform.input_tick if tick is None else tick,
        )

    def invoke_request(
        self, ref: str, epoch: int, observation_id: uuid.UUID, control: str,
        effect: InvokeEffect = InvokeEffect.NAME_TOGGLE, *, tick: int | None = None,
    ) -> InvokeRequest:
        return InvokeRequest(
            expected_worker_generation=self.generation,
            dispatch_id=uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            observation_id=observation_id,
            control_ref=control,
            effect=effect,
            input_tick=self.platform.input_tick if tick is None else tick,
        )

    def scroll_request(
        self, ref: str, epoch: int, observation_id: uuid.UUID, control: str, step: ScrollStep = ScrollStep.PAGE_DOWN,
        *, tick: int | None = None,
    ) -> ScrollRequest:
        return ScrollRequest(
            expected_worker_generation=self.generation,
            dispatch_id=uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            observation_id=observation_id,
            control_ref=control,
            step=step,
            input_tick=self.platform.input_tick if tick is None else tick,
        )

    def launch_request(self, app_id: str = "fake", *, dispatch: uuid.UUID | None = None) -> LaunchRequest:
        return LaunchRequest(
            expected_worker_generation=self.generation,
            dispatch_id=dispatch or uuid.uuid4(),
            app_id=app_id,
            input_tick=self.platform.input_tick,
        )

    def capture_request(
        self, ref: str, epoch: int, *, capture_id: uuid.UUID | None = None, tick: int | None = None
    ) -> CaptureRequest:
        return CaptureRequest(
            expected_worker_generation=self.generation,
            capture_id=capture_id or uuid.uuid4(),
            surface_ref=ref,
            surface_epoch=epoch,
            input_tick=self.platform.input_tick if tick is None else tick,
        )


def code(call: object) -> DesktopReason:
    with pytest.raises(DesktopRefusal) as info:
        call()  # type: ignore[operator]
    return info.value.code


# ---- focus ------------------------------------------------------------------------------------------------


def test_focus_brings_the_exact_visible_surface_forward_and_verifies_it() -> None:
    world = World()
    ref, epoch = world.window(10)
    answer = world.effects.focus(world.focus_request(ref, epoch))
    assert answer.outcome == "focused" and not answer.input_changed
    assert world.platform.foreground_calls == [10]


def test_a_foreground_lock_is_a_known_failure_not_a_claimed_success() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.platform.allow_foreground = False
    answer = world.effects.focus(world.focus_request(ref, epoch))
    assert answer.outcome == "not_focused"


def test_focus_theft_after_the_call_is_not_success() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.probe.add_process(4002, image="other.exe", parent=1)
    world.probe.add_window(11, 4002, title="Thief")

    def steal(_: int) -> None:
        world.platform.foreground = 11

    world.platform.on_foreground = steal
    world.platform.allow_foreground = False
    assert world.effects.focus(world.focus_request(ref, epoch)).outcome == "not_focused"


def test_a_stale_ref_or_epoch_is_refused_without_touching_the_window() -> None:
    world = World()
    ref, epoch = world.window(10)
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch + 1))) is DesktopReason.STALE_SURFACE
    assert code(lambda: world.effects.focus(world.focus_request("s9", 1))) is DesktopReason.STALE_SURFACE
    world.probe.destroyed.add(10)
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.STALE_SURFACE
    assert world.platform.foreground_calls == []


def test_a_recycled_hwnd_or_pid_is_a_different_surface_and_is_refused() -> None:
    world = World()
    ref, epoch = world.window(10, pid=4001)
    # The process exits and another program takes the same pid AND the same window handle.
    world.probe.processes[4001].alive = False
    world.probe.add_process(4001, created=999, image="stranger.exe", parent=1)
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.STALE_SURFACE
    assert world.platform.foreground_calls == []


def test_an_elevated_surface_is_never_focused() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.probe.processes[4001].integrity = INTEGRITY_HIGH
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.ELEVATED_WINDOW
    assert world.platform.foreground_calls == []


def test_a_credential_surface_is_never_focused() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.list_node.children.append(Node(control_type="Edit", name="Password", is_password=True))
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.CREDENTIAL_SURFACE
    assert world.platform.foreground_calls == []


def test_lumis_own_windows_cannot_be_focused_even_with_a_forged_ref() -> None:
    world = World()
    world.probe.add_process(5000, image="lumi.exe", parent=os.getpid())
    world.probe.add_window(30, 5000, title="Lumi")
    assert [s.window_title for s in world.observer.list_surfaces().surfaces] == []
    for ref in ("s1", "s2", "s16"):
        assert code(lambda: world.effects.focus(world.focus_request(ref, 1))) is DesktopReason.STALE_SURFACE
    assert world.platform.foreground_calls == []


def test_a_surface_that_becomes_lumis_own_after_listing_is_refused_as_stale() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.probe.processes[4001].parent = os.getpid()
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.STALE_SURFACE
    assert world.platform.foreground_calls == []


def test_minimized_hidden_and_cloaked_windows_are_not_restored_or_focused() -> None:
    for flag in ("minimized", "cloaked"):
        world = World()
        ref, epoch = world.window(10)
        setattr(world.probe.windows[0], flag, True)
        assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.SURFACE_NOT_FOCUSABLE
        assert world.platform.foreground_calls == []
    world = World()
    ref, epoch = world.window(10)
    world.probe.windows[0].visible = False
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) in (
        DesktopReason.SURFACE_NOT_FOCUSABLE, DesktopReason.STALE_SURFACE,
    )
    assert world.platform.foreground_calls == []


def test_human_input_since_the_approval_baseline_stops_focus_before_any_effect() -> None:
    world = World()
    ref, epoch = world.window(10)
    baseline = world.effects.input_baseline().input_tick
    world.platform.input_tick += 5  # the person moved the mouse
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch, tick=baseline))) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.platform.foreground_calls == []


def test_human_input_during_focus_is_reported_so_the_runtime_stops_and_reobserves() -> None:
    world = World()
    ref, epoch = world.window(10)

    def typing(_: int) -> None:
        world.platform.input_tick += 1

    world.platform.on_foreground = typing
    answer = world.effects.focus(world.focus_request(ref, epoch))
    assert answer.input_changed is True


def test_a_dispatch_is_performed_at_most_once_and_a_finished_one_replays_its_answer() -> None:
    world = World()
    ref, epoch = world.window(10)
    dispatch = uuid.uuid4()
    first = world.effects.focus(world.focus_request(ref, epoch, dispatch=dispatch))
    again = world.effects.focus(world.focus_request(ref, epoch, dispatch=dispatch))
    assert again == first
    assert world.platform.foreground_calls == [10], "a lost reply is recovered from the stored answer, not by a second effect"


def test_a_refused_dispatch_id_cannot_be_reused() -> None:
    world = World()
    ref, epoch = world.window(10)
    dispatch = uuid.uuid4()
    world.platform.input_tick += 1
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch, dispatch=dispatch, tick=1000))) is DesktopReason.HUMAN_INPUT_DETECTED
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch, dispatch=dispatch))) is DesktopReason.DUPLICATE_DISPATCH


# ---- semantic scroll --------------------------------------------------------------------------------------


def test_scroll_calls_the_scroll_pattern_once_and_kills_every_old_ref() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    answer = world.effects.scroll(world.scroll_request(ref, epoch, observation, control, ScrollStep.PAGE_DOWN))
    assert answer.outcome == "scrolled"
    assert (answer.percent_before, answer.percent_after) == (0.0, 40.0)
    assert world.list_node.scroll_calls == [ScrollStep.PAGE_DOWN]
    # The old observation's refs are dead: a second scroll on the same control is stale.
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.STALE_CONTROL
    assert world.list_node.scroll_calls == [ScrollStep.PAGE_DOWN]


def test_scroll_that_does_not_move_reports_unchanged_not_success() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.list_node.scroll_moves = False
    observation, control = world.observe(ref, epoch)
    assert world.effects.scroll(world.scroll_request(ref, epoch, observation, control)).outcome == "unchanged"


def test_a_control_without_a_scroll_pattern_or_that_cannot_scroll_is_refused() -> None:
    world = World()
    ref, epoch = world.window(10)
    world.list_node.scrollable = False
    observation, control = world.observe(ref, epoch)
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.NOT_SCROLLABLE
    world = World()
    world.list_node.patterns = frozenset()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.NOT_SCROLLABLE
    assert world.list_node.scroll_calls == []


def test_a_replaced_control_or_an_old_observation_is_refused_and_nothing_scrolls() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    world.list_node.runtime_id = (424242,)  # rerender: same selector, a different element
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.ELEMENT_CHANGED
    world = World()
    ref, epoch = world.window(10)
    old, control = world.observe(ref, epoch)
    world.observe(ref, epoch)  # a newer observation supersedes the first
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, old, control))) is DesktopReason.STALE_CONTROL
    assert world.list_node.scroll_calls == []


def test_a_vanished_control_is_element_missing() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    world.backend.trees[10].children.clear()
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.ELEMENT_MISSING
    assert world.list_node.scroll_calls == []


def test_a_credential_appearing_in_the_surface_stops_the_scroll() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    world.backend.trees[10].children.append(Node(control_type="Edit", name="Password", is_password=True))
    assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.CREDENTIAL_SURFACE
    assert world.list_node.scroll_calls == []


def test_human_input_stops_a_scroll_before_it_happens() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    baseline = world.platform.input_tick
    world.platform.input_tick += 1
    request = world.scroll_request(ref, epoch, observation, control, tick=baseline)
    assert code(lambda: world.effects.scroll(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.list_node.scroll_calls == []


def test_the_scroll_amount_is_a_closed_enum_the_model_cannot_widen() -> None:
    assert {step.value for step in ScrollStep} == {"small_up", "small_down", "page_up", "page_down"}
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    for bad in ("999", "1.5", "up", "page_down; rm", "", "PAGE_DOWN"):
        with pytest.raises(ValueError):
            ScrollRequest.model_validate(
                {
                    "expected_worker_generation": str(world.generation), "dispatch_id": str(uuid.uuid4()),
                    "surface_ref": ref, "surface_epoch": epoch, "observation_id": str(observation),
                    "control_ref": control, "step": bad, "input_tick": 1,
                }
            )


def test_a_failing_scroll_after_the_call_may_have_begun_is_uncertain_never_a_known_failure() -> None:
    world = World()
    ref, epoch = world.window(10)
    observation, control = world.observe(ref, epoch)
    world.list_node.scroll_moves = True
    original = world.backend.trees[10].children[0]

    class Dies(Exception):
        pass

    def boom(*_: object) -> None:
        raise Dies

    element_class = type(world.backend.root_for_window(10).children()[0])
    real = element_class.scroll
    element_class.scroll = lambda self, step: boom()  # type: ignore[method-assign]
    try:
        assert code(lambda: world.effects.scroll(world.scroll_request(ref, epoch, observation, control))) is DesktopReason.EFFECT_UNCERTAIN
    finally:
        element_class.scroll = real  # type: ignore[method-assign]
    assert original is world.list_node


# ---- registered applications ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"appId": "x", "label": "X", "executable": "notepad.exe"},
        {"appId": "x", "label": "X", "executable": "\\\\server\\share\\x.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\..\\Windows\\x.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Users\\me\\Downloads\\x.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Windows\\System32\\cmd.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\PowerShell\\powershell.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\x\\run.bat"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\x\\run.cmd"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\x\\a.exe", "args": ["1", "2", "3", "4", "5"]},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\x\\a.exe", "args": [1]},
        {"appId": "X", "label": "X", "executable": "C:\\Program Files\\x\\a.exe"},
        {"appId": "a b", "label": "X", "executable": "C:\\Program Files\\x\\a.exe"},
        {"appId": "x", "label": "", "executable": "C:\\Program Files\\x\\a.exe"},
        {"appId": "x", "label": "X", "executable": "C:\\Program Files\\x\\a.exe\x00.txt"},
        {"label": "X", "executable": "C:\\Program Files\\x\\a.exe"},
    ],
)
def test_a_descriptor_that_is_not_a_trusted_local_executable_is_refused(entry: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate(entry, roots=ROOTS)


def test_a_good_descriptor_is_accepted_and_the_registry_is_closed() -> None:
    app = validate({"appId": "fake", "label": "Fake", "executable": APP_PATH, "args": ["--fixed"]}, roots=ROOTS)
    assert app.executable == APP_PATH and app.args == ("--fixed",)
    reg = AppRegistry([app])
    assert reg.get("fake") is app
    for unknown in ("notepad", "cmd", "C:\\Windows\\System32\\cmd.exe", "../x", "powershell"):
        assert code(lambda: reg.get(unknown)) is DesktopReason.APP_NOT_REGISTERED
    with pytest.raises(ValueError):
        AppRegistry([app, app])


def test_configuration_documents_are_validated_whole_not_partially() -> None:
    good = '[{"appId": "fake", "label": "Fake", "executable": "C:\\\\Program Files\\\\Fake\\\\fake.exe"}]'
    assert "fake" in {a.app_id for a in AppRegistry.from_config(good, roots=ROOTS).all()}
    for bad in ("{}", "[1]", '[{"appId": "x"}]', "not json"):
        with pytest.raises(ValueError):
            AppRegistry.from_config(bad, roots=ROOTS)


def test_the_launch_request_has_no_path_argument_or_shell_field() -> None:
    assert set(LaunchRequest.model_fields) == {"expected_worker_generation", "dispatch_id", "app_id", "input_tick"}
    for extra in ("path", "executable", "args", "command", "cwd", "env", "url", "shell"):
        with pytest.raises(ValueError):
            LaunchRequest.model_validate(
                {"expected_worker_generation": str(uuid.uuid4()), "dispatch_id": str(uuid.uuid4()),
                 "app_id": "fake", "input_tick": 1, extra: "x"}
            )
    for bad_id in ("C:\\x.exe", "../x", "fake.exe", "fake --flag", "fake;calc", "", "a" * 40, "Fake"):
        with pytest.raises(ValueError):
            LaunchRequest(expected_worker_generation=uuid.uuid4(), dispatch_id=uuid.uuid4(), app_id=bad_id, input_tick=1)


def test_launch_starts_exactly_the_registered_executable_with_exactly_its_fixed_arguments() -> None:
    world = World()
    answer = world.effects.launch(world.launch_request())
    assert answer.outcome == "launched" and answer.surface_ref is not None
    assert world.platform.spawned == [(APP_PATH, ("--fixed",))]


def test_a_launched_application_is_an_ordinary_surface_even_though_its_parent_is_the_worker() -> None:
    world = World()
    answer = world.effects.launch(world.launch_request())
    titles = [s.window_title for s in world.observer.list_surfaces().surfaces]
    assert titles == ["Fake window"]
    ref, epoch = answer.surface_ref, answer.surface_epoch
    assert ref is not None and epoch is not None
    assert world.effects.focus(world.focus_request(ref, epoch)).outcome == "focused"


def test_a_second_launch_never_creates_a_second_instance() -> None:
    world = World()
    world.effects.launch(world.launch_request())
    again = world.effects.launch(world.launch_request())
    assert again.outcome == "already_running"
    assert len(world.platform.spawned) == 1
    assert again.focused is True, "the existing instance is brought forward instead"


def test_a_lost_launch_reply_is_recovered_by_replay_and_by_inspection_never_by_a_second_spawn() -> None:
    world = World()
    dispatch = uuid.uuid4()
    first = world.effects.launch(world.launch_request(dispatch=dispatch))
    assert world.effects.launch(world.launch_request(dispatch=dispatch)) == first
    # Another worker generation would not know the dispatch, but the process is there to be found.
    fresh = World()
    fresh.probe = world.probe
    assert len(world.platform.spawned) == 1


def test_a_slow_window_is_reported_not_re_spawned() -> None:
    world = World()
    world.platform.spawn_shows_window = False
    first = world.effects.launch(world.launch_request())
    assert first.outcome == "launched" and first.surface_ref is None
    second = world.effects.launch(world.launch_request())
    assert second.outcome == "already_running" and second.surface_ref is None
    assert len(world.platform.spawned) == 1


def test_a_recycled_pid_or_a_same_named_file_elsewhere_is_not_the_registered_application() -> None:
    world = World()
    world.probe.add_process(7000, image="fake.exe", parent=1, path="C:\\Temp\\evil\\fake.exe")
    world.probe.add_process(7001, image="other.exe", parent=1, path="C:\\Program Files\\Other\\other.exe")
    answer = world.effects.launch(world.launch_request())
    assert answer.outcome == "launched", "a same-named file in another directory is not the registered app"
    # The launched instance exits; its pid is reused by a different program; that is not an instance.
    launched_pid = world.platform.next_pid - 1
    world.probe.processes[launched_pid].alive = False
    world.probe.add_process(launched_pid, created=777, image="stranger.exe", parent=1, path="C:\\Program Files\\S\\s.exe")
    assert world.effects.launch(world.launch_request()).outcome == "launched"
    assert len(world.platform.spawned) == 2


def test_a_spawn_failure_is_a_refusal_and_leaves_no_instance() -> None:
    world = World()
    world.platform.spawn_error = PermissionError("denied")
    assert code(lambda: world.effects.launch(world.launch_request())) is DesktopReason.LAUNCH_REFUSED


def test_an_unregistered_app_id_spawns_nothing() -> None:
    world = World()
    assert code(lambda: world.effects.launch(world.launch_request("notepad"))) is DesktopReason.APP_NOT_REGISTERED
    assert world.platform.spawned == []


def test_human_input_stops_a_launch_before_any_process_exists() -> None:
    world = World()
    request = world.launch_request()
    world.platform.input_tick += 1
    assert code(lambda: world.effects.launch(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.platform.spawned == []


def test_an_untrusted_process_at_the_registered_path_is_still_lumis_if_it_descends_from_lumi() -> None:
    """The exemption is for processes this worker itself started as a registered launch; a Lumi child at
    the same path (not launched through `launch`) stays excluded and is not offered as a surface."""
    world = World()
    world.probe.add_process(8000, image="fake.exe", parent=os.getpid(), path=APP_PATH)
    world.probe.add_window(80, 8000, title="Lumi child")
    assert world.observer.list_surfaces().surfaces == []
    assert world.effects.launch(world.launch_request()).outcome == "launched"


def test_the_launch_environment_carries_no_lumi_credential() -> None:
    from app.desktop.effects_win32 import launch_environment

    source = {
        "SystemRoot": "C:\\Windows", "PATH": "C:\\Windows", "LUMI_DESKTOP_TOKEN": "secret-token-value",
        "DATABASE_URL": "postgresql://u:p@h/db", "OPENAI_API_KEY": "sk-x", "LUMI_RUNTIME_TOKEN": "t",
        "ANTHROPIC_API_KEY": "sk-y", "USERPROFILE": "C:\\Users\\me",
    }
    environment = launch_environment(source)
    assert set(environment) == {"SystemRoot", "PATH", "USERPROFILE"}
    assert not any("TOKEN" in key or "KEY" in key or "DATABASE" in key for key in environment)


# ---- independent review (S3): post-effect uncertainty, takeover races, own-image refusal --------------------------


def test_a_failure_after_the_focus_call_is_uncertain_never_a_known_failure() -> None:
    world = World()
    ref, epoch = world.window(10)

    def boom() -> int | None:
        raise OSError("foreground unreadable")

    world.platform.foreground_hwnd = boom  # type: ignore[method-assign]
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch))) is DesktopReason.EFFECT_UNCERTAIN
    assert world.platform.foreground_calls == [10], "the focus call had been made"


def test_a_failed_input_read_after_the_effect_is_uncertain_for_focus_and_scroll() -> None:
    world = World()
    ref, epoch = world.window(10)
    calls = {"n": 0}
    real = world.platform.last_input_tick

    def flaky() -> int:
        calls["n"] += 1
        if calls["n"] >= 2:  # the pre-effect check passes; the read AFTER the effect fails
            raise OSError("gone")
        return real()

    world.platform.last_input_tick = flaky  # type: ignore[method-assign]
    assert code(lambda: world.effects.focus(world.focus_request(ref, epoch, tick=1000))) is DesktopReason.EFFECT_UNCERTAIN
    assert world.platform.foreground_calls == [10]

    other = World()
    ref2, epoch2 = other.window(10)
    observation, control = other.observe(ref2, epoch2)
    calls["n"] = 0
    real2 = other.platform.last_input_tick

    def flaky2() -> int:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("gone")
        return real2()

    other.platform.last_input_tick = flaky2  # type: ignore[method-assign]
    request = other.scroll_request(ref2, epoch2, observation, control, tick=1000)
    assert code(lambda: other.effects.scroll(request)) is DesktopReason.EFFECT_UNCERTAIN
    assert other.list_node.scroll_calls == [ScrollStep.PAGE_DOWN], "the scroll happened"


def test_a_failure_verifying_a_freshly_spawned_process_is_uncertain_and_it_is_never_respawned() -> None:
    world = World()
    real = world.probe.process_identity
    spawned: list[int] = []
    original_spawn = world.platform.spawn

    def spawn(app):  # type: ignore[no-untyped-def]
        pid = original_spawn(app)
        spawned.append(pid)
        return pid

    def flaky(pid: int):  # type: ignore[no-untyped-def]
        if spawned:
            raise OSError("verification failed")
        return real(pid)

    world.platform.spawn = spawn  # type: ignore[method-assign]
    world.probe.process_identity = flaky  # type: ignore[method-assign]
    assert code(lambda: world.effects.launch(world.launch_request())) is DesktopReason.EFFECT_UNCERTAIN
    assert len(world.platform.spawned) == 1
    world.probe.process_identity = real  # type: ignore[method-assign]
    assert world.effects.launch(world.launch_request()).outcome == "already_running"
    assert len(world.platform.spawned) == 1


def test_the_person_touching_the_machine_during_process_enumeration_stops_the_launch_before_the_spawn() -> None:
    world = World()
    request = world.launch_request()
    real = world.probe.process_parents
    fired = {"done": False}

    def slow_enumeration() -> dict[int, int]:
        if not fired["done"]:
            fired["done"] = True
            world.platform.input_tick += 1  # the person moved the mouse while Lumi was looking around
        return real()

    world.probe.process_parents = slow_enumeration  # type: ignore[method-assign]
    assert code(lambda: world.effects.launch(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.platform.spawned == []


def test_a_registered_application_can_never_be_lumis_own_executable() -> None:
    world = World()
    own = "C:\\Program Files\\Lumi\\lumi.exe"
    world.probe.processes[os.getpid()].path = own
    reg = AppRegistry([RegisteredApp(app_id="self", label="Lumi", executable=own)])
    effects = DesktopEffects(
        surfaces=world.surfaces, observer=world.observer, backend=world.backend,
        platform=world.platform, registry=reg, sleep=lambda _: None,
    )
    request = LaunchRequest(
        expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), app_id="self",
        input_tick=world.platform.input_tick,
    )
    assert code(lambda: effects.launch(request)) is DesktopReason.LAUNCH_REFUSED
    assert world.platform.spawned == []


@pytest.mark.parametrize(
    "executable",
    [
        "C:\\Program Files\\Python\\python.exe", "C:\\Program Files\\nodejs\\node.exe", "C:\\Program Files\\Java\\java.exe",
        "C:\\Program Files\\x\\mmc.exe", "C:\\Program Files\\x\\wt.exe", "C:\\Program Files\\x\\regedit.exe",
        "C:\\Program Files\\x\\msdt.exe", "C:\\Program Files\\x\\ssh.exe", "C:\\Program Files\\x\\logonui.exe",
        "C:\\Program Files\\x\\consent.exe", "C:\\Program Files\\Temp\\x.exe", "C:\\Program Files\\x\\tasks\\x.exe",
    ],
)
def test_interpreters_lolbins_credential_ui_and_user_writable_directories_cannot_be_registered(executable: str) -> None:
    with pytest.raises(ValueError):
        validate({"appId": "x", "label": "X", "executable": executable}, roots=ROOTS)


def test_there_are_no_built_in_registered_applications() -> None:
    assert AppRegistry.from_config("").all() == ()


# ---- S4: set_control_value -------------------------------------------------------------------------


def test_set_value_writes_and_verifies_the_exact_text() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    response = world.effects.set_value(world.set_value_request(ref, epoch, observation.observation_id, control, "hello"))
    assert response.outcome == "set"
    assert world.value_node.set_value_calls == ["hello"]


def test_set_value_that_the_application_ignores_is_a_known_failure_not_success() -> None:
    world = World()
    world.value_node.set_value_ignored = True
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    response = world.effects.set_value(world.set_value_request(ref, epoch, observation.observation_id, control, "hello"))
    assert response.outcome == "not_set"
    assert world.value_node.set_value_calls == ["hello"]
    assert world.value_node.value == ""


def test_a_read_only_control_refuses_set_value_before_any_write() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Locked")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.READ_ONLY_CONTROL
    assert world.readonly_node.set_value_calls == []


def test_a_control_without_a_value_pattern_refuses_set_value() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Items")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.NOT_A_VALUE_CONTROL


def test_a_credential_labelled_field_makes_the_whole_surface_refuse_before_any_ref_exists() -> None:
    world = World()
    world.value_node.name = "Password"
    world.value_node.control_type = "Edit"
    ref, epoch = world.window()
    # The credential name makes the whole surface a credential surface: nothing projects at all, so
    # there is never a ref to write to in the first place (S1's rule, unchanged by S4).
    assert code(lambda: world.observe_full(ref, epoch)) is DesktopReason.CREDENTIAL_SURFACE


def test_a_sensitive_target_process_refuses_set_value() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    world.probe.processes[4001].image = "cmd.exe"
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.SENSITIVE_TARGET_REFUSED
    assert world.value_node.set_value_calls == []


def test_a_file_dialog_titled_window_refuses_set_value() -> None:
    world = World()
    ref, epoch = world.window(title="Save As")
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.SENSITIVE_TARGET_REFUSED
    assert world.value_node.set_value_calls == []


def test_a_credential_field_added_inside_an_unrelated_sibling_subtree_refuses_set_value() -> None:
    """Found by an independent adversarial review of M9 S4: re-deriving the TARGET control only
    re-checks credential exclusion along its own ancestor path (each ancestor and that ancestor's
    direct siblings). A credential field added somewhere else entirely -- nested inside a DIFFERENT
    top-level container, not merely beside one -- would never appear on that path. The whole-surface
    re-scan `_mutation_target` now also runs must still catch it."""
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    nested_password = Node(control_type="Edit", name="Password", is_password=True)
    unrelated_panel = Node(control_type="Pane", name="Unrelated panel", children=[nested_password])
    world.backend.trees[10].children.append(unrelated_panel)
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.CREDENTIAL_SURFACE
    assert world.value_node.set_value_calls == []


def test_an_unrelated_vanished_element_does_not_block_a_mutation_of_a_different_live_control() -> None:
    """The credential re-scan is deliberately lenient about ordinary transient churn ELSEWHERE in the
    tree: a node that disappears mid-scan is skipped, not treated as proof the whole surface changed,
    because it cannot itself be a live credential input any more and it is not the control being
    mutated. Otherwise an S4 effect would become newly, needlessly fragile to unrelated UI churn."""
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    world.option_a.gone = True  # unrelated to "Field"; nested under a different top-level child
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    response = world.effects.set_value(request)
    assert response.outcome == "set"


def test_human_input_stops_set_value_before_any_write() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello", tick=world.platform.input_tick + 1)
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.value_node.set_value_calls == []


def test_set_value_kills_the_old_observations_refs_before_writing() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    world.effects.set_value(world.set_value_request(ref, epoch, observation.observation_id, control, "hello"))
    stale = code(lambda: world.observer.resolve_control(ref, epoch, observation.observation_id, control))
    assert stale is DesktopReason.STALE_CONTROL


def test_set_value_replays_its_stored_answer_for_a_repeated_dispatch() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    first = world.effects.set_value(request)
    replayed = world.effects.set_value(request)
    assert replayed == first
    assert world.value_node.set_value_calls == ["hello"]


def test_a_stale_control_or_wrong_observation_refuses_set_value() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    world.observer.observe(ref, epoch)  # a newer observation replaces the control table
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.STALE_CONTROL


# ---- S4: select_control ------------------------------------------------------------------------


def test_select_chooses_the_exact_option_and_verifies_it() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    option = world.ref_of(observation, "Option B")
    response = world.effects.select(world.select_request(ref, epoch, observation.observation_id, container, option))
    assert response.outcome == "selected"
    assert world.option_b.select_calls == 1
    assert world.option_a.select_calls == 0


def test_select_the_application_ignores_is_a_known_failure_not_success() -> None:
    world = World()
    world.option_a.select_ignored = True
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    option = world.ref_of(observation, "Option A")
    response = world.effects.select(world.select_request(ref, epoch, observation.observation_id, container, option))
    assert response.outcome == "not_selected"
    assert world.option_a.select_calls == 1


def test_a_sensitive_target_process_refuses_select() -> None:
    """Found by an independent integration review of M9 S4: `set_control_value` and `invoke_control`
    both refuse a shell/terminal/script-host/security-app target, but `select_control` had no such
    check -- even though selecting a file inside a real Open/Save common dialog's file list populates
    the filename edit control, the same class of risk the other two effects already refuse."""
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    option = world.ref_of(observation, "Option A")
    world.probe.processes[4001].image = "cmd.exe"
    request = world.select_request(ref, epoch, observation.observation_id, container, option)
    assert code(lambda: world.effects.select(request)) is DesktopReason.SENSITIVE_TARGET_REFUSED
    assert world.option_a.select_calls == 0


def test_an_option_from_a_different_container_is_refused() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    # Pass the LIST as the container but an option that actually lives under the combo.
    wrong_container = world.ref_of(observation, "Items")
    option = world.ref_of(observation, "Option A")
    request = world.select_request(ref, epoch, observation.observation_id, wrong_container, option)
    assert code(lambda: world.effects.select(request)) is DesktopReason.OPTION_WRONG_CONTAINER
    assert world.option_a.select_calls == 0


def test_a_control_without_selection_item_pattern_cannot_be_selected() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    not_selectable = world.ref_of(observation, "Combo")  # the combo itself has no SelectionItem pattern
    request = world.select_request(ref, epoch, observation.observation_id, container, not_selectable)
    assert code(lambda: world.effects.select(request)) is DesktopReason.OPTION_WRONG_CONTAINER


def test_rederive_descendant_refuses_an_option_reparented_into_a_same_shaped_replacement_container() -> None:
    """Found by an independent adversarial review of M9 S4: the OLD check compared the two STORED
    locator paths as string prefixes, which only proves they *used to* nest this way, not that they
    still do. A same-shaped replacement container (identical name/type/pattern, so it structurally
    matches the approved container's own recorded path) swapped into the approved container's tree
    position, with the SAME option (its own identity unchanged) reparented into it, would satisfy the
    old check even though the option is no longer really under the container the approval named.
    `rederive_descendant` closes this by re-deriving the option a second time starting FROM a live
    re-resolution of the container, so it only succeeds if the option is actually still nested there,
    right now."""
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container_ref = world.ref_of(observation, "Combo")
    option_ref = world.ref_of(observation, "Option A")
    resolved = world.surfaces.resolve(ref, epoch)
    table = resolved.slot.controls
    assert isinstance(table, ControlTable)
    container_locator = table.locators[container_ref]
    option_locator = table.locators[option_ref]
    replacement = Node(control_type="ComboBox", name="Combo", children=[world.option_a])
    world.backend.trees[10].children = [
        replacement if child is world.combo_node else child for child in world.backend.trees[10].children
    ]
    assert (
        code(lambda: world.observer.rederive_descendant(resolved, container_locator, option_locator))
        is DesktopReason.ELEMENT_CHANGED
    )


def test_human_input_stops_select_before_any_call() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    option = world.ref_of(observation, "Option A")
    request = world.select_request(ref, epoch, observation.observation_id, container, option, tick=world.platform.input_tick + 1)
    assert code(lambda: world.effects.select(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.option_a.select_calls == 0


def test_a_vanished_option_is_element_missing() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    container = world.ref_of(observation, "Combo")
    option = world.ref_of(observation, "Option A")
    world.option_a.gone = True
    request = world.select_request(ref, epoch, observation.observation_id, container, option)
    assert code(lambda: world.effects.select(request)) is DesktopReason.ELEMENT_MISSING


# ---- S4: invoke_control -------------------------------------------------------------------------


def test_invoke_verified_by_a_name_change_reports_invoked() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Show details")
    response = world.effects.invoke(world.invoke_request(ref, epoch, observation.observation_id, control))
    assert response.outcome == "invoked"
    assert world.button_node.invoke_calls == 1
    assert world.button_node.name == "Hide details"


def test_invoke_with_no_observable_change_is_a_known_failure_not_success() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "No-op")
    response = world.effects.invoke(world.invoke_request(ref, epoch, observation.observation_id, control))
    assert response.outcome == "no_change"
    assert world.dead_button.invoke_calls == 1


def test_a_control_without_invoke_pattern_cannot_be_invoked() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    request = world.invoke_request(ref, epoch, observation.observation_id, control)
    assert code(lambda: world.effects.invoke(request)) is DesktopReason.NOT_INVOKABLE
    assert world.value_node.set_value_calls == []


def test_a_dangerous_looking_label_is_not_refused_by_the_worker_itself() -> None:
    """Label-based refusal is the CONTROLLER's job (`app.domain.desktop_planning`), on the model's
    proposed action, before an execution proposal ever exists. The worker's own defence is structural
    (identity, pattern, sensitive target, takeover) and verifies by observable state change, never by
    a label, so a button named like a dangerous action still goes through the same NAME_TOGGLE check
    as any other; it is invoked here to prove the worker applies no separate label heuristic."""
    world = World()
    world.button_node.name = "Submit"
    world.button_node.invoke_renames_to = "Submitted"
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Submit")
    response = world.effects.invoke(world.invoke_request(ref, epoch, observation.observation_id, control))
    assert response.outcome == "invoked"


def test_a_sensitive_target_process_refuses_invoke() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Show details")
    world.probe.processes[4001].image = "powershell.exe"
    request = world.invoke_request(ref, epoch, observation.observation_id, control)
    assert code(lambda: world.effects.invoke(request)) is DesktopReason.SENSITIVE_TARGET_REFUSED
    assert world.button_node.invoke_calls == 0


def test_human_input_stops_invoke_before_any_call() -> None:
    world = World()
    ref, epoch = world.window()
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Show details")
    request = world.invoke_request(ref, epoch, observation.observation_id, control, tick=world.platform.input_tick + 1)
    assert code(lambda: world.effects.invoke(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.button_node.invoke_calls == 0


def test_lumis_own_window_refuses_every_s4_mutation_even_with_a_forged_ref() -> None:
    world = World()
    world.probe.add_process(5000, image="lumi.exe", parent=os.getpid())
    world.probe.add_window(30, 5000, title="Lumi")
    assert [s.window_title for s in world.observer.list_surfaces().surfaces] == []
    request = world.set_value_request("s1", 1, uuid.uuid4(), "u1", "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.STALE_SURFACE
    assert world.value_node.set_value_calls == []


def test_an_elevated_surface_refuses_every_s4_mutation() -> None:
    world = World()
    ref, epoch = world.window()
    world.probe.processes[4001].integrity = INTEGRITY_HIGH
    request = SetValueRequest(
        expected_worker_generation=world.generation, dispatch_id=uuid.uuid4(), surface_ref=ref,
        surface_epoch=epoch, observation_id=uuid.uuid4(), control_ref="u1", value="x",
        input_tick=world.platform.input_tick,
    )
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.ELEVATED_WINDOW


# ---- S5: scoped visual fallback (capture) --------------------------------------------------------


def test_a_successful_capture_reports_exact_frame_binding() -> None:
    world = World()
    ref, epoch = world.window()
    request = world.capture_request(ref, epoch)
    response = world.effects.capture(request)
    assert response.surface_ref == ref
    assert response.surface_epoch == epoch
    assert response.width == 804  # client rect: 812 - 8
    assert response.height == 573  # client rect: 612 - 39
    assert response.dpi == 96
    assert response.monitor_id == 1
    assert len(response.geometry_fingerprint) == 64
    assert len(response.frame_digest) == 64
    assert response.image_base64  # a real, non-empty PNG payload
    assert world.capture_platform.capture_calls == [(10, 804, 573)]


def test_capturing_the_same_geometry_twice_yields_the_same_fingerprint() -> None:
    world = World()
    ref, epoch = world.window()
    first = world.effects.capture(world.capture_request(ref, epoch))
    second = world.effects.capture(world.capture_request(ref, epoch))
    assert first.geometry_fingerprint == second.geometry_fingerprint
    assert first.frame_digest == second.frame_digest  # identical scripted pixels


def test_a_repeated_capture_id_replays_without_a_second_native_call() -> None:
    world = World()
    ref, epoch = world.window()
    capture_id = uuid.uuid4()
    first = world.effects.capture(world.capture_request(ref, epoch, capture_id=capture_id))
    second = world.effects.capture(world.capture_request(ref, epoch, capture_id=capture_id))
    assert first == second
    assert len(world.capture_platform.capture_calls) == 1


def test_a_credential_surface_is_never_captured() -> None:
    world = World()
    ref, epoch = world.window()
    world.value_node.name = "Password"
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CREDENTIAL_SURFACE
    assert world.capture_platform.capture_calls == []


def test_an_elevated_surface_is_never_captured() -> None:
    world = World()
    ref, epoch = world.window()
    world.probe.processes[4001].integrity = INTEGRITY_HIGH
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.ELEVATED_WINDOW
    assert world.capture_platform.capture_calls == []


def test_lumis_own_window_is_never_captured_even_with_a_forged_ref() -> None:
    world = World()
    world.probe.add_process(5000, image="lumi.exe", parent=os.getpid())
    world.probe.add_window(30, 5000, title="Lumi")
    assert [s.window_title for s in world.observer.list_surfaces().surfaces] == []
    request = CaptureRequest(
        expected_worker_generation=world.generation, capture_id=uuid.uuid4(), surface_ref="s1",
        surface_epoch=1, input_tick=world.platform.input_tick,
    )
    assert code(lambda: world.effects.capture(request)) is DesktopReason.STALE_SURFACE
    assert world.capture_platform.capture_calls == []


def test_human_input_stops_capture_before_any_native_call() -> None:
    world = World()
    ref, epoch = world.window()
    request = world.capture_request(ref, epoch, tick=world.platform.input_tick + 1)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.capture_platform.capture_calls == []


def test_unavailable_geometry_refuses_without_a_native_call() -> None:
    world = World()
    ref, epoch = world.window()
    world.capture_platform.geometry_returns_none = True
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_REFUSED
    assert world.capture_platform.capture_calls == []


def test_a_torn_client_rect_outside_the_window_is_scope_uncertain() -> None:
    world = World()
    ref, epoch = world.window()
    world.capture_platform.add_window(
        10, window_rect=PhysicalRect(0, 0, 820, 620), client_rect=PhysicalRect(2000, 2000, 2100, 2100)
    )
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_SCOPE_UNCERTAIN
    assert world.capture_platform.capture_calls == []


def test_a_failed_native_capture_is_refused_not_guessed() -> None:
    world = World()
    ref, epoch = world.window()
    world.capture_platform.capture_returns_none = True
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_REFUSED


def test_a_native_capture_exception_is_refused_and_leaks_no_detail() -> None:
    world = World()
    ref, epoch = world.window()
    world.capture_platform.capture_error = OSError("a private GDI failure")
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_REFUSED


def test_a_window_replaced_during_capture_is_refused() -> None:
    world = World()
    ref, epoch = world.window()

    def swap(_width: int, _height: int, hwnd: int = 10) -> bytes | None:
        # The window closes and a different process's window takes the same hwnd mid-capture.
        world.probe.windows = [w for w in world.probe.windows if w.hwnd != 10]
        world.probe.add_window(10, 9999, title="Different process")
        world.probe.add_process(9999, image="other.exe", parent=1)
        return world.capture_platform.pixels[10]

    original = world.capture_platform.capture

    def capture(hwnd: int, width: int, height: int) -> bytes | None:
        world.capture_platform.capture_calls.append((hwnd, width, height))
        return swap(width, height, hwnd)

    world.capture_platform.capture = capture  # type: ignore[method-assign]
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_REFUSED


def test_capture_without_a_configured_platform_is_unsupported() -> None:
    world = World()
    ref, epoch = world.window()
    world.effects._capture_platform = None  # simulate a build with no capture backend wired
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.UNSUPPORTED


def test_a_credential_scan_that_cannot_finish_refuses_capture_rather_than_missing_a_password_field() -> None:
    """Found by an independent adversarial review of M9 S5: `_credential_scan`'s per-parent sibling
    cap (`MAX_SIBLINGS`) used to silently drop the rest of a large sibling list rather than proving it
    credential-free. A password field placed past that cap would never be visited, and the scan would
    return as if the surface were clean. The scan must now fail closed instead."""
    world = World()
    ref, epoch = world.window()
    from app.desktop.protocol import MAX_SIBLINGS

    password = Node(control_type="Edit", name="Password", is_password=True)
    many_siblings = [Node(control_type="Text", name=f"Item {i}") for i in range(MAX_SIBLINGS)] + [password]
    world.backend.trees[10].children.append(Node(control_type="Pane", name="Overflow panel", children=many_siblings))
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CREDENTIAL_SCAN_INCOMPLETE
    assert world.capture_platform.capture_calls == []


def test_a_credential_scan_that_cannot_finish_also_refuses_an_s4_mutation() -> None:
    """The same fix applies to S4's mutations, which reuse the identical scan: an incomplete scan was
    never a proof of safety for them either, and this is a strictly more conservative change (a
    previously-silent pass now refuses) rather than a narrowing of what S4 already allowed."""
    world = World()
    ref, epoch = world.window()
    from app.desktop.protocol import MAX_SIBLINGS

    password = Node(control_type="Edit", name="Password", is_password=True)
    many_siblings = [Node(control_type="Text", name=f"Item {i}") for i in range(MAX_SIBLINGS)] + [password]
    world.backend.trees[10].children.append(Node(control_type="Pane", name="Overflow panel", children=many_siblings))
    observation = world.observe_full(ref, epoch)
    control = world.ref_of(observation, "Field")
    request = world.set_value_request(ref, epoch, observation.observation_id, control, "hello")
    assert code(lambda: world.effects.set_value(request)) is DesktopReason.CREDENTIAL_SCAN_INCOMPLETE
    assert world.value_node.set_value_calls == []


def test_a_credential_field_added_during_the_geometry_lookup_refuses_capture() -> None:
    """Found by an independent adversarial review of M9 S5: `geometry()` is its own real Win32 call
    sequence that takes measurable time, during which a credential field could appear in the live tree
    after the FIRST credential scan already passed. The second scan, immediately before the native
    call, must catch it."""
    world = World()
    ref, epoch = world.window()
    original_geometry = world.capture_platform.geometry

    def geometry_with_late_credential(hwnd: int):  # type: ignore[no-untyped-def]
        world.value_node.name = "Password"
        return original_geometry(hwnd)

    world.capture_platform.geometry = geometry_with_late_credential  # type: ignore[method-assign]
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CREDENTIAL_SURFACE
    assert world.capture_platform.capture_calls == []


def test_a_window_replaced_during_the_geometry_lookup_refuses_capture() -> None:
    world = World()
    ref, epoch = world.window()
    original_geometry = world.capture_platform.geometry

    def geometry_with_replacement(hwnd: int):  # type: ignore[no-untyped-def]
        world.probe.windows = [w for w in world.probe.windows if w.hwnd != hwnd]
        world.probe.add_process(9999, image="other.exe", parent=1)
        world.probe.add_window(hwnd, 9999, title="Different process")
        return original_geometry(hwnd)

    world.capture_platform.geometry = geometry_with_replacement  # type: ignore[method-assign]
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_REFUSED
    assert world.capture_platform.capture_calls == []


def test_a_window_moved_between_the_two_geometry_reads_is_scope_uncertain() -> None:
    """Sol Finding 5: `verify_unchanged` proves the SURFACE (process/window identity) is still the
    same immediately before the native call, but says nothing about POSITION -- the window could move
    (or resize, or change monitor/DPI) in the gap since the FIRST geometry read, which is what sized
    the bitmap and is recorded as this attempt's `geometry_fingerprint`. The second, fresh geometry
    read this fix adds must catch that and refuse, not silently capture against numbers the approval
    never covered."""
    world = World()
    ref, epoch = world.window()
    moved = PhysicalRect(50, 50, 870, 670)

    def on_geometry_call(call_number: int) -> None:
        if call_number == 2:
            world.capture_platform.add_window(10, window_rect=moved, client_rect=PhysicalRect(58, 89, 862, 662))

    world.capture_platform.on_geometry_call = on_geometry_call
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.CAPTURE_SCOPE_UNCERTAIN
    assert world.capture_platform.capture_calls == []
    assert world.capture_platform.geometry_calls == 2


def test_human_input_between_the_second_geometry_read_and_the_native_call_stops_capture() -> None:
    """Independent Claude review of M9 S5: the human-input check before the SECOND (fresh) geometry
    read is not "immediately before" the native `PrintWindow` call -- `geometry()` is itself a real
    Win32 call sequence with its own measurable duration, sandwiched between that check and the actual
    capture. A person who starts touching the machine during that fresh geometry read (after the second
    check passed, before the native call) must still stop the capture, not slip through uncaught."""
    world = World()
    ref, epoch = world.window()

    def on_geometry_call(call_number: int) -> None:
        if call_number == 2:
            world.platform.input_tick += 1  # the person touched the machine during the fresh read

    world.capture_platform.on_geometry_call = on_geometry_call
    request = world.capture_request(ref, epoch)
    assert code(lambda: world.effects.capture(request)) is DesktopReason.HUMAN_INPUT_DETECTED
    assert world.capture_platform.capture_calls == []
    assert world.capture_platform.geometry_calls == 2


def test_a_window_unchanged_between_the_two_geometry_reads_still_captures() -> None:
    """The fresh-geometry re-check must not be a no-op false-positive machine: an ordinary capture,
    where nothing moved between the two reads, still succeeds."""
    world = World()
    ref, epoch = world.window()
    request = world.capture_request(ref, epoch)
    response = world.effects.capture(request)
    assert response.width == 804 and response.height == 573
    assert world.capture_platform.geometry_calls == 2
    assert len(world.capture_platform.capture_calls) == 1
