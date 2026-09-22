"""Pure DPI/monitor/geometry math for the S5 visual fallback. No ctypes, no real display needed."""

import pytest

from app.desktop.dpi import (
    MAX_CAPTURE_DIMENSION,
    GeometrySnapshot,
    MonitorInfo,
    PhysicalRect,
    capture_rect,
    capture_scope_certain,
    geometry_fingerprint,
    logical_to_physical,
    monitor_for_rect,
    physical_to_logical,
)


def test_a_malformed_rectangle_is_refused() -> None:
    with pytest.raises(ValueError):
        PhysicalRect(left=100, top=0, right=0, bottom=10)


# ---- DPI scale -----------------------------------------------------------------------------


@pytest.mark.parametrize(("dpi", "logical", "physical"), [(96, 100, 100), (120, 100, 125), (144, 100, 150)])
def test_logical_to_physical_at_100_125_and_150_percent(dpi: int, logical: int, physical: int) -> None:
    assert logical_to_physical(logical, dpi) == physical


@pytest.mark.parametrize(("dpi", "physical", "logical"), [(96, 100, 100), (120, 125, 100), (144, 150, 100)])
def test_physical_to_logical_is_the_exact_inverse(dpi: int, physical: int, logical: int) -> None:
    assert physical_to_logical(physical, dpi) == logical


def test_zero_or_negative_dpi_is_refused() -> None:
    with pytest.raises(ValueError):
        logical_to_physical(100, 0)
    with pytest.raises(ValueError):
        physical_to_logical(100, -96)


# ---- monitor matching ------------------------------------------------------------------------


def test_a_window_on_one_monitor_of_two_matches_that_monitor() -> None:
    left = MonitorInfo(monitor_id=1, rect=PhysicalRect(0, 0, 1920, 1080), primary=True)
    right = MonitorInfo(monitor_id=2, rect=PhysicalRect(1920, 0, 3840, 1080), primary=False)
    window = PhysicalRect(2000, 100, 2500, 600)
    assert monitor_for_rect(window, [left, right]) == right


def test_a_negative_virtual_screen_origin_monitor_matches_correctly() -> None:
    """A monitor placed left of or above the primary has negative virtual-screen coordinates."""
    primary = MonitorInfo(monitor_id=1, rect=PhysicalRect(0, 0, 1920, 1080), primary=True)
    left_of_primary = MonitorInfo(monitor_id=2, rect=PhysicalRect(-1920, -200, 0, 880), primary=False)
    window = PhysicalRect(-1800, -100, -100, 700)
    assert monitor_for_rect(window, [left_of_primary, primary]) == left_of_primary


def test_a_window_spanning_two_monitors_matches_the_larger_overlap() -> None:
    left = MonitorInfo(monitor_id=1, rect=PhysicalRect(0, 0, 1920, 1080), primary=True)
    right = MonitorInfo(monitor_id=2, rect=PhysicalRect(1920, 0, 3840, 1080), primary=False)
    # Mostly on the left monitor, a sliver on the right.
    window = PhysicalRect(1700, 100, 2000, 600)
    assert monitor_for_rect(window, [left, right]) == left


def test_a_window_moved_between_monitors_resolves_to_the_new_one() -> None:
    left = MonitorInfo(monitor_id=1, rect=PhysicalRect(0, 0, 1920, 1080), primary=True)
    right = MonitorInfo(monitor_id=2, rect=PhysicalRect(1920, 0, 3840, 1080), primary=False)
    before = PhysicalRect(100, 100, 500, 400)
    after = PhysicalRect(2100, 100, 2500, 400)
    assert monitor_for_rect(before, [left, right]) == left
    assert monitor_for_rect(after, [left, right]) == right


def test_a_window_fully_off_every_monitor_falls_back_to_the_nearest() -> None:
    primary = MonitorInfo(monitor_id=1, rect=PhysicalRect(0, 0, 1920, 1080), primary=True)
    window = PhysicalRect(5000, 5000, 5100, 5100)
    assert monitor_for_rect(window, [primary]) == primary


def test_no_monitors_at_all_resolves_to_none() -> None:
    assert monitor_for_rect(PhysicalRect(0, 0, 100, 100), []) is None


# ---- geometry fingerprint --------------------------------------------------------------------


_DEFAULT_MONITOR_RECT = PhysicalRect(0, 0, 1920, 1080)


def _snapshot(**overrides: object) -> GeometrySnapshot:
    base = dict(
        window_rect=PhysicalRect(100, 100, 900, 700),
        client_rect=PhysicalRect(108, 131, 892, 692),
        monitor_id=1,
        monitor_rect=_DEFAULT_MONITOR_RECT,
        dpi=96,
        process_pid=4242,
        process_created=1000,
    )
    base.update(overrides)
    return GeometrySnapshot(**base)  # type: ignore[arg-type]


