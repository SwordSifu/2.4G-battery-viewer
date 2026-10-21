"""Opt-in real-desktop smoke test (Windows only, standard-library runtime).

Run explicitly: python tests/windows_corner_smoke.py --run --idle-seconds 75
Creates ONE widget, pumps on its creator thread, opens native menus, injects
queued menu keys, and destroys only its own windows. No HID/config/startup IO.
Optional --screenshot PATH uses Pillow installed in the test environment only.
Do not run while another battery overlay is covering the same location.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corner_widget as cw

u = cw.user32
u.PeekMessageW.argtypes = [C.POINTER(cw.MSG), C.c_void_p, W.UINT, W.UINT, W.UINT]
u.DispatchMessageW.argtypes = [C.POINTER(cw.MSG)]
u.DispatchMessageW.restype = C.c_ssize_t
u.TranslateMessage.argtypes = [C.POINTER(cw.MSG)]
u.MsgWaitForMultipleObjects.argtypes = [W.DWORD, C.c_void_p, W.BOOL, W.DWORD, W.DWORD]
u.IsWindowVisible.argtypes = [C.c_void_p]
u.IsWindow.argtypes = [C.c_void_p]
u.WindowFromPoint.argtypes = [cw.POINT]  # POINT by VALUE, never by reference
u.WindowFromPoint.restype = C.c_void_p
u.GetWindowRect.argtypes = [C.c_void_p, C.POINTER(cw.RECT)]
u.GetMenuItemRect.argtypes = [C.c_void_p, C.c_void_p, W.UINT, C.POINTER(cw.RECT)]
u.GetMenuItemCount.argtypes = [C.c_void_p]
u.GetClassNameW.argtypes = [C.c_void_p, W.LPWSTR, C.c_int]
u.GetGuiResources.argtypes = [C.c_void_p, W.DWORD]
k = cw.kernel32
k.GetCurrentProcess.restype = C.c_void_p
process = k.GetCurrentProcess()
TIMER_TEST = 901


def pump(seconds):
    deadline = time.monotonic() + seconds
    msg = cw.MSG()
    while time.monotonic() < deadline:
        while u.PeekMessageW(C.byref(msg), None, 0, 0, 1):
            u.TranslateMessage(C.byref(msg))
            u.DispatchMessageW(C.byref(msg))
        left = max(0, int((deadline - time.monotonic()) * 1000))
        u.MsgWaitForMultipleObjects(0, None, False, min(left, 200), 0x04FF)


def resources():
    class COUNTERS(C.Structure):
        _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD)] + [
            (name, C.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
            "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]
    psapi = C.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [C.c_void_p, C.POINTER(COUNTERS), W.DWORD]
    counters = COUNTERS()
    counters.cb = C.sizeof(counters)
    assert psapi.GetProcessMemoryInfo(process, C.byref(counters), counters.cb)
    return dict(gdi=u.GetGuiResources(process, 0), user=u.GetGuiResources(process, 1),
                working_set_mib=round(counters.WorkingSetSize / 1048576, 2),
                private_mib=round(counters.PrivateUsage / 1048576, 2))


class SmokeWidget(cw.CornerWidget):
    def __init__(self, screenshot):
        super().__init__(lambda: None, lambda: None, lambda: None,
                         self.hide, self.destroy, lambda: None, lambda: 60, lambda v: None)
        self.screenshot = screenshot
        self.failure = None
        self.mode = "cancel"
        self.menu_started = 0
        self.menu_checks = 0
        self.presentations = 0
        self.last_item = None
        self._initial_submit = 0

    def _submit(self, *args):
        if self._menu_active:
            raise AssertionError("pixel upload during native menu")
        self.presentations += 1
        return super()._submit(*args)

    def raise_to_top(self):
        if self._menu_active:
            raise AssertionError("owner raised during native menu")
        return super().raise_to_top()

    def _wnd_proc(self, hwnd, msg, wp, lp):
        # ctypes callbacks swallow Python exceptions: capture and re-raise them
        # outside the native loop so failures cannot masquerade as success.
        try:
            if msg in (cw.WM_CLOSE, cw.WM_DESTROY):
                print(json.dumps(dict(stage="native_lifetime", message=msg, mode=self.mode,
                                      visible=self._visible, menu=self._menu_active)), flush=True)
            if msg == cw.WM_TIMER and wp == TIMER_TEST:
                self.menu_probe()
                return 0
            return super()._wnd_proc(hwnd, msg, wp, lp)
        except BaseException as error:
            self.failure = error
            u.EndMenu()
            return 0

    def menu_probe(self):
        if not self._menu_active:
            return
        elapsed = time.monotonic() - self.menu_started
        if elapsed > 8:
            raise AssertionError("native menu failed to dismiss")
        if self.keys_sent:
            return
        # Native timers are running INSIDE TrackPopupMenu on this same thread.
        self.update([SimpleNamespace(level=45, coarse="", error=""),
                     SimpleNamespace(level=80, coarse="", error=""),
                     SimpleNamespace(level=None, coarse="中档", error="")])
        self._keepalive_tick()
        self._restore_tick()
        self._on_foreground_change(None, 3, 0, 0, 0, 0, 0)
        self._wnd_proc(self.hwnd, cw.MSG_RESTORE, 0, 0)
        self._wnd_proc(self.hwnd, cw.WM_PAINT, 0, 0)
        assert self._last_submit == self._initial_submit
        self.menu_checks += 1
        if elapsed < (1.8 if self.mode == "cancel" else 0.4):
            return
        count = u.GetMenuItemCount(self._menu)
        rect = cw.RECT()
        assert count > 0 and u.GetMenuItemRect(self.hwnd, self._menu, count - 1, C.byref(rect))
        self.last_item = [rect.left, rect.top, rect.right, rect.bottom]
        # Sample across the whole last command, including the ring-overlap side.
        for fx in (0.05, 0.25, 0.5, 0.75, 0.95):
            pt = cw.POINT(int(rect.left + (rect.right - rect.left) * fx),
                          (rect.top + rect.bottom) // 2)
            hit = u.WindowFromPoint(pt)
            name = C.create_unicode_buffer(128)
            u.GetClassNameW(hit, name, len(name))
            assert name.value == "#32768", ("last item obscured", fx, name.value, self.last_item)
        if self.mode == "cancel" and self.screenshot:
            # Test-only optional dependency, never imported by the application.
            from PIL import ImageGrab
            wr = cw.RECT()
            u.GetWindowRect(self.hwnd, C.byref(wr))
            first = cw.RECT()
            u.GetMenuItemRect(self.hwnd, self._menu, 0, C.byref(first))
            ImageGrab.grab(bbox=(max(0, min(rect.left, wr.left) - 12),
                                max(0, first.top - 12),
                                max(rect.right, wr.right) + 12,
                                max(rect.bottom, wr.bottom) + 4)).save(self.screenshot)
        self.keys_sent = True
        if self.mode == "cancel":
            u.PostMessageW(self.hwnd, 0x0100, 0x1B, 0)  # WM_KEYDOWN / Escape
        elif self.mode == "close":
            u.PostMessageW(self.hwnd, cw.WM_CLOSE, 0, 0)
        elif self.mode == "external":
            u.DestroyWindow(self.hwnd)
        else:
            # Use an actual mouse click, not synthetic queued navigation keys:
            # menu-manager key coalescing can select a different command.
            target = cw.RECT()
            if self.mode == "outside":
                assert u.GetWindowRect(self.hwnd, C.byref(target))
                x = target.left + self.height // 2
                y = target.bottom - self.height // 4
                assert y > rect.bottom and u.WindowFromPoint(cw.POINT(x, y)) == self.hwnd
            else:
                index = count - 3 if self.mode == "hide" else count - 1
                assert u.GetMenuItemRect(self.hwnd, self._menu, index, C.byref(target))
                x, y = (target.left + target.right) // 2, (target.top + target.bottom) // 2
            u.SetCursorPos(x, y)
            u.mouse_event.argtypes = [W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.c_size_t]
            u.mouse_event(0x0002, 0, 0, 0, 0)  # left down
            u.mouse_event(0x0004, 0, 0, 0, 0)  # left up

    def menu(self, mode):
        rect = cw.RECT()
        assert u.GetWindowRect(self.hwnd, C.byref(rect))
        u.SetCursorPos(rect.left + self.height // 2, rect.top + self.height // 2)
        self.mode = mode
        self.menu_started = time.monotonic()
        self._initial_submit = self._last_submit
        self.keys_sent = False
        assert u.SetTimer(self.hwnd, TIMER_TEST, 100, None)
        self._show_menu()
        if self.hwnd:
            u.KillTimer(self.hwnd, TIMER_TEST)
        if self.failure:
            raise self.failure
        assert time.monotonic() - self.menu_started < 8, "menu failed to dismiss"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=75)
    parser.add_argument("--screenshot")
    parser.add_argument("--repeat-menus", type=int, default=4)
    parser.add_argument("--termination", choices=("quit", "close", "external"), default="quit")
    args = parser.parse_args()
    if not args.run:
        parser.error("real desktop interaction requires --run")
    # Match tray_app's process DPI setup for consistent screen coordinates.
    u.SetProcessDPIAware()
    widget = SmokeWidget(args.screenshot)
    before = resources()
    try:
        assert widget.create(), "CreateWindowEx failed"
        widget.update([SimpleNamespace(level=90, coarse="", error="")] * 3)
        pump(0.4)
        # Put the cursor over the first ring, where the user's menu originates.
        wr = cw.RECT()
        assert u.GetWindowRect(widget.hwnd, C.byref(wr))
        old_cursor = cw.POINT()
        u.GetCursorPos(C.byref(old_cursor))
        try:
            u.SetCursorPos(wr.left + widget.height // 2, wr.top + widget.height // 2)
            widget.menu("cancel")
            pump(0.6)
            assert widget._restore_remaining == 0 and not widget._paint_pending
            assert u.IsWindowVisible(widget.hwnd)
            assert u.WindowFromPoint(cw.POINT(wr.left + widget.height // 2,
                                             wr.top + widget.height // 2)) == widget.hwnd
            print(json.dumps(dict(stage="menu_cancel", checks=widget.menu_checks,
                                  last_item=widget.last_item, resources=resources())), flush=True)
            repeat_before = resources()
            for _ in range(args.repeat_menus):
                widget.menu("cancel")
                pump(0.4)
            widget.menu("outside")
            pump(0.5)
            repeat_after = resources()
            assert repeat_after["gdi"] <= repeat_before["gdi"] + 1
            assert repeat_after["user"] <= repeat_before["user"] + 1
            print(json.dumps(dict(stage="repeated_dismissal", repeats=args.repeat_menus,
                                  outside_click=True, before=repeat_before, after=repeat_after)), flush=True)
            idle_before = resources()
            cpu = time.process_time()
            uploads = widget.presentations
            pump(args.idle_seconds)
            cpu = time.process_time() - cpu
            print(json.dumps(dict(stage="idle", seconds=args.idle_seconds,
                                  cpu_seconds=round(cpu, 6), uploads=widget.presentations - uploads,
                                  before=idle_before, after=resources())), flush=True)
            assert cpu < max(0.5, args.idle_seconds * 0.01), "unexpected idle CPU"
            widget.menu("hide")
            assert widget.hwnd and not widget._destroyed
            assert not widget._visible and not u.IsWindowVisible(widget.hwnd)
            uploads = widget.presentations
            for _ in range(4):
                widget.update([])
                widget._wnd_proc(widget.hwnd, cw.MSG_RESTORE, 0, 0)
                widget._wnd_proc(widget.hwnd, cw.WM_TIMER, cw.TIMER_RESTORE, 0)
                widget._wnd_proc(widget.hwnd, cw.WM_TIMER, cw.TIMER_KEEPALIVE, 0)
            pump(1.8)
            assert not u.IsWindowVisible(widget.hwnd) and widget.presentations == uploads
            widget.show()
            pump(0.4)
            assert u.IsWindowVisible(widget.hwnd), dict(
                hwnd=widget.hwnd, visible=widget._visible, shown=widget._shown,
                destroyed=widget._destroyed, menu_active=widget._menu_active,
                pending=widget._paint_pending, failure=repr(widget.failure),
                width=widget.width, height=widget.height)
            native_hwnd = widget.hwnd
            widget.menu(args.termination)
            assert widget._destroyed and widget.hwnd is None and not u.IsWindow(native_hwnd)
            widget.update([])
            widget.show()
            pump(0.4)
            if widget.failure:
                raise widget.failure
            print(json.dumps(dict(stage="hide_quit_passed", before=before, after=resources())), flush=True)
        finally:
            u.SetCursorPos(old_cursor.x, old_cursor.y)
    finally:
        widget.destroy()


if __name__ == "__main__":
    main()
