"""The six reviewed desktop effects and the rules that surround every one of them.

    focus_surface       UIA `SetFocus` on one already-visible, ordinary top-level window
    scroll_control      one UIA `ScrollPattern.Scroll` by a closed step on one re-resolved control
    launch_app          start (or bring forward) one registered application
    set_control_value   one UIA `ValuePattern.SetValue` on one re-resolved control (S4)
    select_control      one UIA `SelectionItem.Select` on one re-resolved control (S4)
    invoke_control       one UIA `InvokePattern.Invoke` on one re-resolved control, for one closed,
                          reviewed, deterministically-verifiable effect (S4)

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

S4 adds a second identity check before step 4 for all three mutation effects: the control's own
re-resolution and the surface's own trust checks already refuse Lumi, an elevated process, a
credential/consent broker and a credential input, but a value written, selected or invoked in a shell,
terminal, script host or Windows security surface is dangerous by *what runs it*, not by the label on
the field, so those process images (and file-picker window titles) are refused again here regardless
of role or name.
"""

import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Final, Literal, Protocol

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.observer import (
    DesktopObserver,
    ElementUnavailable,
    ResolvedControl,
    UiaBackend,
    _credential_scan,
    _guarded_walk,
)
from app.desktop.protocol import (
    DesktopPattern,
    FocusRequest,
    FocusResponse,
    InputBaselineResponse,
    InvokeEffect,
    InvokeRequest,
    InvokeResponse,
    LaunchRequest,
    LaunchResponse,
    ScrollRequest,
    ScrollResponse,
    ScrollStep,
    SelectRequest,
    SelectResponse,
    SetValueRequest,
    SetValueResponse,
)
from app.desktop.registry import AppRegistry, RegisteredApp, same_file
from app.desktop.surfaces import DENIED_IMAGES, ProcessIdentity, ResolvedSurface, SurfaceTable

MAX_REMEMBERED_DISPATCHES: Final = 256
#: How long a launch waits for the new application's window before reporting `surface: None`.
LAUNCH_WINDOW_SECONDS: Final = 4.0
LAUNCH_POLL_SECONDS: Final = 0.25
#: Bound for the fresh whole-surface credential re-scan every S4 mutation performs immediately before
#: acting. Short: this only needs to find a credential input if one exists, not project the whole tree.
MUTATION_CREDENTIAL_SCAN_SECONDS: Final = 5.0

