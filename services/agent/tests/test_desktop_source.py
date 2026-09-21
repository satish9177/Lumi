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


def test_only_the_probe_touches_win32_and_only_the_backend_touches_com() -> None:
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
        assert ("ctypes" in found) == (path.name == "win32.py"), path.name
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
    }, outside


def test_only_the_reviewed_disclosure_service_reads_an_observation_back() -> None:
    """Without a confirmed, exact disclosure NO provider path can read an observation; with one, exactly the
    S2 service can. `get_observation` is the only read, and this pins every caller."""
    callers = {
        path.relative_to(APP).as_posix()
        for path in APP.rglob("*.py")
        if ".get_observation(" in path.read_text(encoding="utf-8") and "desktop" in path.read_text(encoding="utf-8").lower()
    }
    assert callers == {"services/desktop_disclosure.py"}, callers


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
