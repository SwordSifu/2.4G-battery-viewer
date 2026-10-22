# -*- coding: utf-8 -*-
"""tray_app.py —— 左下角三设备电量环（纯 ctypes，无第三方依赖）

* 仅保留左下角鼠标 / 键盘 / 手柄三个电量环，不注册右下角通知区图标。
* 手柄只有系统电池档位，不伪造精确百分比。
* 右键电量环打开菜单，可打开/前置面板或立即刷新。
* 低 CPU：SetTimer 驱动，空闲不轮询；读取在后台线程。
* 不写任何日志文件；只有 --debug 时才往控制台打印。

    pythonw tray_app.py          # 静默驻留
    python  tray_app.py --debug  # 前台运行并打印过程
    python  tray_app.py --status
    python  tray_app.py --quit
"""
from __future__ import annotations

import argparse
import ctypes
import json
import sys
import threading
import time
import traceback
from ctypes import wintypes
from typing import Optional

import personal_devices as pd
import win_icon
import tray_common as tc

BASE_DIR = tc.BASE_DIR
APP_NAME = tc.APP_NAME
MUTEX_NAME = tc.MUTEX_NAME
WND_CLASS = tc.WND_CLASS

USER32 = tc.USER32
KERNEL32 = tc.KERNEL32
SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)

WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
MSG_TRAY_CALLBACK = WM_APP + 1
MSG_REFRESH_DONE = WM_APP + 2

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04

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


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", ctypes.c_void_p),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", ctypes.c_void_p), ("message", wintypes.UINT),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
                ("pt_y", wintypes.LONG)]


USER32.RegisterClassExW.restype = wintypes.WORD
USER32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
USER32.CreateWindowExW.restype = ctypes.c_void_p
USER32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
USER32.DefWindowProcW.restype = ctypes.c_ssize_t
USER32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
USER32.DestroyWindow.restype = wintypes.BOOL
USER32.DestroyWindow.argtypes = [ctypes.c_void_p]
USER32.SetTimer.restype = ctypes.c_size_t
USER32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.UINT,
                            ctypes.c_void_p]
USER32.KillTimer.restype = wintypes.BOOL
USER32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
USER32.GetMessageW.restype = ctypes.c_int
USER32.GetMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p,
                               wintypes.UINT, wintypes.UINT]
USER32.PostQuitMessage.restype = None
USER32.PostQuitMessage.argtypes = [ctypes.c_int]
USER32.TranslateMessage.restype = wintypes.BOOL
USER32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
USER32.DispatchMessageW.restype = ctypes.c_ssize_t
USER32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

SHELL32.Shell_NotifyIconW.restype = wintypes.BOOL
SHELL32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]

KERNEL32.GetModuleHandleW.restype = ctypes.c_void_p
KERNEL32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# (uID, key, 是否带底板)
TRAY_ICONS = [
    (1, "mouse", False),
    (2, "keyboard", True),
    (3, "gamepad", True),
]


