"""Milestone 9 S1: structural proofs, checked on the source rather than on trust.

* the production desktop code contains no way to operate a UI (with a scanner that is itself
  proven to catch planted violations, so a passing scan means something);
* the wire schemas have no field for native identity, geometry or an action;
* nothing outside the desktop boundary can read a desktop observation, so it cannot reach a
  planner, an answer, a memory, a research context or a task summary.
"""

import ast
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.desktop import protocol
from tests.desktop_source_scan import FORBIDDEN, scan_package, scan_source

AGENT = Path(__file__).resolve().parents[1]
DESKTOP = AGENT / "app" / "desktop"
APP = AGENT / "app"


# ---- the scanner proves it can detect what it forbids -----------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "pattern.Invoke()",
        "pattern.invoke()",
        "InvokePattern.Invoke(button)",
        "value_pattern.SetValue('x')",
        "item.Select()",
        "selection_item.Select()",
        "toggle_pattern.Toggle()",
        "element.toggle()",
        "scroll_pattern.Scroll(0, 1)",
        "window.scroll()",
        "element.SetFocus()",
        "element.set_focus()",
        "user32.SetForegroundWindow(hwnd)",
        "user32.BringWindowToTop(hwnd)",
        "user32.ShowWindow(hwnd, 9)",
        "user32.SendInput(1, ptr, 40)",
        "user32.mouse_event(2, 0, 0, 0, 0)",
        "user32.keybd_event(65, 0, 0, 0)",
        "keyboard.send_keys('abc')",
        "control.type_keys('abc')",
        "control.click()",
        "control.click_input()",
        "control.double_click()",
        "control.press('a')",
        "control.hotkey('ctrl', 'c')",
        "control.drag()",
        "control.move()",
        "user32.MoveWindow(hwnd, 0, 0, 1, 1, True)",
        "user32.SetWindowPos(hwnd, 0, 0, 0, 1, 1, 0)",
        "control.resize(1, 1)",
        "window.close()",
        "user32.CloseWindow(hwnd)",
        "user32.DestroyWindow(hwnd)",
        "app.launch()",
        "shell32.ShellExecuteW(0, 'open', 'x.exe', None, None, 1)",
        "kernel32.CreateProcessW(None, 'x', None, None, 0, 0, None, None, s, p)",
        "user32.OpenClipboard(0)",
        "user32.SetClipboardData(1, h)",
        "user32.PostMessageW(hwnd, 1, 0, 0)",
        "user32.SendMessageW(hwnd, 1, 0, 0)",
        "os.startfile('x')",
        "process.terminate()",
        "process.kill()",
        "kernel32.TerminateProcess(h, 1)",
        "kernel32.ReadProcessMemory(h, a, b, 1, None)",
        "kernel32.WriteProcessMemory(h, a, b, 1, None)",
        "kernel32.CreateRemoteThread(h, None, 0, a, b, 0, None)",
        "user32.SetWindowsHookExW(13, cb, 0, 0)",
        "kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)",
        "kernel32.OpenProcess(PROCESS_VM_READ, False, pid)",
        "kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)",
        "kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)",
        "getattr(pattern, 'Invoke')()",
        "setattr(control, 'value', 1)",
        "eval('1')",
        "exec('x = 1')",
        "compile('1', 'x', 'eval')",
        "__import__('subprocess')",
        "importlib.import_module('pyautogui')",
        "dll['SendInput']",
        "user32['SetForegroundWindow']",
        "ImageGrab.grab()",
        "window.capture_as_image()",
        "pyautogui.screenshot()",
        "text.copy()",
        "element.expand()",
        "element.collapse()",
        "elem.GetCurrentPattern(UIA_InvokePatternId)",
        "elem.QueryInterface(IUIAutomationInvokePattern)",
        "elem.QueryInterface(IUIAutomationScrollPattern)",
        "elem.QueryInterface(IUIAutomationWindowPattern)",
        "elem.QueryInterface(IUIAutomationRangeValuePattern)",
        "elem.QueryInterface(IUIAutomationLegacyIAccessiblePattern)",
        "import pywinauto.keyboard",
        "from pywinauto import mouse\nfrom pywinauto.mouse import click",
        "from pywinauto.application import Application",
        "import pyautogui",
        "import pynput",
        "import subprocess",
        "import win32clipboard",
        "import webbrowser",
        "from PIL import ImageGrab",
        "import mss",
        "import win32process",
        # Found by the independent review: all of these once scanned clean.
        "user32.SetWindowTextW(hwnd, 'x')",
        "user32.EnableWindow(hwnd, False)",
        "user32.SetParent(a, b)",
        "user32.SendMessageCallbackW(hwnd, 1, 0, 0, cb, 0)",
        "user32.AttachThreadInput(a, b, True)",
        "user32.SetKeyboardState(buf)",
        "user32.LockWorkStation()",
        "user32.ExitWindowsEx(0, 0)",
        "user32.FlashWindowEx(ptr)",
        "ntdll.NtTerminateProcess(h, 1)",
        "user32.GetAsyncKeyState(65)",
        "user32.GetCursorPos(ptr)",
        "operator.attrgetter('SendInput')(user32)",
        "operator.methodcaller('Invoke')(pattern)",
        "user32.__getattr__('SendInput')",
        "vars(user32).get('SendInput')",
        "user32[1234]",
        "asyncio.create_subprocess_exec('x')",
        "os.spawnv(0, 'x', [])",
        "os.execv('x', [])",
        "ProcessPoolExecutor()",
        "pywinauto.mouse.release()",
        "pywinauto.keyboard.KeyAction()",
        "element.DoDefaultAction()",
        "element.accDoDefaultAction(0)",
        "element.Realize()",
        "control.set_edit_text('x')",
        "element.GetCurrentPattern(10001)",
        "def click(self): ...",
        "def invoke(self): ...",
        "class Focus: ...",
        "class launch: ...",
    ],
)
def test_the_scanner_detects_planted_violations(snippet: str) -> None:
    assert scan_source(snippet, "app/desktop/planted.py"), f"missed: {snippet}"


