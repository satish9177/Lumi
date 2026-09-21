"""Milestone 9 S1: surface identity, exclusion, trust boundaries and bounded projection.

Everything here runs against scripted fakes, so it is exact and platform-independent.
`test_desktop_uia_windows.py` proves the same things on the real UI Automation backend.
"""

import json
import uuid
from collections.abc import Sequence

import pytest

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import DesktopObserver
from app.desktop.protocol import (
    MAX_DEPTH,
    MAX_NODES,
    MAX_SURFACES,
    MAX_TEXT_PER_NODE,
    MAX_TOTAL_TEXT,
    CheckedState,
    DesktopPattern,
    DesktopRole,
    Truncation,
)
from app.desktop.surfaces import (
    INTEGRITY_HIGH,
    INTEGRITY_LOW,
    ExclusionPolicy,
    SurfaceTable,
)
from tests.desktop_fakes import FakeBackend, FakeProbe, Node, window_tree


class World:
    def __init__(self, roots: Sequence[int] = ()) -> None:
        self.probe = FakeProbe()
        self.backend = FakeBackend()
        self._roots = tuple(roots)
        self.observer: DesktopObserver | None = None

    def start(self) -> DesktopObserver:
        exclusion = ExclusionPolicy.resolve(self.probe, self._roots)
        self.observer = DesktopObserver(
            surfaces=SurfaceTable(probe=self.probe, exclusion=exclusion),
            backend=self.backend,
            worker_generation=uuid.uuid4(),
        )
        return self.observer

    def window(self, hwnd: int, pid: int, tree: Node | None = None, **kwargs: object) -> Node:
        self.probe.add_window(hwnd, pid, **kwargs)  # type: ignore[arg-type]
        node = tree if tree is not None else window_tree("A window")
        self.backend.trees[hwnd] = node
        return node


def refusal_code(call: object) -> DesktopReason:
    with pytest.raises(DesktopRefusal) as caught:
        call()  # type: ignore[operator]
    return caught.value.code


# ---- inventory -------------------------------------------------------------------------


def test_visible_surfaces_get_opaque_refs_and_no_native_identity() -> None:
    world = World()
    world.probe.add_process(4001, created=200, image="notepad.exe")
    world.window(0x1234, 4001, title="Notes - Notepad")
    inventory = world.start().list_surfaces()
    assert [(s.surface_ref, s.surface_epoch) for s in inventory.surfaces] == [("s1", 1)]
    surface = inventory.surfaces[0]
    assert (surface.application_label, surface.window_title) == ("notepad", "Notes - Notepad")
    dumped = json.dumps([s.model_dump(mode="json") for s in inventory.surfaces])
    for forbidden in ("4001", "0x1234", str(0x1234), "notepad.exe", "C:\\"):
        assert forbidden not in dumped
    assert set(surface.model_dump()) == {
        "surface_ref", "surface_epoch", "application_label", "window_title", "visible", "minimized",
    }


def test_only_user_visible_windows_are_surfaces() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="Visible")
    world.window(2, 4001, title="Hidden", visible=False)
    world.window(3, 4001, title="Cloaked (another virtual desktop)", cloaked=True)
    world.window(4, 4001, title="Tool palette", tool_window=True)
    world.window(5, 4001, title="Owned dialog child", owned=True)
    world.window(6, 4001, title="Minimized", visible=False, minimized=True)
    world.window(7, 4001, title="Hung", hung=True)
    world.window(8, 4001, title="Tool window with appwindow style", tool_window=True, app_window=True)
    titles = [s.window_title for s in world.start().list_surfaces().surfaces]
    assert titles == ["Visible", "Minimized", "Tool window with appwindow style"]
    minimized = next(s for s in world.observer.list_surfaces().surfaces if s.window_title == "Minimized")  # type: ignore[union-attr]
    assert minimized.minimized is True


def test_inventory_is_bounded_and_says_so() -> None:
    world = World()
    world.probe.add_process(4001)
    for index in range(MAX_SURFACES + 4):
        world.window(100 + index, 4001, title=f"Window {index}")
    inventory = world.start().list_surfaces()
    assert len(inventory.surfaces) == MAX_SURFACES and inventory.truncated is True
    assert {s.surface_ref for s in inventory.surfaces} == {f"s{n}" for n in range(1, MAX_SURFACES + 1)}


