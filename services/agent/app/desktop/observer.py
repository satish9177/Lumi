"""Bounded semantic observation of one Windows surface. Backend-independent.

This module decides *what* an observation is; a `UiaBackend` only says what the
accessibility tree contains. That split is what lets every rule below be tested
against a deterministic fake as well as against the real UI Automation backend.

Rules enforced here, in order of importance:

* **A credential input makes the whole surface a credential surface.** The walk keeps
  looking past the projection cap (up to `MAX_SCAN_ELEMENTS`) and, if it finds one,
  everything gathered so far is discarded and only the refusal comes back. Password
  values are never read: the backend reads the password flag first.
* **Bounded.** 200 nodes, depth 12, 120 characters per string, 12 KB of text in
  total, 300 siblings per parent, and a time budget. Hitting any bound is declared in the
  observation, never silent: a window too big to read in time comes back marked `time`
  rather than dying at the deadline and taking the worker with it.
* **Stable or refused.** The tree is walked twice; if the structure differs between the
  two passes, or the window was replaced while reading, the result is `surface_changed`
  and nothing is stored. Two unrelated trees are never stitched into one snapshot.
* **Refs die with the observation they came from.** A new observation replaces the
  control table, so every older `uN` stops resolving.
* **Passive.** Nothing here can focus, click, type, select, scroll or launch. A control
  reference is re-derived from the live tree by exact match: zero matches is
  `element_missing`, several is `element_ambiguous`, a match that is a different
  element instance is `element_changed`. There is no nearest-label fallback.
"""

import hashlib
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

from app.desktop.errors import DesktopReason, DesktopRefusal
from app.desktop.protocol import (
    CONTROL_REF_PATTERN,
    MAX_DEPTH,
    MAX_NODES,
    MAX_SIBLINGS,
    MAX_SCAN_DEPTH,
    MAX_SCAN_ELEMENTS,
    MAX_TEXT_PER_NODE,
    MAX_TOTAL_TEXT,
    ROLE_BY_CONTROL_TYPE,
    CheckedState,
    DesktopNode,
    DesktopObservation,
    DesktopPattern,
    DesktopRole,
    ScrollStep,
    Truncation,
    clean_text,
)
from app.desktop.surfaces import Inventory, ResolvedSurface, SurfaceTable

#: Longest raw string a locator keeps in worker memory.
MAX_LOCATOR_TEXT: Final = 512
#: How long the first, full-detail pass may run before it stops and declares `time` truncation.
DEFAULT_TIME_BUDGET_SECONDS: Final = 10.0

_CREDENTIAL_NAME: Final = re.compile(
    r"\b(pass(?:word|code|phrase)|pin|otp|one[- ]time|verification code|security code|cvv|cvc|secret|"
    r"token|api[ _-]?key|private key|seed phrase|recovery (?:phrase|code|key)|card number|"
    r"passwort|contrase\u00f1a|mot de passe|senha|\u5bc6\u7801|\u30d1\u30b9\u30ef\u30fc\u30c9)\b",
    re.IGNORECASE,
)
_CREDENTIAL_TYPES: Final = frozenset({"Edit", "ComboBox"})


class ElementUnavailable(Exception):
    """The backend lost the element mid-read (it was destroyed or replaced)."""


@dataclass(frozen=True, slots=True)
class RawProps:
    """One element as the backend read it. Worker-internal; never crosses the wire.

    `scan` reads fill only the fields a credential decision needs. `structure` reads add
    what the fingerprint and the locator use. `full` reads add the projected state.
    """

    control_type: str
    is_password: bool
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    runtime_id: tuple[int, ...] = ()
    enabled: bool = True
    patterns: frozenset[DesktopPattern] = frozenset()
    offscreen: bool = False
    focused: bool = False
    focusable: bool = False
    value: str | None = None
    checked: CheckedState | None = None
    selected: bool | None = None
    expanded: bool | None = None


@dataclass(frozen=True, slots=True)
class ScrollState:
    """UIA ScrollPattern state for one control. Worker-internal."""

    vertically_scrollable: bool
    vertical_percent: float | None


