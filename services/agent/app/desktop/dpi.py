"""Pure coordinate and identity math for the S5 visual fallback.

No ctypes and no I/O anywhere in this module: `capture_win32.py` reads real geometry from the OS and
calls these functions with plain numbers, so every DPI, multi-monitor and negative-virtual-origin case
is unit-tested here with synthetic input, independent of what monitors this machine actually has.
"""

import hashlib
from dataclasses import dataclass
from typing import Final

#: 96 DPI is Windows' 100% scale factor; every other scale is dpi/96.
BASE_DPI: Final = 96

#: Bound generously above any real display estate; a proposed crop larger than this is a bug or a
#: torn/spoofed geometry read, never a bigger desktop, and `capture_scope_certain` must fail closed
#: on it rather than let it become an unbounded allocation downstream.
MAX_CAPTURE_DIMENSION: Final = 8192


@dataclass(frozen=True, slots=True)
class PhysicalRect:
    """A rectangle in physical-pixel virtual-screen coordinates.

    May be negative (a monitor left of or above the primary). `left`/`top` are always <= `right`/`bottom`.
    """

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.right < self.left or self.bottom < self.top:
            raise ValueError("a rectangle's far edge cannot be before its near edge")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def clamp_to(self, bounds: "PhysicalRect") -> "PhysicalRect":
        """This rectangle, cut down to fit entirely inside `bounds`. Empty (zero-area) if disjoint."""
        left = max(self.left, bounds.left)
        top = max(self.top, bounds.top)
        right = max(left, min(self.right, bounds.right))
        bottom = max(top, min(self.bottom, bounds.bottom))
        return PhysicalRect(left, top, right, bottom)

    def intersection_area(self, other: "PhysicalRect") -> int:
        left = max(self.left, other.left)
        top = max(self.top, other.top)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return 0
        return (right - left) * (bottom - top)


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """One display, as `EnumDisplayMonitors`/`GetMonitorInfoW` report it.

    `monitor_id` is a stable, opaque per-refresh index -- never the native `HMONITOR`, which is not
    itself a durable identity -- it exists only so two captures can say "the same monitor" or "a
    different one".
    """

    monitor_id: int
    rect: PhysicalRect
    primary: bool


def logical_to_physical(size: int, dpi: int) -> int:
    """A logical (96-DPI) length at `dpi`'s actual physical pixel size, rounded to the nearest pixel."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    return round(size * dpi / BASE_DPI)


def physical_to_logical(size: int, dpi: int) -> int:
    """The inverse of `logical_to_physical`: the 96-DPI length that `size` physical pixels represent at `dpi`."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    return round(size * BASE_DPI / dpi)


def _distance_squared(a: PhysicalRect, b: PhysicalRect) -> int:
    dx = max(b.left - a.right, a.left - b.right, 0)
    dy = max(b.top - a.bottom, a.top - b.bottom, 0)
    return dx * dx + dy * dy


def monitor_for_rect(window: PhysicalRect, monitors: list[MonitorInfo]) -> MonitorInfo | None:
    """The monitor `window` belongs to: `MonitorFromWindow(MONITOR_DEFAULTTONEAREST)`'s own contract,
    reimplemented here so it is testable without a real multi-monitor rig.

    The monitor with the greatest overlap area wins; a window that touches none of them (fully off
    every display, which Windows itself never actually allows for a visible top-level window, but a
    stale or scripted rect must still resolve to something sane) falls back to the physically nearest
    one. `None` only when `monitors` is empty.
    """
    if not monitors:
        return None
    best = max(monitors, key=lambda monitor: window.intersection_area(monitor.rect))
    if window.intersection_area(best.rect) > 0:
        return best
    return min(monitors, key=lambda monitor: _distance_squared(window, monitor.rect))


@dataclass(frozen=True, slots=True)
class GeometrySnapshot:
    """Everything about WHERE and HOW a capture was taken, bound together for one fingerprint.

    Never serialized to the wire directly -- only `geometry_fingerprint()`'s digest crosses any
    boundary, exactly like `DesktopObservation.fingerprint` never carries raw text.
    """

    window_rect: PhysicalRect
    client_rect: PhysicalRect
    monitor_id: int
    #: The resolved monitor's own physical bounds, not just its opaque id -- `capture_scope_certain`
    #: needs the actual rectangle to prove the capture genuinely overlaps a real display (catching a
    #: negative-origin overflow or an off-every-monitor rect that `monitor_id` alone cannot reveal),
    #: and a monitor's rect can change (a resolution or arrangement change) without its id changing,
    #: which `geometry_fingerprint` must also be able to see.
    monitor_rect: PhysicalRect
    dpi: int
    process_pid: int
    process_created: int


