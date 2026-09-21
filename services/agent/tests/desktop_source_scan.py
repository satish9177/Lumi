"""A source-level scanner: production desktop code must contain no way to operate a UI.

Milestone 9 slice 1 is observation only. Code review is not the control; this is. The
scanner parses every module under `app/desktop/` with `ast` and fails on any:

* **member or name that is an input or window-mutation primitive**: `Invoke`, `SetValue`,
  `Select`, `Toggle`, `Scroll`, `SetFocus`, `SetForegroundWindow`, `ShowWindow`, `SendInput`,
  `send_keys`, `type_keys`, `click`, `press`, `drag`, `mouse_event`, `keybd_event`, `move`,
  `resize`, `close`, `launch`, `ShellExecute`, `CreateProcess`, clipboard writes, message
  sending, and so on (see `FORBIDDEN`). Matching is by exact member name, not substring, so
  reading `CurrentToggleState` or `IUIAutomationTogglePattern` is allowed and calling
  `Toggle()` is not. SCREAMING_CASE names (enum members like `DesktopPattern.SCROLL`, which only
  *name* an advertised pattern) are constants, not calls, and are ignored.
* **import of an input, automation, capture or process-launch library**: `pywinauto.keyboard`,
  `pywinauto.mouse`, `pywinauto.application`, `pyautogui`, `pynput`, `keyboard`, `mouse`,
  `win32clipboard`, `subprocess`, `webbrowser`, `PIL.ImageGrab`, `mss`, and so on.
* **dynamic dispatch** that would defeat a name check: `getattr`, `setattr`, `eval`, `exec`,
  `compile`, `__import__`, `import_module`, `globals`, `locals`.
* **a process-access right stronger than a query**: any `PROCESS_*` name other than
  `PROCESS_QUERY_LIMITED_INFORMATION`, and memory or thread primitives.
* **a string subscript that names a forbidden function** (`dll["SendInput"]`).

The one file allowed to start a process is `managed.py`, and only to start the worker itself.

**This is a tripwire and a pinned surface, not a proof.** A deny-list of names cannot show that
nothing else can act, so the two files that touch the operating system also have an *exact
allowlist* of what they may call: `win32.py` may call only the listed kernel/user/advapi/dwm entry
points, and `uia_backend.py` may use only the listed COM members and pattern interfaces. Adding
any other call fails the suite and forces a deliberate edit of the allowlist, which is the review
point. The deny-list stays as a second layer for everything else in the package.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_CONCEPTS: Final = (
    # UIA patterns and Win32 verbs that act on a control or window.
    "invoke", "setvalue", "set_value", "select", "addtoselection", "removefromselection",
    "toggle", "scroll", "scrollintoview", "scroll_into_view", "expand", "collapse",
    "setfocus", "set_focus", "focus", "activate", "setactivewindow", "setforegroundwindow",
    "set_foreground", "bringwindowtotop", "switchtothiswindow", "showwindow", "showwindowasync",
    "setwindowpos", "movewindow", "move_window", "move", "moveto", "resize", "setvisualstate",
    "close", "closewindow", "destroywindow", "endtask", "terminate", "kill", "terminateprocess",
    # Input synthesis.
    "sendinput", "mouse_event", "keybd_event", "send_keys", "sendkeys", "type_keys", "typekeys",
    "click", "click_input", "double_click", "doubleclick", "right_click", "rightclick",
    "press", "hotkey", "keydown", "keyup", "drag", "drag_mouse_input", "setcursorpos",
    "postmessage", "sendmessage", "sendmessagetimeout", "sendnotifymessage", "postthreadmessage",
    "setcapture", "clipcursor", "blockinput",
    # Launching.
    "launch", "start", "startfile", "shellexecute", "shellexecutew", "shellexecuteex",
    "createprocess", "createprocessw", "createprocessasuser", "winexec", "popen", "system",
    # Clipboard.
    "openclipboard", "setclipboarddata", "emptyclipboard", "setclipboard", "copy", "paste",
    # Capture (screenshots, OCR and coordinates are a later slice).
    "grabwindow", "grab", "screenshot", "capture", "bitblt", "printwindow", "getdc",
    "getwindowdc", "capture_as_image",
    # Reading or writing another process, hooks, injection.
    "readprocessmemory", "writeprocessmemory", "virtualallocex", "createremotethread",
    "setwindowshook", "setwindowshookex", "setwineventhook", "loadlibrary", "openprocesstoken_all",
    "adjusttokenprivileges", "impersonateloggedonuser", "duplicatetokenex",
    # Further mutation, input-state, host-control and launch primitives.
    "setwindowtext", "enablewindow", "setparent", "sendmessagecallback", "attachthreadinput",
    "setkeyboardstate", "lockworkstation", "exitwindows", "flashwindow", "ntterminateprocess",
    "getasynckeystate", "getkeystate", "getkeyboardstate", "getcursorpos", "setcursor",
    "setwindowlong", "setwindowlongptr", "setlayeredwindowattributes", "setwindowrgn",
    "getclipboarddata", "getclipboardtext", "dodefaultaction", "accdodefaultaction", "realize",
    "set_edit_text", "create_subprocess_exec", "create_subprocess_shell", "spawnv", "spawnl",
    "execv", "execl", "processpoolexecutor", "sendkeystoprocess", "release", "keyaction",
)
FORBIDDEN: Final = frozenset(_CONCEPTS)

FORBIDDEN_MODULES: Final = frozenset(
    {
        "pywinauto.keyboard", "pywinauto.mouse", "pywinauto.application", "pywinauto.clipboard",
        "pywinauto.win32functions", "pywinauto.win32_hooks", "pywinauto.actionlogger",
        "pyautogui", "pynput", "keyboard", "mouse", "pyperclip", "win32clipboard", "win32gui_struct",
        "win32api_input", "webbrowser", "PIL.ImageGrab", "mss", "pyscreeze", "pytesseract", "cv2",
        "win32com", "pythoncom", "win32process", "win32ui", "win32print", "os.startfile",
        "multiprocessing", "concurrent.futures.process", "code", "pty", "subprocess",
    }
)
DYNAMIC: Final = frozenset(
    {
        "getattr", "setattr", "delattr", "eval", "exec", "compile", "__import__", "import_module", "globals",
        "locals", "vars", "attrgetter", "methodcaller", "__getattr__", "__getattribute__",
    }
)

#: The exact kernel/user/advapi/dwm entry points `win32.py` may call. Every one is a query.
PINNED_WIN32: Final = frozenset(
    {
        "CloseHandle", "CreateToolhelp32Snapshot", "DwmGetWindowAttribute", "EnumWindows", "GetCurrentProcess",
        "GetExitCodeProcess", "GetProcessTimes", "GetSidSubAuthority", "GetSidSubAuthorityCount",
        "GetTokenInformation", "GetWindow", "GetWindowLongW", "GetWindowTextW", "GetWindowThreadProcessId",
        "IsHungAppWindow", "IsIconic", "IsWindow", "IsWindowVisible", "OpenProcess", "OpenProcessToken",
        "Process32FirstW", "Process32NextW", "QueryFullProcessImageNameW", "QueryInformationJobObject",
    }
)
#: The exact COM members and interface/constant names `uia_backend.py` may use. Reads of properties and
#: pattern *state*; the acting members of the same patterns are absent by design.
PINNED_COM: Final = frozenset(
    {
        "AddProperty", "COMError", "CachedAutomationId", "CachedClassName", "CachedControlType",
        "CachedHasKeyboardFocus", "CachedIsEnabled", "CachedIsKeyboardFocusable", "CachedIsOffscreen",
        "CachedIsPassword", "CachedName", "ControlViewWalker", "CreateCacheRequest",
        "CurrentExpandCollapseState", "CurrentIsSelected", "CurrentToggleState", "CurrentValue",
        "DocumentRange", "ElementFromHandleBuildCache", "GetCachedPropertyValue", "GetCurrentPattern",
        "GetFirstChildElementBuildCache", "GetModule", "GetNextSiblingElementBuildCache", "GetText",
        "IUIAutomationExpandCollapsePattern", "IUIAutomationSelectionItemPattern", "IUIAutomationTextPattern",
        "IUIAutomationTogglePattern", "IUIAutomationValuePattern", "QueryInterface", "TreeScope",
        "TreeScope_Element", "UIA_AutomationIdPropertyId", "UIA_ClassNamePropertyId", "UIA_ControlTypePropertyId",
        "UIA_ExpandCollapsePatternId", "UIA_HasKeyboardFocusPropertyId", "UIA_IsEnabledPropertyId",
        "UIA_IsExpandCollapsePatternAvailablePropertyId", "UIA_IsInvokePatternAvailablePropertyId",
        "UIA_IsKeyboardFocusablePropertyId", "UIA_IsOffscreenPropertyId", "UIA_IsPasswordPropertyId",
        "UIA_IsRangeValuePatternAvailablePropertyId", "UIA_IsScrollPatternAvailablePropertyId",
        "UIA_IsSelectionItemPatternAvailablePropertyId", "UIA_IsSelectionPatternAvailablePropertyId",
        "UIA_IsTextPatternAvailablePropertyId", "UIA_IsTogglePatternAvailablePropertyId",
        "UIA_IsValuePatternAvailablePropertyId", "UIA_IsWindowPatternAvailablePropertyId", "UIA_NamePropertyId",
        "UIA_RuntimeIdPropertyId", "UIA_SelectionItemPatternId", "UIA_TextPatternId", "UIA_TogglePatternId",
        "UIA_ValuePatternId",
    }
)
_DLL_VARIABLES: Final = frozenset({"kernel32", "user32", "advapi32", "dwmapi", "_kernel32", "_user32", "_advapi32", "_dwmapi"})
_PROTOTYPE_ATTRIBUTES: Final = frozenset({"argtypes", "restype"})
#: UIA control-pattern interfaces and ids whose purpose is to *act*. Reading state needs only Value,
#: Toggle, SelectionItem, ExpandCollapse and Text, so the action-bearing ones are not even referenced.
_ACTION_PATTERNS: Final = (
    "Invoke|Scroll|ScrollItem|LegacyIAccessible|Window|Transform2?|Dock|VirtualizedItem|ItemContainer|"
    "SynchronizedInput|RangeValue|Selection2?|MultipleView|TextEdit|Drag|DropTarget|SpreadsheetItem"
)
_ACTION_PATTERN_NAME: Final = re.compile(rf"^(?:IUIAutomation(?:{_ACTION_PATTERNS})Pattern\d?|UIA_(?:{_ACTION_PATTERNS})PatternId)$")
ALLOWED_PROCESS_RIGHT: Final = "PROCESS_QUERY_LIMITED_INFORMATION"
_PROCESS_RIGHT: Final = re.compile(r"^_?PROCESS_[A-Z_]+$")
_MEMORY_OR_THREAD: Final = re.compile(r"(VM_READ|VM_WRITE|VM_OPERATION|CREATE_THREAD|PROCESS_ALL_ACCESS|SET_INFORMATION|TERMINATE|DUP_HANDLE)")

#: Where a file may legitimately do something the rest of the package may not. `managed.py` starts
#: (and stops) the worker process itself; nothing else does.
FILE_ALLOWANCES: Final[dict[str, frozenset[str]]] = {
    "managed.py": frozenset({"popen", "terminate", "kill", "close", "start"}),
    # `os.close` on the file descriptors of the readiness pipe. Nothing to do with a window.
    "main.py": frozenset({"close"}),
    # `Thread.start()` runs the dedicated UIA thread inside the worker; it starts no other process.
    "worker.py": frozenset({"start"}),
}
MODULE_ALLOWANCES: Final[dict[str, frozenset[str]]] = {"managed.py": frozenset({"subprocess"})}


@dataclass(frozen=True, slots=True)
class Violation:
    file: str
    line: int
    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: {self.kind} {self.name!r}"


def _is_constant(name: str) -> bool:
    return name.isupper() or (name.startswith("_") and name.lstrip("_").isupper())


_WIN32_SUFFIXES: Final = ("exw", "exa", "ex", "w", "a")


def _spellings(name: str) -> set[str]:
    """The name, and the name without a Win32 wide/ANSI/Ex suffix (`PostMessageW` -> `postmessage`)."""
    lowered = name.lower()
    spellings = {lowered}
    for suffix in _WIN32_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 3:
            spellings.add(lowered[: -len(suffix)])
    return spellings


def _forbidden_name(name: str, allowed: frozenset[str]) -> bool:
    if _is_constant(name):
        return False
    return any(spelling in FORBIDDEN and spelling not in allowed for spelling in _spellings(name))


def scan_source(source: str, filename: str = "<memory>") -> list[Violation]:
    """Every violation in one module's source."""
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    allowed_names = FILE_ALLOWANCES.get(base, frozenset())
    allowed_modules = MODULE_ALLOWANCES.get(base, frozenset())
    found: list[Violation] = []
    tree = ast.parse(source, filename=filename)

    def add(node: ast.AST, kind: str, name: str) -> None:
        found.append(Violation(filename, getattr(node, "lineno", 0), kind, name))

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if _forbidden_name(node.attr, allowed_names):
                add(node, "member", node.attr)
            if _MEMORY_OR_THREAD.search(node.attr) or (
                _PROCESS_RIGHT.fullmatch(node.attr) and node.attr.lstrip("_") != ALLOWED_PROCESS_RIGHT
            ):
                add(node, "process-right", node.attr)
            if _ACTION_PATTERN_NAME.fullmatch(node.attr):
                add(node, "action-pattern", node.attr)
            if node.attr in DYNAMIC and node.attr != "compile":
                add(node, "dynamic", node.attr)
        elif isinstance(node, ast.Name):
            if _forbidden_name(node.id, allowed_names):
                add(node, "name", node.id)
            if _MEMORY_OR_THREAD.search(node.id) or (
                _PROCESS_RIGHT.fullmatch(node.id) and node.id.lstrip("_") != ALLOWED_PROCESS_RIGHT
            ):
                add(node, "process-right", node.id)
            if node.id in DYNAMIC:
                add(node, "dynamic", node.id)
            if _ACTION_PATTERN_NAME.fullmatch(node.id):
                add(node, "action-pattern", node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _forbidden_name(node.name, allowed_names):
                add(node, "definition", node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            )
            for module in modules:
                root = module.split(".")[0]
                if (module in FORBIDDEN_MODULES or root in FORBIDDEN_MODULES) and root not in allowed_modules:
                    add(node, "import", module)
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if _forbidden_name(alias.name, allowed_names):
                        add(node, "import-name", alias.name)
        elif isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else None
            if called == "GetCurrentPattern" and any(
                isinstance(argument, ast.Constant) and isinstance(argument.value, int) for argument in node.args
            ):
                # A numeric pattern id (`GetCurrentPattern(10000)`) names an acting pattern past every name check.
                add(node, "numeric-pattern-id", "GetCurrentPattern")
        elif isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                if index.value.lower() in FORBIDDEN and not _is_constant(index.value):
                    add(node, "string-subscript", index.value)
            if isinstance(index, ast.Constant) and isinstance(index.value, int) and not isinstance(index.value, bool):
                if index.value >= 256:
                    # `dll[1234]` reaches an export by ordinal, past every name check.
                    add(node, "ordinal-subscript", str(index.value))
    found.extend(_pinned_surface_violations(tree, filename, base))
    return sorted(set(found), key=lambda violation: (violation.file, violation.line, violation.kind, violation.name))


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _pinned_surface_violations(tree: ast.AST, filename: str, base: str) -> list[Violation]:
    """Anything `win32.py` or `uia_backend.py` calls that is not on its exact allowlist."""
    found: list[Violation] = []
    if base == "win32.py":
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and _base_name(node.value) in _DLL_VARIABLES
                and node.attr not in _PROTOTYPE_ATTRIBUTES
                and node.attr not in PINNED_WIN32
            ):
                found.append(Violation(filename, node.lineno, "unpinned-win32-call", node.attr))
    elif base == "uia_backend.py":
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr[:1].isupper()
                and not _is_constant(node.attr)
                and node.attr not in PINNED_COM
            ):
                found.append(Violation(filename, node.lineno, "unpinned-com-member", node.attr))
    return found


def scan_package(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path)))
    return violations