def test_a_surface_keeps_its_ref_across_refreshes_and_new_ones_take_free_slots() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="First")
    observer = world.start()
    first = observer.list_surfaces().surfaces[0]
    world.window(2, 4001, title="Second")
    listed = {s.window_title: (s.surface_ref, s.surface_epoch) for s in observer.list_surfaces().surfaces}
    assert listed["First"] == (first.surface_ref, first.surface_epoch)
    assert listed["Second"][0] != first.surface_ref


# ---- identity ---------------------------------------------------------------------------


def test_a_closed_window_makes_its_ref_stale_and_the_pair_is_never_reissued() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="First")
    observer = world.start()
    old = observer.list_surfaces().surfaces[0]
    world.probe.destroyed.add(1)
    assert refusal_code(lambda: observer.observe(old.surface_ref, old.surface_epoch)) is DesktopReason.STALE_SURFACE
    # A different window takes the same slot: the ref is the same, the epoch is not.
    world.window(2, 4001, title="Second")
    world.backend.trees[2] = window_tree("Second")
    replacement = observer.list_surfaces().surfaces[0]
    assert replacement.surface_ref == old.surface_ref
    assert replacement.surface_epoch > old.surface_epoch
    assert refusal_code(lambda: observer.observe(old.surface_ref, old.surface_epoch)) is DesktopReason.STALE_SURFACE


def test_process_restart_with_the_same_pid_and_hwnd_is_a_different_surface() -> None:
    """HWND and PID reuse: the recycled numbers must not inherit the old ref."""
    world = World()
    process = world.probe.add_process(4001, created=200)
    world.window(0x55, 4001, title="Doc")
    observer = world.start()
    old = observer.list_surfaces().surfaces[0]
    # The process exits and a new one is started that happens to get the same pid and hwnd.
    process.created = 999
    assert refusal_code(lambda: observer.observe(old.surface_ref, old.surface_epoch)) is DesktopReason.STALE_SURFACE
    reissued = observer.list_surfaces().surfaces[0]
    assert reissued.surface_epoch > old.surface_epoch


def test_hwnd_reuse_by_another_process_is_stale() -> None:
    world = World()
    world.probe.add_process(4001, created=200)
    world.probe.add_process(4002, created=300)
    world.window(0x77, 4001, title="Doc")
    observer = world.start()
    old = observer.list_surfaces().surfaces[0]
    world.probe.windows[0].pid = 4002  # the same HWND now belongs to a different process
    assert refusal_code(lambda: observer.observe(old.surface_ref, old.surface_epoch)) is DesktopReason.STALE_SURFACE


def test_process_exit_makes_the_ref_stale() -> None:
    world = World()
    process = world.probe.add_process(4001)
    world.window(1, 4001)
    observer = world.start()
    old = observer.list_surfaces().surfaces[0]
    process.alive = False
    assert refusal_code(lambda: observer.observe(old.surface_ref, old.surface_epoch)) is DesktopReason.STALE_SURFACE


@pytest.mark.parametrize("ref", ["s0", "s17", "u1", "s", "S1", "s01", "", "s1 "])
def test_malformed_or_unissued_refs_are_stale(ref: str) -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001)
    observer = world.start()
    observer.list_surfaces()
    assert refusal_code(lambda: observer.observe(ref, 1)) is DesktopReason.STALE_SURFACE


def test_a_wrong_epoch_is_stale_never_fuzzy_matched() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="Doc")
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch + 1)) is DesktopReason.STALE_SURFACE


def test_close_kills_every_ref() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001)
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    observer.release_all()
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.STALE_SURFACE


# ---- exclusion --------------------------------------------------------------------------


def test_lumi_processes_and_all_their_descendants_are_never_surfaces() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100, image="electron.exe")            # trusted root
    world.probe.add_process(501, created=110, image="electron.exe", parent=500)  # renderer
    world.probe.add_process(502, created=120, image="python.exe", parent=500)    # runtime
    world.probe.add_process(503, created=130, image="chrome.exe", parent=502)    # a browser-worker Chromium
    world.probe.add_process(504, created=140, image="chrome.exe", parent=503)    # its sign-in window process
    world.probe.add_process(900, created=150, image="notepad.exe")               # unrelated
    for hwnd, pid, title in [(1, 500, "Lumi"), (2, 501, "DevTools"), (3, 503, "Sign in"), (4, 504, "Draft form"), (5, 900, "Notepad")]:
        world.window(hwnd, pid, title=title)
    titles = [s.window_title for s in world.start().list_surfaces().surfaces]
    assert titles == ["Notepad"]


