# -*- coding: utf-8 -*-
"""corner_widget.py —— 屏幕左下角的环状电量浮窗（纯 ctypes，零依赖）

设计约束（不能违反）：
* 运行在**托盘进程内**，不另起进程、不重复查询 HID。
* 用分层窗口（WS_EX_LAYERED + UpdateLayeredWindow）画环，天然抗锯齿、
  不需要 tkinter，也不创建任何临时文件。
* 面板不抢焦点（WS_EX_NOACTIVATE），鼠标穿透只在需要时才关闭。
* 交互（全部是显式操作）：
    - 左键单击任意环  → 立刻刷新一次
    - 右键            → 弹出管理菜单（刷新 / 显示托盘图标 / 隐藏浮窗 /
                        打开详细面板 / 固定或自由拖动 / 刷新间隔 / 退出）
    - 拖动            → 仅当菜单里选过"解锁位置"才可移动，位置会记住
* 双击 → 打开详细面板。

不要在这里做任何猜测性 HID 操作：数据全部来自调用方传入的 Reading。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import ring_render as rr

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_ERASEBKGND = 0x0014
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CAPTURECHANGED = 0x0215
WM_APP = 0x8000
WM_TIMER = 0x0113

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_ssize_t)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", ctypes.c_void_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", ctypes.c_void_p), ("message", wintypes.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wintypes.DWORD), ("pt", POINT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.CreateWindowExW.restype = ctypes.c_void_p
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    ctypes.c_void_p, ctypes.POINTER(POINT), wintypes.DWORD,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         ctypes.c_void_p, wintypes.UINT]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.SetCapture.argtypes = [ctypes.c_void_p]
user32.ReleaseCapture.restype = wintypes.BOOL
# TPM_RETURNCMD returns a command id; handles must retain pointer width.
user32.TrackPopupMenu.restype = wintypes.UINT
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.ValidateRect.restype = wintypes.BOOL
user32.ValidateRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                               ctypes.c_size_t, ctypes.c_ssize_t]
user32.SetTimer.restype = ctypes.c_size_t
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                            wintypes.UINT, ctypes.c_void_p]
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
user32.EndMenu.restype = wintypes.BOOL
user32.EndMenu.argtypes = []
user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetModuleHandleW.restype = ctypes.c_void_p
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]

gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFOHEADER),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.c_void_p, wintypes.DWORD]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]

SPI_GETWORKAREA = 0x0030
SM_CXSMICON = 49
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_CHECKED = 0x0008
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
# 菜单超出屏幕时用来翻转展开方向：靠下向上展开、靠右向左展开
TPM_RIGHTALIGN = 0x0008
TPM_BOTTOMALIGN = 0x0020
GWL_EXSTYLE = -20
RGN_OR = 2
TIMER_KEEPALIVE = 1
TIMER_RESTORE = 2
MSG_RESTORE = WM_APP + 20
GW_HWNDPREV = 3
# 前台窗口切换事件：任务栏被点击/激活时触发，用它来补重传画面比轮询省得多
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
WINEVENTPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.LONG, wintypes.LONG,
                                  wintypes.DWORD, wintypes.DWORD)


def work_area() -> tuple:
    rect = RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, 1920, 1080


def taskbar_rect():
    """任务栏条 (left, top, right, bottom)。

    使用当前进程 DPI 上下文的屏幕坐标；托盘入口设置 DPI-aware，
    本机此时返回物理像素。不要另乘一次缩放系数。取不到返回 None。
    """
    try:
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return None
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        if rect.right - rect.left < 2 or rect.bottom - rect.top < 2:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def physical_taskbar_band() -> tuple:
    """返回任务栏内的 (x, y, height)，与窗口使用相同 DPI 坐标系。

    不混用物理/虚拟化坐标，也不对 GetWindowRect 的结果重复缩放。
    """
    tb = taskbar_rect()
    if tb is None:
        return None
    left, top, right, bottom = tb
    height = max(8, bottom - top)
    margin = max(1, int(round(height * 0.05)))
    usable = max(8, height - margin * 2)
    return left + margin, top + margin, usable


def _dpi_scale(hwnd=None) -> float:
    try:
        user32.GetDpiForWindow.restype = ctypes.c_uint
        user32.GetDpiForWindow.argtypes = [ctypes.c_void_p]
        dpi = user32.GetDpiForWindow(hwnd) if hwnd else 96
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


class CornerWidget:
    """浮窗本体。数据由托盘进程通过 update() 推入。"""

    WND_CLASS = "PersonalBatteryCornerWnd.v1"

    def __init__(self, on_refresh, on_open_panel, on_toggle_tray,
                 on_hide, on_quit, on_toggle_lock, interval_getter,
                 on_interval_change, locked: bool = True, position=None):
        self.on_refresh = on_refresh
        self.on_open_panel = on_open_panel
        self.on_toggle_tray = on_toggle_tray
        self.on_hide = on_hide
        self.on_quit = on_quit
        self.on_toggle_lock = on_toggle_lock
        self.interval_getter = interval_getter
        self.on_interval_change = on_interval_change
        self.locked = locked
        self.position = position
        self.hwnd = None
        self.wndproc_ref = None
        self.readings = []
        self.width = self.height = 0
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._dragging = False
        self._drag_origin = (0, 0)
        self._window_origin = (0, 0)
        self._menu = None
        self._was_buried = False
        self._surface = None
        self._hook = None
        self._hook_proc = None
        self._last_submit = 0.0
        self._visible = True
        self._shown = False
        self._destroyed = False
        self._menu_active = False
        self._paint_pending = False
        self._restore_remaining = 0

    def _can_present(self) -> bool:
        return bool(self.hwnd and self._visible and not self._destroyed
                    and not self._menu_active)

    # ---- 创建 / 销毁 ----
    def create(self, hinst=None) -> bool:
        hinst = hinst or kernel32.GetModuleHandleW(None)
        self.wndproc_ref = WNDPROC(self._wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0x0008  # CS_DBLCLKS
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self.WND_CLASS
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() not in (0, 1410):
                return False
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        # 只靠 SetWindowRgn 裁剪可点击区域：区域之外系统根本不会把点击
        # 投给我们，自然穿透到任务栏。
        # 绝对不要加 WS_EX_TRANSPARENT：那个标志会让窗口完全收不到鼠标
        # 消息，表现为"环就在那里，但点了没反应、像消失了一样"。
        ex |= WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(ex, self.WND_CLASS, "personal-battery-corner",
                                      WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)
        if not hwnd:
            return False
        self.hwnd = hwnd
        self.width = self.height = 0
        return True

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._visible = False
        self._stop_keepalive()
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_RESTORE)
            self._restore_remaining = 0
            if self._menu_active:
                # TrackPopupMenu owns the nested loop; its finally block frees
                # the menu after EndMenu returns, avoiding double destruction.
                user32.EndMenu()
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        self._surface = None

    # ---- 绘制 ----
    def _taskbar_band(self) -> tuple:
        """返回 (x, y, height)，把浮窗贴在任务栏条里。

        任务栏是 topmost 窗口；本窗口在它之后创建并同为 topmost，
        因此能画在它上面（实测通过）。任务栏缺席时退回工作区左下角。
        """
        band = physical_taskbar_band()
        if band is not None:
            return band
        left, top, right, bottom = work_area()
        scale = _dpi_scale(None)
        usable = max(8, int(round(48 * scale)))
        return left + 6, bottom - usable - 6, usable

    def _taskbar_color(self):
        """采样任务栏底色，用来让底板与任务栏同色。

        取任务栏条右侧一块空白区域的中位色，避开图标与文字。
        采样失败时返回 None，回退到默认深色主题。
        """
        tb = taskbar_rect()
        if tb is None:
            return None
        left, top, right, bottom = tb
        try:
            user32.GetDC.restype = ctypes.c_void_p
            user32.GetDC.argtypes = [ctypes.c_void_p]
            user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            gdi32.GetPixel.restype = wintypes.DWORD
            gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            hdc = user32.GetDC(None)
            if not hdc:
                return None
            try:
                samples = []
                # 任务栏右半部分的空白带，纵向取中间
                y = (top + bottom) // 2
                span = max(1, (right - left) // 4)
                for i in range(6):
                    x = right - span // 2 - i * max(1, span // 8)
                    px = gdi32.GetPixel(hdc, int(x), int(y))
                    if px == 0xFFFFFFFF:
                        continue
                    samples.append(((px >> 0) & 0xFF, (px >> 8) & 0xFF, (px >> 16) & 0xFF))
                if not samples:
                    return None
                samples.sort(key=lambda c: c[0] + c[1] + c[2])
                return samples[len(samples) // 2]
            finally:
                user32.ReleaseDC(None, hdc)
        except Exception:
            return None

    def _paint(self) -> None:
        """渲染并提交画面。渲染结果会缓存，重复提交走 _resubmit。"""
        if not self._can_present():
            self._paint_pending = True
            return
        self._paint_pending = False
        scale = _dpi_scale(self.hwnd)
        x0, y0, band_h = self._taskbar_band()
        # 与任务栏同色：底板用采样色，几乎是实色，环直接浮在任务栏上
        base = self._taskbar_color()
        plate_color = base
        plate_alpha = 250 if base else None
        # 任务栏偏亮时用深色数字，偏暗时用环本身的亮色
        text_color = None
        if base is not None:
            luminance = 0.299 * base[0] + 0.587 * base[1] + 0.114 * base[2]
            text_color = (26, 28, 32) if luminance > 140 else None
        data, w, h = rr.render_panel(self.readings, scale=scale, height_px=band_h,
                                     plate_color=plate_color, plate_alpha=plate_alpha,
                                     text_color=text_color)
        if w <= 0 or h <= 0:
            return
        self._surface = (data, w, h, band_h)
        self._submit(data, w, h, x0, y0)
        if not self._shown:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self._shown = True
            self.raise_to_top()

    def _resubmit(self) -> bool:
        """用缓存的位图重新提交一次，不重新渲染（亚毫秒级）。

        菜单关闭 / 任务栏激活后的有界恢复复用缓存，避免重复采样底色和渲染。
        此方法本身不显示窗口，也不改变 Z 序。
        """
        if not self._can_present() or not self._surface:
            return False
        data, w, h, band_h = self._surface
        x0, y0, _ = self._taskbar_band()
        self._submit(data, w, h, x0, y0)
        return True

    def _submit(self, data, w: int, h: int, x0: int, y0: int) -> None:
        if not self._can_present():
            return
        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = w
        bmi.biHeight = -h          # 负数 = 自上而下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0      # BI_RGB
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0,
                                      ctypes.byref(bits), None, 0)
        if not hbmp or not bits:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            return
        old = gdi32.SelectObject(hdc_mem, hbmp)
        try:
            ctypes.memmove(bits, data, len(data))
            x, y = self._target_xy(w, h, x0, y0)
            pt_dst = POINT(x, y)
            size = SIZE(w, h)
            src = POINT(0, 0)
            if not user32.UpdateLayeredWindow(
                    self.hwnd, hdc_screen, ctypes.byref(pt_dst),
                    ctypes.byref(size), hdc_mem, ctypes.byref(src),
                    0, ctypes.byref(self._blend), ULW_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())
            resized = (self.width, self.height) != (w, h)
            self.width, self.height = w, h
            # Uploading pixels must NOT implicitly show or raise the owner.
            # TrackPopupMenu pumps our timer/refresh messages while it is open.
            if resized:
                self._apply_hit_region(w, h)
            self._last_submit = time.monotonic()
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)

    def _target_xy(self, w: int, h: int, default_x: int, default_y: int) -> tuple:
        """锁定/默认时贴任务栏；解锁且有保存位置时用保存位置。

        托盘入口启用 DPI-aware，保存位置、GetSystemMetrics 及任务栏矩形
        均沿用该上下文的屏幕坐标；用完整屏幕范围校验，不用工作区边界
        排除任务栏内的合法位置。这里只处理主屏幕范围。
        """
        if self.position is not None and not self.locked:
            px, py = self.position
            try:
                user32.GetSystemMetrics.restype = ctypes.c_int
                screen_w = user32.GetSystemMetrics(0)
                screen_h = user32.GetSystemMetrics(1)
            except Exception:
                screen_w, screen_h = 1920, 1080
            # 至少保留 40x40 在屏幕内，避免拖到看不见的地方
            if 0 <= px <= screen_w - 40 and 0 <= py <= screen_h - 40:
                return int(px), int(py)
        return int(default_x), int(default_y)

    def update(self, readings) -> None:
        self.readings = list(readings)
        self._paint_pending = True
        if self._can_present():
            self._paint()
            self._start_keepalive()

    def show(self) -> None:
        if self.hwnd and not self._destroyed:
            self._visible = True
            self._paint()
            self._start_keepalive()

    def hide(self) -> None:
        self._visible = False
        self._shown = False
        self._restore_remaining = 0
        self._stop_keepalive()
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_RESTORE)
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def refresh_position(self) -> None:
        if self._can_present() and self.width:
            x0, y0, _band = self._taskbar_band()
            x, y = self._target_xy(self.width, self.height, x0, y0)
            user32.SetWindowPos(self.hwnd, ctypes.c_void_p(HWND_TOPMOST), x, y, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)

    def raise_to_top(self) -> None:
        """重新抢回 topmost 的 Z 序。

        任务栏被点击/激活后会把自己的 topmost 顺序前移，把我们的浮窗压到
        下面。这里每次调用都很便宜（同一 topmost 组内重排），但真正触发
        条件的判断在 _keepalive_tick 里做。
        """
        if self._can_present():
            user32.SetWindowPos(self.hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _is_covered_by_taskbar(self) -> bool:
        """浮窗当前是否被任务栏压在下面。

        按 z 序从自己的前一个窗口往上走，若途中先遇到任务栏，说明被盖住了。
        判断不出来时返回 True（保守）：宁可多抬一次，也不要让环消失。
        """
        if not self.hwnd:
            return False
        try:
            user32.GetWindow.restype = ctypes.c_void_p
            user32.GetWindow.argtypes = [ctypes.c_void_p, wintypes.UINT]
            tray = user32.FindWindowW("Shell_TrayWnd", None)
            if not tray:
                return True
            hwnd = self.hwnd
            for _ in range(64):          # 有界遍历，绝不无限循环
                hwnd = user32.GetWindow(hwnd, GW_HWNDPREV)
                if not hwnd:
                    return False
                if int(hwnd) == int(tray):
                    return True
            return True
        except Exception:
            # 判断链本身出错时，按"被盖住"处理，保证可见性优先。
            return True

    def _on_foreground_change(self, hook, event, hwnd, id_object, id_child,
                              thread_id, tick) -> None:
        """前台窗口变了（含任务栏被点击）：补一次画面。

        事件驱动，空闲时零开销。但前台事件可能很频繁，所以做 250ms 节流，
        避免每次切换都重传整张位图导致 CPU 偏高。
        """
        if not self._can_present():
            return
        now = time.monotonic()
        if now - self._last_submit < 0.25:
            return
        # Defer until foreground activation has completed. Never re-enter
        # rendering from a WinEvent callback or while a native menu is open.
        user32.PostMessageW(self.hwnd, MSG_RESTORE, 0, 0)

    def _start_foreground_hook(self) -> None:
        if self._hook or not self.hwnd:
            return
        try:
            user32.SetWinEventHook.restype = ctypes.c_void_p
            user32.SetWinEventHook.argtypes = [
                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, WINEVENTPROC,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
            user32.UnhookWinEvent.argtypes = [ctypes.c_void_p]
            if not self._hook_proc:
                self._hook_proc = WINEVENTPROC(self._on_foreground_change)
            self._hook = user32.SetWinEventHook(
                EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                self._hook_proc, 0, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        except Exception:
            self._hook = None

    def _stop_foreground_hook(self) -> None:
        if self._hook:
            try:
                user32.UnhookWinEvent(self._hook)
            except Exception:
                pass
            self._hook = None

    def _schedule_restore(self) -> None:
        """A bounded recovery burst after activation/menu dismissal, not polling."""
        if not self._can_present():
            return
        if self._restore_remaining:
            return
        self._restore_remaining = 3
        if not user32.SetTimer(self.hwnd, TIMER_RESTORE, 80, None):
            self._restore_remaining = 0

    def _restore_tick(self) -> None:
        if not self._can_present():
            if self.hwnd:
                user32.KillTimer(self.hwnd, TIMER_RESTORE)
            self._restore_remaining = 0
            return
        if self._restore_remaining <= 0:
            user32.KillTimer(self.hwnd, TIMER_RESTORE)
            return
        self._restore_remaining -= 1
        if not self._restore_remaining:
            user32.KillTimer(self.hwnd, TIMER_RESTORE)
        self.raise_to_top()
        if self._paint_pending:
            self._paint()
        else:
            self._resubmit()

    def _keepalive_tick(self) -> None:
        """Low-frequency fallback, completely suspended during native menus."""
        if not self._can_present():
            return
        if self._is_covered_by_taskbar():
            self._schedule_restore()

    def _start_keepalive(self) -> None:
        """前台事件钩子 + 低频兜底定时器。"""
        if not self._can_present():
            return
        self._start_foreground_hook()
        # No uploads on an idle timer. Activation/menu events do bounded work.
        user32.SetTimer(self.hwnd, TIMER_KEEPALIVE, 1500, None)

    def _stop_keepalive(self) -> None:
        self._stop_foreground_hook()
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_KEEPALIVE)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        if not locked:
            self.position = None   # 解锁后先回到默认角落再允许拖动

    # ---- 菜单 ----
    def _show_menu(self) -> None:
        if not self._can_present():
            return
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        self._menu = menu
        self._menu_active = True
        try:
            # TrackPopupMenu runs a nested message loop: suspend maintenance and
            # guard ALL presentation paths, including refresh/paint callbacks.
            self._stop_keepalive()
            user32.KillTimer(self.hwnd, TIMER_RESTORE)
            self._restore_remaining = 0
            user32.AppendMenuW(menu, MF_STRING, 1, "立即刷新")
            user32.AppendMenuW(menu, MF_STRING, 4, "打开详细面板")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            try:
                current = int(self.interval_getter())
            except Exception:
                current = 60
            user32.AppendMenuW(menu, MF_STRING, 5,
                               "解锁位置（可拖动）" if self.locked else "锁定位置")
            intervals = {10: 30, 11: 60, 12: 120, 13: 300}
            for cmd_id, label in ((10, "30 秒"), (11, "1 分钟"),
                                  (12, "2 分钟"), (13, "5 分钟")):
                flags = MF_STRING | (MF_CHECKED if current == intervals[cmd_id] else 0)
                user32.AppendMenuW(menu, flags, cmd_id, "刷新间隔：" + label)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, 2, "托盘图标开关")
            user32.AppendMenuW(menu, MF_STRING, 3, "隐藏此浮窗")
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, 90, "退出电量托盘")
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            flags = TPM_RIGHTBUTTON | TPM_RETURNCMD
            if pt.y > user32.GetSystemMetrics(1) // 2:
                flags |= TPM_BOTTOMALIGN
            if pt.x > user32.GetSystemMetrics(0) // 2:
                flags |= TPM_RIGHTALIGN
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(menu, flags, pt.x, pt.y, 0, self.hwnd, None)
            # A nested callback may already have destroyed the owner.
            if self._destroyed or not self.hwnd:
                return
            # Execute hide/quit BEFORE any restoration. Other callbacks may
            # update readings; keep those deferred until the menu lifecycle ends.
            actions = {1: self.on_refresh, 2: self.on_toggle_tray,
                       3: self.on_hide, 4: self.on_open_panel,
                       5: self.on_toggle_lock, 90: self.on_quit}
            if cmd in actions:
                actions[cmd]()
            elif cmd in intervals:
                self.on_interval_change(intervals[cmd])
        finally:
            try:
                user32.DestroyMenu(menu)
            finally:
                self._menu = None
                self._menu_active = False
                if self.hwnd and not self._destroyed:
                    # Required for reliable subsequent notification-area menus.
                    user32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL
                if self._can_present():
                    self._start_keepalive()
                    self._schedule_restore()

    # ---- 命中区域：只有三个环的圆形区域接受点击 ----
    def _apply_hit_region(self, w: int, h: int) -> None:
        """用 SetWindowRgn 把可点击范围限制在三个环上。

        注意：CreateEllipticRgn / CombineRgn 属于 **gdi32**，不是 user32。
        从 user32 取它们会抛 AttributeError；如果再用 except: pass 兜住，
        表面看没报错，实际区域根本没设上，窗口就收不到任何点击。
        """
        if not self.hwnd:
            return
        try:
            gdi32.CreateEllipticRgn.restype = ctypes.c_void_p
            gdi32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
            gdi32.CombineRgn.restype = ctypes.c_int
            gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowRgn.restype = ctypes.c_int
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            wintypes.BOOL]
            gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
            geo = rr.panel_geometry(_dpi_scale(None), h)
            pad = max(2, int(round(geo["thickness"] * 0.55)))
            total = None
            for cx in geo["centers"]:
                r = int(round(geo["radius"] + pad))
                x, y = int(round(cx)), int(round(geo["cy"]))
                piece = gdi32.CreateEllipticRgn(x - r, y - r, x + r, y + r)
                if not piece:
                    continue
                if total is None:
                    total = piece
                else:
                    gdi32.CombineRgn(total, total, piece, RGN_OR)
                    gdi32.DeleteObject(piece)
            if not total:
                return
            if not user32.SetWindowRgn(self.hwnd, total, True):
                # 失败时系统不接管 region，需要自己释放；同时去掉区域限制，
                # 宁可整块可点，也不要变成点不到。
                gdi32.DeleteObject(total)
                user32.SetWindowRgn(self.hwnd, None, True)
            else:
                self._region_ok = True
        except Exception:
            # 任何异常都退回"无区域"：全窗可点，比点不到好
            try:
                user32.SetWindowRgn(self.hwnd, None, True)
            except Exception:
                pass

    # ---- 窗口过程 ----
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TIMER:
            if int(wparam) == TIMER_KEEPALIVE:
                self._keepalive_tick()
            elif int(wparam) == TIMER_RESTORE:
                self._restore_tick()
            return 0
        if msg == MSG_RESTORE:
            self._schedule_restore()
            return 0
        if msg == WM_PAINT:
            # Layered pixels are supplied by UpdateLayeredWindow, not a paint
            # DC. Validate even while hidden/menu-active to avoid a paint loop.
            user32.ValidateRect(hwnd, None)
            if self._paint_pending or not self._surface:
                self._paint()
            else:
                self._resubmit()
            return 0
        if msg == 0x0021:  # WM_MOUSEACTIVATE: accept clicks without activation
            return 3  # MA_NOACTIVATE
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_LBUTTONDBLCLK:
            self.on_open_panel()
            return 0
        if msg == WM_LBUTTONDOWN:
            if not self.locked:
                self._dragging = True
                self._drag_origin = self._cursor()
                self._window_origin = self._window_xy()
                user32.SetCapture(hwnd)
            return 0
        if msg == WM_MOUSEMOVE:
            if self._dragging:
                cx, cy = self._cursor()
                ox, oy = self._drag_origin
                wx, wy = self._window_origin
                self.position = [wx + (cx - ox), wy + (cy - oy)]
                self.refresh_position()
            return 0
        if msg == WM_LBUTTONUP:
            if self._dragging:
                self._dragging = False
                user32.ReleaseCapture()
                self.on_toggle_lock(keep=True)   # 记住新位置但保持解锁
            else:
                self.on_refresh()
            return 0
        if msg == WM_CAPTURECHANGED:
            self._dragging = False
            return 0
        if msg == WM_RBUTTONUP:
            self._show_menu()
            return 0
        if msg == WM_CLOSE:
            self.destroy()
            return 0
        if msg == WM_DESTROY:
            # Also cover external DestroyWindow, not only our destroy() path.
            if not self._destroyed:
                self._destroyed = True
                self._visible = False
                self._stop_keepalive()
                user32.KillTimer(hwnd, TIMER_RESTORE)
                if self._menu_active:
                    user32.EndMenu()
            self._shown = False
            self._restore_remaining = 0
            self._surface = None
            self.hwnd = None
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _cursor(self) -> tuple:
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def _window_xy(self) -> tuple:
        from ctypes import wintypes as w
        rect = RECT()
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
        user32.GetWindowRect(self.hwnd, ctypes.byref(rect))
        return rect.left, rect.top
