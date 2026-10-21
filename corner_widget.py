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
user32.TrackPopupMenu.restype = wintypes.BOOL
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
GWL_EXSTYLE = -20
RGN_OR = 2


def work_area() -> tuple:
    rect = RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, 1920, 1080


def taskbar_rect():
    """任务栏条 (left, top, right, bottom)。

    注意：本进程是 DPI-unaware 时，这里拿到的是虚拟化后的逻辑坐标。
    调用方必须按 _dpi_scale() 换算到物理像素，否则高度会缩水一半。
    取不到返回 None。
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
    """返回物理像素下的 (x, y, height)，浮窗应贴在任务栏的哪一条。

    窗口是 DPI-unaware 的，UpdateLayeredWindow 的位置和尺寸也按虚拟化
    坐标解释，因此这里把任务栏的真实高度换算成逻辑单位后使用。
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

    # ---- 创建 / 销毁 ----
    def create(self, hinst=None) -> bool:
        hinst = hinst or ctypes.WinDLL("kernel32").GetModuleHandleW(None)
        self.wndproc_ref = WNDPROC(self._wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = self.WND_CLASS
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))  # IDC_ARROW
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() not in (0, 1410):
                return False
        ex = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        # 浮窗贴在任务栏上，必须主动处理点击穿透：只有落在环上的点击
        # 才由我们响应，其余交给下层任务栏（开始按钮/搜索）。
        ex |= WS_EX_NOACTIVATE
        hwnd = user32.CreateWindowExW(ex, self.WND_CLASS, "personal-battery-corner",
                                      WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)
        if not hwnd:
            return False
        self.hwnd = hwnd
        self.width = self.height = 0
        # 默认整窗穿透，光标移动时再按位置动态切换
        self._set_click_through(True)
        return True

    def _set_click_through(self, through: bool) -> None:
        """切换 WS_EX_TRANSPARENT；通过 GetWindowLong/SetWindowLong 原地改样式。"""
        if not self.hwnd:
            return
        user32.GetWindowLongW.restype = wintypes.LONG
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.restype = wintypes.LONG
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.LONG]
        ex = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        new = (ex | WS_EX_TRANSPARENT) if through else (ex & ~WS_EX_TRANSPARENT)
        if new != ex:
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, new)
        self._click_through = through

    def destroy(self) -> None:
        if self._menu:
            user32.DestroyMenu(self._menu)
            self._menu = None
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None

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

    def _paint(self) -> None:
        if not self.hwnd:
            return
        scale = _dpi_scale(self.hwnd)
        x0, y0, band_h = self._taskbar_band()
        data, w, h = rr.render_panel(self.readings, scale=scale, height_px=band_h)
        if w <= 0 or h <= 0:
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
            user32.UpdateLayeredWindow(self.hwnd, hdc_screen, ctypes.byref(pt_dst),
                                       ctypes.byref(size), hdc_mem, ctypes.byref(src),
                                       0, ctypes.byref(self._blend), ULW_ALPHA)
            self.width, self.height = w, h
            # 先裁好可点击区域，再摆位置显示
            self._apply_hit_region(w, h)
            user32.SetWindowPos(self.hwnd, ctypes.c_void_p(HWND_TOPMOST), x, y, w, h,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
        finally:
            gdi32.SelectObject(hdc_mem, old)
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)

    def _target_xy(self, w: int, h: int, default_x: int, default_y: int) -> tuple:
        """锁定/默认时贴任务栏；解锁且有保存位置时用保存位置（必须可点）。"""
        if self.position is not None and not self.locked:
            px, py = self.position
            left, top, right, bottom = work_area()
            tb = taskbar_rect()
            # 允许松开到整个屏幕，但必须保证还有可点的区域
            if 0 <= px <= right - 40 and 0 <= py <= bottom - 40:
                return int(px), int(py)
            if tb is not None:
                # 任务栏条允许
                l, t, r, b = tb
                if l <= px <= r - 40 and t <= py <= b - 20:
                    return int(px), int(py)
        return int(default_x), int(default_y)

    def update(self, readings) -> None:
        self.readings = list(readings)
        self._paint()

    def show(self) -> None:
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
            self._paint()

    def hide(self) -> None:
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def refresh_position(self) -> None:
        if self.hwnd and self.width:
            x, y = self._target_xy(self.width, self.height)
            user32.SetWindowPos(self.hwnd, ctypes.c_void_p(HWND_TOPMOST), x, y, 0, 0,
                                SWP_NOSIZE | SWP_NOACTIVATE)

    def set_locked(self, locked: bool) -> None:
        self.locked = locked
        if not locked:
            self.position = None   # 解锁后先回到默认角落再允许拖动

    # ---- 菜单 ----
    def _show_menu(self) -> None:
        ids = {"refresh": 1, "tray": 2, "hide": 3, "panel": 4, "lock": 5,
               "int30": 10, "int60": 11, "int120": 12, "int300": 13, "quit": 90}
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        self._menu = menu
        user32.AppendMenuW(menu, MF_STRING, ids["refresh"], "立即刷新")
        user32.AppendMenuW(menu, MF_STRING, ids["panel"], "打开详细面板")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        try:
            current = int(self.interval_getter())
        except Exception:
            current = 60
        user32.AppendMenuW(menu, MF_STRING, ids["lock"],
                           "解锁位置（可拖动）" if self.locked else "锁定位置")
        for key, secs, label in (("int30", 30, "30 秒"), ("int60", 60, "1 分钟"),
                                 ("int120", 120, "2 分钟"), ("int300", 300, "5 分钟")):
            flags = MF_STRING | (MF_CHECKED if current == secs else 0)
            user32.AppendMenuW(menu, flags, ids[key], "刷新间隔：" + label)
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ids["tray"],
                           "显示托盘图标" if False else "托盘图标开关")
        user32.AppendMenuW(menu, MF_STRING, ids["hide"], "隐藏此浮窗")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ids["quit"], "退出电量托盘")
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 必须先成为前台窗口，否则点菜单外部时菜单不会消失
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.DestroyMenu(menu)
        self._menu = None
        if cmd == ids["refresh"]:
            self.on_refresh()
        elif cmd == ids["panel"]:
            self.on_open_panel()
        elif cmd == ids["lock"]:
            self.on_toggle_lock()
        elif cmd == ids["tray"]:
            self.on_toggle_tray()
        elif cmd == ids["hide"]:
            self.on_hide()
        elif cmd == ids["quit"]:
            self.on_quit()
        elif cmd in (ids["int30"], ids["int60"], ids["int120"], ids["int300"]):
            secs = {ids["int30"]: 30, ids["int60"]: 60,
                    ids["int120"]: 120, ids["int300"]: 300}[cmd]
            self.on_interval_change(secs)

    # ---- 命中区域：只有三个环的圆形区域接受点击 ----
    def _apply_hit_region(self, w: int, h: int) -> None:
        """用 SetWindowRgn 把可点击范围限制在三个环上。

        区域之外的像素对系统来说根本不存在，点击会自动落到任务栏上，
        因此不会挡住开始按钮/搜索。这比动态切换 WS_EX_TRANSPARENT 可靠：
        透明窗口收不到鼠标消息，切过去就切不回来了。
        """
        try:
            user32.CreateEllipticRgn.restype = ctypes.c_void_p
            user32.CreateEllipticRgn.argtypes = [ctypes.c_int] * 4
            user32.CombineRgn.restype = ctypes.c_int
            user32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowRgn.restype = ctypes.c_int
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL]
            user32.DeleteObject.argtypes = [ctypes.c_void_p]
            geo = rr.panel_geometry(_dpi_scale(None), h)
            pad = max(2, int(round(geo["thickness"] * 0.55)))
            total = None
            for cx in geo["centers"]:
                r = int(round(geo["radius"] + pad))
                x, y = int(round(cx)), int(round(geo["cy"]))
                piece = user32.CreateEllipticRgn(x - r, y - r, x + r, y + r)
                if not piece:
                    continue
                if total is None:
                    total = piece
                else:
                    user32.CombineRgn(total, total, piece, RGN_OR)
                    user32.DeleteObject(piece)
            if total:
                # SetWindowRgn 成功后由系统接管该 region 的生命周期
                user32.SetWindowRgn(self.hwnd, total, True)
        except Exception:
            pass

    # ---- 窗口过程 ----
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._paint()
            return 0
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
        if msg == WM_DESTROY:
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