def test_the_worker_itself_is_excluded_even_with_no_configured_roots() -> None:
    import os

    world = World()
    world.probe.add_window(1, os.getpid(), title="the worker")
    world.probe.add_process(900)
    world.window(2, 900, title="Other")
    assert [s.window_title for s in world.start().list_surfaces().surfaces] == ["Other"]


def test_exclusion_is_not_a_title_match() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100)
    world.probe.add_process(900, created=150, image="notepad.exe")
    # An unrelated app that merely calls itself Lumi is an ordinary surface...
    world.window(1, 900, title="Lumi")
    # ...and a Lumi-owned window with an innocuous title is still excluded.
    world.window(2, 500, title="Untitled - Notepad")
    assert [s.window_title for s in world.start().list_surfaces().surfaces] == ["Lumi"]


def test_a_recycled_parent_pid_is_not_ancestry() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100)
    # pid 600 claims parent 500, but 500 was created *after* 600: a recycled id, not a parent.
    world.probe.add_process(500, created=100)
    world.probe.add_process(600, created=50, parent=500)
    world.window(1, 600, title="Unrelated")
    assert [s.window_title for s in world.start().list_surfaces().surfaces] == ["Unrelated"]


def test_a_trusted_root_whose_pid_is_recycled_does_not_exclude_the_newcomer() -> None:
    world = World(roots=[500])
    root = world.probe.add_process(500, created=100)
    world.window(1, 500, title="Lumi")
    observer = world.start()
    assert observer.list_surfaces().surfaces == []
    root.created = 777  # the trusted process is gone; a stranger now owns pid 500
    assert [s.window_title for s in observer.list_surfaces().surfaces] == ["Lumi"]


def test_an_unbindable_trusted_root_fails_closed() -> None:
    world = World(roots=[31337])
    assert refusal_code(world.start) is DesktopReason.WORKER_UNAVAILABLE


def test_a_surface_that_becomes_lumis_own_is_refused_as_stale_not_described() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100)
    process = world.probe.add_process(900, created=150)
    world.window(1, 900, title="App")
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    process.parent = 500  # now (impossibly) a descendant of a trusted root
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.STALE_SURFACE


@pytest.mark.parametrize("image", ["consent.exe", "credentialuibroker.exe", "logonui.exe", "lockapp.exe"])
def test_credential_and_consent_broker_processes_are_never_surfaces(image: str) -> None:
    world = World()
    world.probe.add_process(4001, image=image)
    world.window(1, 4001, title="Windows Security")
    assert world.start().list_surfaces().surfaces == []


# ---- elevation --------------------------------------------------------------------------


def test_elevated_windows_are_not_inventoried_and_show_no_title() -> None:
    world = World()
    world.probe.add_process(4001, integrity=INTEGRITY_HIGH)
    world.probe.add_process(4002)
    world.window(1, 4001, title="Administrator: Something private")
    world.window(2, 4002, title="Ordinary")
    inventory = world.start().list_surfaces()
    assert [s.window_title for s in inventory.surfaces] == ["Ordinary"]
    assert "private" not in json.dumps([s.model_dump(mode="json") for s in inventory.surfaces])


def test_lower_integrity_targets_are_allowed() -> None:
    world = World()
    world.probe.add_process(4001, integrity=INTEGRITY_LOW)
    world.window(1, 4001, title="Sandboxed app")
    assert len(world.start().list_surfaces().surfaces) == 1


def test_unknown_target_integrity_fails_closed() -> None:
    world = World()
    world.probe.add_process(4001, integrity=None)
    world.window(1, 4001)
    assert world.start().list_surfaces().surfaces == []


def test_unknown_own_integrity_fails_closed() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001)
    world.probe.own_integrity = None
    assert world.start().list_surfaces().surfaces == []