@dataclass(frozen=True, slots=True)
class ValueState:
    """UIA ValuePattern state for one control. Worker-internal."""

    read_only: bool
    #: The pattern's own current value, read fresh (never the projected/cached one). Used only to
    #: verify a `SetValue` call; never returned on the wire.
    value: str | None


class UiaElement(Protocol):
    def props(self, level: str) -> RawProps:
        """`level` is one of "scan", "structure", "full"."""

    def children(self) -> Sequence["UiaElement"]: ...

    def scroll_state(self) -> ScrollState | None:
        """The ScrollPattern's vertical state, or None when the element exposes no ScrollPattern."""

    def scroll(self, step: ScrollStep) -> None:
        """ONE `ScrollPattern.Scroll` call by a closed step. No wheel, key or coordinate."""

    def focus(self) -> None:
        """ONE `IUIAutomationElement.SetFocus`, on a top-level window root. Nothing else may call it."""

    def value_state(self) -> ValueState | None:
        """The ValuePattern's current state, or None when the element exposes no ValuePattern."""

    def set_value(self, value: str) -> None:
        """ONE `ValuePattern.SetValue` call. No keyboard emulation, no paste."""

    def select(self) -> None:
        """ONE `SelectionItemPattern.Select` call. No index, coordinate or native item id."""

    def invoke(self) -> None:
        """ONE `InvokePattern.Invoke` call."""


class UiaBackend(Protocol):
    def root_for_window(self, hwnd: int) -> UiaElement: ...


def role_for(control_type: str) -> DesktopRole:
    return ROLE_BY_CONTROL_TYPE.get(control_type, DesktopRole.UNKNOWN)


def is_credential(props: RawProps) -> bool:
    if props.is_password:
        return True
    return props.control_type in _CREDENTIAL_TYPES and _CREDENTIAL_NAME.search(props.name) is not None


def _clip(value: str, limit: int) -> tuple[str, bool]:
    return (value, False) if len(value) <= limit else (value[:limit], True)


# ---- locators (worker memory only) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class LocatorStep:
    control_type: str
    automation_id: str
    class_name: str
    name: str


@dataclass(frozen=True, slots=True)
class Locator:
    """The minimum needed to find this control again. No selector language, no handle."""

    path: tuple[LocatorStep, ...]
    runtime_id: tuple[int, ...]
    role: DesktopRole


def _step(props: RawProps) -> LocatorStep:
    return LocatorStep(
        control_type=props.control_type,
        automation_id=props.automation_id[:MAX_LOCATOR_TEXT],
        class_name=props.class_name[:MAX_LOCATOR_TEXT],
        name=props.name[:MAX_LOCATOR_TEXT],
    )


@dataclass(slots=True)
class ControlTable:
    """The `uN` refs of exactly one observation of one surface epoch."""

    observation_id: uuid.UUID
    surface_epoch: int
    locators: dict[str, Locator] = field(default_factory=dict)


# ---- the walk -----------------------------------------------------------------------


@dataclass(slots=True)
class _WalkNode:
    depth: int
    parent: int | None
    props: RawProps
    locator_path: tuple[LocatorStep, ...]


@dataclass(slots=True)
class _Walk:
    nodes: list[_WalkNode] = field(default_factory=list)
    max_depth: int = 0
    visited: int = 0
    truncation: set[Truncation] = field(default_factory=set)


