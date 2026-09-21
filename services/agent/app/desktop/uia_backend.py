"""The real UI Automation backend: pywinauto's UIA layer over comtypes.

Import this module only on the dedicated UIA thread. Importing it initialises COM for
the importing thread, and it must be the multithreaded apartment (`coinit_flags = 0`
below, set before pywinauto and comtypes are first imported), never the main event
loop's thread: UIA providers can block, and a blocked provider must only ever block a
thread the worker can abandon along with its process. Nothing may initialise COM on this
thread before this module is imported (importing `pythoncom` first makes it a single-threaded
apartment and the initialisation here then fails).

Everything here is a *read*. Element properties are fetched in one batch per parent with a
UIA **cache request** (a cross-process call per property per element would take seconds for a
few hundred controls), from the reviewed, non-secret property list below. Control patterns are
used only to read a state (`CurrentValue`, `CurrentToggleState`, `CurrentIsSelected`,
`CurrentExpandCollapseState`, `GetText`); the actions those same patterns offer are not
referenced anywhere in this package, and the source scan fails the build if one appears.

**A credential's value is never requested.** The cache request carries no value property. Values
come from a pattern read that runs only after the (cached) `IsPassword` flag says the element is
not a credential, so a password field's text is never asked for.
"""

import sys
import warnings
from collections.abc import Sequence
from typing import Any, Final

# COINIT_MULTITHREADED. Must precede the first pywinauto / comtypes import.
sys.coinit_flags = 0  # type: ignore[attr-defined]
# pywinauto announces that it honoured the flag above; that is the intended behaviour.
warnings.filterwarnings("ignore", message="Apply externally defined coinit_flags")

import comtypes  # noqa: E402
import comtypes.client  # noqa: E402
from pywinauto.uia_defines import IUIA  # noqa: E402

from app.desktop.observer import ElementUnavailable, RawProps, UiaElement  # noqa: E402
from app.desktop.protocol import (  # noqa: E402
    MAX_SIBLINGS,
    MAX_TEXT_PER_NODE,
    CheckedState,
    DesktopPattern,
    clean_text,
)

# Ask for the type library explicitly. `comtypes.gen.UIAutomationClient` only exists once comtypes
# has generated it, which on a clean install (the packaged build, or any machine that has never run
# this) has not happened yet; importing it by name first would fail there while working on a
# developer machine that had generated it long ago.
_uia = comtypes.client.GetModule("UIAutomationCore.dll")

#: Availability property per advertised pattern.
_PATTERN_PROPERTIES: Final[tuple[tuple[DesktopPattern, int], ...]] = (
    (DesktopPattern.INVOKE, _uia.UIA_IsInvokePatternAvailablePropertyId),
    (DesktopPattern.VALUE, _uia.UIA_IsValuePatternAvailablePropertyId),
    (DesktopPattern.TOGGLE, _uia.UIA_IsTogglePatternAvailablePropertyId),
    (DesktopPattern.SELECTION_ITEM, _uia.UIA_IsSelectionItemPatternAvailablePropertyId),
    (DesktopPattern.SELECTION, _uia.UIA_IsSelectionPatternAvailablePropertyId),
    (DesktopPattern.EXPAND_COLLAPSE, _uia.UIA_IsExpandCollapsePatternAvailablePropertyId),
    (DesktopPattern.SCROLL, _uia.UIA_IsScrollPatternAvailablePropertyId),
    (DesktopPattern.TEXT, _uia.UIA_IsTextPatternAvailablePropertyId),
    (DesktopPattern.RANGE_VALUE, _uia.UIA_IsRangeValuePatternAvailablePropertyId),
    (DesktopPattern.WINDOW, _uia.UIA_IsWindowPatternAvailablePropertyId),
)
#: Everything one batch fetches. Identity, state and pattern *availability*; no value, no text body.
_CACHED_PROPERTIES: Final[tuple[int, ...]] = (
    _uia.UIA_ControlTypePropertyId,
    _uia.UIA_NamePropertyId,
    _uia.UIA_AutomationIdPropertyId,
    _uia.UIA_ClassNamePropertyId,
    _uia.UIA_RuntimeIdPropertyId,
    _uia.UIA_IsEnabledPropertyId,
    _uia.UIA_IsOffscreenPropertyId,
    _uia.UIA_HasKeyboardFocusPropertyId,
    _uia.UIA_IsKeyboardFocusablePropertyId,
    _uia.UIA_IsPasswordPropertyId,
    *(property_id for _, property_id in _PATTERN_PROPERTIES),
)
#: HRESULTs that mean "that element is gone or its provider went away", not "the backend broke".
_GONE: Final = frozenset(
    {
        0x80040201,  # UIA_E_ELEMENTNOTAVAILABLE
        0x80040200,  # UIA_E_ELEMENTNOTENABLED
        0x800706BA,  # RPC server unavailable
        0x800706BE,  # RPC call failed
        0x80070005,  # access denied (protected or higher-integrity provider)
        0x80131505,  # UIA_E_TIMEOUT
        0x80004005,  # E_FAIL from a torn-down provider
    }
)
_TEXT_ROLES: Final = frozenset({"Edit", "Document"})
_TOGGLE: Final = {0: CheckedState.OFF, 1: CheckedState.ON, 2: CheckedState.MIXED}