def test_a_target_that_becomes_elevated_is_refused_before_any_traversal() -> None:
    world = World()
    process = world.probe.add_process(4001)
    world.window(1, 4001, tree=window_tree("Doc", Node(control_type="Text", name="secret")))
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    process.integrity = INTEGRITY_HIGH
    reads_before = world.backend.reads
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.ELEVATED_WINDOW
    assert world.backend.reads == reads_before, "an elevated window must not be traversed at all"
    process.integrity = None
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.INTEGRITY_UNVERIFIABLE
    assert world.backend.reads == reads_before


# ---- credentials ------------------------------------------------------------------------


def _surface(world: World, tree: Node) -> tuple[DesktopObserver, str, int]:
    world.probe.add_process(4001)
    world.window(1, 4001, tree=tree)
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    return observer, surface.surface_ref, surface.surface_epoch


def test_a_password_input_makes_the_whole_surface_a_credential_surface_with_zero_content() -> None:
    world = World()
    secret = Node(control_type="Edit", name="Password", is_password=True, value="hunter2-SECRET")
    tree = window_tree("Sign in", Node(control_type="Text", name="Welcome back"), Node(control_type="Group", children=[Node(control_type="Group", children=[secret])]))
    observer, ref, epoch = _surface(world, tree)
    error = pytest.raises(DesktopRefusal, observer.observe, ref, epoch)
    assert error.value.code is DesktopReason.CREDENTIAL_SURFACE
    assert "Welcome" not in str(error.value) and "hunter2" not in str(error.value)
    assert secret.sensitive_reads == 0, "a password control's name and value must never be read"
    assert observer.last_stats is None, "nothing may be recorded for a credential surface"


def test_a_credential_input_past_the_node_cap_still_refuses_the_surface() -> None:
    world = World()
    filler = [Node(control_type="Button", name=f"b{n}") for n in range(MAX_NODES + 50)]
    secret = Node(control_type="Edit", is_password=True)
    observer, ref, epoch = _surface(world, window_tree("Big", *filler, secret))
    assert refusal_code(lambda: observer.observe(ref, epoch)) is DesktopReason.CREDENTIAL_SURFACE
    assert secret.sensitive_reads == 0


@pytest.mark.parametrize("name", ["Password", "Enter your PIN", "One-time code", "OTP", "Security code", "CVV", "Passphrase"])
def test_an_edit_named_like_a_credential_is_treated_as_one(name: str) -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("Form", Node(control_type="Edit", name=name, value="123456")))
    assert refusal_code(lambda: observer.observe(ref, epoch)) is DesktopReason.CREDENTIAL_SURFACE


def test_ordinary_controls_that_mention_passwords_are_not_credentials() -> None:
    world = World()
    tree = window_tree("Settings", Node(control_type="CheckBox", name="Show password"), Node(control_type="Text", name="Reset your password"))
    observer, ref, epoch = _surface(world, tree)
    assert observer.observe(ref, epoch).node_count == 3


# ---- bounds -----------------------------------------------------------------------------


def test_node_bound_is_enforced_and_declared() -> None:
    world = World()
    tree = window_tree("Big", *[Node(control_type="Button", name=f"b{n}") for n in range(MAX_NODES + 40)])
    observer, ref, epoch = _surface(world, tree)
    observation = observer.observe(ref, epoch)
    assert observation.node_count == len(observation.nodes) == MAX_NODES
    assert observation.truncated is True and Truncation.NODES in observation.truncation
    assert [n.control_ref for n in observation.nodes][:3] == ["u1", "u2", "u3"]
    assert observation.nodes[-1].control_ref == f"u{MAX_NODES}"


def test_depth_bound_is_enforced_and_declared() -> None:
    world = World()
    node = Node(control_type="Text", name="deep leaf")
    for level in range(20, 0, -1):
        node = Node(control_type="Pane", name=f"level {level}", children=[node])
    observer, ref, epoch = _surface(world, window_tree("Deep", node))
    observation = observer.observe(ref, epoch)
    assert observation.depth == MAX_DEPTH
    assert Truncation.DEPTH in observation.truncation and observation.truncated
    assert all(n.name != "deep leaf" for n in observation.nodes)


def test_per_node_text_is_bounded_and_declared() -> None:
    world = World()
    long_text = "x" * 500
    edit = Node(control_type="Edit", name="Body", value=long_text)
    observer, ref, epoch = _surface(world, window_tree("Doc", edit, Node(control_type="Text", name=long_text)))
    observation = observer.observe(ref, epoch)
    texts = [n.text for n in observation.nodes if n.text] + [n.name for n in observation.nodes if n.name]
    assert max(len(t) for t in texts) == MAX_TEXT_PER_NODE
    assert observation.truncation == [Truncation.TEXT]