@pytest.mark.parametrize(
    "snippet",
    [
        # Reading state is the point of the slice.
        "state = pattern.CurrentToggleState",
        "selected = item.CurrentIsSelected",
        "expanded = pattern.CurrentExpandCollapseState",
        "value = pattern.CurrentValue",
        "text = pattern.DocumentRange.GetText(121)",
        "elem.QueryInterface(IUIAutomationTogglePattern)",
        "elem.QueryInterface(IUIAutomationSelectionItemPattern)",
        "elem.QueryInterface(IUIAutomationExpandCollapsePattern)",
        "elem.QueryInterface(IUIAutomationValuePattern)",
        "elem.QueryInterface(IUIAutomationTextPattern)",
        "elem.GetCurrentPropertyValue(UIA_IsInvokePatternAvailablePropertyId)",
        "elem.GetCurrentPattern(UIA_TogglePatternId)",
        "elem.FindAll(TreeScope_Children, condition)",
        "user32.IsWindowVisible(hwnd)",
        "user32.IsIconic(hwnd)",
        "user32.EnumWindows(cb, 0)",
        "kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)",
        "kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)",
        # Enum members that merely *name* an advertised pattern are constants, not calls.
        "patterns = {DesktopPattern.INVOKE, DesktopPattern.SCROLL, DesktopPattern.TOGGLE}",
        "SCROLL = 'scroll'",
        "class DesktopPattern(StrEnum):\n    INVOKE = 'invoke'\n    TOGGLE = 'toggle'",
        "process_id = 5\nfocused = True\nfocusable = False\nstarted_at = 1",
    ],
)
def test_the_scanner_allows_read_only_code(snippet: str) -> None:
    assert scan_source(snippet, "app/desktop/ok.py") == []


