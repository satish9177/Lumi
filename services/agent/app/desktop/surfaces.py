"""Windows surface inventory and identity. Pure logic over a passive system probe.

A window handle is not an identity. Windows recycles HWNDs and PIDs, so a ref that
only remembered "hwnd 0x1234" could silently start naming a different program.
Each slot here binds `(pid, hwnd, process creation time)` and carries an epoch that
only ever goes up. The pair `(surface_ref, surface_epoch)` is therefore never issued
twice in one worker generation: a window that closes, a process that restarts, or
an HWND that is reused all end up at a higher epoch, and the old pair is stale.

Nothing here can change a window. The `SystemProbe` interface has no method that
focuses, shows, moves, closes or launches anything, and the source scanner in
`tests/desktop_source_scan.py` keeps it that way.

**Exclusion is by process ancestry, never by title.** The supervisor passes trusted
process identities (the runtime and Electron). Any process descended from one of them
is Lumi's own: the renderer, DevTools, the runtime, the browser worker and every
Chromium it owns (sign-in, takeover and form-preparation windows), and this worker.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    MAX_APPLICATION_LABEL,
    MAX_SURFACES,
    MAX_TITLE,
    SURFACE_REF_PATTERN,
    SurfaceRecord,
    clean_text,
)

#: Windows mandatory integrity RIDs (SECURITY_MANDATORY_*_RID).
INTEGRITY_UNTRUSTED: Final = 0x0000
INTEGRITY_LOW: Final = 0x1000
INTEGRITY_MEDIUM: Final = 0x2000
INTEGRITY_HIGH: Final = 0x3000
INTEGRITY_SYSTEM: Final = 0x4000

#: Processes that own credential, consent or lock-screen UI. A second layer beside
#: the integrity check: some of them are same-integrity brokers that host credential
#: prompts for other programs.
DENIED_IMAGES: Final = frozenset(
    {
        "consent.exe",
        "credentialuibroker.exe",
        "credentialenrollmentmanager.exe",
        "logonui.exe",
        "lockapp.exe",
        "winlogon.exe",
        "securityhealthsystray.exe",
        "windowsdefenderslauncher.exe",
        "userfeedbackservice.exe",
    }
)

MAX_ANCESTRY_HOPS: Final = 64
MAX_LAUNCHED: Final = 32


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A process, not a number: the creation time separates PID reuse."""

    pid: int
    created: int


@dataclass(frozen=True, slots=True)
class SurfaceIdentity:
    pid: int
    hwnd: int
    created: int


@dataclass(frozen=True, slots=True)
class WindowFacts:
    """What a passive enumeration learns about one top-level window. Worker-internal."""

    hwnd: int
    pid: int
    visible: bool
    minimized: bool
    cloaked: bool
    owned: bool
    tool_window: bool
    app_window: bool
    hung: bool


class SystemProbe(Protocol):
    """Read-only view of the desktop. Every method is a query."""

    def enumerate_windows(self) -> Sequence[WindowFacts]:
        """Top-level windows in z-order, front to back."""

    def window_pid(self, hwnd: int) -> int | None:
        """The process that owns `hwnd` right now, or None if the window is gone."""

    def window_title(self, hwnd: int) -> str: ...

    def process_identity(self, pid: int) -> ProcessIdentity | None: ...

    def process_image(self, pid: int) -> str | None:
        """Lower-case executable file name, no directory."""

    def integrity_level(self, pid: int) -> int | None:
        """Mandatory integrity RID, or None when it cannot be established."""

    def own_integrity_level(self) -> int | None: ...

    def process_parents(self) -> dict[int, int]:
        """pid -> parent pid, from one process snapshot. Raises `OSError` if it cannot be taken:
        an empty answer would silently mean "nothing descends from Lumi"."""

    def job_members(self) -> frozenset[int]:
        """Every process in this process's own Windows job object (empty when it is in none).
        Membership is live, so a process that has exited is not in it and a recycled PID cannot be."""