def test_total_text_is_bounded_and_declared() -> None:
    world = World()
    chunk = "y" * MAX_TEXT_PER_NODE
    tree = window_tree("Text heavy", *[Node(control_type="Text", name=chunk) for _ in range(190)])
    observer, ref, epoch = _surface(world, tree)
    observation = observer.observe(ref, epoch)
    spent = sum(len((n.name or "").encode()) + len((n.text or "").encode()) for n in observation.nodes)
    assert spent <= MAX_TOTAL_TEXT
    assert Truncation.TEXT in observation.truncation
    assert observation.node_count == 191, "structure is kept even when the text budget is spent"


def test_scan_budget_is_bounded_and_declared() -> None:
    world = World()
    tree = window_tree("Vast", *[Node(control_type="Group", children=[Node(control_type="Text") for _ in range(20)]) for _ in range(80)])
    observer, ref, epoch = _surface(world, tree)
    observation = observer.observe(ref, epoch)
    assert Truncation.SCAN in observation.truncation and Truncation.NODES in observation.truncation


def test_a_small_surface_is_not_truncated() -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("Small", Node(control_type="Button", name="OK")))
    observation = observer.observe(ref, epoch)
    assert (observation.truncated, observation.truncation, observation.depth, observation.node_count) == (False, [], 1, 2)


# ---- projection -------------------------------------------------------------------------


def test_hierarchy_roles_and_states_are_projected() -> None:
    world = World()
    tree = window_tree(
        "Form",
        Node(control_type="Text", name="Heading"),
        Node(control_type="Edit", name="Notes", value="hello", focusable=True, patterns=frozenset({DesktopPattern.VALUE, DesktopPattern.TEXT})),
        Node(control_type="CheckBox", name="Enable", checked=CheckedState.ON, patterns=frozenset({DesktopPattern.TOGGLE})),
        Node(control_type="RadioButton", name="Small", selected=True, patterns=frozenset({DesktopPattern.SELECTION_ITEM})),
        Node(control_type="Button", name="Disabled", enabled=False),
        Node(control_type="Button", name="Away", offscreen=True),
        Node(control_type="TreeItem", name="Folder", expanded=False, patterns=frozenset({DesktopPattern.EXPAND_COLLAPSE})),
        Node(control_type="Pane", name="Group", children=[Node(control_type="Button", name="Nested", focused=True)]),
        Node(control_type="Somethingelse", name="Odd"),
    )
    observer, ref, epoch = _surface(world, tree)
    by_name = {n.name: n for n in observer.observe(ref, epoch).nodes}
    assert by_name["Form"].role is DesktopRole.WINDOW and by_name["Form"].parent_ref is None
    assert by_name["Notes"].text == "hello" and by_name["Notes"].focusable
    assert by_name["Notes"].patterns == [DesktopPattern.TEXT, DesktopPattern.VALUE]
    assert by_name["Enable"].checked is CheckedState.ON
    assert by_name["Small"].selected is True and by_name["Small"].role is DesktopRole.RADIO_BUTTON
    assert by_name["Disabled"].enabled is False
    assert by_name["Away"].visible is False
    assert by_name["Folder"].expanded is False
    assert by_name["Nested"].parent_ref == by_name["Group"].control_ref and by_name["Nested"].focused
    assert by_name["Odd"].role is DesktopRole.UNKNOWN
    assert by_name["Heading"].parent_ref == by_name["Form"].control_ref


def test_the_observation_carries_no_native_identity_or_geometry() -> None:
    world = World()
    node = Node(control_type="Button", name="OK", automation_id="btn_ok", class_name="Btn32", runtime_id=(42, 7))
    observer, ref, epoch = _surface(world, window_tree("W", node))
    payload = json.dumps(observer.observe(ref, epoch).model_dump(mode="json"))
    # The planted identity values must not appear anywhere; the exact key set below is what
    # proves no identity or geometry *field* exists.
    for planted in ("btn_ok", "Btn32", "[42, 7]"):
        assert planted not in payload, planted
    keys: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            keys.update(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(json.loads(payload))
    assert keys == {
        "observation_id", "surface_ref", "surface_epoch", "worker_generation", "schema_version",
        "classification", "trust", "nodes", "node_count", "depth", "truncated", "truncation", "fingerprint",
        "control_ref", "parent_ref", "role", "name", "text", "enabled", "visible", "focused", "focusable",
        "selected", "checked", "expanded", "patterns",
    }


def test_every_observation_is_classified_private_and_untrusted() -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("W"))
    observation = observer.observe(ref, epoch)
    assert (observation.classification, observation.trust, observation.schema_version) == ("desktop_private", "untrusted_environment", 1)