def test_win32_may_call_only_its_pinned_query_entry_points() -> None:
    assert scan_source("user32.IsWindowVisible(h)\nself._kernel32.OpenProcess(a, b, c)", "app/desktop/win32.py") == []
    assert [v.kind for v in scan_source("user32.Beep(1, 1)", "app/desktop/win32.py")] == ["unpinned-win32-call"]
    assert [v.kind for v in scan_source("self._advapi32.SomethingNew(x)", "app/desktop/win32.py")] == ["unpinned-win32-call"]
    # The same call is not "unpinned" anywhere else: the allowlist is per file.
    assert scan_source("user32.Beep(1, 1)", "app/desktop/other.py") == []


def test_the_uia_backend_may_use_only_its_pinned_com_members() -> None:
    assert scan_source("value = pattern.CurrentValue\nchild = walker.GetFirstChildElementBuildCache(e, r)", "app/desktop/uia_backend.py") == []
    assert [v.kind for v in scan_source("element.DoSomethingNew()", "app/desktop/uia_backend.py")] == ["unpinned-com-member"]
    assert [v.kind for v in scan_source("element.CurrentFooBar", "app/desktop/uia_backend.py")] == ["unpinned-com-member"]
    # Python's own lower-case members are not COM members.
    assert scan_source("found.append(x)\nvalues.get(k)", "app/desktop/uia_backend.py") == []


def test_only_the_supervisor_may_start_or_stop_a_process() -> None:
    launching = "import subprocess\nprocess = subprocess.Popen(['x'])\nprocess.kill()\nprocess.stdout.close()"
    assert scan_source(launching, "app/desktop/managed.py") == []
    assert scan_source(launching, "app/desktop/worker.py"), "the worker must not launch or kill a process"
    assert scan_source(launching, "app/desktop/observer.py")
    assert scan_source("thread.start()", "app/desktop/worker.py") == []
    assert scan_source("thread.start()", "app/desktop/observer.py"), "only the worker may start its own UIA thread"


# ---- S3: the effects are pinned to their modules ------------------------------------------------------


def test_the_effect_platform_may_call_only_its_four_pinned_entry_points() -> None:
    allowed = "self._user32.GetForegroundWindow()\nself._user32.GetLastInputInfo(i)\nself._kernel32.OpenProcess(a, b, c)"
    assert scan_source(allowed, "app/desktop/effects_win32.py") == []
    for planted in ("user32.ShowWindow(h, 9)", "user32.SendInput(1, p, 40)", "user32.BringWindowToTop(h)",
                    "user32.AttachThreadInput(a, b, True)", "user32.keybd_event(1, 0, 0, 0)", "user32.mouse_event(2, 0, 0, 0, 0)",
                    "user32.SetCursorPos(1, 1)", "user32.PostMessageW(h, 1, 0, 0)", "user32.Beep(1, 1)"):
        assert scan_source(planted, "app/desktop/effects_win32.py"), planted
    # The raw foreground call is allowed NOWHERE: focus is UIA SetFocus, in one reviewed method.
    for other in ("win32.py", "effects_win32.py", "effects.py", "worker.py", "observer.py", "surfaces.py", "client.py", "uia_backend.py"):
        assert scan_source("user32.SetForegroundWindow(h)", f"app/desktop/{other}"), other


def test_uia_set_focus_may_be_called_once_inside_the_reviewed_focus_method_only() -> None:
    good = "class E:\n    def focus(self):\n        self._element.SetFocus()\n"
    assert scan_source(good, "app/desktop/uia_backend.py") == []
    assert scan_source("element.SetFocus()", "app/desktop/uia_backend.py"), "outside the method"
    assert scan_source(good.replace("def focus", "def nudge"), "app/desktop/uia_backend.py")
    assert scan_source(good + "        self._element.SetFocus()\n", "app/desktop/uia_backend.py")
    assert scan_source(good.replace("SetFocus()", "SetFocus(1)"), "app/desktop/uia_backend.py")
    for other in ("observer.py", "effects.py", "effects_win32.py", "worker.py", "surfaces.py", "win32.py"):
        assert scan_source("element.SetFocus()", f"app/desktop/{other}"), other
        assert scan_source("element.set_focus()", f"app/desktop/{other}"), other