class TrayApp:
    def __init__(self, debug: bool = False):
        self.debug = debug
        self.cfg = tc.load_config()
        self.hwnd: Optional[int] = None
        self.wndproc_ref = None
        self.busy = False
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.readings = {key: pd.Reading(pd.DEVICE_TITLES[key], "",
                                         error="正在读取…", key=key)
                         for key in pd.DEVICE_KEYS}
        self.icons_added = False
        self.closing = False
        self.corner = None
        self.corner_locked = True
        self.taskbar_created_msg = USER32.RegisterWindowMessageW("TaskbarCreated")
        self.msg_quit = USER32.RegisterWindowMessageW(tc.MSG_QUIT)
        self.msg_reload = USER32.RegisterWindowMessageW(tc.MSG_RELOAD)
        self.log("启动 " + json.dumps(self.cfg, ensure_ascii=False))

    def log(self, message: str) -> None:
        if self.debug:
            print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    # ---- icons ----
    @staticmethod
    def _color(level: Optional[int]) -> tuple[int, int, int]:
        if level is None:
            return (150, 155, 160)
        if level >= 60:
            return (93, 220, 138)
        if level >= 30:
            return (245, 196, 81)
        return (255, 107, 107)

    def _tip(self, key: str) -> str:
        reading = self.readings[key]
        stamp = (time.strftime("%H:%M:%S", time.localtime(reading.updated_at))
                 if reading.updated_at else "未读取")
        if not reading.ok:
            return f"{pd.DEVICE_TITLES[key]}：{reading.error or '未知'}（{stamp}）"
        text = f"{pd.DEVICE_TITLES[key]}：{reading.display_value}"
        if reading.status:
            text += f"，{reading.status}"
        text += f"（{stamp} 更新）"
        if reading.level is None:
            text += "｜无精确百分比"
        return text

    def _nid(self, uid: int):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = uid
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = MSG_TRAY_CALLBACK
        return nid

    def _apply_icon(self, action: int, uid: int, key: str, plate: bool) -> bool:
        """Notification-area registration is disabled, including legacy callers."""
        return False

    def add_icons(self) -> None:
        """Compatibility no-op: the left corner rings are the only entry."""

    def update_icons(self) -> None:
        """Compatibility no-op; refresh must never register notification icons."""

    def remove_icons(self) -> None:
        if not self.icons_added:
            return
        for uid, _, _ in TRAY_ICONS:
            nid = self._nid(uid)
            nid.uFlags = 0
            SHELL32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.icons_added = False

    # ---- timer / refresh ----
    def apply_timer(self) -> None:
        if not self.hwnd:
            return
        USER32.KillTimer(self.hwnd, 1)
        USER32.SetTimer(self.hwnd, 1, int(self.cfg["interval"]) * 1000, None)

    def apply_config(self) -> None:
        """Re-read interval only. Autostart is never 'repaired' here."""
        fresh = tc.load_config()
        if fresh["interval"] != self.cfg["interval"]:
            self.cfg = fresh
            self.apply_timer()
            self.log(f"刷新间隔 -> {fresh['interval']} 秒")

    def refresh_now(self) -> None:
        if self.busy or not self.hwnd or self.closing:
            return
        self.busy = True
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def _work(self) -> None:
        try:
            rows = pd.read_all(stop_event=self.stop_event)
            for reading in rows:
                self.readings[reading.key] = reading
        except Exception:
            self.log("读取异常:\n" + traceback.format_exc())
        finally:
            hwnd = self.hwnd
            if hwnd and not self.closing:
                USER32.PostMessageW(hwnd, MSG_REFRESH_DONE, 0, 0)

    def on_refresh_done(self) -> None:
        self.busy = False
        self.log("刷新完成: " + ", ".join(
            f"{k}={self.readings[k].display_value}" for k in pd.DEVICE_KEYS))
        self.update_corner()

    # ---- 左下角环状浮窗 ----
    def create_corner(self) -> bool:
        """浮窗跑在本进程内；创建失败就退出，不回退到通知区图标。"""
        corner = None
        try:
            import corner_widget
            corner = corner_widget.CornerWidget(
                on_refresh=self.refresh_now,
                on_open_panel=self.open_ui,
                on_toggle_tray=self.toggle_icons,
                on_hide=self.hide_corner,
                on_quit=self.quit,
                on_toggle_lock=self.toggle_corner_lock,
                interval_getter=lambda: self.cfg["interval"],
                on_interval_change=self.set_interval_from_menu,
                locked=self.corner_locked,
                position=self.cfg.get("corner_pos"),
            )
            if not corner.create():
                raise RuntimeError("无法创建左下角电量环")
            self.corner = corner
            self.update_corner()
            self.log(f"左下角电量环已创建 size={corner.width}x{corner.height} "
                     f"scale={getattr(corner, 'last_scale', '?')}")
            return True
        except Exception:
            self.corner = None
            if corner is not None:
                try:
                    corner.destroy()
                except Exception:
                    self.log("清理创建失败的电量环出错:\n" + traceback.format_exc())
            self.log("电量环创建失败，不启用通知区图标:\n" + traceback.format_exc())
            return False

    def update_corner(self) -> None:
        if not self.corner:
            return
        rows = [self.readings[key] for key in pd.DEVICE_KEYS]
        self.corner.update(rows)

    def hide_corner(self) -> None:
        if self.corner:
            self.corner.hide()
        self.cfg["corner"] = False
        tc.save_config(self.cfg)
        self.log("浮窗已隐藏")

    def toggle_corner_lock(self, keep: bool = False) -> None:
        if not self.corner:
            return
        if keep:
            # 拖动结束：记住位置，但仍然保持解锁，方便继续微调
            self.cfg["corner_pos"] = list(self.corner.position or [])
            tc.save_config(self.cfg)
            return
        self.corner_locked = not self.corner_locked
        self.corner.set_locked(self.corner_locked)
        if self.corner_locked and self.corner.position:
            self.cfg["corner_pos"] = list(self.corner.position)
            tc.save_config(self.cfg)
        self.log("浮窗" + ("锁定" if self.corner_locked else "解锁"))

    def set_interval_from_menu(self, seconds: int) -> None:
        self.cfg["interval"] = int(seconds)
        if tc.save_config(self.cfg):
            self.apply_timer()
        self.log(f"刷新间隔 -> {seconds} 秒")

    def toggle_icons(self) -> None:
        """Legacy menu callback retained as a no-op; notification icons stay off."""

    # ---- UI ----
    def open_ui(self) -> None:
        if tc.start_ui():
            self.log("面板已打开/前置")
        else:
            self.log("打开面板失败")

    def quit(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.log("退出")
        self.stop_event.set()
        try:
            if self.corner:
                # 记住最后位置，下次启动回到原处
                if self.corner.position:
                    self.cfg["corner_pos"] = list(self.corner.position)
                    tc.save_config(self.cfg)
                self.corner.destroy()
                self.corner = None
        except Exception:
            self.log("销毁浮窗出错（忽略）:\n" + traceback.format_exc())
        self.remove_icons()
        try:
            win_icon.destroy_all()
        except Exception:
            self.log("清理图标出错（忽略）:\n" + traceback.format_exc())
        if self.hwnd:
            USER32.KillTimer(self.hwnd, 1)
            USER32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3.0)

    # ---- window proc ----
    def wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == MSG_TRAY_CALLBACK:
            event = int(lparam) & 0xFFFF
            if event == WM_RBUTTONUP:
                self.open_ui()
            elif event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.refresh_now()
            return 0
        if msg == MSG_REFRESH_DONE:
            self.on_refresh_done()
            return 0
        if msg == WM_TIMER:
            self.apply_config()
            self.refresh_now()
            return 0
        if msg in (self.msg_quit, WM_CLOSE):
            self.quit()
            return 0
        if msg == self.msg_reload:
            self.apply_config()
            return 0
        if msg == self.taskbar_created_msg:
            # Explorer rebuilding must never create notification-area icons.
            self.icons_added = False
            return 0
        if msg == WM_DESTROY:
            # Without this, a destroyed window leaves a live process holding
            # the single-instance mutex, and the tray can never start again.
            USER32.PostQuitMessage(0)
            return 0
        return USER32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self) -> int:
        hinst = KERNEL32.GetModuleHandleW(None)
        self.wndproc_ref = WNDPROC(self.wnd_proc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self.wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = WND_CLASS
        if not USER32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):
                self.log(f"RegisterClassExW 失败 err={err}")
                return 1
        hwnd = USER32.CreateWindowExW(0, WND_CLASS, APP_NAME, 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            self.log(f"CreateWindowExW 失败 err={ctypes.get_last_error()}")
            return 1
        self.hwnd = hwnd
        self.apply_config()
        if self.cfg.get("corner", True) and not self.create_corner():
            self.quit()
            return 1
        self.apply_timer()
        self.refresh_now()
        message = MSG()
        while USER32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            USER32.TranslateMessage(ctypes.byref(message))
            USER32.DispatchMessageW(ctypes.byref(message))
        self.log("消息循环结束")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="左下角三设备电量环")
    parser.add_argument("--debug", action="store_true", help="前台运行并打印过程")
    parser.add_argument("--status", action="store_true", help="打印当前状态")
    parser.add_argument("--quit", action="store_true", help="让运行中的电量环后台退出")
    args = parser.parse_args()

    if args.status:
        print("配置：", json.dumps(tc.load_config(), ensure_ascii=False))
        print("开机自启：", tc.is_autostart_enabled())
        print("左下角电量环后台运行中：", tc.is_tray_running())
        return 0
    if args.quit:
        ok = tc.post_to_tray(tc.MSG_QUIT)
        print("已通知电量环后台退出" if ok else "电量环后台未运行")
        return 0 if ok else 1

    handle, first = tc.acquire_instance(MUTEX_NAME)
    if not first:
        tc.close_instance(handle)
        if tc.is_tray_running():
            tc.post_to_tray(tc.MSG_RELOAD)
            print("已经在运行了（右键左下角电量环可打开面板）")
            return 0
        # The mutex is orphaned (previous process died). Never kill unknown
        # processes; report honestly and let the user retry.
        print("检测到电量环后台单实例锁被占用，但没有后台窗口。请稍后重试。")
        return 1

    try:
        USER32.SetProcessDPIAware()
    except Exception:
        pass

    app = TrayApp(debug=args.debug)
    try:
        return app.run()
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        try:
            app.quit()
        finally:
            tc.close_instance(handle)


if __name__ == "__main__":
    sys.exit(main())