def _unavailable(error: comtypes.COMError) -> bool:
    return (error.args[0] & 0xFFFFFFFF) in _GONE if error.args else False


class _Element:
    """One live IUIAutomationElement with its properties already cached. Held only during a read."""

    def __init__(self, element: Any, backend: "PywinautoBackend") -> None:
        self._element = element
        self._backend = backend

    def children(self) -> Sequence[UiaElement]:
        """At most `MAX_SIBLINGS` children, read one sibling at a time so a huge list is never
        pulled across the process boundary just to keep the first few hundred rows of it."""
        found: list[UiaElement] = []
        try:
            walker = self._backend.walker
            request = self._backend.request
            child = walker.GetFirstChildElementBuildCache(self._element, request)
            while child and len(found) < MAX_SIBLINGS:
                found.append(_Element(child, self._backend))
                child = walker.GetNextSiblingElementBuildCache(child, request)
        except comtypes.COMError as error:
            if _unavailable(error):
                raise ElementUnavailable from None
            raise
        except ValueError:
            raise ElementUnavailable from None
        return found

    def props(self, level: str) -> RawProps:
        try:
            return self._read(level)
        except comtypes.COMError as error:
            if _unavailable(error):
                raise ElementUnavailable from None
            raise
        except ValueError:
            # comtypes reports a NULL interface pointer (an element that vanished) as ValueError.
            raise ElementUnavailable from None

    def _read(self, level: str) -> RawProps:
        element = self._element
        control_type = self._backend.control_type_name(element.CachedControlType)
        # The password flag decides, before anything else, whether this element's content is ever read.
        if bool(element.CachedIsPassword):
            return RawProps(control_type=control_type, is_password=True)
        name = clean_text(str(element.CachedName or ""))
        if level == "scan":
            return RawProps(
                control_type=control_type,
                is_password=False,
                name=name if control_type in ("Edit", "ComboBox") else "",
            )

        patterns = frozenset(
            pattern
            for pattern, property_id in _PATTERN_PROPERTIES
            if element.GetCachedPropertyValue(property_id) is True
        )
        base: dict[str, Any] = {
            "control_type": control_type,
            "is_password": False,
            "name": name,
            "automation_id": clean_text(str(element.CachedAutomationId or "")),
            "class_name": clean_text(str(element.CachedClassName or "")),
            "runtime_id": tuple(int(part) for part in element.GetCachedPropertyValue(_uia.UIA_RuntimeIdPropertyId)),
            "enabled": bool(element.CachedIsEnabled),
            "patterns": patterns,
        }
        if level == "structure":
            return RawProps(**base)
        return RawProps(
            **base,
            offscreen=bool(element.CachedIsOffscreen),
            focused=bool(element.CachedHasKeyboardFocus),
            focusable=bool(element.CachedIsKeyboardFocusable),
            value=self._text(control_type, patterns),
            checked=self._checked(patterns),
            selected=self._selected(patterns),
            expanded=self._expanded(patterns),
        )

    # Each pattern read is best-effort: a provider that advertises a pattern and then
    # fails to answer simply has no value, which is the honest result.

    def _pattern(self, pattern_id: int, interface: Any) -> Any:
        try:
            return self._element.GetCurrentPattern(pattern_id).QueryInterface(interface)
        except (comtypes.COMError, ValueError):
            return None

    def _text(self, control_type: str, patterns: frozenset[DesktopPattern]) -> str | None:
        # Text-bearing controls are read through the text pattern first: it can be asked for at most
        # `MAX_TEXT_PER_NODE + 1` characters, so a document of any size costs the same to read. The
        # value pattern hands back the whole string, so it is used for everything else, where a
        # value is a short field content.
        if DesktopPattern.TEXT in patterns and control_type in _TEXT_ROLES:
            text = self._pattern(_uia.UIA_TextPatternId, _uia.IUIAutomationTextPattern)
            if text is not None:
                try:
                    return clean_text(str(text.DocumentRange.GetText(MAX_TEXT_PER_NODE + 1) or "")) or None
                except comtypes.COMError:
                    return None
        if DesktopPattern.VALUE in patterns:
            value = self._pattern(_uia.UIA_ValuePatternId, _uia.IUIAutomationValuePattern)
            if value is not None:
                try:
                    return clean_text(str(value.CurrentValue or "")) or None
                except comtypes.COMError:
                    return None
        return None

    def _checked(self, patterns: frozenset[DesktopPattern]) -> CheckedState | None:
        if DesktopPattern.TOGGLE not in patterns:
            return None
        reader = self._pattern(_uia.UIA_TogglePatternId, _uia.IUIAutomationTogglePattern)
        if reader is None:
            return None
        try:
            return _TOGGLE.get(int(reader.CurrentToggleState))
        except comtypes.COMError:
            return None

    def _selected(self, patterns: frozenset[DesktopPattern]) -> bool | None:
        if DesktopPattern.SELECTION_ITEM not in patterns:
            return None
        item = self._pattern(_uia.UIA_SelectionItemPatternId, _uia.IUIAutomationSelectionItemPattern)
        if item is None:
            return None
        try:
            return bool(item.CurrentIsSelected)
        except comtypes.COMError:
            return None

    def _expanded(self, patterns: frozenset[DesktopPattern]) -> bool | None:
        if DesktopPattern.EXPAND_COLLAPSE not in patterns:
            return None
        reader = self._pattern(_uia.UIA_ExpandCollapsePatternId, _uia.IUIAutomationExpandCollapsePattern)
        if reader is None:
            return None
        try:
            state = int(reader.CurrentExpandCollapseState)
        except comtypes.COMError:
            return None
        # 0 collapsed, 1 expanded, 2 partially expanded, 3 leaf node.
        return {0: False, 1: True, 2: True}.get(state)


class PywinautoBackend:
    """Implements `app.desktop.observer.UiaBackend`."""

    def __init__(self) -> None:
        automation = IUIA()
        self._automation = automation
        self.walker = automation.iuia.ControlViewWalker
        self._control_types: dict[int, str] = dict(automation.known_control_type_ids)
        request = automation.iuia.CreateCacheRequest()
        for property_id in _CACHED_PROPERTIES:
            request.AddProperty(property_id)
        request.TreeScope = _uia.TreeScope_Element
        self.request = request

    def control_type_name(self, control_type_id: int) -> str:
        return self._control_types.get(int(control_type_id), "Unknown")

    def root_for_window(self, hwnd: int) -> UiaElement:
        try:
            return _Element(self._automation.iuia.ElementFromHandleBuildCache(hwnd, self.request), self)
        except comtypes.COMError as error:
            if _unavailable(error):
                raise ElementUnavailable from None
            raise