GOLDEN_SPAWN = (
    "import subprocess" + chr(10)
    + "subprocess.Popen([app.executable, *app.args], shell=False, env=launch_environment(), cwd=ntpath.dirname(app.executable), "
    + "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, "
    + "creationflags=_CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_BREAKAWAY_FROM_JOB)"
)


def test_the_one_process_start_must_be_exactly_the_reviewed_call() -> None:
    assert scan_source(GOLDEN_SPAWN, "app/desktop/effects_win32.py") == []
    nl = chr(10)
    mutations = {
        "shell": GOLDEN_SPAWN.replace("shell=False", "shell=True"),
        "no shell": GOLDEN_SPAWN.replace("shell=False, ", ""),
        "string command": GOLDEN_SPAWN.replace("[app.executable, *app.args]", "app.executable"),
        "other argv": GOLDEN_SPAWN.replace("[app.executable, *app.args]", "['cmd.exe', '/c', app.executable]"),
        "argv without args": GOLDEN_SPAWN.replace("[app.executable, *app.args]", "[app.executable]"),
        "inherited environment": GOLDEN_SPAWN.replace("env=launch_environment()", "env=os.environ"),
        "no environment": GOLDEN_SPAWN.replace("env=launch_environment(), ", ""),
        "other cwd": GOLDEN_SPAWN.replace("cwd=ntpath.dirname(app.executable)", "cwd='C:/Users'"),
        "inherited handles": GOLDEN_SPAWN.replace("close_fds=True", "close_fds=False"),
        "no close_fds": GOLDEN_SPAWN.replace("close_fds=True, ", ""),
        "pipe stdin": GOLDEN_SPAWN.replace("stdin=subprocess.DEVNULL", "stdin=subprocess.PIPE"),
        "other flags": GOLDEN_SPAWN.replace("_DETACHED_PROCESS | ", ""),
        "extra flags": GOLDEN_SPAWN.replace("_CREATE_BREAKAWAY_FROM_JOB)", "_CREATE_BREAKAWAY_FROM_JOB | 0x10)"),
        "startupinfo": GOLDEN_SPAWN[:-1] + ", startupinfo=si)",
        "executable override": GOLDEN_SPAWN[:-1] + ", executable='cmd.exe')",
        "two starts": GOLDEN_SPAWN + nl + GOLDEN_SPAWN.split(nl, 1)[1],
        "run": "import subprocess" + nl + "subprocess.run(['cmd', '/c', 'x'])",
        "check_output": "import subprocess" + nl + "subprocess.check_output(['x'])",
        "call": "import subprocess" + nl + "subprocess.call('x', shell=True)",
        "alias import": "import subprocess as sp" + nl + "sp.run('cmd', shell=True)",
        "from import": "from subprocess import Popen" + nl + "Popen('cmd', shell=True)",
        "from import run": "from subprocess import run" + nl + "run('cmd', shell=True)",
        "os.popen": GOLDEN_SPAWN + nl + "os.popen('cmd')",
        "os.system": "import os" + nl + "os.system('cmd')",
        "startfile": "import os" + nl + "os.startfile('x')",
    }
    for name, source in mutations.items():
        assert scan_source(source, "app/desktop/effects_win32.py"), name
    # Starting a process is not allowed in any other module.
    for other in ("effects.py", "worker.py", "observer.py", "registry.py", "client.py", "surfaces.py"):
        assert scan_source(GOLDEN_SPAWN, f"app/desktop/{other}"), other


def test_only_the_effect_module_may_leave_the_runtimes_job() -> None:
    from tests.desktop_source_scan import breakaway_violations

    assert breakaway_violations(APP) == []
    # A planted flag anywhere else is found.
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "desktop").mkdir()
        (root / "services").mkdir()
        (root / "services" / "other.py").write_text("FLAG = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB" + chr(10), encoding="utf-8")
        (root / "desktop" / "effects_win32.py").write_text("X = 'CREATE_BREAKAWAY_FROM_JOB'" + chr(10), encoding="utf-8")
        assert [v.file for v in breakaway_violations(root)] == ["services/other.py"]


