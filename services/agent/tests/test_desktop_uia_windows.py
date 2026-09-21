"""Milestone 9 S1: the REAL UI Automation backend against a REAL Win32 window.

Nothing here is mocked. A deterministic fixture process (`desktop_fixture_app.py`) owns a real
window with standard controls; the production probe and pywinauto backend observe it on a
dedicated MTA thread. The fixture counts every message that reaches it from outside, so the
tests can prove not only what was read but that reading changed nothing.

Windows only, and it needs an interactive desktop session.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.config import AGENT_ROOT
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    MAX_DEPTH,
    MAX_NODES,
    MAX_TEXT_PER_NODE,
    CheckedState,
    DesktopObservation,
    DesktopPattern,
    DesktopRole,
    Truncation,
)
from tests.desktop_harness import (
    EFFECT_COUNTERS,
    FixtureApp,
    RealObserver,
    fixture,
    foreground_window,
    start_fixture,
    wait_for,
)

pytestmark = [
    pytest.mark.desktop_uia,
    pytest.mark.skipif(os.name != "nt", reason="Windows UI Automation"),
]

MARKER = "M9_S1_DESKTOP_PRIVATE_MARKER_71A"


@pytest.fixture(scope="module")
def observer() -> Any:
    return RealObserver()


def node(observation: DesktopObservation, name: str, role: DesktopRole | None = None) -> Any:
    matches = [n for n in observation.nodes if n.name == name and (role is None or n.role is role)]
    assert matches, f"no {role} named {name!r} in {[(n.role.value, n.name) for n in observation.nodes]}"
    return matches[0]


def assert_no_effects(app: FixtureApp) -> dict[str, int]:
    counters = app.counters()
    assert {key: counters[key] for key in EFFECT_COUNTERS} == dict.fromkeys(EFFECT_COUNTERS, 0), counters
    return counters


# ---- inventory ----------------------------------------------------------------------------


def test_the_fixture_appears_with_an_opaque_ref_and_no_native_identity(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path) as app:
        surface = observer.surface_for(app.title)
        assert surface.surface_ref.startswith("s") and surface.surface_ref[1:].isdigit()
        assert (surface.application_label, surface.visible, surface.minimized) == ("python", True, False)
        listing = observer.run(lambda o: o.list_surfaces())
        dumped = json.dumps([s.model_dump(mode="json") for s in listing.surfaces])
        assert str(app.pid) not in dumped.replace(app.title, "") and str(app.hwnd) not in dumped
        assert "python.exe" not in dumped and "\\" not in dumped.replace("\\\\", "")


def test_a_recycled_process_cannot_inherit_a_ref_after_the_fixture_restarts(observer: RealObserver, tmp_path: Path) -> None:
    title = "Lumi Fixture Restart"
    first = start_fixture(tmp_path, title=title)
    try:
        old = observer.surface_for(title)
        assert observer.run(lambda o: o.observe(old.surface_ref, old.surface_epoch)).node_count > 1
    finally:
        first.stop()
    assert wait_for(lambda: title not in [s.window_title for s in observer.run(lambda o: o.list_surfaces()).surfaces])
    with pytest.raises(DesktopRefusal) as gone:
        observer.run(lambda o: o.observe(old.surface_ref, old.surface_epoch))
    assert gone.value.code is DesktopReason.STALE_SURFACE
    with fixture(tmp_path, title=title):
        new = observer.surface_for(title)
        assert new.surface_epoch > old.surface_epoch or new.surface_ref != old.surface_ref
        with pytest.raises(DesktopRefusal) as stale:
            observer.run(lambda o: o.observe(old.surface_ref, old.surface_epoch))
        assert stale.value.code is DesktopReason.STALE_SURFACE


# ---- projection ---------------------------------------------------------------------------


def test_standard_controls_are_projected_with_their_semantics(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--marker", MARKER) as app:
        observation = observer.observe_title(app.title)
        assert observation.classification == "desktop_private" and observation.trust == "untrusted_environment"
        assert node(observation, "Fixture heading", DesktopRole.TEXT)
        edit = node(observation, "Notes", DesktopRole.EDIT)
        assert edit.text == MARKER and edit.enabled and edit.visible
        assert {DesktopPattern.VALUE} <= set(edit.patterns)
        assert node(observation, "Submit", DesktopRole.BUTTON).enabled is True
        assert DesktopPattern.INVOKE in node(observation, "Submit", DesktopRole.BUTTON).patterns
        assert node(observation, "Disabled action", DesktopRole.BUTTON).enabled is False
        assert node(observation, "Enable option", DesktopRole.CHECK_BOX).checked is CheckedState.ON
        assert node(observation, "Small", DesktopRole.RADIO_BUTTON).selected is True
        assert node(observation, "Large", DesktopRole.RADIO_BUTTON).selected is False
        combo = node(observation, "Size", DesktopRole.COMBO_BOX)
        assert combo.text == "Beta" and DesktopPattern.EXPAND_COLLAPSE in combo.patterns and combo.expanded is False
        assert node(observation, "Choices", DesktopRole.LIST)
        items = {n.name: n for n in observation.nodes if n.role is DesktopRole.LIST_ITEM}
        assert {"One", "Two", "Three"} <= set(items)
        assert [items[name].selected for name in ("One", "Two", "Three")] == [False, True, False]
        assert all("hidden secret text" not in (n.text or "") + (n.name or "") for n in observation.nodes), "a hidden control must not appear"
        assert_no_effects(app)


def test_hierarchy_is_preserved(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path) as app:
        observation = observer.observe_title(app.title)
        by_ref = {n.control_ref: n for n in observation.nodes}
        window = observation.nodes[0]
        assert window.role is DesktopRole.WINDOW and window.parent_ref is None and window.name == app.title
        nested = node(observation, "Nested button")
        pane = by_ref[nested.parent_ref]
        assert (pane.role, pane.name) == (DesktopRole.PANE, "Fixture pane")
        assert by_ref[pane.parent_ref] is window
        assert node(observation, "Inside pane").parent_ref == pane.control_ref
        # Refs are issued in document order: a parent always precedes its children.
        refs = [int(n.control_ref[1:]) for n in observation.nodes]
        assert refs == list(range(1, len(refs) + 1))
        assert all(by_ref[n.parent_ref] is not None and int(n.parent_ref[1:]) < int(n.control_ref[1:]) for n in observation.nodes if n.parent_ref)


def test_an_offscreen_list_item_is_reported_not_visible(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--overflow-items", "10") as app:
        observation = observer.observe_title(app.title)
        items = [n for n in observation.nodes if n.role is DesktopRole.LIST_ITEM and (n.name or "").startswith("Overflow")]
        assert items, [(n.role.value, n.name) for n in observation.nodes if n.role is DesktopRole.LIST_ITEM]
        assert any(not n.visible for n in items) and any(n.visible for n in items)


def test_the_marker_is_only_in_the_observation_not_in_its_identity_fields(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--marker", MARKER) as app:
        observation = observer.observe_title(app.title)
        payload = json.dumps(observation.model_dump(mode="json"))
        assert payload.count(MARKER) == 1
        for forbidden in ("automation", "class_name", "hwnd", "pid", "LumiFixture", "Static", "Button32", "runtime_id", "rect", "bounding"):
            assert forbidden.lower() not in payload.lower().replace("classification", ""), forbidden


# ---- bounds -------------------------------------------------------------------------------


def test_the_node_bound_is_enforced_and_declared(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--mode", "bulk", "--bulk-nodes", "250") as app:
        observation = observer.observe_title(app.title)
        assert observation.node_count == MAX_NODES
        assert observation.truncated and Truncation.NODES in observation.truncation
        assert_no_effects(app)


def test_the_depth_bound_is_enforced_and_declared(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--mode", "bulk", "--bulk-depth", "14") as app:
        observation = observer.observe_title(app.title)
        assert observation.depth == MAX_DEPTH
        assert Truncation.DEPTH in observation.truncation
        assert not any(n.name == "Deep leaf" for n in observation.nodes)


def test_a_window_too_big_to_read_in_time_comes_back_marked_not_killed(tmp_path: Path) -> None:
    hurried = RealObserver(time_budget_seconds=0.4)
    with fixture(tmp_path, "--mode", "bulk", "--bulk-nodes", "250") as app:
        started = time.monotonic()
        observation = hurried.observe_title(app.title)
        assert time.monotonic() - started < 30
        assert observation.truncated and Truncation.TIME in observation.truncation
        assert 1 < observation.node_count < MAX_NODES
        # Re-reading the same window is compatible with the first read: no needless new epoch.
        surface = hurried.surface_for(app.title)
        again = hurried.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        assert again.surface_epoch == observation.surface_epoch == surface.surface_epoch
        assert_no_effects(app)


def test_long_text_is_bounded_and_declared(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--label-length", "400") as app:
        observation = observer.observe_title(app.title)
        assert node(observation, "Notes", DesktopRole.EDIT).text is not None
        assert len(node(observation, "Notes", DesktopRole.EDIT).text) == MAX_TEXT_PER_NODE
        assert Truncation.TEXT in observation.truncation


# ---- freshness, epochs and stale refs ---------------------------------------------------------


def test_a_dynamic_label_gives_a_fresh_observation_and_kills_old_control_refs(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--mode", "dynamic") as app:
        surface = observer.surface_for(app.title)
        first = observer.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        heading = node(first, "Fixture heading", DesktopRole.TEXT)
        submit = node(first, "Submit", DesktopRole.BUTTON)
        assert observer.run(lambda o: o.resolve_control(surface.surface_ref, surface.surface_epoch, first.observation_id, submit.control_ref)).name == "Submit"

        app.relabel()
        app.recreate_submit()
        second = observer.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        assert second.observation_id != first.observation_id
        assert node(second, "Heading 1", DesktopRole.TEXT) and node(second, "Submit 1", DesktopRole.BUTTON)
        assert all(n.name != "Fixture heading" for n in second.nodes)
        # The recreated button is a different structure: the surface moved to a new epoch.
        assert second.surface_epoch == surface.surface_epoch + 1 and second.fingerprint != first.fingerprint

        # The old observation's refs are dead, by epoch and by observation, and nothing is guessed.
        with pytest.raises(DesktopRefusal) as stale:
            observer.run(lambda o: o.resolve_control(surface.surface_ref, surface.surface_epoch, first.observation_id, submit.control_ref))
        assert stale.value.code is DesktopReason.STALE_SURFACE
        with pytest.raises(DesktopRefusal) as old_observation:
            observer.run(lambda o: o.resolve_control(surface.surface_ref, second.surface_epoch, first.observation_id, submit.control_ref))
        assert old_observation.value.code is DesktopReason.STALE_CONTROL
        assert heading.control_ref  # the old ref existed; it is simply no longer valid
        assert_no_effects(app)


def test_re_resolution_finds_the_live_control_and_reports_a_renamed_one_missing(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--mode", "dynamic") as app:
        surface = observer.surface_for(app.title)
        observation = observer.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        combo = node(observation, "Size", DesktopRole.COMBO_BOX)
        submit = node(observation, "Submit", DesktopRole.BUTTON)
        resolved = observer.run(lambda o: o.resolve_control(surface.surface_ref, surface.surface_epoch, observation.observation_id, combo.control_ref))
        assert (resolved.role, resolved.name) == (DesktopRole.COMBO_BOX, "Size")
        app.recreate_submit()  # the button is destroyed and rebuilt with a different label
        with pytest.raises(DesktopRefusal) as missing:
            observer.run(lambda o: o.resolve_control(surface.surface_ref, surface.surface_epoch, observation.observation_id, submit.control_ref))
        assert missing.value.code is DesktopReason.ELEMENT_MISSING


# ---- trust boundaries --------------------------------------------------------------------------


def test_a_credential_surface_returns_no_content_and_the_password_is_never_read(
    observer: RealObserver, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "M9_S1_PASSWORD_SECRET_77"
    with fixture(tmp_path, "--mode", "credential", "--marker", MARKER) as app:
        before = app.dump()
        surface = observer.surface_for(app.title)
        stats_before = observer.observer.last_stats
        with pytest.raises(DesktopRefusal) as refused:
            observer.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        assert refused.value.code is DesktopReason.CREDENTIAL_SURFACE
        # Neither the ordinary text on the same surface nor the password leaks anywhere.
        for text in (str(refused.value), repr(refused.value), caplog.text):
            assert secret not in text and MARKER not in text
        assert observer.observer.last_stats is stats_before, "nothing may be recorded for a credential surface"
        after = app.dump()
        assert after["password_text"] == before["password_text"] == secret
        # The real proof that the value is never requested: nothing asked the password control for its text.
        assert app.counters()["password_reads"] == 0
        assert_no_effects(app)


def test_the_excluded_process_and_its_descendants_are_absent(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, title="Lumi Fixture Trusted") as trusted, fixture(tmp_path, title="Lumi Fixture Ordinary"):
        lumi_like = RealObserver(excluded_pids=(trusted.pid,))
        titles = [s.window_title for s in lumi_like.run(lambda o: o.list_surfaces()).surfaces]
        assert "Lumi Fixture Ordinary" in titles and "Lumi Fixture Trusted" not in titles
        # A trusted root that is only a launcher: its child window is Lumi's own by ancestry.
        launcher = subprocess.Popen(
            [
                sys.executable, "-c",
                "import subprocess, sys, time; "
                "p = subprocess.Popen([sys.executable, '-m', 'tests.desktop_fixture_app', '--title', 'Lumi Fixture Child']); "
                "time.sleep(60)",
            ],
            cwd=AGENT_ROOT,
        )
        try:
            assert wait_for(lambda: "Lumi Fixture Child" in [s.window_title for s in observer.run(lambda o: o.list_surfaces()).surfaces], 30)
            by_ancestry = RealObserver(excluded_pids=(launcher.pid,))
            assert "Lumi Fixture Child" not in [s.window_title for s in by_ancestry.run(lambda o: o.list_surfaces()).surfaces]
            assert "Lumi Fixture Ordinary" in [s.window_title for s in by_ancestry.run(lambda o: o.list_surfaces()).surfaces]
        finally:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(launcher.pid)], capture_output=True, check=False)


def test_the_real_probe_fails_closed_for_processes_it_cannot_describe() -> None:
    from app.desktop.surfaces import INTEGRITY_MEDIUM
    from app.desktop.win32 import WindowsSystemProbe

    probe = WindowsSystemProbe()
    own = probe.own_integrity_level()
    assert own is not None and own >= INTEGRITY_MEDIUM
    assert probe.integrity_level(os.getpid()) == own
    identity = probe.process_identity(os.getpid())
    assert identity is not None and identity.created > 0
    assert (probe.process_image(os.getpid()) or "").endswith(".exe") and "\\" not in (probe.process_image(os.getpid()) or "")
    # The System process (pid 4) cannot be opened, so its integrity is unknown, not assumed.
    assert probe.integrity_level(4) is None
    assert probe.process_identity(0x7FFFFFF0) is None
    assert probe.window_pid(0x7FFFFFF0) is None


# ---- observation is passive ---------------------------------------------------------------------


def test_observation_changes_nothing_on_the_target(observer: RealObserver, tmp_path: Path) -> None:
    with fixture(tmp_path, "--mode", "dynamic", "--marker", MARKER) as app:
        state_before = app.dump()
        # Let any startup activation settle first, so the check below compares like with like.
        foreground = foreground_window()
        for _ in range(6):
            time.sleep(0.3)
            latest = foreground_window()
            if latest == foreground:
                break
            foreground = latest
        surface = observer.surface_for(app.title)
        for _ in range(3):
            observation = observer.run(lambda o: o.observe(surface.surface_ref, surface.surface_epoch))
        observer.run(lambda o: o.resolve_control(surface.surface_ref, surface.surface_epoch, observation.observation_id, "u2"))
        counters = assert_no_effects(app)
        assert counters["getobject"] > 0, "UI Automation really reached the fixture"
        assert foreground_window() == foreground, "observation must not change the foreground window"
        state_after = app.dump()
        for key in ("edit_text", "checkbox_checked", "radio_selected", "combo_index", "list_index", "heading_text", "submit_text", "submit_hwnd"):
            assert state_after[key] == state_before[key], key