def test_identical_geometry_fingerprints_identically() -> None:
    assert geometry_fingerprint(_snapshot()) == geometry_fingerprint(_snapshot())


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_rect": PhysicalRect(200, 100, 1000, 700)},  # moved
        {"window_rect": PhysicalRect(100, 100, 1200, 900)},  # resized
        {"monitor_id": 2},  # monitor change
        {"monitor_rect": PhysicalRect(1920, 0, 3840, 1080)},  # the SAME monitor id, but its own bounds
                                                                # changed (a resolution/arrangement change)
        {"dpi": 120},  # DPI change
        {"process_created": 1001},  # the window was replaced by a different process instance
    ],
)
def test_any_material_change_invalidates_the_fingerprint(overrides: dict[str, object]) -> None:
    assert geometry_fingerprint(_snapshot(**overrides)) != geometry_fingerprint(_snapshot())


def test_the_fingerprint_never_contains_raw_coordinates() -> None:
    digest = geometry_fingerprint(_snapshot())
    assert len(digest) == 64
    int(digest, 16)  # a plain hex digest, nothing else


# ---- capture rect / scope certainty ------------------------------------------------------------


def test_capture_rect_is_the_client_area_clamped_to_the_window() -> None:
    snapshot = _snapshot()
    rect = capture_rect(snapshot)
    assert rect == snapshot.client_rect
    assert capture_scope_certain(snapshot) is True


def test_a_client_rect_outside_the_window_rect_is_scope_uncertain() -> None:
    # A torn read: the client rect no longer overlaps the window rect at all.
    snapshot = _snapshot(client_rect=PhysicalRect(2000, 2000, 2100, 2100))
    assert capture_scope_certain(snapshot) is False


def test_a_zero_area_client_rect_is_scope_uncertain() -> None:
    snapshot = _snapshot(client_rect=PhysicalRect(500, 400, 500, 400))
    assert capture_scope_certain(snapshot) is False


# ---- Sol Finding 4: capture_scope_certain must PROVE the crop, never merely clamp to it ------------


def test_a_client_rect_only_partially_inside_the_window_is_scope_uncertain() -> None:
    """A torn read that still clamps to a NONZERO rect must be refused too: a positive-area result
    after clamping is not proof the client rect was ever really nested inside the window -- only that
    the two rects happened to overlap somewhere. `capture_rect` must equal the raw `client_rect`
    unchanged, or the crop was never proven, only corrected."""
    snapshot = _snapshot(
        window_rect=PhysicalRect(100, 100, 900, 700),
        # Mostly inside the window, but 50px past its right edge: a torn ClientToScreen/GetClientRect
        # pair, not a legitimate client area.
        client_rect=PhysicalRect(200, 200, 950, 650),
    )
    rect = capture_rect(snapshot)
    assert rect.width > 0 and rect.height > 0  # the clamp alone still "succeeds"
    assert rect != snapshot.client_rect  # but it had to cut something to do it
    assert capture_scope_certain(snapshot) is False


def test_zero_or_negative_dpi_is_scope_uncertain() -> None:
    assert capture_scope_certain(_snapshot(dpi=0)) is False


def test_a_degenerate_window_rect_is_scope_uncertain() -> None:
    snapshot = _snapshot(window_rect=PhysicalRect(500, 400, 500, 700))  # zero width
    assert capture_scope_certain(snapshot) is False


def test_a_capture_rect_wider_than_the_maximum_dimension_is_scope_uncertain() -> None:
    huge = PhysicalRect(0, 0, MAX_CAPTURE_DIMENSION + 100, 600)
    snapshot = _snapshot(window_rect=huge, client_rect=huge, monitor_rect=PhysicalRect(0, 0, 20000, 20000))
    assert capture_scope_certain(snapshot) is False


def test_a_window_entirely_off_every_monitor_is_scope_uncertain() -> None:
    """`monitor_for_rect` itself falls back to the nearest monitor even with zero overlap (matching
    `MonitorFromWindow`'s own real contract), so a caller cannot tell "genuinely on this monitor" from
    "nowhere near any monitor, nearest picked anyway" from `monitor_id` alone. A real, visible
    top-level window is never fully off every display, so zero overlap with the resolved monitor's own
    bounds must fail closed -- catching a negative-origin bug or an overflowed/spoofed rect."""
    far_away = PhysicalRect(50_000, 50_000, 50_800, 50_600)
    snapshot = _snapshot(
        window_rect=far_away,
        client_rect=PhysicalRect(50_008, 50_031, 50_792, 50_592),
        monitor_rect=_DEFAULT_MONITOR_RECT,  # (0, 0, 1920, 1080) -- nowhere near `far_away`
    )
    assert capture_scope_certain(snapshot) is False


def test_a_window_only_partially_on_its_monitor_is_still_scope_certain() -> None:
    """Ordinary desktop use: a window dragged half past its monitor's top edge is legitimately
    partially off-screen, not corrupted data, and must still capture."""
    snapshot = _snapshot(
        window_rect=PhysicalRect(100, -300, 900, 300),
        client_rect=PhysicalRect(108, -269, 892, 292),
        monitor_rect=_DEFAULT_MONITOR_RECT,
    )
    assert capture_scope_certain(snapshot) is True
