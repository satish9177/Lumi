"""The real, one-shot window capture backend for the S5 visual fallback.

Exactly one native action happens here: `PrintWindow` into an off-screen bitmap sized to the target
window's own client area, read back with `GetDIBits`. Everything else in this module is geometry and
monitor queries -- reads, exactly like `win32.py`'s `WindowsSystemProbe` -- so the source scanner pins
this file's native surface as an exact allowlist the same way it pins `win32.py`'s.

No mouse, no keyboard, no coordinate input anywhere here, and the captured pixels never include
another window, the taskbar or another monitor: the bitmap this module creates is exactly the size of
the one client rect `capture()` is asked to fill.

`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` is set once, at construction: without it every
geometry call below (`GetWindowRect`, `GetClientRect`, `ClientToScreen`) would return DPI-virtualized
coordinates scaled to the primary monitor rather than the window's own true physical pixels, which
would silently corrupt the exact crop this whole module exists to prove. The call is best-effort: a
manifest or an earlier caller may already have fixed the process's DPI awareness, and a second attempt
to set it then fails harmlessly (Windows refuses to change it twice).
"""

import ctypes
import os
import zlib
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Final

from app.desktop.dpi import MonitorInfo, PhysicalRect, monitor_for_rect

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: Final = ctypes.c_void_p(-4)
_PW_CLIENTONLY: Final = 0x00000001
_PW_RENDERFULLCONTENT: Final = 0x00000002
_MONITORINFOF_PRIMARY: Final = 0x00000001
_DIB_RGB_COLORS: Final = 0
_BI_RGB: Final = 0
#: Bound generously above any real display estate; a request for anything larger is a bug, not a
#: bigger desktop, and must not become an unbounded allocation.
_MAX_CAPTURE_DIMENSION: Final = 8192


class _Rect(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 3)]


@dataclass(frozen=True, slots=True)
class WindowGeometry:
    window_rect: PhysicalRect
    client_rect: PhysicalRect
    monitor: MonitorInfo
    dpi: int


class WindowsCaptureBackend:
    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("desktop capture only runs on Windows")
        user32: Any = ctypes.WinDLL("user32", use_last_error=True)
        gdi32: Any = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32, self._gdi32 = user32, gdi32

        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)

        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_Point)]
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        self._enum_monitor_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(_Rect), wintypes.LPARAM
        )
        user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(_Rect), self._enum_monitor_proc, wintypes.LPARAM
        ]
        user32.EnumDisplayMonitors.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MonitorInfoExW)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int

        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
            ctypes.POINTER(_BitmapInfo), wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

    # -- geometry (queries only) --------------------------------------------------------------

    def monitors(self) -> list[MonitorInfo]:
        found: list[MonitorInfo] = []

        def visit(handle: int, _hdc: int, _rect: object, _lparam: int) -> bool:
            info = _MonitorInfoExW()
            info.cbSize = ctypes.sizeof(_MonitorInfoExW)
            if self._user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                monitor_id = zlib.crc32(info.szDevice.encode("utf-16-le", "surrogatepass"))
                rect = info.rcMonitor
                found.append(
                    MonitorInfo(
                        monitor_id=monitor_id,
                        rect=PhysicalRect(rect.left, rect.top, rect.right, rect.bottom),
                        primary=bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                    )
                )
            return True

        callback = self._enum_monitor_proc(visit)
        self._user32.EnumDisplayMonitors(None, None, callback, 0)
        return found

    def geometry(self, hwnd: int) -> WindowGeometry | None:
        """`None` on any torn or failed read: geometry is never partially trusted."""
        window = _Rect()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(window)):
            return None
        client = _Rect()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(client)):
            return None
        origin = _Point(0, 0)
        if not self._user32.ClientToScreen(hwnd, ctypes.byref(origin)):
            return None
        client_on_screen = PhysicalRect(origin.x, origin.y, origin.x + client.right, origin.y + client.bottom)
        window_rect = PhysicalRect(window.left, window.top, window.right, window.bottom)
        monitor = monitor_for_rect(window_rect, self.monitors())
        if monitor is None:
            return None
        dpi = int(self._user32.GetDpiForWindow(hwnd)) or 96
        return WindowGeometry(window_rect=window_rect, client_rect=client_on_screen, monitor=monitor, dpi=dpi)

    # -- the one capture call -------------------------------------------------------------------

    def capture(self, hwnd: int, width: int, height: int) -> bytes | None:
        """`PrintWindow`, client-only, into a bitmap sized exactly `width` x `height`.

        Returns top-down, tightly-packed 32bpp BGRA rows, or `None` if the OS call reports failure,
        the read-back produced nothing, or an implausible size was asked for.
        """
        if width <= 0 or height <= 0 or width > _MAX_CAPTURE_DIMENSION or height > _MAX_CAPTURE_DIMENSION:
            return None
        screen_dc = self._user32.GetWindowDC(None)
        if not screen_dc:
            return None
        try:
            mem_dc = self._gdi32.CreateCompatibleDC(screen_dc)
            if not mem_dc:
                return None
            try:
                bitmap = self._gdi32.CreateCompatibleBitmap(screen_dc, width, height)
                if not bitmap:
                    return None
                try:
                    previous = self._gdi32.SelectObject(mem_dc, bitmap)
                    try:
                        if not self._user32.PrintWindow(hwnd, mem_dc, _PW_CLIENTONLY | _PW_RENDERFULLCONTENT):
                            return None
                        return self._read_bits(mem_dc, bitmap, width, height)
                    finally:
                        self._gdi32.SelectObject(mem_dc, previous)
                finally:
                    self._gdi32.DeleteObject(bitmap)
            finally:
                self._gdi32.DeleteDC(mem_dc)
        finally:
            self._user32.ReleaseDC(None, screen_dc)

    def _read_bits(self, dc: int, bitmap: int, width: int, height: int) -> bytes | None:
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # negative: a top-down DIB, row 0 first.
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        copied = self._gdi32.GetDIBits(dc, bitmap, 0, height, buffer, ctypes.byref(info), _DIB_RGB_COLORS)
        if copied == 0:
            return None
        return bytes(buffer.raw)