def test_diagnostics_hold_only_counts() -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("Private title", Node(control_type="Text", name="PRIVATE_TEXT_MARK")))
    observer.observe(ref, epoch)
    stats = observer.last_stats
    assert stats is not None
    import dataclasses

    assert {f.name for f in dataclasses.fields(stats)} == {"node_count", "depth", "truncated", "duration_ms"}
    assert "PRIVATE_TEXT_MARK" not in repr(stats) and "Private title" not in repr(stats)


# ---- epoch, fingerprint and stale control refs ---------------------------------------------


def test_reobserving_an_unchanged_surface_keeps_the_epoch_but_replaces_control_refs() -> None:
    world = World()
    edit = Node(control_type="Edit", name="Notes", value="one", patterns=frozenset({DesktopPattern.VALUE}))
    observer, ref, epoch = _surface(world, window_tree("W", edit, Node(control_type="Button", name="Go")))
    first = observer.observe(ref, epoch)
    edit.value = "two"  # a value change is not a structural change
    second = observer.observe(ref, epoch)
    assert second.surface_epoch == epoch and second.fingerprint == first.fingerprint
    assert second.observation_id != first.observation_id
    button = next(n for n in first.nodes if n.name == "Go").control_ref
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, first.observation_id, button)) is DesktopReason.STALE_CONTROL
    assert observer.resolve_control(ref, epoch, second.observation_id, button).name == "Go"


def test_a_material_structure_change_bumps_the_epoch_and_kills_old_pairs() -> None:
    world = World()
    button = Node(control_type="Button", name="Submit")
    observer, ref, epoch = _surface(world, window_tree("W", button))
    first = observer.observe(ref, epoch)
    button.name = "Submit 2"
    second = observer.observe(ref, epoch)
    assert second.surface_epoch == epoch + 1 and second.fingerprint != first.fingerprint
    assert refusal_code(lambda: observer.observe(ref, epoch)) is DesktopReason.STALE_SURFACE
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, second.observation_id, "u2")) is DesktopReason.STALE_SURFACE
    assert observer.observe(ref, second.surface_epoch).surface_epoch == second.surface_epoch


def test_a_changing_static_label_alone_does_not_bump_the_epoch() -> None:
    world = World()
    clock = Node(control_type="Text", name="12:00:01")
    observer, ref, epoch = _surface(world, window_tree("W", clock, Node(control_type="Button", name="Stop")))
    first = observer.observe(ref, epoch)
    clock.name = "12:00:02"
    second = observer.observe(ref, epoch)
    assert second.fingerprint == first.fingerprint and second.surface_epoch == epoch
    assert next(n for n in second.nodes if n.role is DesktopRole.TEXT).name == "12:00:02"


def test_control_refs_from_another_surface_or_a_forged_id_are_stale() -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("W", Node(control_type="Button", name="Go")))
    observation = observer.observe(ref, epoch)
    for control in ("u9", "u201", "x1", "", "u0"):
        assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, control)) is DesktopReason.STALE_CONTROL
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, uuid.uuid4(), "u2")) is DesktopReason.STALE_CONTROL


# ---- re-resolution ----------------------------------------------------------------------


def _resolution_world() -> tuple[World, DesktopObserver, str, int, Node, Node]:
    world = World()
    target = Node(control_type="Button", name="Submit", automation_id="a1")
    group = Node(control_type="Pane", name="Actions", children=[target])
    observer, ref, epoch = _surface(world, window_tree("W", group))
    return world, observer, ref, epoch, group, target


def test_a_control_is_re_derived_from_the_live_tree() -> None:
    _, observer, ref, epoch, _, _ = _resolution_world()
    observation = observer.observe(ref, epoch)
    submit = next(n for n in observation.nodes if n.name == "Submit")
    resolved = observer.resolve_control(ref, epoch, observation.observation_id, submit.control_ref)
    assert (resolved.role, resolved.name) == (DesktopRole.BUTTON, "Submit")