def geometry_fingerprint(snapshot: GeometrySnapshot) -> str:
    """A value-free digest of a capture's geometry identity.

    Two captures with the same digest were taken with the window at the same position, size, monitor
    (both its id and its own bounds) and DPI, of the same process instance; any of those changing
    (move, resize, monitor change, a monitor's own bounds changing under a stable id, DPI change, or
    the window being replaced by a different process) changes the digest -- which is what lets a
    frame's staleness be checked without ever comparing raw coordinates across a process boundary.
    """
    canonical = (
        f"{snapshot.window_rect.left},{snapshot.window_rect.top},{snapshot.window_rect.right},"
        f"{snapshot.window_rect.bottom}|{snapshot.client_rect.left},{snapshot.client_rect.top},"
        f"{snapshot.client_rect.right},{snapshot.client_rect.bottom}|{snapshot.monitor_id}|"
        f"{snapshot.monitor_rect.left},{snapshot.monitor_rect.top},{snapshot.monitor_rect.right},"
        f"{snapshot.monitor_rect.bottom}|{snapshot.dpi}|{snapshot.process_pid}|{snapshot.process_created}"
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def capture_rect(snapshot: GeometrySnapshot) -> PhysicalRect:
    """The exact physical-pixel rectangle to capture: the client area, clamped to the window's own
    bounds.

    Clamping is defensive -- a well-formed `GetClientRect`/`ClientToScreen` pair is always inside the
    window rect, but a capture must never trust that without checking. The client area is preferred
    over the full window so the captured pixels exclude the title bar where practical (the brief's
    window-title-honesty section); when a caller cannot tell whether the crop excludes chrome, it must
    say so rather than claim it does.
    """
    return snapshot.client_rect.clamp_to(snapshot.window_rect)


def capture_scope_certain(snapshot: GeometrySnapshot) -> bool:
    """False unless the geometry evidence actually PROVES the proposed capture rectangle is exactly
    the client area of a real, on-screen window -- never merely "clamps to something nonzero".

    Every check below is a fail-closed proof, not a best-effort guess:

    * `dpi` must be positive -- a zero or negative DPI makes every physical-pixel value downstream
      meaningless (the "physical vs logical coordinates" transform has nothing to invert).
    * `window_rect`/`client_rect` must each already have positive area on their own, before any
      clamping -- a degenerate rect (minimized, occluded, or a torn read) proves nothing.
    * `capture_rect(snapshot)` (the client rect clamped to the window rect) must equal the RAW
      `client_rect` unchanged. If clamping actually had to cut anything, the client rect was not
      already proven to nest inside the window rect -- it might be a torn read spanning past the
      window's own edge, silently "corrected" into some smaller, unspecified crop that was never
      the real client area. That is exactly the gap this function exists to close: a corrected
      rectangle is not a proven one.
    * the (now-proven) capture rect must have positive overlap with the resolved monitor's own
      physical bounds. A real, visible top-level window is never fully off every display (Windows
      itself does not allow it); zero overlap means the geometry is untrustworthy -- a negative
      virtual-screen-origin bug, an overflowed transform, or spoofed data -- even though a window
      that is merely PARTIALLY off-screen (dragged half past a monitor's edge) still has positive
      overlap and is accepted, matching ordinary desktop use.
    * the capture rect must not exceed `MAX_CAPTURE_DIMENSION` in either axis -- an overflowed or
      otherwise implausible transform must fail closed here, not become an unbounded allocation in
      the native capture call downstream.
    """
    if snapshot.dpi <= 0:
        return False
    if snapshot.window_rect.width <= 0 or snapshot.window_rect.height <= 0:
        return False
    if snapshot.client_rect.width <= 0 or snapshot.client_rect.height <= 0:
        return False
    rect = capture_rect(snapshot)
    if rect != snapshot.client_rect:
        return False
    if rect.width <= 0 or rect.height <= 0:
        return False
    if rect.width > MAX_CAPTURE_DIMENSION or rect.height > MAX_CAPTURE_DIMENSION:
        return False
    if rect.intersection_area(snapshot.monitor_rect) <= 0:
        return False
    return True
