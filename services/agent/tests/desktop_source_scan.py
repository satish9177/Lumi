"""A source-level scanner: production desktop code may perform only the reviewed effects.

Milestone 9 S1 was observation only; S3 adds exactly three effects (focus one surface, one UIA
`ScrollPattern.Scroll`, start one registered application). Each lives in ONE named module and is
pinned by an exact allowlist below; everything else stays forbidden everywhere, including in those
modules. Code review is not the control; this is. The
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

#: The S3 effect module's exact native surface: three queries and one image-path query. No focus here.
#: (`spawn` uses `subprocess.Popen`, pinned separately below.)
PINNED_EFFECT_WIN32: Final = frozenset(
    {"CloseHandle", "GetForegroundWindow", "GetLastInputInfo", "OpenProcess", "QueryFullProcessImageNameW"}
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
        # S3: the ScrollPattern, and only its vertical state and its single `Scroll` method.
        "CurrentVerticalScrollPercent", "CurrentVerticallyScrollable", "IUIAutomationScrollPattern",
        "Scroll", "UIA_ScrollPatternId", "SetFocus",
    }
)
#: Action-bearing pattern names that ONE reviewed file may reference. Nothing else, nowhere else.
ACTION_PATTERN_ALLOWANCES: Final[dict[str, frozenset[str]]] = {
    "uia_backend.py": frozenset({"IUIAutomationScrollPattern", "UIA_ScrollPatternId"}),
}
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
    # S3 effects. Each name is allowed only in the file that legitimately owns it.
    "effects.py": frozenset({"focus", "scroll", "launch"}),
    "effects_win32.py": frozenset({"popen"}),
    "observer.py": frozenset({"scroll", "focus"}),
    "uia_backend.py": frozenset({"scroll", "focus", "setfocus"}),
    "client.py": frozenset({"focus", "scroll", "launch"}),
    # `os.close` on the file descriptors of the readiness pipe. Nothing to do with a window.
    "main.py": frozenset({"close"}),
    # `Thread.start()` runs the dedicated UIA thread inside the worker; it starts no other process.
    "worker.py": frozenset({"start", "focus", "scroll", "launch"}),
}
MODULE_ALLOWANCES: Final[dict[str, frozenset[str]]] = {
    "managed.py": frozenset({"subprocess"}),
    "effects_win32.py": frozenset({"subprocess"}),
}


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
    allowed_patterns = ACTION_PATTERN_ALLOWANCES.get(base, frozenset())
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
            if _ACTION_PATTERN_NAME.fullmatch(node.attr) and node.attr not in allowed_patterns:
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
            if _ACTION_PATTERN_NAME.fullmatch(node.id) and node.id not in allowed_patterns:
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
    elif base == "effects.py":
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "focus":
                target = node.func.value
                if not (isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute) and target.func.attr == "root_for"):
                    found.append(Violation(filename, node.lineno, "focus-target", "focus may only be called on a window root"))
    elif base == "effects_win32.py":
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and _base_name(node.value) in _DLL_VARIABLES
                and node.attr not in _PROTOTYPE_ATTRIBUTES
                and node.attr not in PINNED_EFFECT_WIN32
            ):
                found.append(Violation(filename, node.lineno, "unpinned-win32-call", node.attr))
        found.extend(_spawn_violations(tree, filename))
    elif base == "uia_backend.py":
        found.extend(_scroll_call_violations(tree, filename))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr[:1].isupper()
                and not _is_constant(node.attr)
                and node.attr not in PINNED_COM
            ):
                found.append(Violation(filename, node.lineno, "unpinned-com-member", node.attr))
    return found


_SUBPROCESS_OK: Final = frozenset({"Popen", "DEVNULL"})


def _dump(expression: str) -> str:
    return ast.dump(ast.parse(expression, mode="eval").body)


#: The ONE process start, as an exact shape. Every argument is pinned by its AST, so `shell=True`, a string
#: command line, `env=os.environ`, an extra `executable=`/`startupinfo=` argument, a different argument vector
#: or a different set of creation flags fails, not just a missing keyword.
_GOLDEN_SPAWN_ARGV: Final = _dump("[app.executable, *app.args]")
_GOLDEN_SPAWN_KEYWORDS: Final[dict[str, str]] = {
    "shell": _dump("False"),
    "env": _dump("launch_environment()"),
    "cwd": _dump("ntpath.dirname(app.executable)"),
    "stdin": _dump("subprocess.DEVNULL"),
    "stdout": _dump("subprocess.DEVNULL"),
    "stderr": _dump("subprocess.DEVNULL"),
    "close_fds": _dump("True"),
    "creationflags": _dump("_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB"),
}


def _spawn_violations(tree: ast.AST, filename: str) -> list[Violation]:
    """Exactly one `subprocess.Popen`, in exactly the golden shape, and no other way to start a process."""
    found: list[Violation] = []
    for node in ast.walk(tree):
        # No alias (`import subprocess as sp`) and nothing but Popen/DEVNULL from the module.
        if isinstance(node, ast.Import):
            found.extend(
                Violation(filename, node.lineno, "spawn-alias", alias.name)
                for alias in node.names
                if alias.name == "subprocess" and alias.asname is not None
            )
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            found.append(Violation(filename, node.lineno, "spawn-import-from", node.module))
        if isinstance(node, ast.Attribute):
            base = _base_name(node.value)
            if base == "subprocess" and node.attr not in _SUBPROCESS_OK:
                found.append(Violation(filename, node.lineno, "spawn-api", node.attr))
            if node.attr.lower() == "popen" and base != "subprocess":
                found.append(Violation(filename, node.lineno, "spawn-api", f"{base}.{node.attr}"))
        if isinstance(node, ast.Name) and node.id.lower() in ("popen", "system", "startfile"):
            found.append(Violation(filename, node.lineno, "spawn-api", node.id))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen" and _base_name(node.func.value) == "subprocess"
    ]
    if len(calls) > 1:
        found.append(Violation(filename, 0, "spawn-count", str(len(calls))))
    for call in calls:
        if len(call.args) != 1 or ast.dump(call.args[0]) != _GOLDEN_SPAWN_ARGV:
            found.append(Violation(filename, call.lineno, "spawn-command", "argument vector must be [app.executable, *app.args]"))
        seen: dict[str | None, str] = {keyword.arg: ast.dump(keyword.value) for keyword in call.keywords}
        for keyword_name, golden in _GOLDEN_SPAWN_KEYWORDS.items():
            if seen.get(keyword_name) != golden:
                found.append(Violation(filename, call.lineno, f"spawn-{keyword_name}", "not the reviewed value"))
        for extra in seen.keys() - _GOLDEN_SPAWN_KEYWORDS.keys():
            found.append(Violation(filename, call.lineno, "spawn-extra-argument", str(extra)))
    return found


def _single_call_violations(tree: ast.AST, filename: str, attr: str, function: str, argc: int) -> list[Violation]:
    """`.attr(` may be called once, inside a function named `function`, with exactly `argc` arguments."""
    found: list[Violation] = []

    def is_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr

    reviewed: set[int] = set()
    for item in ast.walk(tree):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function:
            for node in ast.walk(item):
                if is_call(node) and len(node.args) == argc:  # type: ignore[attr-defined]
                    reviewed.add(id(node))
    calls = [node for node in ast.walk(tree) if is_call(node)]
    for node in calls:
        if id(node) not in reviewed:
            found.append(Violation(filename, getattr(node, "lineno", 0), f"{attr.lower()}-call", "outside the reviewed method"))
    # `f = element.SetFocus; f()` names the member without calling it here: every mention must BE the call.
    called_members = {id(node.func) for node in calls if isinstance(node, ast.Call)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == attr and id(node) not in called_members:
            found.append(Violation(filename, node.lineno, f"{attr.lower()}-reference", "the member may only be called"))
    if len(calls) > 1:
        found.append(Violation(filename, 0, f"{attr.lower()}-count", str(len(calls))))
    return found


def _scroll_call_violations(tree: ast.AST, filename: str) -> list[Violation]:
    return [
        *_single_call_violations(tree, filename, "Scroll", "scroll", 2),
        *_single_call_violations(tree, filename, "SetFocus", "focus", 0),
    ]


def breakaway_violations(app_root: Path) -> list[Violation]:
    """`CREATE_BREAKAWAY_FROM_JOB` (leaving the runtime's kill-on-close job) may be passed by ONE module."""
    allowed = {"desktop/effects_win32.py", "services/windows_job.py"}
    found: list[Violation] = []
    for path in sorted(app_root.rglob("*.py")):
        relative = path.relative_to(app_root).as_posix()
        if relative in allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "BREAKAWAY" in line.upper():
                found.append(Violation(relative, number, "breakaway", "the job breakaway flag"))
    return found


def scan_package(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path)))
    return violations