def test_a_missing_control_is_element_missing() -> None:
    _, observer, ref, epoch, group, target = _resolution_world()
    observation = observer.observe(ref, epoch)
    ref_u = next(n for n in observation.nodes if n.name == "Submit").control_ref
    group.children.remove(target)
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, ref_u)) is DesktopReason.ELEMENT_MISSING


def test_an_ambiguous_control_is_element_ambiguous() -> None:
    _, observer, ref, epoch, group, target = _resolution_world()
    observation = observer.observe(ref, epoch)
    ref_u = next(n for n in observation.nodes if n.name == "Submit").control_ref
    group.children.append(Node(control_type="Button", name="Submit", automation_id="a1"))
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, ref_u)) is DesktopReason.ELEMENT_AMBIGUOUS


def test_a_replaced_instance_with_the_same_description_is_element_changed() -> None:
    _, observer, ref, epoch, group, target = _resolution_world()
    observation = observer.observe(ref, epoch)
    ref_u = next(n for n in observation.nodes if n.name == "Submit").control_ref
    group.children[0] = Node(control_type="Button", name="Submit", automation_id="a1")
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, ref_u)) is DesktopReason.ELEMENT_CHANGED


def test_there_is_no_nearest_label_fallback() -> None:
    _, observer, ref, epoch, group, target = _resolution_world()
    observation = observer.observe(ref, epoch)
    ref_u = next(n for n in observation.nodes if n.name == "Submit").control_ref
    target.name = "Submit "  # a near-identical label must not be adopted
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, ref_u)) is DesktopReason.ELEMENT_MISSING


def test_resolution_of_a_control_that_became_a_credential_field_refuses() -> None:
    world = World()
    field = Node(control_type="Edit", name="Notes")
    observer, ref, epoch = _surface(world, window_tree("W", field))
    observation = observer.observe(ref, epoch)
    field.is_password = True
    assert refusal_code(lambda: observer.resolve_control(ref, epoch, observation.observation_id, "u2")) is DesktopReason.CREDENTIAL_SURFACE


# ---- stability --------------------------------------------------------------------------


def test_a_tree_that_changes_between_passes_is_surface_changed_and_stores_nothing() -> None:
    world = World()
    button = Node(control_type="Button", name="Go")
    observer, ref, epoch = _surface(world, window_tree("W", button, Node(control_type="Button", name="Stop")))
    reads = 0

    def mutate(count: int) -> None:
        nonlocal reads
        reads = count
        if count == 5:  # part way through the second pass (reads 1-3 are the first)
            button.name = "Went"

    world.backend.on_read = mutate
    assert refusal_code(lambda: observer.observe(ref, epoch)) is DesktopReason.SURFACE_CHANGED
    assert observer.last_stats is None
    world.backend.on_read = None
    assert observer.observe(ref, epoch).surface_epoch == epoch


def test_an_element_that_vanishes_mid_read_is_surface_changed() -> None:
    world = World()
    child = Node(control_type="Button", name="Go")
    observer, ref, epoch = _surface(world, window_tree("W", child))
    world.backend.on_read = lambda count: setattr(child, "gone", count >= 2)
    assert refusal_code(lambda: observer.observe(ref, epoch)) is DesktopReason.SURFACE_CHANGED


def test_a_window_replaced_while_reading_is_surface_changed() -> None:
    world = World()
    process = world.probe.add_process(4001, created=200)
    world.window(1, 4001, tree=window_tree("W", Node(control_type="Button", name="Go")))
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]

    def replace(count: int) -> None:
        if count == 4:
            process.created = 555

    world.backend.on_read = replace
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.SURFACE_CHANGED


def test_a_backend_failure_carries_no_text() -> None:
    world = World()
    observer, ref, epoch = _surface(world, window_tree("W"))
    world.backend.raise_on_root = RuntimeError("PRIVATE_TEXT_MARK: com error 0x8007")
    with pytest.raises(DesktopRefusal) as caught:
        observer.observe(ref, epoch)
    assert caught.value.code is DesktopReason.BACKEND_FAILED
    assert "PRIVATE_TEXT_MARK" not in str(caught.value) and caught.value.__cause__ is None
