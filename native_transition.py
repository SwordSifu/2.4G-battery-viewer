"""A tiny owned, click-through layered strip between native glass and Tk paint.

Call ``present`` on the Tk thread after layout, and again on explicit movement /
resize. Coordinates are already-scaled ROOT CLIENT pixels, not window-frame or
screen coordinates. The band must contain no ink or controls. There is no timer,
idle polling, whole-window alpha, colour key, or dependency on corner_widget.
Importing this module (and constructing a strip) never loads a native DLL.

Windows owns the popup relative to the verified Tk wrapper. Implicit show/move
operations are prevented from changing Z order; the sole explicit Z placement is
relative to that owner, never HWND_TOP / HWND_TOPMOST. Unmapping the Tk root also
hides the strip. Call destroy before disposing the root; the Destroy binding is
a safety net. Failed native operations simply permit a painted fallback.
"""
from __future__ import annotations

import ctypes as C
import math
from numbers import Real
import os
import sys

MAX_WIDTH = 8192
MAX_HEIGHT = 512
DEFAULT_COLOR = (24, 37, 58)

GA_ROOT = 2
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
GWL_EXSTYLE = -20
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_MOUSEACTIVATE = 0x0021
WM_WINDOWPOSCHANGING = 0x0046
WM_NCDESTROY = 0x0082
WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1
MA_NOACTIVATE = 3
ULW_ALPHA = 2
AC_SRC_ALPHA = 1
HGDI_ERROR = C.c_void_p(-1).value

# Fixed-width Win32 fields also make the pure/headless tests portable to LP64.
WNDPROC = getattr(C, "WINFUNCTYPE", C.CFUNCTYPE)(
    C.c_ssize_t, C.c_void_p, C.c_uint32, C.c_size_t, C.c_ssize_t)


class WNDCLASSEXW(C.Structure):
    _fields_ = [
        ("cbSize", C.c_uint32), ("style", C.c_uint32),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", C.c_int32),
        ("cbWndExtra", C.c_int32), ("hInstance", C.c_void_p),
        ("hIcon", C.c_void_p), ("hCursor", C.c_void_p),
        ("hbrBackground", C.c_void_p), ("lpszMenuName", C.c_wchar_p),
        ("lpszClassName", C.c_wchar_p), ("hIconSm", C.c_void_p),
    ]


class POINT(C.Structure):
    _fields_ = [("x", C.c_int32), ("y", C.c_int32)]


class SIZE(C.Structure):
    _fields_ = [("cx", C.c_int32), ("cy", C.c_int32)]


class RECT(C.Structure):
    _fields_ = [(name, C.c_int32) for name in ("left", "top", "right", "bottom")]


class WINDOWPOS(C.Structure):
    _fields_ = [("hwnd", C.c_void_p), ("hwndInsertAfter", C.c_void_p),
                ("x", C.c_int32), ("y", C.c_int32),
                ("cx", C.c_int32), ("cy", C.c_int32), ("flags", C.c_uint32)]


class BITMAPINFOHEADER(C.Structure):
    _fields_ = [
        ("biSize", C.c_uint32), ("biWidth", C.c_int32), ("biHeight", C.c_int32),
        ("biPlanes", C.c_uint16), ("biBitCount", C.c_uint16),
        ("biCompression", C.c_uint32), ("biSizeImage", C.c_uint32),
        ("biXPelsPerMeter", C.c_int32), ("biYPelsPerMeter", C.c_int32),
        ("biClrUsed", C.c_uint32), ("biClrImportant", C.c_uint32),
    ]


class BITMAPINFO(C.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", C.c_uint32 * 1)]


class BLENDFUNCTION(C.Structure):
    _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte),
                ("SourceConstantAlpha", C.c_ubyte), ("AlphaFormat", C.c_ubyte)]


def _color(color_rgb):
    if not isinstance(color_rgb, (tuple, list)) or len(color_rgb) != 3:
        raise ValueError("color_rgb must contain three finite RGB channels")
    if any(isinstance(value, bool) or not isinstance(value, Real)
           or not math.isfinite(value) or not 0 <= value <= 255 for value in color_rgb):
        raise ValueError("RGB channels must be finite numbers between 0 and 255")
    return tuple(int(round(value)) for value in color_rgb)


def _dimension(value, maximum):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum


