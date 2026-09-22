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
    #: The whole-surface credential scan hit its time/element budget before it could prove the WHOLE
    #: surface is credential-free. An incomplete scan is never treated as a clean one: whatever asked
    #: for it (an S4 mutation or an S5 capture) is refused, fail-closed, exactly as if a credential
    #: field had actually been found.
    CREDENTIAL_SCAN_INCOMPLETE = "credential_scan_incomplete"
    # Re-resolving a control from the live tree (no action uses it yet).
    ELEMENT_MISSING = "element_missing"
    ELEMENT_AMBIGUOUS = "element_ambiguous"
    ELEMENT_CHANGED = "element_changed"
    BACKEND_FAILED = "desktop_backend_failed"
    # S3 effects. Every one of these is raised BEFORE an effect could begin, except where the
    # worker's own response says otherwise.
    HUMAN_INPUT_DETECTED = "human_input_detected"
    SURFACE_NOT_FOCUSABLE = "surface_not_focusable"
    NOT_SCROLLABLE = "not_scrollable"
    APP_NOT_REGISTERED = "app_not_registered"
    LAUNCH_REFUSED = "launch_refused"
    DUPLICATE_DISPATCH = "duplicate_dispatch"
    # S4 effects. Also raised BEFORE an effect could begin.
    NOT_A_VALUE_CONTROL = "not_a_value_control"
    READ_ONLY_CONTROL = "read_only_control"
    SENSITIVE_TARGET_REFUSED = "sensitive_target_refused"
    NOT_SELECTABLE = "not_selectable"
    OPTION_WRONG_CONTAINER = "option_wrong_container"
    NOT_INVOKABLE = "not_invokable"
    UNSUPPORTED_OR_UNKNOWN_EFFECT = "unsupported_or_unknown_effect"
    # Raised only from the part of an effect that runs after the OS call may have begun.
    EFFECT_UNCERTAIN = "desktop_effect_uncertain"
    # S5 visual fallback. Every one of these is raised BEFORE a capture could begin, except
    # `CAPTURE_UNCERTAIN` (after the one native call) and `CAPTURE_SCOPE_UNCERTAIN` (the crop itself
    # could not be proven exact, so the worker stops rather than guess).
    FALLBACK_NOT_ELIGIBLE = "fallback_not_eligible"
    CAPTURE_BLOCKED = "capture_blocked"
    CAPTURE_SCOPE_UNCERTAIN = "capture_scope_uncertain"
    CAPTURE_REFUSED = "capture_refused"
    CAPTURE_UNCERTAIN = "desktop_capture_uncertain"
    FRAME_STALE = "frame_stale"
    FRAME_EXPIRED = "frame_expired"
    VISION_GRANT_NOT_ACTIVE = "vision_grant_not_active"
    VISION_IMAGE_ALREADY_USED = "vision_image_already_used"


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
    DesktopReason.HUMAN_INPUT_DETECTED: 409,
    DesktopReason.SURFACE_NOT_FOCUSABLE: 409,
    DesktopReason.NOT_SCROLLABLE: 409,
    DesktopReason.APP_NOT_REGISTERED: 404,
    DesktopReason.LAUNCH_REFUSED: 409,
    DesktopReason.DUPLICATE_DISPATCH: 409,
    DesktopReason.NOT_A_VALUE_CONTROL: 409,
    DesktopReason.READ_ONLY_CONTROL: 409,
    DesktopReason.SENSITIVE_TARGET_REFUSED: 403,
    DesktopReason.NOT_SELECTABLE: 409,
    DesktopReason.OPTION_WRONG_CONTAINER: 409,
    DesktopReason.NOT_INVOKABLE: 409,
    DesktopReason.UNSUPPORTED_OR_UNKNOWN_EFFECT: 409,
    DesktopReason.EFFECT_UNCERTAIN: 502,
    DesktopReason.FALLBACK_NOT_ELIGIBLE: 409,
    DesktopReason.CAPTURE_BLOCKED: 403,
    DesktopReason.CAPTURE_SCOPE_UNCERTAIN: 409,
    DesktopReason.CAPTURE_REFUSED: 502,
    DesktopReason.CAPTURE_UNCERTAIN: 502,
    DesktopReason.FRAME_STALE: 409,
    DesktopReason.FRAME_EXPIRED: 409,
    DesktopReason.VISION_GRANT_NOT_ACTIVE: 409,
    DesktopReason.VISION_IMAGE_ALREADY_USED: 409,
    DesktopReason.CREDENTIAL_SCAN_INCOMPLETE: 403,
}


class DesktopRefusal(Exception):
    """A refused or failed desktop operation. `code` is stable and text-free."""

    def __init__(self, code: DesktopReason) -> None:
        super().__init__(f"The desktop operation was refused ({code.value}).")
        self.code = code

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]