#: Process images an S4 mutation may never target, whatever the control's role or name says: terminals,
#: shells and script hosts (a value typed there is arbitrary code, not app input -- the brief's own
#: "terminal/console/PowerShell/cmd" denial list), the credential/consent broker images S1 already
#: withholds as whole surfaces, and the Windows security app itself (readable, unlike the broker
#: dialogs, but not a place Lumi writes into). Deliberately narrower than the launch registry's
#: `FORBIDDEN_EXECUTABLES`: that set also denies ordinary interpreter/runtime hosts (python.exe,
#: node.exe, java.exe, ...) and admin tools that are not themselves a command surface -- appropriate to
#: refuse as a *launch target* (nothing should start a bare interpreter) but not a reason to refuse
#: writing into an ordinary GUI app that merely happens to run on one of those runtimes.
_SENSITIVE_TARGET_IMAGES: Final = DENIED_IMAGES | frozenset(
    {
        "cmd.exe", "powershell.exe", "pwsh.exe", "powershell_ise.exe", "wscript.exe", "cscript.exe",
        "mshta.exe", "bash.exe", "wsl.exe", "conhost.exe", "wt.exe", "ssh.exe", "scp.exe", "sftp.exe",
        "telnet.exe", "winrs.exe", "wsmprovhost.exe", "securityhealthservice.exe", "sechealthui.exe",
    }
)
#: Best-effort title match for a Windows common-dialog (file open/save/browse) surface. UIA exposes no
#: reliable "this is a file picker" signal; a window titled like one is refused for `set_control_value`
#: regardless of role. English-centric, like the credential-name heuristic; documented as a residual.
_FILE_DIALOG_TITLES: Final = frozenset(
    {"open", "save as", "save", "browse for folder", "select folder", "choose file", "choose files"}
)


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

    # -- S4: bounded semantic mutations -------------------------------------------

    def _mutation_target(
        self, surface_ref: str, surface_epoch: int, observation_id: uuid.UUID, control_ref: str
    ) -> tuple[ResolvedSurface, ResolvedControl]:
        """Re-prove the surface and re-derive the control, exactly as `scroll` does.

        Killed refs, Lumi/elevation re-checks and exact-match re-resolution are all inherited from
        `SurfaceTable.resolve` and `DesktopObserver.resolve_control`; nothing here re-implements them.
        Credential exclusion is re-checked TWICE: `resolve_control` -> `_rederive` re-checks it only
        along the target's own ancestor path (itself and each ancestor's siblings), the same bound a
        read-only re-resolution needs; a whole-surface mutation gets the STRONGER, S1-grade guarantee
        below as well, because an application can add a credential field anywhere in the live tree
        between the approved observation and this exact moment, not only beside the target's own path.
        The specific target is resolved FIRST, so an ordinary vanished/replaced/ambiguous target still
        reports its own specific reason; the broader whole-surface scan runs only once the target
        itself is known to still exist, so it is never the thing that masks a plain stale-target case.
        """
        resolved = self._surfaces.resolve(surface_ref, surface_epoch)
        control = self._observer.resolve_control(surface_ref, surface_epoch, observation_id, control_ref)
        if control.element is None or control.locator is None:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING)
        self._refuse_if_credential_surface(resolved)
        return resolved, control

    def _refuse_if_credential_surface(self, resolved: ResolvedSurface) -> None:
        """A fresh, whole-tree credential scan of the LIVE surface, immediately before any S4 mutation.

        `resolve_control`'s own re-derivation only re-checks credential exclusion along the target
        control's own ancestor path (each ancestor and that ancestor's siblings) -- enough for an
        ordinary stale-target check, but not enough to prove the WHOLE surface is still credential-free
        the way the original observation was: a credential field added inside some unrelated sibling
        subtree since the approved observation would never appear on that path. This runs a dedicated,
        lenient scan (`_credential_scan`, not `observe()`'s own stricter walk): unrelated transient UI
        churn elsewhere in the tree must never be the reason a mutation aimed at a specific, still-live
        control is refused, so a node that disappears mid-scan is skipped rather than treated as proof
        the whole surface changed. It still refuses on the first credential input actually found.
        """
        root = self._observer.root_for(resolved)
        _credential_scan(root, deadline=time.monotonic() + MUTATION_CREDENTIAL_SCAN_SECONDS)

    def _refuse_if_sensitive_target(self, resolved: ResolvedSurface) -> None:
        """A second, role-independent refusal for `set_control_value` and `invoke_control`.

        Checked against the CURRENT process image (not a cached one): a target re-verified a moment
        earlier as an ordinary window is refused again here if it is a shell, script host, terminal or
        the Windows security app, and again if its current window title looks like a file picker.
        """
        image = self._surfaces.probe.process_image(resolved.identity.pid)
        if image is not None and image.lower() in _SENSITIVE_TARGET_IMAGES:
            raise DesktopRefusal(DesktopReason.SENSITIVE_TARGET_REFUSED)
        title = self._surfaces.probe.window_title(resolved.identity.hwnd).strip().lower()
        if title in _FILE_DIALOG_TITLES:
            raise DesktopRefusal(DesktopReason.SENSITIVE_TARGET_REFUSED)

    def _kill_old_refs(self, resolved: ResolvedSurface) -> None:
        """Before any S4 effect: whatever it does, no `uN` of the observation it was proposed
        against may be used again (the same rule `scroll` follows)."""
        resolved.slot.controls = None

    def set_value(self, request: SetValueRequest) -> SetValueResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, SetValueResponse):
            return replay
        resolved, control = self._mutation_target(
            request.surface_ref, request.surface_epoch, request.observation_id, request.control_ref
        )
        assert control.element is not None and control.locator is not None
        self._refuse_if_sensitive_target(resolved)
        if DesktopPattern.VALUE not in control.patterns:
            raise DesktopRefusal(DesktopReason.NOT_A_VALUE_CONTROL)
        try:
            state = control.element.value_state()
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING) from None
        if state is None:
            raise DesktopRefusal(DesktopReason.NOT_A_VALUE_CONTROL)
        if state.read_only:
            raise DesktopRefusal(DesktopReason.READ_ONLY_CONTROL)
        self._refuse_if_human_input(request.input_tick)
        self._kill_old_refs(resolved)
        locator = control.locator
        # A second, narrower check immediately before the actual OS call: the gap this closes is COM
        # pattern acquisition inside `set_value` itself, which can block for a moment the person spends
        # touching the machine. It cannot be closed to zero -- the native call still has to acquire its
        # own pattern reference -- but this keeps the unchecked window to that one call, not everything
        # since the surface/control re-resolution above.
        self._refuse_if_human_input(request.input_tick)
        try:
            control.element.set_value(request.value)
            after = self._observer.rederive(resolved, locator)
            self._surfaces.verify_unchanged(resolved)
            after_state = after.element.value_state() if after.element is not None else None
        except (ElementUnavailable, DesktopRefusal):
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        # Known success only if a fresh, re-resolved read of the CANONICAL value equals exactly what
        # was approved. `SetValue` returning without an exception is never trusted on its own. A read
        # that comes back empty (`after_state is None`, e.g. a transient COM failure inside
        # `value_state()`) is NOT the same as a read that came back with a genuinely different value:
        # the former means the write's real effect is unknown, not that it is known to have failed.
        if after_state is None:
            outcome: Literal["set", "not_set", "uncertain"] = "uncertain"
        elif after_state.value == request.value:
            outcome = "set"
        else:
            outcome = "not_set"
        response = SetValueResponse(
            worker_generation=self.generation,
            dispatch_id=request.dispatch_id,
            outcome=outcome,
            input_changed=self._input_changed(request.input_tick),
        )
        self._dispatches.finish(request.dispatch_id, response)
        return response

    def select(self, request: SelectRequest) -> SelectResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, SelectResponse):
            return replay
        resolved, container = self._mutation_target(
            request.surface_ref, request.surface_epoch, request.observation_id, request.container_ref
        )
        _, option = self._mutation_target(
            request.surface_ref, request.surface_epoch, request.observation_id, request.option_ref
        )
        assert option.element is not None and option.locator is not None and container.locator is not None
        # Found by an independent integration review: `set_control_value` and `invoke_control` both
        # refuse a shell/terminal/script-host/security-app target or a file-picker window; `select`
        # had no such check, even though selecting a file inside a real Open/Save common dialog's file
        # list populates the filename edit control, and selecting inside a terminal's own UI is the
        # same class of risk the other two effects already refuse.
        self._refuse_if_sensitive_target(resolved)
        # A second, LIVE proof of containment: `_mutation_target` already proved `container` and
        # `option` each still independently match their OWN previously-recorded path, but that alone
        # does not prove `option` is actually found hanging off the live element `container` resolves
        # to, right now -- a same-shaped replacement container could have been swapped in with the
        # option reparented into it. `rederive_descendant` re-resolves `option` a second time starting
        # FROM the live `container` element, so only a genuinely still-nested option passes.
        live_option = self._observer.rederive_descendant(resolved, container.locator, option.locator)
        if live_option.element is None or live_option.locator is None:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING)
        if DesktopPattern.SELECTION_ITEM not in live_option.patterns:
            raise DesktopRefusal(DesktopReason.NOT_SELECTABLE)
        self._refuse_if_human_input(request.input_tick)
        self._kill_old_refs(resolved)
        locator = live_option.locator
        # See `set_value`'s matching check: narrows the unchecked window to the native call's own
        # pattern acquisition, which cannot itself be checked mid-flight.
        self._refuse_if_human_input(request.input_tick)
        try:
            live_option.element.select()
            after = self._observer.rederive(resolved, locator)
            self._surfaces.verify_unchanged(resolved)
            # The actual selected STATE, not merely that the pattern is still advertised: `Select()`
            # returning is never trusted on its own.
            is_selected = self._is_selected(after)
        except (ElementUnavailable, DesktopRefusal):
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        # `is_selected is None`: the verifying read itself did not produce an answer (`element is None`
        # after re-derivation with no exception raised) -- unknown, never the same as a verified "no".
        if is_selected is None:
            outcome: Literal["selected", "not_selected", "uncertain"] = "uncertain"
        else:
            outcome = "selected" if is_selected else "not_selected"
        response = SelectResponse(
            worker_generation=self.generation,
            dispatch_id=request.dispatch_id,
            outcome=outcome,
            input_changed=self._input_changed(request.input_tick),
        )
        self._dispatches.finish(request.dispatch_id, response)
        return response

    @staticmethod
    def _is_selected(control: ResolvedControl) -> bool | None:
        if control.element is None:
            return None
        props = control.element.props("full")
        return bool(props.selected)

    def invoke(self, request: InvokeRequest) -> InvokeResponse:
        replay = self._dispatches.begin(request.dispatch_id)
        if isinstance(replay, InvokeResponse):
            return replay
        resolved, control = self._mutation_target(
            request.surface_ref, request.surface_epoch, request.observation_id, request.control_ref
        )
        assert control.element is not None and control.locator is not None
        self._refuse_if_sensitive_target(resolved)
        if DesktopPattern.INVOKE not in control.patterns:
            raise DesktopRefusal(DesktopReason.NOT_INVOKABLE)
        if request.effect is not InvokeEffect.NAME_TOGGLE:
            # Every closed effect this worker knows how to verify is listed above; anything else is
            # refused rather than invoked and hoped for.
            raise DesktopRefusal(DesktopReason.UNSUPPORTED_OR_UNKNOWN_EFFECT)
        name_before = control.name
        self._refuse_if_human_input(request.input_tick)
        self._kill_old_refs(resolved)
        locator = control.locator
        # See `set_value`'s matching check: narrows the unchecked window to the native call's own
        # pattern acquisition, which cannot itself be checked mid-flight.
        self._refuse_if_human_input(request.input_tick)
        try:
            control.element.invoke()
            # NAME_TOGGLE expects the control's own name to have changed: re-deriving by the pre-effect
            # locator step (which includes that name) would always miss it, so the last hop matches by
            # identity instead. Every ancestor step is still matched structurally.
            after = self._observer.rederive_after_mutation(resolved, locator)
            self._surfaces.verify_unchanged(resolved)
        except (ElementUnavailable, DesktopRefusal):
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.EFFECT_UNCERTAIN) from None
        changed = after.name != name_before
        response = InvokeResponse(
            worker_generation=self.generation,
            dispatch_id=request.dispatch_id,
            outcome="invoked" if changed else "no_change",
            input_changed=self._input_changed(request.input_tick),
        )
        self._dispatches.finish(request.dispatch_id, response)
        return response


def _ref_of(resolved: ResolvedSurface) -> str:
    return f"s{resolved.slot.index}"


#: Every ScrollStep the worker will ever act on. A test asserts this equals the protocol enum.
SCROLL_STEPS: Final = frozenset(ScrollStep)