def transition_bgra(width, height, color_rgb=DEFAULT_COLOR) -> bytes:
    """Return bounded top-down, premultiplied BGRA with smoothstep opacity.

    Dimensions must be positive integers (at most 8192 by 512); RGB channels are
    finite numbers in [0, 255], rounded to bytes. For height >= 2 the first row is
    transparent and the last opaque. A degenerate one-row band is opaque, so its
    sole row still meets the dark body. Invalid inputs raise ValueError.
    """
    if not _dimension(width, MAX_WIDTH) or not _dimension(height, MAX_HEIGHT):
        raise ValueError("strip dimensions must be integers in 1..8192 by 1..512")
    red, green, blue = _color(color_rgb)
    rows = []
    for y in range(height):
        t = y / (height - 1) if height > 1 else 1.0
        alpha = round(255 * t * t * (3 - 2 * t))
        pixel = bytes(((blue * alpha + 127) // 255,
                       (green * alpha + 127) // 255,
                       (red * alpha + 127) // 255, alpha))
        rows.append(pixel * width)
    return b"".join(rows)


class _WindowsAPI:
    """All DLL access is here, reached only by the first Windows present call."""
    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        ptr, uint, boolean = C.c_void_p, C.c_uint32, C.c_int32
        signatures = (
            (self.user, "GetAncestor", ptr, [ptr, uint]),
            (self.user, "GetWindowThreadProcessId", uint, [ptr, C.POINTER(uint)]),
            (self.user, "IsWindow", boolean, [ptr]),
            (self.user, "IsWindowVisible", boolean, [ptr]),
            (self.user, "IsIconic", boolean, [ptr]),
            (self.user, "RegisterClassExW", C.c_uint16, [C.POINTER(WNDCLASSEXW)]),
            (self.user, "UnregisterClassW", boolean, [C.c_wchar_p, ptr]),
            (self.user, "CreateWindowExW", ptr,
             [uint, C.c_wchar_p, C.c_wchar_p, uint, C.c_int32, C.c_int32,
              C.c_int32, C.c_int32, ptr, ptr, ptr, ptr]),
            (self.user, "DestroyWindow", boolean, [ptr]),
            (self.user, "DefWindowProcW", C.c_ssize_t,
             [ptr, uint, C.c_size_t, C.c_ssize_t]),
            (self.user, "ValidateRect", boolean, [ptr, C.POINTER(RECT)]),
            (self.user, "GetDC", ptr, [ptr]),
            (self.user, "ReleaseDC", C.c_int32, [ptr, ptr]),
            (self.user, "UpdateLayeredWindow", boolean,
             [ptr, ptr, C.POINTER(POINT), C.POINTER(SIZE), ptr, C.POINTER(POINT),
              uint, C.POINTER(BLENDFUNCTION), uint]),
            (self.user, "SetWindowPos", boolean,
             [ptr, ptr, C.c_int32, C.c_int32, C.c_int32, C.c_int32, uint]),
            (self.user, "ShowWindow", boolean, [ptr, C.c_int32]),
            (self.gdi, "CreateCompatibleDC", ptr, [ptr]),
            (self.gdi, "CreateDIBSection", ptr,
             [ptr, C.POINTER(BITMAPINFO), uint, C.POINTER(ptr), ptr, uint]),
            (self.gdi, "SelectObject", ptr, [ptr, ptr]),
            (self.gdi, "DeleteObject", boolean, [ptr]),
            (self.gdi, "DeleteDC", boolean, [ptr]),
            (self.kernel, "GetModuleHandleW", ptr, [C.c_wchar_p]),
        )
        for dll, name, result, arguments in signatures:
            function = getattr(dll, name)
            function.restype, function.argtypes = result, arguments
        try:
            self.get_style = self.user.GetWindowLongPtrW
            self.get_style.restype = C.c_ssize_t
        except AttributeError:  # GetWindowLongPtr is a macro on 32-bit Windows.
            self.get_style = self.user.GetWindowLongW
            self.get_style.restype = C.c_int32
        self.get_style.argtypes = [ptr, C.c_int32]


def _load_native():
    return _WindowsAPI()


# A native class keeps a raw callback address, NOT a Python strong reference.
# Retain the entire owner until both its window and registered class are gone,
# including failed DestroyWindow / UnregisterClass branches.
_LIVE_CLASSES = {}


class TransitionStrip:
    """One nonactivating, non-topmost owned overlay; all methods fail safely."""
    def __init__(self, tk_root):
        self._root = tk_root
        self._native = None
        self._owner = None
        self._hwnd = None
        self._instance = None
        self._class_name = None
        self._registered = False
        self._callback = None
        self._closed = False
        self._shown = False
        self._placing = False
        self._stacked = False
        self._position = None
        self._pixel_key = None
        self._pixels = None
        self._uploaded_key = None
        self._bindings = []

    def _owned(self, hwnd):
        if not hwnd or not self._native.user.IsWindow(hwnd):
            return False
        pid = C.c_uint32()
        thread = self._native.user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        return bool(thread) and pid.value == os.getpid()

    def _root_hwnd(self):
        hwnd = self._native.user.GetAncestor(int(self._root.winfo_id()), GA_ROOT)
        if not self._owned(hwnd):
            raise OSError("refusing unavailable or foreign Tk root HWND")
        if self._owner is not None and hwnd != self._owner:
            raise OSError("Tk owner HWND changed")
        return hwnd

    def _bind_root(self):
        if self._bindings:
            return
        for event, callback in (("<Unmap>", self._on_unmap), ("<Destroy>", self._on_destroy)):
            try:
                token = self._root.bind(event, callback, add="+")
                if token:
                    self._bindings.append((event, token))
            except Exception:
                pass

    def _on_unmap(self, event):
        if event.widget is self._root:
            self.hide()

    def _on_destroy(self, event):
        if event.widget is self._root:
            self.destroy()

    def _unbind_root(self):
        for event, token in self._bindings:
            try:
                self._root.unbind(event, token)
            except Exception:
                pass
        self._bindings.clear()

    def _unregister(self):
        if self._hwnd:
            return False
        if self._registered:
            try:
                if not self._native.user.UnregisterClassW(self._class_name, self._instance):
                    return False
            except Exception:
                return False
            self._registered = False
        _LIVE_CLASSES.pop(self._class_name, None)
        self._callback = None
        self._class_name = None
        self._instance = None
        return True

    def _ensure_window(self, owner):
        if self._hwnd:
            return self._owned(self._hwnd)
        if not self._registered:
            self._instance = self._native.kernel.GetModuleHandleW(None)
            if not self._instance:
                return False
            self._class_name = f"BatteryTransitionStrip.{os.getpid()}.{id(self):x}"
            self._callback = WNDPROC(self._wnd_proc)
            window_class = WNDCLASSEXW()
            window_class.cbSize = C.sizeof(window_class)
            window_class.lpfnWndProc = self._callback
            window_class.hInstance = self._instance
            window_class.lpszClassName = self._class_name
            if not self._native.user.RegisterClassExW(C.byref(window_class)):
                self._unregister()
                return False
            self._registered = True
            _LIVE_CLASSES[self._class_name] = self
        self._owner = owner
        try:
            self._hwnd = self._native.user.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
                self._class_name, "", WS_POPUP, 0, 0, 0, 0,
                owner, None, self._instance, None)
        except Exception:
            self._unregister()
            return False
        if not self._hwnd:
            self._unregister()
            return False
        self._bind_root()
        return self._owned(self._hwnd)

    def _wnd_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_NCHITTEST:
                return HTTRANSPARENT
            if message == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if message == WM_PAINT:
                self._native.user.ValidateRect(hwnd, None)
                return 0
            if message == WM_ERASEBKGND:
                return 1
            if message == WM_WINDOWPOSCHANGING and lparam:
                position = C.cast(lparam, C.POINTER(WINDOWPOS)).contents
                position.flags |= SWP_NOACTIVATE | SWP_NOOWNERZORDER
                if not self._placing:
                    # In particular SW_SHOWNOACTIVATE must not implicitly raise
                    # this strip above an unrelated foreground application.
                    position.flags |= SWP_NOZORDER
            if message == WM_NCDESTROY and hwnd == self._hwnd:
                self._hwnd = None
                self._shown = False
                self._closed = True
                self._uploaded_key = None
                self._position = None
            return self._native.user.DefWindowProcW(hwnd, message, wparam, lparam)
        except Exception:
            # Python exceptions must never escape a native callback.
            return 0

    def _upload(self, width, height, x, y):
        user, gdi = self._native.user, self._native.gdi
        screen = memory = bitmap = previous = None
        succeeded = False
        cleanup_ok = True
        try:
            screen = user.GetDC(None)
            if not screen:
                return False
            memory = gdi.CreateCompatibleDC(screen)
            if not memory:
                return False
            info = BITMAPINFO()
            info.bmiHeader = BITMAPINFOHEADER(
                C.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0,
                width * height * 4, 0, 0, 0, 0)
            bits = C.c_void_p()
            bitmap = gdi.CreateDIBSection(screen, C.byref(info), 0, C.byref(bits), None, 0)
            if not bitmap or not bits.value:
                return False
            previous = gdi.SelectObject(memory, bitmap)
            if not previous or previous == HGDI_ERROR:
                previous = None
                return False
            C.memmove(bits, self._pixels, len(self._pixels))
            destination, size, source = POINT(x, y), SIZE(width, height), POINT(0, 0)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            succeeded = bool(user.UpdateLayeredWindow(
                self._hwnd, screen, C.byref(destination), C.byref(size), memory,
                C.byref(source), 0, C.byref(blend), ULW_ALPHA))
        except Exception:
            succeeded = False
        finally:
            # Delete the DC before the bitmap even if restoring the old selection
            # fails: the DIB must not remain selected into a live DC on deletion.
            operations = []
            if previous:
                operations.append((gdi.SelectObject, (memory, previous)))
            if memory:
                operations.append((gdi.DeleteDC, (memory,)))
            if bitmap:
                operations.append((gdi.DeleteObject, (bitmap,)))
            if screen:
                operations.append((user.ReleaseDC, (None, screen)))
            for operation, arguments in operations:
                try:
                    result = operation(*arguments)
                    if not result or result == HGDI_ERROR:
                        cleanup_ok = False
                except Exception:
                    cleanup_ok = False
        return succeeded and cleanup_ok

    def present(self, y_top_px, y_bottom_px, color_rgb=DEFAULT_COLOR, side_margin_px=0) -> bool:
        """Present in a blank client band, or hide and return False on failure.

        Pixel bytes have a single-entry (width, height, rounded RGB) cache. Native
        pixels are uploaded only when that key changes, never for position-only
        updates. Invalid/out-of-client geometry is rejected before DLL loading.
        """
        if self._closed or sys.platform != "win32":
            return False
        try:
            color = _color(color_rgb)
            coordinates = (y_top_px, y_bottom_px, side_margin_px)
            if any(not isinstance(value, int) or isinstance(value, bool) for value in coordinates):
                raise ValueError("band coordinates must be pixel integers")
            if not self._root.winfo_viewable():
                self.hide()
                return False
            root_width, root_height = self._root.winfo_width(), self._root.winfo_height()
            width, height = root_width - 2 * side_margin_px, y_bottom_px - y_top_px
            if (side_margin_px < 0 or y_top_px < 0 or y_bottom_px > root_height
                    or not _dimension(width, MAX_WIDTH) or not _dimension(height, MAX_HEIGHT)):
                raise ValueError("transition band is outside the bounded root client")
            x = int(self._root.winfo_rootx()) + side_margin_px
            y = int(self._root.winfo_rooty()) + y_top_px
            if not all(-(2 ** 31) <= value < 2 ** 31 for value in (x, y, x + width, y + height)):
                raise ValueError("screen position exceeds Win32 coordinates")
            if self._native is None:
                self._native = _load_native()
            owner = self._root_hwnd()
            if (self._native.user.IsIconic(owner) or not self._native.user.IsWindowVisible(owner)
                    or self._native.get_style(owner, GWL_EXSTYLE) & WS_EX_TOPMOST):
                self.hide()
                return False
            if not self._ensure_window(owner):
                self.hide()
                return False
            key = (width, height, color)
            if self._pixel_key != key:
                self._pixels = transition_bgra(width, height, color)
                self._pixel_key = key
            uploaded = self._uploaded_key != key
            if uploaded:
                if not self._upload(width, height, x, y):
                    self._uploaded_key = None
                    self.hide()
                    return False
                self._uploaded_key = key
                self._position = (x, y)
            if not self._stacked:
                # Owned popups are always above their owner. Express initial Z
                # placement ONLY relative to that same verified non-topmost HWND;
                # never move the owner or choose a global top-of-stack sentinel.
                self._placing = True
                try:
                    if not self._native.user.SetWindowPos(
                            self._hwnd, owner, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER):
                        raise OSError("owner-relative placement failed")
                finally:
                    self._placing = False
                self._stacked = True
            if not uploaded and self._position != (x, y):
                if not self._native.user.SetWindowPos(
                        self._hwnd, None, x, y, 0, 0,
                        SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER):
                    raise OSError("strip movement failed")
                self._position = (x, y)
            if not self._shown:
                self._shown = True
                self._native.user.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                if not self._native.user.IsWindowVisible(self._hwnd):
                    raise OSError("strip did not become visible")
            return True
        except Exception:
            self.hide()
            return False

    def hide(self) -> bool:
        """Hide only this owned strip; repeat calls do not issue native writes."""
        if sys.platform != "win32" or self._native is None or not self._hwnd:
            return False
        try:
            if not self._owned(self._hwnd):
                return False
            if self._shown:
                self._native.user.ShowWindow(self._hwnd, SW_HIDE)
                self._shown = False
            return True
        except Exception:
            return False

    def destroy(self) -> bool:
        """Terminal and idempotent; keep callbacks alive if native cleanup fails."""
        self._closed = True
        self._unbind_root()
        self._pixels = None
        self._pixel_key = self._uploaded_key = None
        if sys.platform != "win32":
            return False
        if self._native is None:
            return True
        if self._hwnd:
            try:
                if not self._native.user.IsWindow(self._hwnd):
                    self._hwnd = None  # Owner may already have destroyed it.
                elif not self._owned(self._hwnd):
                    return False
                else:
                    self.hide()
                    if not self._native.user.DestroyWindow(self._hwnd):
                        return False
                    self._hwnd = None
            except Exception:
                return False
        self._shown = False
        return self._unregister()