def _walk(
    root: UiaElement, level: str, *, deadline: float | None = None, visit_limit: int | None = None
) -> _Walk:
    """Depth-first, document order. Raises `credential_surface` on the first credential input.

    `deadline` ends the walk early and declares `time`. `visit_limit` makes the confirming second
    pass look at exactly the elements the first one did, so the two are comparable.
    """
    walk = _Walk()
    # (element, depth, parent index in walk.nodes, locator path of the parent)
    stack: list[tuple[UiaElement, int, int | None, tuple[LocatorStep, ...]]] = [(root, 0, None, ())]
    while stack:
        if visit_limit is not None and walk.visited >= visit_limit:
            break
        element, depth, parent, parent_path = stack.pop()
        if walk.visited >= MAX_SCAN_ELEMENTS:
            walk.truncation.add(Truncation.SCAN)
            break
        if deadline is not None and time.monotonic() > deadline:
            walk.truncation.add(Truncation.TIME)
            break
        walk.visited += 1
        projecting = len(walk.nodes) < MAX_NODES and depth <= MAX_DEPTH
        try:
            props = element.props(level if projecting else "scan")
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.SURFACE_CHANGED) from None
        if is_credential(props):
            # Nothing gathered so far leaves this function.
            raise DesktopRefusal(DesktopReason.CREDENTIAL_SURFACE)

        index: int | None = None
        path = parent_path
        if projecting:
            path = (*parent_path, _step(props)) if depth > 0 else ()
            index = len(walk.nodes)
            walk.nodes.append(_WalkNode(depth=depth, parent=parent, props=props, locator_path=path))
            walk.max_depth = max(walk.max_depth, depth)
        elif len(walk.nodes) >= MAX_NODES:
            walk.truncation.add(Truncation.NODES)
        else:
            walk.truncation.add(Truncation.DEPTH)

        if depth >= MAX_SCAN_DEPTH:
            walk.truncation.add(Truncation.SCAN)
            continue
        try:
            children = element.children()
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.SURFACE_CHANGED) from None
        if len(children) >= MAX_SIBLINGS:
            # The backend stops reading at the cap; there may be more, and they were not looked at.
            walk.truncation.add(Truncation.SCAN)
            children = children[:MAX_SIBLINGS]
        # Reversed so the first child is visited first.
        stack.extend((child, depth + 1, index, path) for child in reversed(children))
    return walk


def _guarded_walk(
    root: UiaElement, level: str, *, deadline: float | None = None, visit_limit: int | None = None
) -> _Walk:
    """`_walk`, with every unexpected backend failure reduced to one text-free code."""
    try:
        return _walk(root, level, deadline=deadline, visit_limit=visit_limit)
    except (DesktopRefusal, ElementUnavailable) as refusal:
        if isinstance(refusal, ElementUnavailable):
            raise DesktopRefusal(DesktopReason.SURFACE_CHANGED) from None
        raise
    except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
        raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None


def _credential_scan(root: UiaElement, *, deadline: float) -> None:
    """A lenient, credential-only re-scan of a live tree, for one purpose: does the surface contain a
    credential input ANYWHERE, right now. Unlike `_walk`/`_guarded_walk`, a node that has become
    unavailable mid-scan is simply skipped, not treated as proof the whole surface changed -- a node
    that is gone cannot itself be a live credential input a person could type into, so its own
    disappearance is never a reason to refuse a mutation aimed at a completely different, still-live
    control. `_walk`'s stricter "stable or refused" rule exists to protect an OBSERVATION's fidelity as
    a snapshot; this is not one -- it is a yes/no safety net run again immediately before a mutation,
    and ordinary transient UI churn elsewhere in the tree must never be the reason it refuses. A
    genuine backend failure (anything other than the element having disappeared) still fails closed.
    """
    stack: list[tuple[UiaElement, int]] = [(root, 0)]
    visited = 0
    while stack:
        if visited >= MAX_SCAN_ELEMENTS or time.monotonic() > deadline:
            return
        element, depth = stack.pop()
        visited += 1
        try:
            props = element.props("scan")
        except ElementUnavailable:
            continue
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None
        if is_credential(props):
            raise DesktopRefusal(DesktopReason.CREDENTIAL_SURFACE)
        if depth >= MAX_SCAN_DEPTH:
            continue
        try:
            children = element.children()
        except ElementUnavailable:
            continue
        except Exception:  # noqa: BLE001 - a COM error carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None
        stack.extend((child, depth + 1) for child in children[:MAX_SIBLINGS])


