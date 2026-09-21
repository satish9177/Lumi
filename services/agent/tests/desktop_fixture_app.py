"""Deterministic Win32 fixture application for the read-only UIA observer tests.

TEST-ONLY. Pure Python + ctypes (stdlib only). It builds a small top-level
window (class ``LumiFixtureWindow``) with real standard controls (STATIC, EDIT,
BUTTON, COMBOBOX, LISTBOX) plus a custom pane class (``LumiFixturePane``) so a
UI Automation client sees a stable, known tree. Every window in the process is
subclassed (or owns its proc) and counts the messages that arrive from outside;
the counters are dumped as JSON so a test can prove that observing the tree
caused no clicks, typing, focus changes, toggles, selections or text changes.

Run as ``python -m tests.desktop_fixture_app ...`` (cwd = services/agent) or
``python tests/desktop_fixture_app.py ...``. After the window is shown (with
``SW_SHOWNOACTIVATE``; the fixture never calls SetFocus / SetForegroundWindow /
BringWindowToTop on itself) one JSON line is printed to stdout::

    {"event":"fixture-ready","hwnd":<int>,"pid":<int>}

Runtime commands (``SendMessage`` to the top-level HWND):

* ``WM_APP+1``  CMD_RELABEL          heading STATIC becomes ``Heading N`` (returns N)
* ``WM_APP+2``  CMD_RECREATE_BUTTON  Submit is destroyed and recreated as
                                     ``Submit N`` with a new HWND, same place in
                                     the tree (same parent, same z-order slot,
                                     same rectangle). Returns N.
* ``WM_APP+10`` CMD_DUMP             atomically write the counters JSON to
                                     ``--state-file``; returns 0
* ``WM_APP+11`` CMD_GET_SUBMIT_HWND  returns the current Submit HWND
* ``WM_APP+20`` CMD_HANG             a hostile accessibility provider: every later
                                     WM_GETOBJECT blocks this process's UI thread for
                                     ``wparam`` seconds (0 clears it). Used only to prove
                                     that a hung provider cannot hang the observer.

Counters are zeroed after the window is shown and the setup traffic drained,
immediately before the ready line is printed. The fixture's own mutations
(RELABEL / RECREATE, initial setup) are performed under a "self-mutation" flag
that suppresses every counter except ``getobject``.

Extras beyond the base spec: ``--overflow-items N`` (opt-in) adds a LISTBOX named
"Overflow" whose items past the third are scrolled out of view and therefore
report UIA ``IsOffscreen=True`` (the off-screen "Offscreen action" button does
not; see the comment in ``_build_standard``). When ``--bulk-nodes`` /
``--bulk-depth`` are used the window is widened/heightened and the bulk pane and
nested chain live in a second column so they stay on-screen.

Layout note: MSAA/UIA derives the accessible Name of an EDIT / COMBOBOX /
LISTBOX from the STATIC that immediately precedes it in sibling z-order. Child
windows are appended at the bottom of the sibling z-order in creation order, so
creating the label first and the control right after it is all that is needed
(verified: UIA Name of the edit is "Notes"). RECREATE re-inserts the new button
right after the sibling that preceded the old one to preserve that ordering.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import math
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Final, Iterator

if sys.platform != "win32":  # pragma: no cover - Windows-only fixture
    raise ImportError("desktop_fixture_app is a Windows-only test fixture")

# ---------------------------------------------------------------- constants --

WM_APP: Final = 0x8000
CMD_RELABEL: Final = WM_APP + 1
CMD_RECREATE_BUTTON: Final = WM_APP + 2
CMD_DUMP: Final = WM_APP + 10
CMD_GET_SUBMIT_HWND: Final = WM_APP + 11
CMD_HANG: Final = WM_APP + 20

WM_DESTROY: Final = 0x0002
WM_MOVE: Final = 0x0003
WM_SIZE: Final = 0x0005
WM_ACTIVATE: Final = 0x0006
WM_SETFOCUS: Final = 0x0007
WM_KILLFOCUS: Final = 0x0008
WM_SETTEXT: Final = 0x000C
WM_CLOSE: Final = 0x0010
WM_ACTIVATEAPP: Final = 0x001C
WM_SETFONT: Final = 0x0030
WM_GETOBJECT: Final = 0x003D
WM_NCDESTROY: Final = 0x0082
WM_NCACTIVATE: Final = 0x0086
WM_KEYDOWN: Final = 0x0100
WM_CHAR: Final = 0x0102
WM_SYSKEYDOWN: Final = 0x0104
WM_COMMAND: Final = 0x0111
WM_SYSCOMMAND: Final = 0x0112
WM_HSCROLL: Final = 0x0114
WM_VSCROLL: Final = 0x0115
WM_LBUTTONDOWN: Final = 0x0201
WM_LBUTTONUP: Final = 0x0202
WM_LBUTTONDBLCLK: Final = 0x0203
WM_RBUTTONDOWN: Final = 0x0204
WM_MOUSEWHEEL: Final = 0x020A

EM_SETLIMITTEXT: Final = 0x00C5
EM_REPLACESEL: Final = 0x00C2
BM_GETCHECK: Final = 0x00F0
BM_SETCHECK: Final = 0x00F1
BM_CLICK: Final = 0x00F5
CB_GETCURSEL: Final = 0x0147
CB_ADDSTRING: Final = 0x0143
CB_SETCURSEL: Final = 0x014E
LB_ADDSTRING: Final = 0x0180
LVS_REPORT: Final = 0x0001
LVS_SINGLESEL: Final = 0x0004
LVS_NOCOLUMNHEADER: Final = 0x4000
LVM_INSERTITEMW: Final = 0x104D
LVM_INSERTCOLUMNW: Final = 0x1061
LVIF_TEXT: Final = 0x0001
LVCF_WIDTH: Final = 0x0002
LB_SETCURSEL: Final = 0x0186
LB_GETCURSEL: Final = 0x0188
LB_SETTOPINDEX: Final = 0x0197

BN_CLICKED: Final = 0
EN_CHANGE: Final = 0x0300
CBN_SELCHANGE: Final = 1
LBN_SELCHANGE: Final = 1

SC_MINIMIZE: Final = 0xF020
SC_MAXIMIZE: Final = 0xF030
SC_CLOSE: Final = 0xF060
SC_RESTORE: Final = 0xF120

WS_CHILD: Final = 0x40000000
WS_VISIBLE: Final = 0x10000000
WS_DISABLED: Final = 0x08000000
WS_CLIPSIBLINGS: Final = 0x04000000
WS_CLIPCHILDREN: Final = 0x02000000
WS_BORDER: Final = 0x00800000
WS_VSCROLL: Final = 0x00200000
WS_GROUP: Final = 0x00020000
WS_TABSTOP: Final = 0x00010000
WS_OVERLAPPEDWINDOW: Final = 0x00CF0000
WS_EX_CLIENTEDGE: Final = 0x00000200

ES_PASSWORD: Final = 0x0020
ES_AUTOHSCROLL: Final = 0x0080
BS_PUSHBUTTON: Final = 0x0000
BS_AUTOCHECKBOX: Final = 0x0003
BS_AUTORADIOBUTTON: Final = 0x0009
CBS_DROPDOWNLIST: Final = 0x0003
LBS_NOTIFY: Final = 0x0001
LBS_NOINTEGRALHEIGHT: Final = 0x0100

SW_SHOWNOACTIVATE: Final = 4
SWP_NOSIZE: Final = 0x0001
SWP_NOMOVE: Final = 0x0002
SWP_NOZORDER: Final = 0x0004
SWP_NOACTIVATE: Final = 0x0010
GW_HWNDPREV: Final = 3
GWLP_WNDPROC: Final = -4
PM_REMOVE: Final = 0x0001
COLOR_BTNFACE: Final = 15
DEFAULT_GUI_FONT: Final = 17
IDC_ARROW: Final = 32512
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final = -4

TOP_CLASS: Final = "LumiFixtureWindow"
PANE_CLASS: Final = "LumiFixturePane"

# Fixed control ids for the well-known controls (dynamic ids start at 1000).
ID_SUBMIT: Final = 101
ID_CHECK: Final = 102
ID_RADIO_SMALL: Final = 103
ID_RADIO_LARGE: Final = 104
ID_COMBO: Final = 105
ID_LIST: Final = 106
ID_NESTED: Final = 107
ID_DISABLED: Final = 108
ID_OFFSCREEN: Final = 109
ID_MAIN_EDIT: Final = 110
ID_HIDDEN_EDIT: Final = 111
ID_PASSWORD_EDIT: Final = 112
ID_OVERFLOW: Final = 114
ID_SCROLL_LIST: Final = 115

COUNTER_KEYS: Final = (
    "getobject",
    "password_reads",
    "focus_events",
    "activations",
    "button_clicks",
    "bm_click_msgs",
    "toggle_msgs",
    "edit_changes",
    "selection_changes",
    "settext_msgs",
    "scroll_msgs",
    "mouse_msgs",
    "key_msgs",
    "window_moves",
    "window_close_msgs",
)

# ------------------------------------------------------------- win32 binding --

_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 - ctypes protocol
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


def _bind() -> None:
    hwnd, uint, wp, lp = wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    vp = ctypes.c_void_p
    sigs: list[tuple[Any, str, list[Any], Any]] = [
        (user32, "RegisterClassExW", [ctypes.POINTER(WNDCLASSEXW)], wintypes.ATOM),
        (
            user32,
            "CreateWindowExW",
            [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                hwnd, vp, vp, vp,
            ],
            vp,
        ),
        (user32, "DefWindowProcW", [hwnd, uint, wp, lp], _LRESULT),
        (user32, "CallWindowProcW", [vp, hwnd, uint, wp, lp], _LRESULT),
        (user32, "SetWindowLongPtrW", [hwnd, ctypes.c_int, vp], vp),
        (user32, "ShowWindow", [hwnd, ctypes.c_int], wintypes.BOOL),
        (user32, "UpdateWindow", [hwnd], wintypes.BOOL),
        (user32, "DestroyWindow", [hwnd], wintypes.BOOL),
        (user32, "GetMessageW", [ctypes.POINTER(wintypes.MSG), hwnd, uint, uint], ctypes.c_int),
        (user32, "PeekMessageW", [ctypes.POINTER(wintypes.MSG), hwnd, uint, uint, uint], wintypes.BOOL),
        (user32, "TranslateMessage", [ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
        (user32, "DispatchMessageW", [ctypes.POINTER(wintypes.MSG)], _LRESULT),
        (user32, "PostQuitMessage", [ctypes.c_int], None),
        (user32, "SendMessageW", [hwnd, uint, wp, lp], _LRESULT),
        (user32, "SetWindowTextW", [hwnd, wintypes.LPCWSTR], wintypes.BOOL),
        (user32, "GetWindowTextW", [hwnd, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user32, "GetWindowTextLengthW", [hwnd], ctypes.c_int),
        (user32, "GetWindow", [hwnd, uint], vp),
        (
            user32,
            "SetWindowPos",
            [hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, uint],
            wintypes.BOOL,
        ),
        (user32, "LoadCursorW", [vp, vp], vp),
        (user32, "AdjustWindowRectEx", [ctypes.POINTER(wintypes.RECT), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
        (user32, "SetProcessDpiAwarenessContext", [vp], wintypes.BOOL),
        (kernel32, "GetModuleHandleW", [wintypes.LPCWSTR], vp),
        (gdi32, "GetStockObject", [ctypes.c_int], vp),
    ]
    for lib, name, argtypes, restype in sigs:
        fn = getattr(lib, name)
        fn.argtypes = argtypes
        fn.restype = restype


_bind()

# ------------------------------------------------------------------- state --


@dataclass
class _Runtime:
    """Mutable process-wide state (single UI thread, so no locking)."""
    hang_seconds: float = 0.0

    state_file: str | None = None
    mode: str = "standard"
    pid: int = 0
    top: int = 0
    font: int = 0
    heading: int = 0
    submit: int = 0
    edit: int = 0
    password_edit: int = 0
    checkbox: int = 0
    radio_small: int = 0
    radio_large: int = 0
    combo: int = 0
    listbox: int = 0
    submit_rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    heading_n: int = 0
    submit_n: int = 0
    muted: int = 0
    next_id: int = 1000
    final_written: bool = False
    counters: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTER_KEYS, 0))


_RT = _Runtime()
_KIND: dict[int, str] = {}  # control id -> "push" | "toggle" | "edit" | "select"
_ORIG_PROCS: dict[int, int] = {}  # subclassed hwnd -> original WNDPROC pointer
_KEEPALIVE: list[Any] = []  # ctypes callbacks must outlive their windows


@contextlib.contextmanager
def _self_mutation() -> Iterator[None]:
    """Mark changes the fixture makes to itself so they are not counted."""
    _RT.muted += 1
    try:
        yield
    finally:
        _RT.muted -= 1


def _bump(key: str) -> None:
    _RT.counters[key] += 1


def _count_message(msg: int, wparam: int) -> None:
    """Count one message received by any fixture window (from outside)."""
    if msg == WM_GETOBJECT:
        _bump("getobject")
        if _RT.hang_seconds > 0:
            time.sleep(_RT.hang_seconds)
        return
    if _RT.muted:
        return
    if msg in (WM_SETFOCUS, WM_KILLFOCUS):
        _bump("focus_events")
    elif msg in (WM_ACTIVATE, WM_ACTIVATEAPP, WM_NCACTIVATE):
        _bump("activations")
    elif msg == BM_CLICK:
        _bump("bm_click_msgs")
    elif msg == BM_SETCHECK:
        _bump("toggle_msgs")
    elif msg in (CB_SETCURSEL, LB_SETCURSEL):
        _bump("selection_changes")
    elif msg in (WM_SETTEXT, EM_REPLACESEL):
        _bump("settext_msgs")
    elif msg in (WM_VSCROLL, WM_HSCROLL, WM_MOUSEWHEEL, LB_SETTOPINDEX):
        _bump("scroll_msgs")
    elif msg in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONDOWN):
        _bump("mouse_msgs")
    elif msg in (WM_KEYDOWN, WM_CHAR, WM_SYSKEYDOWN):
        _bump("key_msgs")
    elif msg in (WM_MOVE, WM_SIZE):
        _bump("window_moves")
    elif msg == WM_CLOSE:
        _bump("window_close_msgs")
    elif msg == WM_SYSCOMMAND and (wparam & 0xFFF0) in (
        SC_CLOSE, SC_MINIMIZE, SC_MAXIMIZE, SC_RESTORE
    ):
        _bump("window_close_msgs")


#: Messages that read a text control's contents (WM_GETTEXT, WM_GETTEXTLENGTH, EM_GETLINE, WM_COPY).
_TEXT_READ_MESSAGES: Final = (0x000D, 0x000E, 0x00C4, 0x0301)


def _count_password_read(hwnd: int, msg: int) -> None:
    """Count anything asking the password edit for its text. The fixture's own dump is muted."""
    if hwnd == _RT.password_edit and not _RT.muted and msg in _TEXT_READ_MESSAGES:
        _bump("password_reads")


