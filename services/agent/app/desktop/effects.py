"""The three reviewed desktop effects and the rules that surround every one of them.

    focus_surface   UIA `SetFocus` on one already-visible, ordinary top-level window
    scroll_control  one UIA `ScrollPattern.Scroll` by a closed step on one re-resolved control
    launch_app      start (or bring forward) one registered application

Everything else stays forbidden: no text injection, pointer or wheel input, coordinate, shortcut, shell,
path or argument supplied by a caller. The OS calls themselves live behind `EffectPlatform`, whose members
are exactly the primitives S3 needs, and the source scanner pins that list.

Every effect obeys the same order:

1. refuse a dispatch id this worker has already begun (one dispatch is at most one effect; a finished
   one replays its stored answer, so a lost reply can be recovered without a second effect);
2. re-prove the surface (worker generation, `(ref, epoch)`, process creation time, not Lumi, not
   elevated, not a credential broker) and that it holds no credential input;
3. refuse if the human touched the machine since the approval baseline;
4. perform ONE effect;
5. verify from fresh evidence and report what is actually known.

Anything that fails in step 4 or 5 is `desktop_effect_uncertain`, never a guess.
"""

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Final, Literal, Protocol

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import DesktopObserver, ElementUnavailable, UiaBackend, _guarded_walk
from app.desktop.protocol import (
    FocusRequest,
    FocusResponse,
    InputBaselineResponse,
    LaunchRequest,
    LaunchResponse,
    ScrollRequest,
    ScrollResponse,
    ScrollStep,
)
from app.desktop.registry import AppRegistry, RegisteredApp, same_file
from app.desktop.surfaces import ProcessIdentity, ResolvedSurface, SurfaceTable

MAX_REMEMBERED_DISPATCHES: Final = 256
#: How long a launch waits for the new application's window before reporting `surface: None`.
LAUNCH_WINDOW_SECONDS: Final = 4.0
LAUNCH_POLL_SECONDS: Final = 0.25


class EffectPlatform(Protocol):
    """The whole native effect surface. Nothing else in the worker may focus, spawn or read input."""

    def foreground_hwnd(self) -> int | None: ...

    def last_input_tick(self) -> int:
        """`GetLastInputInfo`'s tick. Compared for equality only; the input itself is never seen."""

    def process_path(self, pid: int) -> str | None:
        """The full image path of a live process. Used only to compare against a registered path."""

    def spawn(self, app: RegisteredApp) -> int:
        """Start exactly `app.executable` with exactly `app.args`, no shell. Returns the new pid."""


class _Dispatches:
    """Per-generation memory of which dispatch ids began, and what they answered."""

    def __init__(self) -> None:
        self._seen: OrderedDict[uuid.UUID, object | None] = OrderedDict()

    def begin(self, dispatch_id: uuid.UUID) -> object | None:
        """None: first time. Otherwise the stored answer of a finished dispatch (replay).

        An in-flight or failed dispatch is refused: it may already have had its effect.
        """
        if dispatch_id in self._seen:
            answer = self._seen[dispatch_id]
            if answer is None:
                raise DesktopRefusal(DesktopReason.DUPLICATE_DISPATCH)
            return answer
        self._seen[dispatch_id] = None
        while len(self._seen) > MAX_REMEMBERED_DISPATCHES:
            self._seen.popitem(last=False)
        return None

    def finish(self, dispatch_id: uuid.UUID, answer: object) -> None:
        self._seen[dispatch_id] = answer


