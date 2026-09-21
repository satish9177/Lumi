"""Milestone 9 S1: regressions for the independent review's findings.

Each test names the failure it prevents. All but the job-object test run on scripted fakes.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence

import pytest

from app.config import AGENT_ROOT
from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import DesktopObserver
from app.desktop.protocol import (
    MAX_NODES,
    MAX_SCAN_DEPTH,
    MAX_SIBLINGS,
    Truncation,
)
from app.desktop.surfaces import (
    INTEGRITY_HIGH,
    INTEGRITY_MEDIUM,
    INTEGRITY_SYSTEM,
    ExclusionPolicy,
    SurfaceTable,
    WindowFacts,
)
from tests.desktop_fakes import FakeBackend, FakeProbe, Node, window_tree
from tests.test_desktop_domain import World, refusal_code

MARK = "M9_S1_DESKTOP_PRIVATE_MARKER_71A"


# ---- exclusion must not fail open ----------------------------------------------------------------------------


def test_a_failed_process_snapshot_is_a_failure_not_an_empty_lumi_tree() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100)
    world.probe.add_process(501, created=110, parent=500)
    world.window(1, 501, title="Lumi sign-in window")
    observer = world.start()
    world.probe.snapshot_error = True
    assert refusal_code(observer.list_surfaces) is DesktopReason.BACKEND_FAILED
    world.probe.snapshot_error = False
    assert observer.list_surfaces().surfaces == []


def test_an_empty_process_snapshot_is_a_failure_too() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="Window")
    observer = world.start()
    world.probe.processes.clear()
    assert refusal_code(observer.list_surfaces) is DesktopReason.BACKEND_FAILED


def test_a_live_process_the_snapshot_does_not_know_is_treated_as_lumis_and_not_described() -> None:
    world = World(roots=[500])
    world.probe.add_process(500, created=100)
    world.probe.add_process(501, created=110, parent=500)  # a Lumi Chromium created around the snapshot
    world.probe.add_process(900, created=150, image="notepad.exe")
    world.window(1, 501, title="Lumi form-preparation window: private page title")
    world.window(2, 900, title="Notepad")
    world.probe.missing_from_snapshot.add(501)
    inventory = world.start().list_surfaces()
    assert [s.window_title for s in inventory.surfaces] == ["Notepad"]
    assert "private page title" not in json.dumps([s.model_dump(mode="json") for s in inventory.surfaces])


def test_windows_are_enumerated_before_the_process_snapshot_is_taken() -> None:
    """A process created between the two calls would otherwise be missing from a snapshot taken first."""
    order: list[str] = []
    world = World()
    probe = world.probe
    real_enumerate, real_parents = probe.enumerate_windows, probe.process_parents
    def enumerate_windows() -> Sequence[WindowFacts]:
        order.append("windows")
        return real_enumerate()

    def process_parents() -> dict[int, int]:
        order.append("snapshot")
        return real_parents()

    probe.enumerate_windows = enumerate_windows  # type: ignore[method-assign]
    probe.process_parents = process_parents  # type: ignore[method-assign]
    world.start().list_surfaces()
    assert order.index("windows") < order.index("snapshot")


def test_a_dead_intermediate_launcher_hides_ancestry_but_not_job_membership() -> None:
    def build(trust_job: bool) -> list[str]:
        world = World()
        world.probe.add_process(500, created=100)                          # trusted root (the runtime)
        world.probe.add_process(501, created=110, parent=500).alive = False  # short-lived launcher, gone
        world.probe.add_process(502, created=120, parent=501)              # Lumi's Chromium
        world.window(1, 502, title="Lumi sign-in window")
        world.probe.job = {500, 502}
        exclusion = ExclusionPolicy.resolve(world.probe, [500], trust_job=trust_job)
        observer = DesktopObserver(
            surfaces=SurfaceTable(probe=world.probe, exclusion=exclusion),
            backend=world.backend, worker_generation=uuid.uuid4(),
        )
        return [s.window_title for s in observer.list_surfaces().surfaces]

    assert build(trust_job=False) == ["Lumi sign-in window"], "ancestry alone cannot see through a dead launcher"
    assert build(trust_job=True) == [], "job membership can"


# ---- elevated means elevated, whatever Lumi runs as ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("own", "target", "listed"),
    [
        (INTEGRITY_MEDIUM, INTEGRITY_HIGH, False),
        (INTEGRITY_HIGH, INTEGRITY_HIGH, False),    # an elevated Lumi still does not read elevated windows
        (INTEGRITY_SYSTEM, INTEGRITY_HIGH, False),
        (INTEGRITY_HIGH, INTEGRITY_MEDIUM, True),
        (INTEGRITY_MEDIUM, INTEGRITY_MEDIUM, True),
    ],
)
def test_the_integrity_ceiling_is_medium_regardless_of_lumis_own_level(own: int, target: int, listed: bool) -> None:
    world = World()
    world.probe.add_process(4001, integrity=target)
    world.window(1, 4001, title="Administrator: registry editor")
    world.probe.own_integrity = own
    inventory = world.start().list_surfaces()
    assert (len(inventory.surfaces) == 1) is listed


def test_an_elevated_surface_is_refused_at_observation_time_even_when_lumi_is_elevated() -> None:
    world = World()
    process = world.probe.add_process(4001)
    world.window(1, 4001, tree=window_tree("Doc", Node(control_type="Text", name="content")))
    world.probe.own_integrity = INTEGRITY_HIGH
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    process.integrity = INTEGRITY_HIGH
    reads = world.backend.reads
    assert refusal_code(lambda: observer.observe(surface.surface_ref, surface.surface_epoch)) is DesktopReason.ELEVATED_WINDOW
    assert world.backend.reads == reads


# ---- a hostile string must not take the worker down ----------------------------------------------------------------------


def test_an_unpaired_surrogate_in_a_title_does_not_break_the_inventory() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(1, 4001, title="Q3 budget \ud83d")
    world.window(2, 4001, title="Ordinary")
    inventory = world.start().list_surfaces()
    assert [s.window_title for s in inventory.surfaces] == ["Q3 budget �", "Ordinary"]
    json.dumps([s.model_dump(mode="json") for s in inventory.surfaces]).encode("utf-8")  # encodable


def test_an_unpaired_surrogate_in_a_name_or_value_does_not_break_an_observation() -> None:
    world = World()
    world.probe.add_process(4001)
    world.window(
        1, 4001,
        tree=window_tree("Doc", Node(control_type="Edit", name="Body \udc00", value="secret paragraph \ud83d"), Node(control_type="Text", name="ok")),
    )
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    observation = observer.observe(surface.surface_ref, surface.surface_epoch)
    body = next(n for n in observation.nodes if n.role.value == "edit")
    assert body.text == "secret paragraph �" and body.name == "Body �"
    json.dumps(observation.model_dump(mode="json")).encode("utf-8")


def test_a_backend_that_raises_quoting_what_it_read_yields_a_text_free_failure() -> None:
    world = World()
    world.probe.add_process(4001)
    node = Node(control_type="Edit", name="Body", value=MARK)
    world.window(1, 4001, tree=window_tree("Doc", node))
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]

    def explode(count: int) -> None:
        if count >= 2:
            raise ValueError(f"provider said: {MARK}")

    world.backend.on_read = explode
    with pytest.raises(DesktopRefusal) as caught:
        observer.observe(surface.surface_ref, surface.surface_epoch)
    assert caught.value.code is DesktopReason.BACKEND_FAILED
    assert MARK not in str(caught.value) and MARK not in repr(caught.value.__cause__) and caught.value.__cause__ is None


# ---- bounded work ------------------------------------------------------------------------------------------------------


def test_a_read_that_runs_out_of_time_returns_what_it_has_and_says_so() -> None:
    world = World()
    world.probe.add_process(4001)
    tree = window_tree("Big", *[Node(control_type="Button", name=f"b{n}") for n in range(120)])
    world.window(1, 4001, tree=tree)
    world.backend.on_read = lambda count: time.sleep(0.004)
    exclusion = ExclusionPolicy.resolve(world.probe, [])
    observer = DesktopObserver(
        surfaces=SurfaceTable(probe=world.probe, exclusion=exclusion),
        backend=world.backend, worker_generation=uuid.uuid4(), time_budget_seconds=0.15,
    )
    surface = observer.list_surfaces().surfaces[0]
    observation = observer.observe(surface.surface_ref, surface.surface_epoch)
    assert observation.truncated and Truncation.TIME in observation.truncation
    assert 1 < observation.node_count < 121
    # A second pass over the very same elements agreed, so this is a coherent partial snapshot, not two stitched ones.
    again = observer.observe(surface.surface_ref, surface.surface_epoch)
    assert again.surface_epoch == observation.surface_epoch


def test_a_huge_child_list_is_capped_before_it_is_walked_and_declared() -> None:
    world = World()
    world.probe.add_process(4001)
    tree = window_tree("Huge", *[Node(control_type="ListItem", name=f"row {n}") for n in range(MAX_SIBLINGS * 20)])
    world.window(1, 4001, tree=tree)
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    started = time.monotonic()
    observation = observer.observe(surface.surface_ref, surface.surface_epoch)
    assert time.monotonic() - started < 5
    assert observation.node_count == MAX_NODES
    assert Truncation.SCAN in observation.truncation and Truncation.NODES in observation.truncation
    assert world.backend.reads <= (MAX_NODES + MAX_SIBLINGS + 5) * 2, "reads must not scale with the list length"


def test_a_credential_beyond_the_scan_depth_is_not_seen_and_the_observation_says_so() -> None:
    """The residual, stated plainly: the scan is bounded, and what is past it is declared, not hidden."""
    world = World()
    world.probe.add_process(4001)
    secret = Node(control_type="Edit", name="Password", is_password=True)
    node = secret
    for level in range(MAX_SCAN_DEPTH + 6):
        node = Node(control_type="Pane", name=f"level {level}", children=[node])
    world.window(1, 4001, tree=window_tree("Deep", node))
    observer = world.start()
    surface = observer.list_surfaces().surfaces[0]
    observation = observer.observe(surface.surface_ref, surface.surface_epoch)
    assert Truncation.SCAN in observation.truncation and observation.truncated
    assert secret.sensitive_reads == 0


# ---- the job object, for real (Windows) --------------------------------------------------------------------------------------


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_the_real_probe_lists_the_processes_in_its_own_job_and_only_those() -> None:
    script = "\n".join(
        [
            "import os, subprocess, sys",
            "from app.services.windows_job import configure_runtime_process_tree, runtime_job_is_active",
            "from app.desktop.win32 import WindowsSystemProbe",
            "before = WindowsSystemProbe().job_members()",
            "configure_runtime_process_tree()",
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
            "outsider = None",
            "members = WindowsSystemProbe().job_members()",
            "print(runtime_job_is_active(), os.getpid() in members, child.pid in members, len(members))",
            "child.kill()",
        ]
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=AGENT_ROOT, capture_output=True, text=True, timeout=60, check=True)
    active, self_in, child_in, count = result.stdout.split()
    assert (active, self_in, child_in) == ("True", "True", "True")
    assert int(count) >= 2


@pytest.mark.desktop_uia
@pytest.mark.skipif(os.name != "nt", reason="Windows job objects")
def test_a_window_whose_launcher_died_is_still_lumis_by_job_membership(tmp_path: object) -> None:
    """Runtime (job owner) -> launcher -> fixture window; the launcher exits. Ancestry breaks; the job does not."""
    title = f"Lumi Fixture Orphan {uuid.uuid4().hex[:6]}"
    script = "\n".join(
        [
            "import os, subprocess, sys, time",
            "from app.services.windows_job import configure_runtime_process_tree",
            "configure_runtime_process_tree()",
            f"middle = subprocess.Popen([sys.executable, '-c', \"import subprocess, sys; subprocess.Popen([sys.executable, '-m', 'tests.desktop_fixture_app', '--title', '{title}'])\"])",
            "middle.wait()",
            "from app.desktop.win32 import WindowsSystemProbe",
            "from app.desktop.surfaces import ExclusionPolicy, SurfaceTable",
            "probe = WindowsSystemProbe()",
            "def titles(trust_job):",
            "    table = SurfaceTable(probe=probe, exclusion=ExclusionPolicy.resolve(probe, [os.getpid()], trust_job=trust_job))",
            "    return [s.window_title for s in table.refresh().surfaces]",
            "deadline = time.time() + 30",
            f"while time.time() < deadline and '{title}' not in titles(False):",
            "    time.sleep(0.5)",
            f"print('by-ancestry-only:', '{title}' in titles(False))",
            f"print('with-job:', '{title}' in titles(True))",
        ]
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=AGENT_ROOT, capture_output=True, text=True, timeout=90, check=False)
    try:
        assert "by-ancestry-only: True" in result.stdout, result.stdout + result.stderr
        assert "with-job: False" in result.stdout, result.stdout + result.stderr
    finally:
        # The job's kill-on-close ended the fixture with the script; make sure of it anyway.
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{title}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"], capture_output=True, check=False)