def test_focus_may_only_be_called_on_a_window_root_in_the_effect_module() -> None:
    assert scan_source("self._observer.root_for(resolved).focus()", "app/desktop/effects.py") == []
    for planted in ("control.focus()", "child.focus()", "self._observer.focus()", "element.children()[0].focus()"):
        assert scan_source(planted, "app/desktop/effects.py"), planted


def test_a_member_reference_cannot_dodge_the_call_site_pin() -> None:
    nl = chr(10)
    dodge = "class E:" + nl + "    def focus(self):" + nl + "        f = self._element.SetFocus" + nl + "        f()" + nl
    assert scan_source(dodge, "app/desktop/uia_backend.py")
    scroll = "class E:" + nl + "    def scroll(self, step):" + nl + "        s = pattern.Scroll" + nl + "        s(3, 3)" + nl
    assert scan_source(scroll, "app/desktop/uia_backend.py")


def test_the_uia_scroll_pattern_may_be_called_once_inside_the_reviewed_method_and_nowhere_else() -> None:
    good = (
        "class E:\n    def scroll(self, step):\n        pattern = self._pattern(UIA_ScrollPatternId, IUIAutomationScrollPattern)\n"
        "        pattern.Scroll(a, b)\n"
    )
    assert scan_source(good, "app/desktop/uia_backend.py") == []
    assert scan_source("pattern.Scroll(a, b)", "app/desktop/uia_backend.py"), "a Scroll outside the method"
    assert scan_source(good.replace("def scroll", "def nudge"), "app/desktop/uia_backend.py")
    assert scan_source(good.replace("Scroll(a, b)", "Scroll(a)"), "app/desktop/uia_backend.py")
    twice = good + "        pattern.Scroll(a, b)\n"
    assert scan_source(twice, "app/desktop/uia_backend.py")
    # The ScrollPattern id is not available to any other file.
    assert scan_source("elem.QueryInterface(IUIAutomationScrollPattern)", "app/desktop/observer.py")
    # And no other acting pattern was opened up by it.
    for acting in ("IUIAutomationInvokePattern", "IUIAutomationWindowPattern", "IUIAutomationRangeValuePattern",
                   "UIA_InvokePatternId", "UIA_WindowPatternId"):
        assert scan_source(f"elem.QueryInterface({acting})", "app/desktop/uia_backend.py"), acting
    for verb in ("pattern.Invoke()", "pattern.SetValue(x)", "pattern.Select()", "pattern.Toggle()"):
        assert scan_source(verb, "app/desktop/uia_backend.py"), verb


def test_production_has_exactly_one_scroll_call_and_one_process_start() -> None:
    def count(file: str, attr: str) -> int:
        tree = ast.parse((DESKTOP / file).read_text(encoding="utf-8"))
        return sum(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == attr for n in ast.walk(tree)
        )

    assert count("uia_backend.py", "Scroll") == 1
    assert count("uia_backend.py", "SetFocus") == 1
    for path in DESKTOP.glob("*.py"):
        if path.name != "uia_backend.py":
            assert count(path.name, "SetFocus") == 0 and count(path.name, "SetForegroundWindow") == 0, path.name
    assert count("effects_win32.py", "Popen") == 1
    assert count("managed.py", "Popen") == 1
    for path in DESKTOP.glob("*.py"):
        if path.name not in ("effects_win32.py", "managed.py"):
            assert count(path.name, "Popen") == 0, path.name
        if path.name != "uia_backend.py":
            assert count(path.name, "Scroll") == 0, path.name


def test_focus_scroll_and_launch_names_are_allowed_only_in_their_own_modules() -> None:
    assert scan_source("def focus(self): ...", "app/desktop/effects.py") == []
    assert scan_source("def launch(self): ...", "app/desktop/effects.py") == []
    for name in ("focus", "scroll", "launch"):
        for other in ("registry.py", "surfaces.py", "protocol.py", "managed.py", "win32.py", "errors.py"):
            assert scan_source(f"def {name}(self): ...", f"app/desktop/{other}"), (name, other)