class DesktopEffects:
    def __init__(
        self,
        *,
        surfaces: SurfaceTable,
        observer: DesktopObserver,
        backend: UiaBackend,
        platform: EffectPlatform,
        registry: AppRegistry,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._surfaces = surfaces
        self._observer = observer
        self._backend = backend
        self._platform = platform
        self._registry = registry
        self._clock = clock
        self._sleep = sleep
        self._dispatches = _Dispatches()
        #: `app_id -> pid` of a process this worker spawned but could not verify. It is a child of this worker, so
        #: ancestry would call it Lumi's own and a later request would not see it and would start a second copy.
        self._pending_launch: dict[str, int] = {}
        self.generation = observer.worker_generation

    # -- human takeover ----------------------------------------------------------

    def input_baseline(self) -> InputBaselineResponse:
        return InputBaselineResponse(worker_generation=self.generation, input_tick=self._platform.last_input_tick())

    def _refuse_if_human_input(self, baseline: int) -> None:
        if self._platform.last_input_tick() != baseline:
            raise DesktopRefusal(DesktopReason.HUMAN_INPUT_DETECTED)

    def _input_changed(self, baseline: int) -> bool:
        """After an effect: a failed read of the input generation is unknown, never "the effect did not happen"."""
        try:
            return self._platform.last_input_tick() != baseline
        except Exception:  # noqa: BLE001 - detail is private; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None

    # -- focus -------------------------------------------------------------------

    def focus(self, request: FocusRequest) -> FocusResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, FocusResponse):
            return replay
        resolved = self._focusable(request.surface_ref, request.surface_epoch)
        self._refuse_if_human_input(request.input_tick)
        focused = self._focus_resolved(resolved)
        response = FocusResponse(
            worker_generation=self.generation,
            dispatch_id=request.dispatch_id,
            surface_ref=request.surface_ref,
            surface_epoch=request.surface_epoch,
            outcome="focused" if focused else "not_focused",
            input_changed=self._input_changed(request.input_tick),
        )
        self._dispatches.finish(request.dispatch_id, response)
        return response

    def _focusable(self, surface_ref: str, surface_epoch: int) -> ResolvedSurface:
        """Re-prove the target and refuse anything that is not an ordinary, visible, non-credential window."""
        resolved = self._surfaces.resolve(surface_ref, surface_epoch)
        facts = next(
            (item for item in self._surfaces.probe.enumerate_windows() if item.hwnd == resolved.identity.hwnd), None
        )
        # Never show, restore or un-cloak anything: focus only changes which visible window is in front.
        if facts is None or not facts.visible or facts.minimized or facts.cloaked or facts.hung:
            raise DesktopRefusal(DesktopReason.SURFACE_NOT_FOCUSABLE)
        _guarded_walk(self._observer.root_for(resolved), "scan")
        return resolved

    def _focus_resolved(self, resolved: ResolvedSurface) -> bool:
        """ONE foreground request, then verify. Any doubt after the call is uncertainty, not failure."""
        hwnd = resolved.identity.hwnd
        try:
            # UI Automation's own SetFocus on the window root. Unlike a raw foreground request from a
            # background process, it is not refused by the foreground lock, and it needs no key or click.
            self._observer.root_for(resolved).focus()
        except (ElementUnavailable, DesktopRefusal):
            return False
        except Exception:  # noqa: BLE001 - detail is private; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        try:
            self._surfaces.verify_unchanged(resolved)
            self._surfaces.resolve(_ref_of(resolved), resolved.slot.epoch)
        except DesktopRefusal:
            # The expected window is gone or was replaced: it is definitely not the focused target.
            return False
        try:
            foreground = self._platform.foreground_hwnd()
            return (
                foreground == hwnd
                and self._surfaces.probe.window_pid(foreground) == resolved.identity.pid
            )
        except Exception:  # noqa: BLE001 - the focus call was made; whether it took is not knowable here.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None

    # -- scroll ------------------------------------------------------------------

    def scroll(self, request: ScrollRequest) -> ScrollResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, ScrollResponse):
            return replay
        resolved = self._surfaces.resolve(request.surface_ref, request.surface_epoch)
        control = self._observer.resolve_control(
            request.surface_ref, request.surface_epoch, request.observation_id, request.control_ref
        )
        element = control.element
        if element is None:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING)
        try:
            state = element.scroll_state()
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING) from None
        if state is None or not state.vertically_scrollable:
            raise DesktopRefusal(DesktopReason.NOT_SCROLLABLE)
        self._refuse_if_human_input(request.input_tick)
        # The old refs die here, before the effect: whatever the scroll does, no `uN` of the
        # observation it was proposed against can be used again.
        resolved.slot.controls = None
        before = state.vertical_percent
        try:
            element.scroll(request.step)
            after_state = element.scroll_state()
            self._surfaces.verify_unchanged(resolved)
        except (ElementUnavailable, DesktopRefusal):
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        after = after_state.vertical_percent if after_state is not None else None
        # `unchanged` only when both positions were read and are equal. If either is unreadable the scroll
        # call returned normally, so it is reported as done with the missing position visible as `None`.
        moved = before is None or after is None or after != before
        try:
            response = ScrollResponse(
                worker_generation=self.generation,
                dispatch_id=request.dispatch_id,
                outcome="scrolled" if moved else "unchanged",
                percent_before=before,
                percent_after=after,
                input_changed=self._input_changed(request.input_tick),
            )
        except DesktopRefusal:
            raise
        except Exception:  # noqa: BLE001 - the scroll happened; describing it failed.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        self._dispatches.finish(request.dispatch_id, response)
        return response

    # -- registered application launch -------------------------------------------

    def launch(self, request: LaunchRequest) -> LaunchResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, LaunchResponse):
            return replay
        app = self._registry.get(request.app_id)
        self._refuse_if_human_input(request.input_tick)
        self._refuse_lumis_own_image(app)
        self._adopt_pending_launch(app)
        existing = self._running_instances(app)
        if existing:
            # Never a second instance: bring the one that exists forward, or say it has no window yet.
            surface = self._surface_of(existing)
            focused = False
            ref = epoch = None
            if surface is not None:
                ref, epoch = surface
                # The inspection above took time: the person may have used the machine since the check.
                self._refuse_if_human_input(request.input_tick)
                try:
                    focused = self._focus_resolved(self._focusable(ref, epoch))
                except DesktopRefusal as refusal:
                    if refusal.code is DesktopReason.EFFECT_UNCERTAIN:
                        raise
                    focused = False
            response = self._launch_response(request, "already_running", ref, epoch, focused)
        else:
            # Re-check immediately before the effect: enumerating every process takes real time.
            self._refuse_if_human_input(request.input_tick)
            try:
                pid = self._platform.spawn(app)
            except DesktopRefusal:
                raise
            except Exception:  # noqa: BLE001 - detail is private; none is kept.
                # Whether a process was created is unknown from here.
                raise DesktopRefusal(DesktopReason.LAUNCH_REFUSED) from None
            # From here the process exists. If it cannot be verified it is remembered as PENDING, so the next request
            # finds it (or refuses) instead of starting another.
            self._pending_launch[app.app_id] = pid
            try:
                identity = self._surfaces.probe.process_identity(pid)
                verified = identity is not None and self._is_app(app, identity)
            except Exception:  # noqa: BLE001 - the process was created; whatever failed, the launch happened.
                raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
            if identity is None or not verified:
                raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN)
            self._pending_launch.pop(app.app_id, None)
            self._surfaces.exclusion.remember_launch(identity)
            try:
                surface = self._wait_for_surface(app)
            except Exception:  # noqa: BLE001 - the process exists; whatever failed, the launch happened.
                raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
            ref, epoch = surface if surface else (None, None)
            response = self._launch_response(request, "launched", ref, epoch, False)
        response = response.model_copy(update={"input_changed": self._input_changed(request.input_tick)})
        self._dispatches.finish(request.dispatch_id, response)
        return response

    def _launch_response(
        self, request: LaunchRequest, outcome: Literal["launched", "already_running"], ref: str | None, epoch: int | None, focused: bool
    ) -> LaunchResponse:
        return LaunchResponse(
            worker_generation=self.generation,
            dispatch_id=request.dispatch_id,
            app_id=request.app_id,
            outcome=outcome,
            surface_ref=ref,
            surface_epoch=epoch,
            focused=focused,
            input_changed=False,
        )

    def _adopt_pending_launch(self, app: RegisteredApp) -> None:
        """Resolve an earlier launch whose verification failed, before anything may spawn again."""
        pid = self._pending_launch.get(app.app_id)
        if pid is None:
            return
        probe = self._surfaces.probe
        try:
            identity = probe.process_identity(pid)
            if identity is None:
                # It exited: nothing is running from that launch.
                del self._pending_launch[app.app_id]
                return
            if not self._is_app(app, identity):
                # Alive but not provably the registered program (or the pid was reused): do not guess.
                raise DesktopRefusal(DesktopReason.LAUNCH_REFUSED)
        except DesktopRefusal:
            raise
        except Exception:  # noqa: BLE001 - still unverifiable, so still no second spawn.
            raise DesktopRefusal(DesktopReason.LAUNCH_REFUSED) from None
        self._surfaces.exclusion.remember_launch(identity)
        del self._pending_launch[app.app_id]

    def _refuse_lumis_own_image(self, app: RegisteredApp) -> None:
        """A registered application can never be one of Lumi's own executables.

        Launching Lumi's own program would start a second Lumi, and the launch exemption would then make that
        process (and its children) targetable, which is exactly the self-automation the exclusion exists to prevent.
        """
        for root in self._surfaces.exclusion.roots:
            path = self._platform.process_path(root.pid)
            if path is not None and same_file(path, app.executable):
                raise DesktopRefusal(DesktopReason.LAUNCH_REFUSED)

    def _is_app(self, app: RegisteredApp, identity: ProcessIdentity) -> bool:
        path = self._platform.process_path(identity.pid)
        return path is not None and same_file(path, app.executable)

    def _running_instances(self, app: RegisteredApp) -> list[ProcessIdentity]:
        """Live processes whose image is exactly the registered executable (by creation-bound identity).

        Lumi's own processes never count. A reused PID is a different creation time, and a process that
        merely has the same file name in another directory is not the registered application.
        """
        parents, job = self._surfaces.process_snapshot()
        found: list[ProcessIdentity] = []
        for pid in parents:
            identity = self._surfaces.probe.process_identity(pid)
            if identity is None or not self._is_app(app, identity):
                continue
            if self._surfaces.exclusion.excludes(self._surfaces.probe, pid, parents, job):
                continue
            found.append(identity)
        return found

    def _surface_of(self, processes: list[ProcessIdentity]) -> tuple[str, int] | None:
        """The current `(ref, epoch)` of an eligible window owned by one of these exact processes."""
        self._surfaces.refresh()
        wanted = set(processes)
        for slot in self._surfaces.slots:
            identity = slot.identity
            if identity is not None and ProcessIdentity(identity.pid, identity.created) in wanted:
                return f"s{slot.index}", slot.epoch
        return None

    def _wait_for_surface(self, app: RegisteredApp) -> tuple[str, int] | None:
        deadline = self._clock() + LAUNCH_WINDOW_SECONDS
        while True:
            found = self._surface_of(self._running_instances(app))
            if found is not None or self._clock() >= deadline:
                return found
            self._sleep(LAUNCH_POLL_SECONDS)


def _ref_of(resolved: ResolvedSurface) -> str:
    return f"s{resolved.slot.index}"


#: Every ScrollStep the worker will ever act on. A test asserts this equals the protocol enum.
SCROLL_STEPS: Final = frozenset(ScrollStep)