@dataclass(frozen=True, slots=True)
class ExclusionPolicy:
    """Which processes are Lumi's own and can never be a target."""

    roots: tuple[ProcessIdentity, ...]
    #: When the supervisor established that the runtime created a kill-on-close job, every process in
    #: this worker's job is Lumi's (the runtime, the browser worker, every Chromium it owns, this
    #: worker). Job membership needs no parent chain, so it survives a short-lived launcher that
    #: exited between a trusted root and a Lumi window.
    trust_job: bool = False
    #: Registered applications this worker started on the user's approved request. A launched process
    #: descends from this worker, so ancestry alone would call it Lumi's own and it could never be
    #: focused or read. Each entry is bound to a creation time; PID reuse cannot borrow it.
    launched: list[ProcessIdentity] = field(default_factory=list, compare=False)

    def remember_launch(self, identity: ProcessIdentity) -> None:
        if identity not in self.launched:
            self.launched.append(identity)
            del self.launched[:-MAX_LAUNCHED]

    @classmethod
    def resolve(
        cls, probe: SystemProbe, root_pids: Iterable[int], *, trust_job: bool = False
    ) -> "ExclusionPolicy":
        """Bind each trusted PID to its creation time now.

        A root that is not running cannot be bound, and binding by number alone would
        let a later process reuse it and either be wrongly excluded or, worse, be
        confused with Lumi. Refusing is the fail-closed answer.
        """
        identities: dict[int, ProcessIdentity] = {}
        for pid in {os.getpid(), *root_pids}:
            identity = probe.process_identity(pid)
            if identity is None:
                raise DesktopRefusal(DesktopReason.WORKER_UNAVAILABLE)
            identities[pid] = identity
        return cls(roots=tuple(identities[pid] for pid in sorted(identities)), trust_job=trust_job)

    def excludes(
        self, probe: SystemProbe, pid: int, parents: dict[int, int], job: frozenset[int] = frozenset()
    ) -> bool:
        """True when `pid` is a trusted root, is in Lumi's job, or descends from a root.

        Each hop is checked against creation times: a parent must be at least as old as
        its child, otherwise the recorded parent id belongs to a recycled process and
        the chain is not real ancestry.

        A live process that the process snapshot does not know at all has an unknowable
        lineage, and unknown is treated as Lumi's own rather than as somebody else's.
        """
        current = probe.process_identity(pid)
        if current is not None and current in self.launched:
            return False
        if self.trust_job and pid in job:
            return True
        trusted = {identity.pid: identity for identity in self.roots}
        if current is None:
            return False
        if current.pid not in parents and current.pid not in trusted:
            return True
        for _ in range(MAX_ANCESTRY_HOPS):
            root = trusted.get(current.pid)
            if root is not None and root.created == current.created:
                return True
            parent_pid = parents.get(current.pid)
            if not parent_pid or parent_pid == current.pid:
                return False
            parent = probe.process_identity(parent_pid)
            if parent is None or parent.created > current.created:
                return False
            current = parent
            if current in self.launched:
                # Reached a registered application we started: everything below it is that application's.
                return False
        return False


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit]


@dataclass(slots=True)
class _Slot:
    index: int
    epoch: int = 0
    identity: SurfaceIdentity | None = None
    #: Owned by the observer: the per-node structure records of the last observation, whether that
    #: observation was cut short by time, and the live control table.
    records: tuple[str, ...] | None = None
    partial: bool = False
    controls: object | None = None


@dataclass(frozen=True, slots=True)
class Inventory:
    surfaces: list[SurfaceRecord]
    truncated: bool