def test_the_forbidden_list_covers_every_verb_the_slice_promises_not_to_have() -> None:
    promised = {
        "click", "double_click", "invoke", "setvalue", "select", "toggle", "scroll", "setfocus", "focus",
        "activate", "setforegroundwindow", "showwindow", "sendinput", "mouse_event", "keybd_event",
        "send_keys", "type_keys", "press", "hotkey", "drag", "move", "resize", "close", "launch",
        "shellexecute", "createprocess", "openclipboard", "setclipboarddata",
    }
    assert promised <= FORBIDDEN


# ---- the production code passes it ---------------------------------------------------------------------


def test_production_desktop_code_contains_no_input_focus_launch_or_capture_primitive() -> None:
    violations = scan_package(DESKTOP)
    assert violations == [], "\n".join(str(v) for v in violations)


def test_the_scan_really_covers_every_desktop_module() -> None:
    scanned = {path.name for path in DESKTOP.glob("*.py")}
    assert {"worker.py", "observer.py", "uia_backend.py", "win32.py", "surfaces.py", "managed.py", "client.py"} <= scanned
    for path in DESKTOP.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))  # every module is at least parseable


def test_only_the_probe_and_the_effect_platform_touch_win32_and_only_the_backend_touches_com() -> None:
    def imports(name: str) -> set[str]:
        tree = ast.parse((DESKTOP / name).read_text(encoding="utf-8"))
        return {
            (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

    for path in DESKTOP.glob("*.py"):
        found = imports(path.name)
        assert ("ctypes" in found) == (path.name in ("win32.py", "effects_win32.py")), path.name
        assert ("comtypes" in found or "pywinauto" in found) == (path.name == "uia_backend.py"), path.name


def test_no_screenshot_capture_or_clipboard_reaches_the_desktop_package() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in DESKTOP.glob("*.py")).lower()
    for term in ("desktopcapturer", "screenshot", "imagegrab", "pytesseract", "ocr(", "clipboard", "bitblt", "sendinput"):
        assert term not in source, term


# ---- the wire schemas have no native identity, geometry or verb ----------------------------------------------


_FORBIDDEN_FIELD_PARTS = (
    "hwnd", "handle", "pid", "process", "path", "command", "exe", "automation", "class_name", "framework",
    "runtime_id", "rect", "bounding", "coordinate", "screen", "x", "y", "left", "top", "width", "height",
    "action", "selector", "script", "property", "verb", "click", "focus_target",
)


def _all_models() -> list[type[BaseModel]]:
    return [
        value for value in vars(protocol).values()
        if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel and value.__module__ == protocol.__name__
    ]


def test_no_wire_model_has_a_field_for_native_identity_geometry_or_an_action() -> None:
    models = _all_models()
    assert {model.__name__ for model in models} >= {"DesktopObservation", "DesktopNode", "SurfaceRecord", "ObserveRequest"}
    for model in models:
        assert model.model_config.get("extra") == "forbid", model.__name__
        for field in model.model_fields:
            assert field not in _FORBIDDEN_FIELD_PARTS and not any(
                part in field.split("_") for part in _FORBIDDEN_FIELD_PARTS if part not in ("x", "y")
            ), (model.__name__, field)


def test_the_role_vocabulary_is_closed() -> None:
    assert protocol.ROLE_BY_CONTROL_TYPE.keys() <= {
        "AppBar", "Button", "Calendar", "CheckBox", "ComboBox", "Custom", "DataGrid", "DataItem", "Document", "Edit",
        "Group", "Header", "HeaderItem", "Hyperlink", "Image", "List", "ListItem", "Menu", "MenuBar", "MenuItem", "Pane",
        "ProgressBar", "RadioButton", "ScrollBar", "SemanticZoom", "Separator", "Slider", "Spinner", "SplitButton",
        "StatusBar", "Tab", "TabItem", "Table", "Text", "Thumb", "TitleBar", "ToolBar", "ToolTip", "Tree", "TreeItem", "Window",
    }
    assert set(protocol.ROLE_BY_CONTROL_TYPE.values()) | {protocol.DesktopRole.UNKNOWN} == set(protocol.DesktopRole)


# ---- the firewall: nothing else can read a desktop observation -----------------------------------------------


def _importers(*needles: str) -> set[str]:
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            if any(m == n or m.startswith(n + ".") for m in modules for n in needles):
                found.add(path.relative_to(APP).as_posix())
    return found


def test_the_importer_scan_is_sound_because_the_app_uses_no_relative_imports() -> None:
    relative = [
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert relative == [], "a relative import would slip past the importer allowlist below"


def test_only_the_desktop_boundary_and_the_reviewed_s2_disclosure_path_import_desktop_code() -> None:
    """S1 allowed nothing outside the boundary. S2 rewrites this ON PURPOSE: exactly the reviewed disclosure
    modules may import desktop code, and each is pinned by name. Anything else fails here until reviewed."""
    importers = _importers("app.desktop", "app.services.desktop", "app.repositories.desktop")
    outside = {name for name in importers if not name.startswith("desktop/")}
    assert outside == {
        "main.py",                             # wires the (opt-in) services
        "api/routes.py",                       # the S1 routes and the S2 routes
        "api/errors.py",                       # the refusal -> HTTP mapping
        "services/desktop.py",                 # the S1 service itself
        "services/desktop_disclosure.py",      # S2: the ONE reviewed path from an observation to a provider
        "domain/desktop_disclosure.py",        # S2: the projection, redaction and grounding rules
        "api/desktop_disclosure_schemas.py",   # S2: the closed wire shapes
        "services/desktop_actions.py",         # S3: focus / scroll / launch through the action ledger
        "domain/desktop_actions.py",           # S3: the closed proposal shapes and failure classification
        "api/desktop_action_schemas.py",       # S3: the closed wire shapes
        "config.py",                           # S3: validates the trusted registered-application list
    }, outside


def test_only_the_reviewed_disclosure_service_reads_an_observation_back() -> None:
    """Without a confirmed, exact disclosure NO provider path can read an observation; with one, exactly the
    S2 service can. `get_observation` is the only read, and this pins every caller."""
    callers = {
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if ".get_observation(" in path.read_text(encoding="utf-8") and "desktop" in path.read_text(encoding="utf-8").lower()
    }
    # S3 adds one more reader, for a LOCAL card only: the runtime reads the observation the person is about to
    # scroll to build the exact approval card (control role and name). It sends nothing to any provider.
    assert callers == {"services/desktop_disclosure.py", "services/desktop_actions.py"}, callers


def test_no_planner_answer_memory_research_or_task_module_can_see_desktop_data() -> None:
    forbidden_readers = [
        "services/tasks.py", "services/research_tasks.py", "services/research_search.py", "services/page_inspection.py",
        "services/authenticated_read.py", "services/booking_preparation.py", "services/booking_tasks.py",
        "services/clinic_info.py", "services/form_draft.py", "services/form_prepare.py", "services/form_state.py",
        "services/actions.py", "services/recovery.py", "services/browser_execution.py",
        "domain/research.py", "domain/authenticated.py", "domain/page_observation.py", "domain/booking.py",
    ]
    sources = {name: (APP / name).read_text(encoding="utf-8") for name in forbidden_readers}
    for name, source in sources.items():
        assert "desktop_observations" not in source and "DesktopObservation" not in source and "app.desktop" not in source, name


def test_the_desktop_observation_table_is_referenced_only_by_its_own_persistence() -> None:
    holders = {
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if "desktop_observations" in path.read_text(encoding="utf-8")
    }
    assert holders == {"db/tables.py", "repositories/desktop.py"}


def test_the_contract_gains_only_the_desktop_error_codes_and_no_desktop_snapshot_schema() -> None:
    contract = json.loads((AGENT.parents[1] / "src" / "shared" / "agent-runtime-contract.json").read_text(encoding="ascii"))
    assert {"desktop_refused", "desktop_disclosure_refused", "desktop_disclosure_state_changed"} <= set(contract["errorCodes"])
    assert not [name for name in contract["schemas"] if "esktop" in name or "Surface" in name]
