"""Detect a foreground client covering the overlay's monitor (read-only Win32).

No game hooks, process scanning, DLL injection, file IO or extra polling thread.
Client bounds exclude normal maximized-window title bars/taskbar work areas.
All coordinates come from the caller's existing DPI context.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W


class POINT(C.Structure):
    _fields_ = [("x", W.LONG), ("y", W.LONG)]


class RECT(C.Structure):
    _fields_ = [("left", W.LONG), ("top", W.LONG), ("right", W.LONG), ("bottom", W.LONG)]


class MONITORINFO(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", W.DWORD)]


SHELL_CLASSES = frozenset(("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "#32768"))
_api = None


def client_covers_monitor(client, monitor, tolerance=2):
    """Cover the FULL monitor, not merely its taskbar-excluding work area."""
    left, top, right, bottom = client
    ml, mt, mr, mb = monitor
    return (right > left and bottom > top and mr > ml and mb > mt
            and left <= ml+tolerance and top <= mt+tolerance
            and right >= mr-tolerance and bottom >= mb-tolerance)


def _user32():
    global _api
    if _api is None:
        u = C.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = C.c_void_p
        u.IsWindowVisible.argtypes = [C.c_void_p]
        u.IsIconic.argtypes = [C.c_void_p]
        u.GetClassNameW.argtypes = [C.c_void_p, W.LPWSTR, C.c_int]
        u.GetClientRect.argtypes = [C.c_void_p, C.POINTER(RECT)]
        u.ClientToScreen.argtypes = [C.c_void_p, C.POINTER(POINT)]
        u.MonitorFromWindow.argtypes = [C.c_void_p, W.DWORD]
        u.MonitorFromWindow.restype = C.c_void_p
        u.GetMonitorInfoW.argtypes = [C.c_void_p, C.POINTER(MONITORINFO)]
        _api = u
    return _api


def foreground_fullscreen(overlay_hwnd, api=None):
    """True/False; None means inspection failed, so retain prior suppression.

    Suppress only on the monitor containing this overlay. Borderless games and
    fullscreen video count; shell/desktop, minimized and ordinary maximized
    windows do not. This is geometry detection, not proof of an exclusive-D3D
    game's rendering mode.
    """
    try:
        u = api if api is not None else _user32()
        foreground = u.GetForegroundWindow()
        if not foreground or foreground == overlay_hwnd:
            return False
        if not u.IsWindowVisible(foreground) or u.IsIconic(foreground):
            return False
        name = C.create_unicode_buffer(128)
        if not u.GetClassNameW(foreground, name, len(name)):
            return None
        if name.value in SHELL_CLASSES:
            return False
        monitor = u.MonitorFromWindow(overlay_hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return None
        if u.MonitorFromWindow(foreground, 2) != monitor:
            return False
        info = MONITORINFO()
        info.cbSize = C.sizeof(info)
        if not u.GetMonitorInfoW(monitor, C.byref(info)):
            return None
        rect = RECT()
        if not u.GetClientRect(foreground, C.byref(rect)):
            return None
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return False
        first, last = POINT(rect.left, rect.top), POINT(rect.right, rect.bottom)
        if not u.ClientToScreen(foreground, C.byref(first)) or not u.ClientToScreen(foreground, C.byref(last)):
            return None
        m = info.rcMonitor
        return client_covers_monitor((first.x, first.y, last.x, last.y),
                                     (m.left, m.top, m.right, m.bottom))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
