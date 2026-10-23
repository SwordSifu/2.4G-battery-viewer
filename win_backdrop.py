"""Opt-in, stdlib-only Windows 11 system backdrop for an owned Tk window.

Only documented DWM attributes are used: 20 (dark caption), 33 (round corners),
38 (3 Desktop Acrylic / 2 Mica, build >= 22621), plus full-frame extension.
https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwm_systembackdrop_type
https://learn.microsoft.com/en-us/windows/win32/dwm/customframe

The custom-frame contract requires ZERO RGBA in extended pixels; Microsoft's
black-brush technique is the basis for recommending black, NOT a guarantee that
Tk/GDI has preserved alpha. Canvas painting can conceal or tint the material;
GDI-colored pixels with zero alpha can even add their RGB to the backdrop, so
ordinary Canvas cards must not be assumed truly opaque on an extended frame.
Set Canvas gaps to result.client_background, then verify actual CLIENT pixels
on the target machine; native_accepted alone must never be labelled "glass".
Mica uses desktop wallpaper, not the windows immediately behind this window.

No UI is created at import, no timers/hooks are installed, no registry is written,
and no full-window alpha, transparentcolor or undocumented Accent API is used.
Call on the Tk thread after widgets exist; call again explicitly after relevant
OS setting/composition changes. Tk destruction needs no native cleanup. The
restore helper removes our material/extension, not arbitrary pre-existing DWM
customization (this adapter is intended for a fresh preview window).
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import os
import sys

SOLID_BACKGROUND = "#10151f"
EXTENDED_BACKGROUND = "#000000"
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMSBT_NONE = 1
DWMSBT_MAINWINDOW = 2
DWMSBT_TRANSIENTWINDOW = 3


@dataclass(frozen=True)
class ApiStatus:
    api: str
    attribute: int | None
    value: int | tuple[int, int, int, int] | None
    hresult: int | None
    error: str = ""

    @property
    def succeeded(self):
        return self.hresult is not None and self.hresult >= 0


@dataclass(frozen=True)
class BackdropResult:
    requested: str
    applied: str
    native_accepted: bool
    reason: str
    client_background: str
    api_status: tuple[ApiStatus, ...] = ()
    frame_extended: bool = False
    client_translucency_verified: bool = False


class _Margins(C.Structure):
    _fields_ = [(name, C.c_int32) for name in
                ("cxLeftWidth", "cxRightWidth", "cyTopHeight", "cyBottomHeight")]


class _HighContrast(C.Structure):
    _fields_ = [("cbSize", C.c_uint32), ("dwFlags", C.c_uint32),
                ("lpszDefaultScheme", C.c_wchar_p)]


class _WindowsAPI:
    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        self.dwm = C.WinDLL("dwmapi", use_last_error=True)
        self.user.GetAncestor.argtypes = [C.c_void_p, C.c_uint32]
        self.user.GetAncestor.restype = C.c_void_p
        self.user.GetWindowThreadProcessId.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
        self.user.GetWindowThreadProcessId.restype = C.c_uint32
        self.user.SystemParametersInfoW.argtypes = [C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32]
        self.user.SystemParametersInfoW.restype = C.c_int32
        self.dwm.DwmSetWindowAttribute.argtypes = [C.c_void_p, C.c_uint32, C.c_void_p, C.c_uint32]
        self.dwm.DwmSetWindowAttribute.restype = C.c_int32
        self.dwm.DwmExtendFrameIntoClientArea.argtypes = [C.c_void_p, C.POINTER(_Margins)]
        self.dwm.DwmExtendFrameIntoClientArea.restype = C.c_int32
        self.dwm.DwmIsCompositionEnabled.argtypes = [C.POINTER(C.c_int32)]
        self.dwm.DwmIsCompositionEnabled.restype = C.c_int32

    def hwnd(self, root):
        root.update_idletasks()
        child = int(root.winfo_id())
        hwnd = self.user.GetAncestor(child, 2)  # GA_ROOT: Tk's native wrapper.
        if not hwnd:
            raise OSError("Tk root HWND unavailable")
        pid = C.c_uint32()
        if not self.user.GetWindowThreadProcessId(hwnd, C.byref(pid)) or pid.value != os.getpid():
            raise OSError("refusing HWND not owned by this process")
        return hwnd

    def high_contrast(self):
        state = _HighContrast()
        state.cbSize = C.sizeof(state)
        if not self.user.SystemParametersInfoW(0x0042, state.cbSize, C.byref(state), 0):
            raise OSError("SPI_GETHIGHCONTRAST failed")
        return bool(state.dwFlags & 1)  # HCF_HIGHCONTRASTON

    def transparency_enabled(self):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                value, kind = winreg.QueryValueEx(key, "EnableTransparency")
                if kind != winreg.REG_DWORD:
                    raise OSError("unexpected EnableTransparency type")
                return bool(value)
        except FileNotFoundError:
            # Unknown is not permission to enable an accessibility-sensitive effect.
            raise OSError("EnableTransparency setting unavailable") from None

    def composition(self):
        enabled = C.c_int32()
        hr = self.dwm.DwmIsCompositionEnabled(C.byref(enabled))
        return hr, bool(enabled.value)

    def set_attribute(self, hwnd, attribute, value):
        data = C.c_int32(value)
        return self.dwm.DwmSetWindowAttribute(hwnd, attribute, C.byref(data), C.sizeof(data))

    def extend_frame(self, hwnd, value):
        margins = _Margins(*value) if isinstance(value, tuple) else _Margins(value, value, value, value)
        return self.dwm.DwmExtendFrameIntoClientArea(hwnd, C.byref(margins))


def _load_native():
    return _WindowsAPI()


def _windows_build():
    return sys.getwindowsversion().build


def _paint_root(root, color):
    try:
        root.configure(background=color)
        return True
    except Exception:  # A destroyed Tk window must not crash its owner.
        return False


def _record(statuses, api, attribute, value, operation):
    try:
        hr = C.c_int32(int(operation())).value
        status = ApiStatus(api, attribute, value, hr)
    except Exception as error:
        status = ApiStatus(api, attribute, value, None, str(error))
    statuses.append(status)
    return status.succeeded


def _attribute(native, hwnd, statuses, attribute, value):
    return _record(statuses, "DwmSetWindowAttribute", attribute, value,
                   lambda: native.set_attribute(hwnd, attribute, value))


def _extend(native, hwnd, statuses, value):
    return _record(statuses, "DwmExtendFrameIntoClientArea", None, value,
                   lambda: native.extend_frame(hwnd, value))


def _solid(root, requested, reason, statuses, native=None, hwnd=None, accepted=False):
    # Roll back even a partially accepted setup. Do not promise opacity if a
    # previously extended GDI surface could not be reset; report every HRESULT.
    if native is not None and hwnd is not None:
        backdrop_reset = _attribute(native, hwnd, statuses, DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_NONE)
        frame_reset = _extend(native, hwnd, statuses, 0)
        if not backdrop_reset or not frame_reset:
            reason += "; native-rollback-incomplete; solid-client-recommended-not-verified"
    if not _paint_root(root, SOLID_BACKGROUND):
        reason += "; root-background-unavailable"
    return BackdropResult(requested, "solid", accepted, reason, SOLID_BACKGROUND, tuple(statuses))


def apply_backdrop(tk_root, kind="acrylic", force_solid=False, frame_margins=None):
    """Select a material, or safe dark solid; never infer glass from HRESULT.

    `applied` describes the configured material (or selected solid fallback),
    not a pixel-verification result. Failed solid rollback/painting is reported
    in reason; a destroyed window or failed native reset cannot be verified. `native_accepted` means the
    requested #38 call succeeded, even if a later operation required rollback.
    `client_translucency_verified` is always False: only an external controlled
    pixel probe can establish visibility. All attempted DWM calls are preserved
    as immutable api_status records, including failures and rollback.
    """
    if kind not in ("acrylic", "mica", "solid"):
        raise ValueError("kind must be 'acrylic', 'mica' or 'solid'")
    if kind == "solid":
        force_solid = True
    margins = -1
    if frame_margins is not None:
        if (not isinstance(frame_margins, (tuple, list)) or len(frame_margins) != 4
                or any(type(value) is not int or not 0 <= value <= 8192 for value in frame_margins)):
            raise ValueError("frame_margins must contain four bounded nonnegative pixel integers")
        margins = tuple(frame_margins)
    statuses = []
    if sys.platform != "win32":
        return _solid(tk_root, kind, "unsupported-platform", statuses)
    try:
        native = _load_native()
        hwnd = native.hwnd(tk_root)
    except Exception as error:
        return _solid(tk_root, kind, "native-unavailable: " + str(error), statuses)
    if force_solid:
        return _solid(tk_root, kind, "forced-solid", statuses, native, hwnd)
    try:
        if _windows_build() < 22621:
            return _solid(tk_root, kind, "unsupported-build-before-22621", statuses, native, hwnd)
        if native.high_contrast():
            return _solid(tk_root, kind, "high-contrast-enabled", statuses, native, hwnd)
        if not native.transparency_enabled():
            return _solid(tk_root, kind, "transparency-disabled", statuses, native, hwnd)
        hr, composed = native.composition()
        composition_ok = _record(statuses, "DwmIsCompositionEnabled", None, None, lambda: hr)
        if not composition_ok or not composed:
            return _solid(tk_root, kind, "composition-unavailable", statuses, native, hwnd)
    except Exception as error:
        return _solid(tk_root, kind, "preference-query-failed: " + str(error), statuses, native, hwnd)

    if not _attribute(native, hwnd, statuses, DWMWA_USE_IMMERSIVE_DARK_MODE, 1):
        return _solid(tk_root, kind, "dark-caption-api-failed", statuses, native, hwnd)
    if not _attribute(native, hwnd, statuses, DWMWA_WINDOW_CORNER_PREFERENCE, 2):  # DWMWCP_ROUND
        return _solid(tk_root, kind, "corner-api-failed", statuses, native, hwnd)
    value = DWMSBT_TRANSIENTWINDOW if kind == "acrylic" else DWMSBT_MAINWINDOW
    if not _attribute(native, hwnd, statuses, DWMWA_SYSTEMBACKDROP_TYPE, value):
        return _solid(tk_root, kind, "system-backdrop-api-failed", statuses, native, hwnd)
    if not _extend(native, hwnd, statuses, margins):
        return _solid(tk_root, kind, "extended-frame-api-failed", statuses, native, hwnd, True)
    if not _paint_root(tk_root, EXTENDED_BACKGROUND):
        return _solid(tk_root, kind, "root-background-unavailable", statuses, native, hwnd, True)
    return BackdropResult(kind, kind, True,
                          "native-accepted-client-unverified; opaque Tk painting can conceal material",
                          EXTENDED_BACKGROUND, tuple(statuses), frame_extended=True)


def style_chrome(tk_root):
    """Best-effort dark caption/round corners without requesting transparency.

    Used for fresh solid-mode panels/details; accessibility or old builds retain
    native chrome. Own-process HWND validation is identical to material setup.
    """
    statuses = []
    if sys.platform != "win32":
        return ()
    try:
        native = _load_native()
        hwnd = native.hwnd(tk_root)
        if _windows_build() < 22000 or native.high_contrast():
            return ()
        _attribute(native, hwnd, statuses, DWMWA_USE_IMMERSIVE_DARK_MODE, 1)
        _attribute(native, hwnd, statuses, DWMWA_WINDOW_CORNER_PREFERENCE, 2)
    except Exception:
        pass  # Cosmetic only: never block a safe solid-content preview.
    return tuple(statuses)


def restore_backdrop(tk_root):
    """Remove our backdrop/extension and return to safe dark solid, idempotently.

    Does not restore unrelated prior frame customization. No cleanup is needed
    after destroying a window; do not call this on a replacement/destroyed HWND.
    """
    return apply_backdrop(tk_root, force_solid=True)