def _count_command(wparam: int, lparam: int) -> None:
    """Count WM_COMMAND notifications (delivered to the top-level window)."""
    if _RT.muted or lparam == 0:
        return
    ctl_id = wparam & 0xFFFF
    code = (wparam >> 16) & 0xFFFF
    kind = _KIND.get(ctl_id)
    if kind == "push" and code == BN_CLICKED:
        _bump("button_clicks")
    elif kind == "toggle" and code == BN_CLICKED:
        _bump("toggle_msgs")
    elif kind == "edit" and code == EN_CHANGE:
        _bump("edit_changes")
    elif kind == "select" and code == CBN_SELCHANGE:  # == LBN_SELCHANGE
        _bump("selection_changes")


# ------------------------------------------------------------ small helpers --


def _get_text(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = int(user32.GetWindowTextLengthW(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


class _LVITEM(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_uint32), ("iItem", ctypes.c_int), ("iSubItem", ctypes.c_int),
        ("state", ctypes.c_uint32), ("stateMask", ctypes.c_uint32), ("pszText", ctypes.c_void_p),
        ("cchTextMax", ctypes.c_int), ("iImage", ctypes.c_int), ("lParam", ctypes.c_void_p),
        ("iIndent", ctypes.c_int), ("iGroupId", ctypes.c_int), ("cColumns", ctypes.c_uint32),
        ("puColumns", ctypes.c_void_p), ("piColFmt", ctypes.c_void_p), ("iGroup", ctypes.c_int),
    ]


def _send(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> int:
    return int(user32.SendMessageW(hwnd, msg, wparam, lparam))


def _send_text(hwnd: int, msg: int, text: str) -> int:
    buf = ctypes.create_unicode_buffer(text)
    return _send(hwnd, msg, 0, ctypes.addressof(buf))


def _alloc_id() -> int:
    _RT.next_id += 1
    return _RT.next_id


def _pad(text: str, length: int) -> str:
    if length > 0 and len(text) < length:
        return text + "x" * (length - len(text))
    return text


# ------------------------------------------------------------- window procs --


def _top_proc(hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
    h = hwnd or 0
    try:
        _count_message(msg, wparam)
        if msg == WM_COMMAND:
            _count_command(wparam, lparam)
            return 0
        if msg == CMD_RELABEL:
            return _relabel()
        if msg == CMD_RECREATE_BUTTON:
            return _recreate_submit()
        if msg == CMD_DUMP:
            _write_state()
            return 0
        if msg == CMD_GET_SUBMIT_HWND:
            return _RT.submit
        if msg == CMD_HANG:
            _RT.hang_seconds = float(wparam)
            return 0
        if msg == WM_DESTROY:
            _write_state(final=True)
            user32.PostQuitMessage(0)
            return 0
    except Exception as exc:  # a ctypes callback must never raise
        print(f"fixture top proc error: {exc!r}", file=sys.stderr, flush=True)
    return int(user32.DefWindowProcW(h, msg, wparam, lparam))


def _pane_proc(hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
    h = hwnd or 0
    try:
        _count_message(msg, wparam)
        if msg == WM_COMMAND:
            # Notifications from controls nested in a pane are forwarded to the
            # top-level window, exactly like a dialog's group box container.
            return _send(_RT.top, WM_COMMAND, wparam, lparam)
    except Exception as exc:
        print(f"fixture pane proc error: {exc!r}", file=sys.stderr, flush=True)
    return int(user32.DefWindowProcW(h, msg, wparam, lparam))


def _control_proc(hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
    """Shared subclass proc for every standard control."""
    h = hwnd or 0
    try:
        _count_message(msg, wparam)
        _count_password_read(h, msg)
    except Exception as exc:
        print(f"fixture control proc error: {exc!r}", file=sys.stderr, flush=True)
    orig = _ORIG_PROCS.get(h)
    if orig is None:
        return int(user32.DefWindowProcW(h, msg, wparam, lparam))
    result = int(user32.CallWindowProcW(orig, h, msg, wparam, lparam))
    if msg == WM_NCDESTROY:
        _ORIG_PROCS.pop(h, None)
    return result


_TOP_CB = _WNDPROC(_top_proc)
_PANE_CB = _WNDPROC(_pane_proc)
_CONTROL_CB = _WNDPROC(_control_proc)
_KEEPALIVE.extend([_TOP_CB, _PANE_CB, _CONTROL_CB])
_CONTROL_CB_PTR: Final = ctypes.cast(_CONTROL_CB, ctypes.c_void_p).value


def _subclass(hwnd: int) -> None:
    prev = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, _CONTROL_CB_PTR)
    if prev:
        _ORIG_PROCS[hwnd] = int(prev)


# ------------------------------------------------------------- construction --


def _register_class(name: str, cb: Any, hinst: int, background: int) -> None:
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = 0
    wc.lpfnWndProc = cb
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
    wc.hbrBackground = background
    wc.lpszClassName = name
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        raise ctypes.WinError(ctypes.get_last_error())


def _create(
    cls: str,
    text: str,
    style: int,
    rect: tuple[int, int, int, int],
    parent: int,
    ctl_id: int,
    ex_style: int = 0,
) -> int:
    x, y, w, h = rect
    hwnd = user32.CreateWindowExW(
        ex_style, cls, text, style, x, y, w, h, parent, ctl_id, _hinst(), None
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    if _RT.font:
        _send(int(hwnd), WM_SETFONT, _RT.font, 1)
    return int(hwnd)


def _hinst() -> int:
    return int(kernel32.GetModuleHandleW(None) or 0)


class _Builder:
    """Creates children and remembers them so they can all be subclassed."""

    def __init__(self, top: int) -> None:
        self.top = top
        self.created: list[int] = []

    def add(
        self,
        cls: str,
        text: str,
        style: int,
        rect: tuple[int, int, int, int],
        parent: int | None = None,
        ctl_id: int | None = None,
        kind: str | None = None,
        ex_style: int = 0,
    ) -> int:
        cid = ctl_id if ctl_id is not None else _alloc_id()
        if kind is not None:
            _KIND[cid] = kind
        hwnd = _create(
            cls, text, WS_CHILD | WS_VISIBLE | style, rect,
            self.top if parent is None else parent, cid, ex_style,
        )
        if cls != PANE_CLASS:
            self.created.append(hwnd)
        return hwnd

    def label(self, text: str, rect: tuple[int, int, int, int], parent: int | None = None, style: int = 0) -> int:
        return self.add("STATIC", text, style, rect, parent)

    def push(
        self, text: str, rect: tuple[int, int, int, int], parent: int | None = None,
        ctl_id: int | None = None, style: int = 0,
    ) -> int:
        return self.add("BUTTON", text, BS_PUSHBUTTON | WS_TABSTOP | style, rect, parent, ctl_id, "push")

    def pane(self, text: str, rect: tuple[int, int, int, int], parent: int | None = None) -> int:
        return self.add(PANE_CLASS, text, WS_BORDER | WS_CLIPCHILDREN, rect, parent)

    def edit(
        self, text: str, rect: tuple[int, int, int, int], ctl_id: int, style: int = 0,
        visible: bool = True, kind: str | None = "edit",
    ) -> int:
        cid = ctl_id
        if kind is not None:
            _KIND[cid] = kind
        st = WS_CHILD | (WS_VISIBLE if visible else 0) | WS_TABSTOP | ES_AUTOHSCROLL | style
        hwnd = _create("EDIT", "", st, rect, self.top, cid, WS_EX_CLIENTEDGE)
        _send(hwnd, EM_SETLIMITTEXT, 0, 0)  # allow very long --label-length text
        user32.SetWindowTextW(hwnd, text)
        self.created.append(hwnd)
        return hwnd


def _build_standard(b: _Builder, args: argparse.Namespace) -> None:
    W = 480
    x = 8
    y = 8
    rt = _RT

    rt.heading = b.label("Fixture heading", (x, y, W, 20))
    y += 24

    b.label("Notes", (x, y, W, 16))
    y += 16
    rt.edit = b.edit(_pad(args.marker, args.label_length), (x, y, W, 22), ID_MAIN_EDIT)
    y += 28

    rt.submit_rect = (x, y, 120, 26)
    rt.submit = b.push("Submit", rt.submit_rect, ctl_id=ID_SUBMIT)
    y += 32

    rt.checkbox = b.add(
        "BUTTON", "Enable option", BS_AUTOCHECKBOX | WS_TABSTOP, (x, y, 200, 20), ctl_id=ID_CHECK, kind="toggle"
    )
    y += 24

    rt.radio_small = b.add(
        "BUTTON", "Small", BS_AUTORADIOBUTTON | WS_GROUP | WS_TABSTOP, (x, y, 100, 20),
        ctl_id=ID_RADIO_SMALL, kind="toggle",
    )
    rt.radio_large = b.add(
        "BUTTON", "Large", BS_AUTORADIOBUTTON, (x + 110, y, 100, 20),
        ctl_id=ID_RADIO_LARGE, kind="toggle",
    )
    y += 26

    b.label("Size", (x, y, W, 16), style=WS_GROUP)
    y += 16
    rt.combo = b.add(
        "COMBOBOX", "", CBS_DROPDOWNLIST | WS_VSCROLL | WS_TABSTOP, (x, y, 200, 120),
        ctl_id=ID_COMBO, kind="select",
    )
    y += 28

    b.label("Choices", (x, y, W, 16))
    y += 16
    rt.listbox = b.add(
        "LISTBOX", "", LBS_NOTIFY | LBS_NOINTEGRALHEIGHT | WS_TABSTOP, (x, y, 200, 56),
        ctl_id=ID_LIST, kind="select", ex_style=WS_EX_CLIENTEDGE,
    )
    y += 64

    pane = b.pane("Fixture pane", (x, y, W, 64))
    b.label("Inside pane", (8, 8, 200, 16), parent=pane)
    b.push("Nested button", (8, 30, 120, 26), parent=pane, ctl_id=ID_NESTED)
    y += 72

    b.push("Disabled action", (x, y, 140, 26), ctl_id=ID_DISABLED, style=WS_DISABLED)
    y += 32

    b.edit("hidden secret text", (x, y, W, 22), ID_HIDDEN_EDIT, visible=False, kind=None)
    y += 28

    # Far outside the parent's client area (and the screen) but WS_VISIBLE.
    # NOTE: the Win32 UIA proxy still reports IsOffscreen=False for such a
    # button (verified); only its BoundingRectangle is outside the screen. Use
    # --overflow-items for elements that really report IsOffscreen=True.
    b.push("Offscreen action", (5000, y, 140, 26), ctl_id=ID_OFFSCREEN)
    y += 32

    if args.mode == "credential":
        b.label("Password", (x, y, W, 16))
        y += 16
        rt.password_edit = b.edit(args.password_text, (x, y, 300, 22), ID_PASSWORD_EDIT, style=ES_PASSWORD)
        y += 28

    overflow: int = int(args.overflow_items)
    overflow_list = 0
    if overflow > 0:
        # Opt-in source of genuine IsOffscreen=true nodes: the Win32 UIA proxy
        # reports IsOffscreen only for list items scrolled out of the list's
        # viewport (plain HWND buttons placed off-screen never report it).
        b.label("Overflow", (x, y, W, 16))
        y += 16
        overflow_list = b.add(
            "LISTBOX", "", LBS_NOTIFY | LBS_NOINTEGRALHEIGHT | WS_TABSTOP, (x, y, 200, 44),
            ctl_id=ID_OVERFLOW, kind="select", ex_style=WS_EX_CLIENTEDGE,
        )
        y += 50

    scroll_items: int = int(args.scroll_items)
    scroll_list = 0
    if scroll_items > 0:
        # A real report-mode ListView: its UIA proxy exposes ScrollPattern (the plain LISTBOX does not).
        ctypes.WinDLL("comctl32").InitCommonControls()
        b.label("Scrollable", (x, y, W, 16))
        y += 16
        scroll_list = b.add(
            "SysListView32", "", LVS_REPORT | LVS_NOCOLUMNHEADER | LVS_SINGLESEL | WS_TABSTOP, (x, y, 260, 60),
            ctl_id=ID_SCROLL_LIST, ex_style=WS_EX_CLIENTEDGE,
        )
        y += 66

    # Initial control state (subclasses are installed later, so none of this counts).
    if scroll_list:
        column = (ctypes.c_uint32 * 12)()
        column[0] = LVCF_WIDTH
        column[2] = 240
        _send(scroll_list, LVM_INSERTCOLUMNW, 0, ctypes.addressof(column))
        for i in range(scroll_items):
            row_text = ctypes.create_unicode_buffer(f"Scroll row {i + 1}")
            row = _LVITEM()
            row.mask = LVIF_TEXT
            row.iItem = i
            row.pszText = ctypes.addressof(row_text)
            _send(scroll_list, LVM_INSERTITEMW, 0, ctypes.addressof(row))
    for i in range(overflow):
        _send_text(overflow_list, LB_ADDSTRING, f"Overflow {i + 1}")
    _send(rt.checkbox, BM_SETCHECK, 1)
    _send(rt.radio_small, BM_SETCHECK, 1)
    for item in ("Alpha", "Beta", "Gamma"):
        _send_text(rt.combo, CB_ADDSTRING, item)
    _send(rt.combo, CB_SETCURSEL, 1)
    for item in ("One", "Two", "Three"):
        _send_text(rt.listbox, LB_ADDSTRING, item)
    _send(rt.listbox, LB_SETCURSEL, 1)


def _build_bulk(b: _Builder, args: argparse.Namespace, x0: int) -> int:
    """Build the bulk pane and nested chain in a second column; return column height."""
    y = 8
    col_w = 480
    n = int(args.bulk_nodes)
    if n > 0:
        cols = max(10, math.ceil(n / 30))
        btn_w = max(8, (col_w - 8) // cols)
        rows = math.ceil(n / cols)
        pane_h = rows * 18 + 10
        pane = b.pane("Bulk pane", (x0, y, col_w, pane_h))
        for i in range(n):
            r, c = divmod(i, cols)
            b.push(
                _pad(f"Bulk {i + 1}", args.label_length),
                (4 + c * btn_w, 4 + r * 18, btn_w - 2, 16),
                parent=pane,
            )
        y += pane_h + 8

    depth = int(args.bulk_depth)
    if depth > 0:
        step = 16
        parent: int | None = None
        px, py = x0, y
        pw = col_w
        ph = depth * step + 34
        for k in range(1, depth + 1):
            pane = b.pane(f"Depth pane {k}", (px, py, max(pw, 24), max(ph, 24)), parent=parent)
            b.label(f"Depth {k}", (2, 1, max(pw - 8, 16), 14), parent=pane)
            parent = pane
            px, py = 3, step
            pw -= 6
            ph -= step + 2
        b.push("Deep leaf", (2, step, 80, 20), parent=parent)
        y += depth * step + 34 + 8
    return y


# ---------------------------------------------------------------- commands --


def _relabel() -> int:
    _RT.heading_n += 1
    with _self_mutation():
        user32.SetWindowTextW(_RT.heading, f"Heading {_RT.heading_n}")
    return _RT.heading_n


def _recreate_submit() -> int:
    _RT.submit_n += 1
    old = _RT.submit
    with _self_mutation():
        prev = int(user32.GetWindow(old, GW_HWNDPREV) or 0)
        user32.DestroyWindow(old)
        new = _create(
            "BUTTON", f"Submit {_RT.submit_n}",
            WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON | WS_TABSTOP,
            _RT.submit_rect, _RT.top, ID_SUBMIT,
        )
        # Keep the same slot among siblings (the tree order UIA reports).
        user32.SetWindowPos(new, prev, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        _subclass(new)
        _RT.submit = new
    return _RT.submit_n


# ------------------------------------------------------------- state output --


def _snapshot() -> dict[str, Any]:
    rt = _RT
    data: dict[str, Any] = dict(rt.counters)
    data["pid"] = rt.pid
    data["hwnd"] = rt.top
    data["submit_hwnd"] = rt.submit
    data["heading_text"] = _get_text(rt.heading)
    data["submit_text"] = _get_text(rt.submit)
    data["edit_text"] = _get_text(rt.edit)
    data["checkbox_checked"] = _send(rt.checkbox, BM_GETCHECK) == 1
    if _send(rt.radio_small, BM_GETCHECK) == 1:
        data["radio_selected"] = "Small"
    elif _send(rt.radio_large, BM_GETCHECK) == 1:
        data["radio_selected"] = "Large"
    else:
        data["radio_selected"] = ""
    data["combo_index"] = _send(rt.combo, CB_GETCURSEL)
    data["list_index"] = _send(rt.listbox, LB_GETCURSEL)
    if rt.password_edit:
        with _self_mutation():
            data["password_text"] = _get_text(rt.password_edit)
    return data


def _atomic_write(path: str, payload: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(50):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader briefly holds the target open
            if attempt == 49:
                raise
            time.sleep(0.01)


def _write_state(final: bool = False) -> None:
    if not _RT.state_file:
        return
    if final and _RT.final_written:
        return
    if final:
        _RT.final_written = True
    _atomic_write(_RT.state_file, json.dumps(_snapshot(), sort_keys=True))


# --------------------------------------------------------------------- main --


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Lumi deterministic Win32 UIA fixture (test only)")
    p.add_argument("--title", default="Lumi Fixture")
    p.add_argument("--state-file", default=None)
    p.add_argument("--mode", choices=("standard", "dynamic", "credential", "bulk"), default="standard")
    p.add_argument("--marker", default="Fixture text")
    p.add_argument("--bulk-nodes", type=int, default=0)
    p.add_argument("--bulk-depth", type=int, default=0)
    p.add_argument("--label-length", type=int, default=0)
    p.add_argument(
        "--overflow-items", type=int, default=0,
        help="opt-in: extra LISTBOX 'Overflow' with N items, ~3 visible (rest IsOffscreen=true)",
    )
    p.add_argument(
        "--scroll-items", type=int, default=0,
        help="opt-in: a report-mode ListView with N rows, whose UIA proxy exposes ScrollPattern",
    )
    p.add_argument("--password-text", default="M9_S1_PASSWORD_SECRET_77")
    p.add_argument(
        "--no-activate", action="store_true", default=True,
        help="accepted for documentation; the fixture never activates itself",
    )
    return p.parse_args(argv)


def _drain_messages() -> None:
    msg = wintypes.MSG()
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _RT.state_file = args.state_file
    _RT.mode = args.mode
    _RT.pid = os.getpid()

    with contextlib.suppress(Exception):  # physical pixels == UIA rectangles
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)

    hinst = _hinst()
    _register_class(TOP_CLASS, _TOP_CB, hinst, COLOR_BTNFACE + 1)
    _register_class(PANE_CLASS, _PANE_CB, hinst, COLOR_BTNFACE + 1)
    _RT.font = int(gdi32.GetStockObject(DEFAULT_GUI_FONT) or 0)

    extra = int(args.bulk_nodes) > 0 or int(args.bulk_depth) > 0 or args.mode == "bulk"
    client_w = 504 + (500 if extra else 0)
    client_h = 600

    style = WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN

    def outer(cw: int, ch: int) -> tuple[int, int]:
        r = wintypes.RECT(0, 0, cw, ch)
        user32.AdjustWindowRectEx(ctypes.byref(r), style, False, 0)
        return r.right - r.left, r.bottom - r.top

    ow, oh = outer(client_w, client_h)
    top = user32.CreateWindowExW(0, TOP_CLASS, args.title, style, 40, 40, ow, oh, None, None, hinst, None)
    if not top:
        raise ctypes.WinError(ctypes.get_last_error())
    _RT.top = int(top)

    builder = _Builder(_RT.top)
    _build_standard(builder, args)
    if extra:
        col_h = _build_bulk(builder, args, 512)
        if col_h + 16 > client_h:
            ow, oh = outer(client_w, col_h + 16)
            user32.SetWindowPos(_RT.top, 0, 0, 0, ow, oh, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)

    for child in builder.created:
        _subclass(child)

    user32.ShowWindow(_RT.top, SW_SHOWNOACTIVATE)
    user32.ShowWindow(_RT.top, SW_SHOWNOACTIVATE)  # first call may be eaten by STARTUPINFO
    user32.UpdateWindow(_RT.top)
    _drain_messages()

    # Setup traffic must not count: zero everything, then announce readiness.
    for key in COUNTER_KEYS:
        _RT.counters[key] = 0
    sys.stdout.write(
        json.dumps({"event": "fixture-ready", "hwnd": _RT.top, "pid": _RT.pid}, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()

    msg = wintypes.MSG()
    while True:
        rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if rc <= 0:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
