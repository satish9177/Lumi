"""Stable refusal codes for the Windows desktop observation boundary.

A refusal carries a closed `code` and nothing else: never a window title, an
accessible name, a process name or any text that came from the observed UI. The
codes are safe to log and to show, which is what lets a diagnostics line say
"credential_surface" without saying what the credential surface was.
"""

from enum import StrEnum


class DesktopReason(StrEnum):
    # The capability is absent, not failing.
    UNSUPPORTED = "desktop_automation_unsupported"
    DISABLED = "desktop_automation_disabled"
    WORKER_UNAVAILABLE = "desktop_worker_unavailable"
    # A hostile or broken accessibility provider. The worker process is the
    # containment boundary: the runtime kills and fences that generation.
    OBSERVATION_TIMEOUT = "desktop_observation_timeout"
    # Identity: a ref that no longer names the surface it was issued for.
    STALE_SURFACE = "stale_surface"
    STALE_WORKER_GENERATION = "stale_worker_generation"
    STALE_CONTROL = "stale_control"
    SURFACE_UNAVAILABLE = "surface_unavailable"
    SURFACE_CHANGED = "surface_changed"
    # Trust boundaries. These surfaces are never inspected, and a refusal
    # returns zero content from them.
    ELEVATED_WINDOW = "elevated_window_refused"
    INTEGRITY_UNVERIFIABLE = "integrity_unverifiable"
    CREDENTIAL_SURFACE = "credential_surface"
    # Re-resolving a control from the live tree (no action uses it yet).
    ELEMENT_MISSING = "element_missing"
    ELEMENT_AMBIGUOUS = "element_ambiguous"
    ELEMENT_CHANGED = "element_changed"
    BACKEND_FAILED = "desktop_backend_failed"


#: What the runtime maps a refusal to on its own HTTP surface.
HTTP_STATUS: dict[DesktopReason, int] = {
    DesktopReason.UNSUPPORTED: 503,
    DesktopReason.DISABLED: 503,
    DesktopReason.WORKER_UNAVAILABLE: 503,
    DesktopReason.OBSERVATION_TIMEOUT: 504,
    DesktopReason.STALE_SURFACE: 409,
    DesktopReason.STALE_WORKER_GENERATION: 409,
    DesktopReason.STALE_CONTROL: 409,
    DesktopReason.SURFACE_UNAVAILABLE: 409,
    DesktopReason.SURFACE_CHANGED: 409,
    DesktopReason.ELEVATED_WINDOW: 403,
    DesktopReason.INTEGRITY_UNVERIFIABLE: 403,
    DesktopReason.CREDENTIAL_SURFACE: 403,
    DesktopReason.ELEMENT_MISSING: 409,
    DesktopReason.ELEMENT_AMBIGUOUS: 409,
    DesktopReason.ELEMENT_CHANGED: 409,
    DesktopReason.BACKEND_FAILED: 502,
}


class DesktopRefusal(Exception):
    """A refused or failed desktop operation. `code` is stable and text-free."""

    def __init__(self, code: DesktopReason) -> None:
        super().__init__(f"The desktop operation was refused ({code.value}).")
        self.code = code

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]