def structure_records(walk: _Walk) -> tuple[str, ...]:
    """One digest per observed node: depth, role, name, patterns and enabled state, in tree order.

    Static-text names are left out: a clock or a counter label changes constantly and
    says nothing about which controls exist. Text values, focus and check state are
    left out for the same reason.
    """
    records: list[str] = []
    for node in walk.nodes:
        role = role_for(node.props.control_type)
        name = "" if role is DesktopRole.TEXT else clean_text(node.props.name[:MAX_TEXT_PER_NODE])
        record = "\x1f".join(
            (
                str(node.depth),
                str(node.parent if node.parent is not None else -1),
                role.value,
                name,
                ",".join(sorted(pattern.value for pattern in node.props.patterns)),
                "1" if node.props.enabled else "0",
            )
        )
        records.append(hashlib.sha256(record.encode("utf-8")).hexdigest()[:24])
    return tuple(records)


def fingerprint(walk: _Walk) -> str:
    """Value-free structural digest of everything the walk observed."""
    return hashlib.sha256("".join(structure_records(walk)).encode("ascii")).hexdigest()


def structurally_different(
    previous: tuple[str, ...], previous_partial: bool, current: tuple[str, ...], current_partial: bool
) -> bool:
    """Did the structure change between two observations of one surface?

    A read cut short by time covers however many nodes it managed, and the walk order is
    deterministic, so two partial reads of an unchanged tree are prefixes of each other. When either
    side is partial only the nodes both saw are compared; comparing whole digests would bump the
    epoch on every read of a slow window and needlessly kill the refs.
    """
    if previous_partial or current_partial:
        common = min(len(previous), len(current))
        return previous[:common] != current[:common]
    return previous != current


# ---- the observer -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservationStats:
    """What a diagnostics line may say. No title, no name, no text."""

    node_count: int
    depth: int
    truncated: bool
    duration_ms: int


@dataclass(frozen=True, slots=True)
class ResolvedControl:
    """A control re-derived from the live tree. Worker-internal: never crosses the wire."""

    role: DesktopRole
    name: str
    patterns: frozenset[DesktopPattern] = frozenset()
    element: UiaElement | None = None
    #: The locator this control was re-derived from. S4 effects keep it to re-derive the SAME control
    #: again, after their own mutation, without depending on a control table an effect may have cleared.
    locator: "Locator | None" = None


