"""Opt-in REAL Win32 geometry/lifecycle test with SYNTHETIC fullscreen windows.

No game injection, HID queries, personal config reads/writes or startup changes.
Does not prove behavior of an actual exclusive-D3D/anti-cheat game.
python tests/windows_fullscreen_smoke.py --run
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corner_widget as cw
import fullscreen_guard as fg

u = cw.user32
u.PeekMessageW.argtypes = [C.POINTER(cw.MSG), C.c_void_p, W.UINT, W.UINT, W.UINT]
u.TranslateMessage.argtypes = [C.POINTER(cw.MSG)]
u.DispatchMessageW.argtypes = [C.POINTER(cw.MSG)]
u.DispatchMessageW.restype = C.c_ssize_t
u.MsgWaitForMultipleObjects.argtypes = [W.DWORD, C.c_void_p, W.BOOL, W.DWORD, W.DWORD]
u.IsWindowVisible.argtypes = [C.c_void_p]
u.IsWindow.argtypes = [C.c_void_p]
u.FindWindowW.argtypes = [W.LPCWSTR, W.LPCWSTR]
u.FindWindowW.restype = C.c_void_p
u.GetWindowThreadProcessId.argtypes = [C.c_void_p, C.POINTER(W.DWORD)]
u.GetForegroundWindow.restype = C.c_void_p
u.WindowFromPoint.argtypes = [cw.POINT]
u.WindowFromPoint.restype = C.c_void_p
u.mouse_event.argtypes = [W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
u.GetWindowRect.argtypes = [C.c_void_p, C.POINTER(cw.RECT)]
set_style = u.SetWindowLongPtrW if C.sizeof(C.c_void_p) == 8 else u.SetWindowLongW
get_style = u.GetWindowLongPtrW if C.sizeof(C.c_void_p) == 8 else u.GetWindowLongW
set_style.argtypes, set_style.restype = [C.c_void_p, C.c_int, C.c_ssize_t], C.c_ssize_t
get_style.argtypes, get_style.restype = [C.c_void_p, C.c_int], C.c_ssize_t


def pump(seconds):
    deadline = time.monotonic()+seconds
    message = cw.MSG()
    while time.monotonic() < deadline:
        while u.PeekMessageW(C.byref(message), None, 0, 0, 1):
            u.TranslateMessage(C.byref(message))
            u.DispatchMessageW(C.byref(message))
        left = max(0, int((deadline-time.monotonic())*1000))
        u.MsgWaitForMultipleObjects(0, None, False, min(left, 100), 0x04FF)


class ProbeWidget(cw.CornerWidget):
    WND_CLASS = "PersonalBattery.FullscreenSmoke.Overlay.v1"

    def __init__(self):
        super().__init__(lambda: None, lambda: None, lambda: None, self.hide,
                         self.destroy, lambda *a, **k: None, lambda: 60, lambda v: None)
        self.creator = threading.get_ident()
        self.error = None
        self.uploads = 0

    def _submit(self, *args):
        super()._submit(*args)
        self.uploads += 1

    def _wnd_proc(self, hwnd, msg, wp, lp):
        try:
            assert threading.get_ident() == self.creator
            return super()._wnd_proc(hwnd, msg, wp, lp)
        except BaseException as error:
            self.error = error
            return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--observe-pid", type=int, help="also observe (never mutate) an already-running ring process")
    args = parser.parse_args()
    if not args.run:
        parser.error("real desktop interaction requires --run")
    u.SetProcessDPIAware()
    observed = None
    if args.observe_pid:
        observed = u.FindWindowW(cw.CornerWidget.WND_CLASS, None)
        assert observed, "no running ring window to observe"
        pid = W.DWORD()
        u.GetWindowThreadProcessId(observed, C.byref(pid))
        assert pid.value == args.observe_pid, "refusing to observe a different process"
    original_foreground = u.GetForegroundWindow()
    original_cursor = cw.POINT()
    u.GetCursorPos(C.byref(original_cursor))
    widget = ProbeWidget()
    windows = []
    proc_errors = []
    creator = threading.get_ident()
    def proc(hwnd, message, wp, lp):
        if threading.get_ident() != creator:
            proc_errors.append("native callback used a different thread")
        return u.DefWindowProcW(hwnd, message, wp, lp)
    callback = cw.WNDPROC(proc)
    wc = cw.WNDCLASSEXW()
    wc.cbSize = C.sizeof(wc)
    wc.lpfnWndProc, wc.hInstance = callback, cw.kernel32.GetModuleHandleW(None)
    wc.lpszClassName = "PersonalBattery.FullscreenSmoke.Foreground.v1"
    wc.hbrBackground = 6
    assert u.RegisterClassExW(C.byref(wc))
    rows = [SimpleNamespace(level=90, coarse="", error="")] * 3
    try:
        assert widget.create()
        api = fg._user32()
        monitor = api.MonitorFromWindow(widget.hwnd, 2)
        info = fg.MONITORINFO()
        info.cbSize = C.sizeof(info)
        assert api.GetMonitorInfoW(monitor, C.byref(info))
        m, work = info.rcMonitor, info.rcWork
        normal_box = (work.left+100, work.top+100, min(700, work.right-work.left-100),
                      min(400, work.bottom-work.top-100))
        for title in ("Synthetic fullscreen test — not a game", "Synthetic normal Alt-Tab target"):
            hwnd = u.CreateWindowExW(0, wc.lpszClassName, title, 0x00CF0000,
                *normal_box, None, None, wc.hInstance, None)
            assert hwnd
            windows.append(hwnd)
        target, other = windows
        normal_style = get_style(target, -16)
        def activate(hwnd):
            u.ShowWindow(hwnd, 5)
            u.SetWindowPos(hwnd, C.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x40)
            u.SetForegroundWindow(hwnd)
            pump(.15)
            if u.GetForegroundWindow() != hwnd:
                # A background-launched test may not own the foreground grant.
                # Click only our verified blank test window, never another app.
                rect = cw.RECT()
                assert u.GetWindowRect(hwnd, C.byref(rect))
                point = cw.POINT((rect.left+rect.right)//2, (rect.top+rect.bottom)//2)
                assert u.WindowFromPoint(point) == hwnd, "test window is not safely clickable"
                u.SetCursorPos(point.x, point.y)
                u.mouse_event(0x2, 0, 0, 0, 0)
                u.mouse_event(0x4, 0, 0, 0, 0)
                pump(.2)
            assert u.GetForegroundWindow() == hwnd, "foreground activation unavailable; not a valid test"
        def fullscreen():
            u.ShowWindow(target, 9)
            set_style(target, -16, 0x90000000)  # visible WS_POPUP
            u.SetWindowPos(target, None, m.left, m.top, m.right-m.left, m.bottom-m.top, 0x20 | 0x40)
            activate(target)
        def normal():
            u.ShowWindow(target, 9)
            set_style(target, -16, normal_style)
            u.SetWindowPos(target, None, *normal_box, 0x20 | 0x40)
            activate(target)
        def assert_state(visible):
            if widget.error:
                raise widget.error
            assert bool(u.IsWindowVisible(widget.hwnd)) is visible, ("actual visibility", visible)
            assert widget._fullscreen_suppressed is (not visible)
            assert widget._visible, "temporary suppression changed user intent"
            if observed:
                # Cross-process WM_TIMER delivery is asynchronous/low-priority.
                # Wait boundedly for that application's creator thread too.
                deadline = time.monotonic()+1.5
                while bool(u.IsWindowVisible(observed)) is not visible and time.monotonic() < deadline:
                    pump(.05)
                assert bool(u.IsWindowVisible(observed)) is visible, (
                    "live application visibility", visible,
                    "external geometry probe", fg.foreground_fullscreen(observed),
                    "foreground", u.GetForegroundWindow())
        activate(target)
        widget.update(rows)
        pump(.7)
        assert fg.foreground_fullscreen(widget.hwnd) is False
        assert_state(True)
        u.ShowWindow(target, 3)  # ordinary maximized client excludes title/taskbar
        activate(target)
        pump(.7)
        assert fg.foreground_fullscreen(widget.hwnd) is False
        assert_state(True)
        print("PASS normal/maximized windows remain visible", flush=True)
        fullscreen()
        assert fg.foreground_fullscreen(widget.hwnd) is True
        pump(.8)  # same-process foreground events are skipped; fallback must catch it
        assert_state(False)
        before = widget.uploads
        widget.update([SimpleNamespace(level=45, coarse="", error="")] * 3)
        widget.raise_to_top()
        widget._restore_tick()
        pump(.7)
        assert widget.uploads == before, "fullscreen refresh/keepalive uploaded pixels"
        print("PASS synthetic fullscreen hides; refresh/keepalive do not resurrect", flush=True)
        normal()  # same HWND, different geometry, no Alt-Tab
        pump(1.0)
        assert_state(True)
        assert not widget._paint_pending and widget.readings[0].level == 45
        fullscreen()
        pump(.8)
        assert_state(False)
        activate(other)  # fullscreen game remains open but is no longer foreground
        pump(1.0)
        assert_state(True)
        activate(target)
        pump(.8)
        assert_state(False)
        print("PASS same-window fullscreen exit and Alt-Tab restore", flush=True)
        widget.hide()  # explicit user intent must remain hidden after game exit
        normal()
        widget.update(rows)
        pump(1.0)
        assert not u.IsWindowVisible(widget.hwnd) and not widget._visible
        widget.show()
        pump(.4)
        assert_state(True)
        fullscreen()
        pump(.8)
        assert_state(False)
        old_hwnd = widget.hwnd
        widget.destroy()
        normal()
        widget.update(rows)
        widget._keepalive_tick()
        pump(.7)
        assert widget.hwnd is None and not u.IsWindow(old_hwnd)
        assert not proc_errors and widget.error is None
        if observed:
            deadline = time.monotonic()+1.5
            while not u.IsWindowVisible(observed) and time.monotonic() < deadline:
                pump(.05)
            assert u.IsWindow(observed) and u.IsWindowVisible(observed), (
                "live final restore", bool(u.IsWindow(observed)), fg.foreground_fullscreen(observed))
            print(f"PASS observed live application PID {args.observe_pid} hides/restores too", flush=True)
        print("PASS manual hide and destroyed window stay hidden; no config/hardware IO", flush=True)
    finally:
        widget.destroy()
        for hwnd in windows:
            u.DestroyWindow(hwnd)
        u.SetCursorPos(original_cursor.x, original_cursor.y)
        if original_foreground:
            u.SetForegroundWindow(original_foreground)


if __name__ == "__main__":
    main()