@dataclass(slots=True)
class SurfaceTable:
    """The worker's surface slots for one generation."""

    probe: SystemProbe
    exclusion: ExclusionPolicy
    slots: list[_Slot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.slots = [_Slot(index=index) for index in range(1, MAX_SURFACES + 1)]

    # -- inventory ---------------------------------------------------------------

    def _snapshot(self) -> tuple[dict[int, int], frozenset[int]]:
        """The process tree and Lumi's job, or a text-free failure. Never an empty answer."""
        try:
            parents = self.probe.process_parents()
            job = self.probe.job_members() if self.exclusion.trust_job else frozenset()
        except OSError:
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None
        if not parents:
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED)
        return parents, job

    def process_snapshot(self) -> tuple[dict[int, int], frozenset[int]]:
        return self._snapshot()

    @staticmethod
    def _ceiling(own_integrity: int | None) -> int | None:
        """The highest integrity level this worker may read: never above Medium.

        Even if Lumi itself were elevated, an elevated application is not inspected in this slice, so
        the comparison is against `min(own, Medium)` and not against whatever Lumi happens to run as.
        """
        return None if own_integrity is None else min(own_integrity, INTEGRITY_MEDIUM)

    def refresh(self) -> Inventory:
        """Enumerate passively and reconcile the slots against what is there now."""
        # Windows first, process snapshot second: every enumerated window belongs to a process that
        # already existed, so it is in a snapshot taken afterwards (or has exited).
        windows = list(self.probe.enumerate_windows())
        parents, job = self._snapshot()
        ceiling = self._ceiling(self.probe.own_integrity_level())
        eligible: list[tuple[SurfaceIdentity, WindowFacts]] = []
        for facts in windows:
            if not self._is_user_surface(facts):
                continue
            identity = self._identity(facts)
            if identity is None:
                continue
            if self._refusal_for(identity.pid, ceiling, parents, job) is not None:
                continue
            eligible.append((identity, facts))

        truncated = len(eligible) > MAX_SURFACES
        admitted = eligible[:MAX_SURFACES]
        live = {identity for identity, _ in admitted}

        for slot in self.slots:
            if slot.identity is not None and slot.identity not in live:
                self._vacate(slot)
        occupied = {slot.identity: slot for slot in self.slots if slot.identity is not None}
        for identity, _ in admitted:
            if identity not in occupied:
                free = next(slot for slot in self.slots if slot.identity is None)
                free.identity = identity
                free.epoch += 1
                free.records = None
                free.partial = False
                free.controls = None
                occupied[identity] = free

        records: list[SurfaceRecord] = []
        for identity, facts in admitted:
            slot = occupied[identity]
            title = _clip(clean_text(self.probe.window_title(facts.hwnd)), MAX_TITLE)
            image = clean_text(self.probe.process_image(identity.pid) or "")
            label = _clip(image.removesuffix(".exe"), MAX_APPLICATION_LABEL)
            records.append(
                SurfaceRecord(
                    surface_ref=f"s{slot.index}",
                    surface_epoch=slot.epoch,
                    application_label=label,
                    window_title=title,
                    visible=facts.visible,
                    minimized=facts.minimized,
                )
            )
        return Inventory(surfaces=records, truncated=truncated)

    @staticmethod
    def _is_user_surface(facts: WindowFacts) -> bool:
        """Roughly the windows a person sees in the taskbar and Alt-Tab."""
        if not facts.visible and not facts.minimized:
            return False
        if facts.cloaked or facts.hung or facts.tool_window and not facts.app_window:
            return False
        return not facts.owned or facts.app_window

    def _identity(self, facts: WindowFacts) -> SurfaceIdentity | None:
        process = self.probe.process_identity(facts.pid)
        if process is None:
            return None
        return SurfaceIdentity(pid=facts.pid, hwnd=facts.hwnd, created=process.created)

    def _refusal_for(
        self, pid: int, ceiling: int | None, parents: dict[int, int], job: frozenset[int]
    ) -> DesktopReason | None:
        """The reason this process may not be observed, or None if it may.

        Order matters and every unknown fails closed: Lumi's own tree first (it is never
        a target and must not even be described), then the credential/consent image
        deny-list, then integrity.
        """
        if self.exclusion.excludes(self.probe, pid, parents, job):
            return DesktopReason.SURFACE_UNAVAILABLE
        image = self.probe.process_image(pid)
        if image is None or image in DENIED_IMAGES:
            return DesktopReason.SURFACE_UNAVAILABLE
        if ceiling is None:
            return DesktopReason.INTEGRITY_UNVERIFIABLE
        target = self.probe.integrity_level(pid)
        if target is None:
            return DesktopReason.INTEGRITY_UNVERIFIABLE
        if target > ceiling:
            return DesktopReason.ELEVATED_WINDOW
        return None

    # -- resolving a ref ---------------------------------------------------------

    def resolve(self, surface_ref: str, surface_epoch: int) -> "ResolvedSurface":
        """The live identity behind `(ref, epoch)`, re-proved against the system.

        Raises `stale_surface` for any pair that is not the slot's current occupant, and
        for an occupant whose process or window is no longer the one that was issued.
        """
        if SURFACE_REF_PATTERN.fullmatch(surface_ref) is None:
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        slot = self.slots[int(surface_ref[1:]) - 1]
        identity = slot.identity
        if identity is None or slot.epoch != surface_epoch:
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        self._verify_live(slot, identity)
        # The trust checks run again at observation time, not only at inventory time.
        parents, job = self._snapshot()
        refusal = self._refusal_for(identity.pid, self._ceiling(self.probe.own_integrity_level()), parents, job)
        if refusal is DesktopReason.SURFACE_UNAVAILABLE:
            # Lumi's own tree and the credential/consent deny-list are not described to a caller.
            self._vacate(slot)
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        if refusal is not None:
            raise DesktopRefusal(refusal)
        return ResolvedSurface(slot=slot, identity=identity)

    def verify_unchanged(self, resolved: "ResolvedSurface") -> None:
        """Re-prove identity after a traversal. A window replaced mid-read is `surface_changed`."""
        if resolved.slot.identity != resolved.identity:
            raise DesktopRefusal(DesktopReason.SURFACE_CHANGED)
        try:
            self._verify_live(resolved.slot, resolved.identity)
        except DesktopRefusal:
            raise DesktopRefusal(DesktopReason.SURFACE_CHANGED) from None

    def _verify_live(self, slot: _Slot, identity: SurfaceIdentity) -> None:
        owner = self.probe.window_pid(identity.hwnd)
        if owner is None or owner != identity.pid:
            self._vacate(slot)
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)
        process = self.probe.process_identity(identity.pid)
        if process is None or process.created != identity.created:
            self._vacate(slot)
            raise DesktopRefusal(DesktopReason.STALE_SURFACE)

    def _vacate(self, slot: _Slot) -> None:
        slot.identity = None
        slot.records = None
        slot.partial = False
        slot.controls = None
        # A vacated slot's old pair must not match whatever is placed here next.
        slot.epoch += 1

    def clear(self) -> None:
        """Explicit session close: every ref dies."""
        for slot in self.slots:
            self._vacate(slot)


@dataclass(frozen=True, slots=True)
class ResolvedSurface:
    slot: _Slot
    identity: SurfaceIdentity