class DesktopObserver:
    def __init__(
        self,
        *,
        surfaces: SurfaceTable,
        backend: UiaBackend,
        worker_generation: uuid.UUID,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> None:
        self._surfaces = surfaces
        self._backend = backend
        self._budget = time_budget_seconds
        self._generation = worker_generation
        self.worker_generation = worker_generation
        self.last_stats: ObservationStats | None = None

    def list_surfaces(self) -> Inventory:
        return self._surfaces.refresh()

    def release_all(self) -> None:
        """Explicit session end: every surface and control ref dies."""
        self._surfaces.clear()

    def observe(self, surface_ref: str, surface_epoch: int) -> DesktopObservation:
        return self.observe_measured(surface_ref, surface_epoch)[0]

    def observe_measured(
        self, surface_ref: str, surface_epoch: int
    ) -> tuple[DesktopObservation, ObservationStats]:
        """One observation and the counts a diagnostics line may carry, from the same read."""
        started = time.monotonic()
        resolved = self._surfaces.resolve(surface_ref, surface_epoch)
        root = self._backend_root(resolved)
        first = _guarded_walk(root, "full", deadline=started + self._budget)
        # A second, cheaper pass over exactly the elements the first one saw. Two different answers
        # are two different windows, and stitching them together would invent a UI that never existed.
        second = _guarded_walk(root, "structure", visit_limit=first.visited)
        try:
            if fingerprint(first) != fingerprint(second):
                raise DesktopRefusal(DesktopReason.SURFACE_CHANGED)
            self._surfaces.verify_unchanged(resolved)
            observation, table = self._project(surface_ref, resolved, first)
        except DesktopRefusal:
            raise
        except Exception:  # noqa: BLE001 - whatever failed carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None
        slot = resolved.slot
        records = structure_records(first)
        partial = Truncation.TIME in first.truncation
        if slot.records is not None and structurally_different(slot.records, slot.partial, records, partial):
            # A materially different structure is a new epoch: refs issued against the
            # old one, including this caller's own surface epoch, no longer match.
            slot.epoch += 1
            observation = observation.model_copy(update={"surface_epoch": slot.epoch})
            table.surface_epoch = slot.epoch
            slot.records, slot.partial = records, partial
        elif slot.records is None or len(records) >= len(slot.records) or not partial:
            # Keep the most complete baseline seen for this structure.
            slot.records, slot.partial = records, partial
        slot.controls = table
        stats = ObservationStats(
            node_count=observation.node_count,
            depth=observation.depth,
            truncated=observation.truncated,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self.last_stats = stats
        return observation, stats

    def root_for(self, resolved: ResolvedSurface) -> UiaElement:
        return self._backend_root(resolved)

    def _backend_root(self, resolved: ResolvedSurface) -> UiaElement:
        try:
            return self._backend.root_for_window(resolved.identity.hwnd)
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.SURFACE_UNAVAILABLE) from None
        except DesktopRefusal:
            raise
        except Exception:  # noqa: BLE001 - a COM failure carries private detail; none is kept.
            raise DesktopRefusal(DesktopReason.BACKEND_FAILED) from None

    def _project(
        self, surface_ref: str, resolved: ResolvedSurface, walk: _Walk
    ) -> tuple[DesktopObservation, ControlTable]:
        observation_id = uuid.uuid4()
        table = ControlTable(observation_id=observation_id, surface_epoch=resolved.slot.epoch)
        truncation = set(walk.truncation)
        budget = MAX_TOTAL_TEXT
        nodes: list[DesktopNode] = []
        refs: list[str] = []
        for position, item in enumerate(walk.nodes):
            ref = f"u{position + 1}"
            refs.append(ref)
            props = item.props
            role = role_for(props.control_type)
            name, name_clipped = _clip(clean_text(props.name), MAX_TEXT_PER_NODE)
            text, text_clipped = (
                _clip(clean_text(props.value), MAX_TEXT_PER_NODE) if props.value is not None else (None, False)
            )
            if name_clipped or text_clipped:
                truncation.add(Truncation.TEXT)
            spent = len(name.encode("utf-8")) + (len(text.encode("utf-8")) if text is not None else 0)
            if spent > budget:
                name, text = "", None
                truncation.add(Truncation.TEXT)
                spent = 0
            budget -= spent
            parent_ref = refs[item.parent] if item.parent is not None else None
            nodes.append(
                DesktopNode(
                    control_ref=ref,
                    parent_ref=parent_ref,
                    role=role,
                    name=name or None,
                    text=text or None,
                    enabled=props.enabled,
                    visible=not props.offscreen,
                    focused=props.focused,
                    focusable=props.focusable,
                    selected=props.selected,
                    checked=props.checked,
                    expanded=props.expanded,
                    patterns=sorted(props.patterns, key=lambda pattern: pattern.value),
                )
            )
            table.locators[ref] = Locator(
                path=item.locator_path, runtime_id=props.runtime_id, role=role
            )
        ordered = sorted(truncation, key=lambda reason: reason.value)
        observation = DesktopObservation(
            observation_id=observation_id,
            surface_ref=surface_ref,
            surface_epoch=resolved.slot.epoch,
            worker_generation=self._generation,
            nodes=nodes,
            node_count=len(nodes),
            depth=walk.max_depth,
            truncated=bool(ordered),
            truncation=ordered,
            fingerprint=fingerprint(walk),
        )
        return observation, table

    # -- re-resolution (no action consumes this in S1) -----------------------------

    def resolve_control(
        self,
        surface_ref: str,
        surface_epoch: int,
        observation_id: uuid.UUID,
        control_ref: str,
    ) -> ResolvedControl:
        """Re-derive one control from the live tree, by exact match only."""
        resolved = self._surfaces.resolve(surface_ref, surface_epoch)
        table = resolved.slot.controls
        if (
            not isinstance(table, ControlTable)
            or table.observation_id != observation_id
            or table.surface_epoch != surface_epoch
            or CONTROL_REF_PATTERN.fullmatch(control_ref) is None
        ):
            raise DesktopRefusal(DesktopReason.STALE_CONTROL)
        locator = table.locators.get(control_ref)
        if locator is None:
            raise DesktopRefusal(DesktopReason.STALE_CONTROL)
        return self._rederive(resolved, locator)

    def rederive(self, resolved: ResolvedSurface, locator: "Locator") -> ResolvedControl:
        """Re-derive a control from a locator captured earlier in the same effect, not from a table.

        An S4 mutation kills the observation's control table before it acts (the same rule scroll
        follows): whatever it does, no `uN` of the observation it was proposed against resolves again.
        Post-effect verification still needs to find the SAME control, so it keeps the locator from the
        pre-effect re-resolution and re-derives directly, bypassing the (now-cleared) table.
        """
        return self._rederive(resolved, locator)

    def rederive_after_mutation(self, resolved: ResolvedSurface, locator: "Locator") -> ResolvedControl:
        """Like `rederive`, but the LAST step matches by identity (runtime id) rather than by name.

        `NAME_TOGGLE` verification deliberately expects the control's own accessible name to have
        changed; the ordinary locator step includes the name, so matching by the pre-effect step would
        always miss the very control the effect was supposed to change. Every ancestor step still
        matches structurally, exactly as `rederive` does, so this cannot be used to jump to an
        unrelated element: only the final hop tolerates a name that moved.
        """
        return self._rederive(resolved, locator, match_last_by_identity=True)

    def rederive_descendant(self, resolved: ResolvedSurface, container: "Locator", option: "Locator") -> ResolvedControl:
        """Re-derive `option`, proving it is currently, LIVE, a descendant of the LIVE `container` --
        not merely that each independently still matches its own previously-recorded path from the
        surface root.

        Comparing the two stored locator paths as string prefixes (the bounds check just below) proves
        only that they *used to* nest this way. Two elements can each independently re-resolve against
        their own old path while the live tree has actually been restructured -- a same-shaped
        replacement container swapped in for the approved one, with the option reparented into it -- so
        this re-derives `container` first, from the surface root as usual, and then re-derives `option`
        a SECOND time from that live container element, walking only the path steps beyond it. `option`
        only comes back resolved if it is actually found hanging off the live element `container`
        resolved to, right now.
        """
        if len(option.path) <= len(container.path) or option.path[: len(container.path)] != container.path:
            raise DesktopRefusal(DesktopReason.OPTION_WRONG_CONTAINER)
        container_control = self._rederive(resolved, container)
        if container_control.element is None:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING)
        return self._rederive(resolved, option, from_element=container_control.element, from_index=len(container.path))

    def _rederive(
        self,
        resolved: ResolvedSurface,
        locator: Locator,
        *,
        match_last_by_identity: bool = False,
        from_element: UiaElement | None = None,
        from_index: int = 0,
    ) -> ResolvedControl:
        current = from_element if from_element is not None else self._backend_root(resolved)
        try:
            props = current.props("structure")
            for index in range(from_index, len(locator.path)):
                step = locator.path[index]
                candidates = [
                    (child, child.props("structure")) for child in current.children()[:MAX_SIBLINGS]
                ]
                if any(is_credential(found) for _, found in candidates):
                    # Same rule as observation: a credential input makes the area off limits.
                    raise DesktopRefusal(DesktopReason.CREDENTIAL_SURFACE)
                if match_last_by_identity and index == len(locator.path) - 1:
                    matches = [(child, found) for child, found in candidates if found.runtime_id == locator.runtime_id]
                else:
                    matches = [(child, found) for child, found in candidates if _step(found) == step]
                if not matches:
                    raise DesktopRefusal(DesktopReason.ELEMENT_MISSING)
                if len(matches) > 1:
                    raise DesktopRefusal(DesktopReason.ELEMENT_AMBIGUOUS)
                current, props = matches[0]
        except ElementUnavailable:
            raise DesktopRefusal(DesktopReason.ELEMENT_MISSING) from None
        if is_credential(props):
            raise DesktopRefusal(DesktopReason.CREDENTIAL_SURFACE)
        if props.runtime_id != locator.runtime_id:
            # The same selector now names a different element instance: it was replaced.
            raise DesktopRefusal(DesktopReason.ELEMENT_CHANGED)
        return ResolvedControl(
            role=role_for(props.control_type), name=props.name, patterns=props.patterns, element=current,
            locator=locator,
        )
