"""Geometry and lifecycle tests with mocked APIs, never touch the real desktop."""
import ctypes as C
import unittest
from unittest.mock import MagicMock, patch

from fullscreen_guard import POINT, RECT, MONITORINFO, foreground_fullscreen, client_covers_monitor
from test_corner_lifecycle import cw


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.GetForegroundWindow.return_value = 222
        self.api.IsWindowVisible.return_value = True
        self.api.IsIconic.return_value = False
        self.api.MonitorFromWindow.return_value = 20
        self.name = "SyntheticFullscreenWindow"
        self.monitor = (0, 0, 1920, 1080)
        self.client = (0, 0, 1920, 1080)
        self.origin = (0, 0)
        def name(hwnd, buf, count):
            buf.value = self.name
            return len(self.name)
        def monitor(handle, out):
            info = C.cast(out, C.POINTER(MONITORINFO)).contents
            info.rcMonitor = RECT(*self.monitor)
            info.rcWork = RECT(0, 0, 1920, 1032)
            return True
        def client(hwnd, out):
            rect = C.cast(out, C.POINTER(RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = self.client
            return True
        def screen(hwnd, out):
            point = C.cast(out, C.POINTER(POINT)).contents
            point.x += self.origin[0]
            point.y += self.origin[1]
            return True
        self.api.GetClassNameW.side_effect = name
        self.api.GetMonitorInfoW.side_effect = monitor
        self.api.GetClientRect.side_effect = client
        self.api.ClientToScreen.side_effect = screen

    def test_borderless_full_monitor_hides(self):
        self.assertIs(foreground_fullscreen(111, self.api), True)

    def test_normal_maximized_work_area_does_not_hide(self):
        self.client = (0, 0, 1920, 1000)
        self.origin = (0, 32)
        self.assertIs(foreground_fullscreen(111, self.api), False)

    def test_negative_monitor_coordinates_and_scaled_pixels(self):
        self.monitor = (-2880, -120, 0, 1680)
        self.client = (0, 0, 2880, 1800)
        self.origin = (-2880, -120)
        self.assertTrue(foreground_fullscreen(111, self.api))

    def test_other_monitor_does_not_hide(self):
        self.api.MonitorFromWindow.side_effect = lambda hwnd, flag: 20 if hwnd == 111 else 30
        self.assertFalse(foreground_fullscreen(111, self.api))

    def test_shell_and_desktop_are_not_fullscreen_games(self):
        for self.name in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "#32768"):
            self.assertFalse(foreground_fullscreen(111, self.api))
        self.api.GetClientRect.assert_not_called()

    def test_minimized_invisible_self_and_no_foreground_do_not_hide(self):
        self.api.IsIconic.return_value = True
        self.assertFalse(foreground_fullscreen(111, self.api))
        self.api.IsIconic.return_value = False
        self.api.IsWindowVisible.return_value = False
        self.assertFalse(foreground_fullscreen(111, self.api))
        for handle in (111, None):
            self.api.GetForegroundWindow.return_value = handle
            self.assertFalse(foreground_fullscreen(111, self.api))

    def test_probe_failure_is_unknown_not_permission_to_show(self):
        self.api.GetClientRect.side_effect = None
        self.api.GetClientRect.return_value = False
        self.assertIsNone(foreground_fullscreen(111, self.api))

    def test_empty_invalid_rects_and_small_tolerance(self):
        self.assertFalse(client_covers_monitor((0, 0, 0, 0), (0, 0, 1920, 1080)))
        self.assertFalse(client_covers_monitor((0, 0, 1920, 1080), (0, 0, 0, 0)))
        self.assertTrue(client_covers_monitor((1, 1, 1919, 1079), (0, 0, 1920, 1080)))
        self.assertFalse(client_covers_monitor((0, 35, 1920, 1080), (0, 0, 1920, 1080)))


class SuppressionTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.SetTimer.return_value = 1
        self.api.CreatePopupMenu.return_value = 123
        self.api.GetSystemMetrics.return_value = 2000
        self.context = patch.object(cw, "user32", self.api)
        self.context.start()
        self.addCleanup(self.context.stop)
        self.probe = MagicMock(return_value=False)
        self.widget = cw.CornerWidget(*[MagicMock() for _ in range(6)], lambda: 60, MagicMock(), fullscreen_probe=self.probe)
        self.widget.hwnd = 456
        self.widget._shown = True
        self.widget._surface = (b"", 1, 1, 1)

    def assert_not_shown_or_raised(self):
        self.api.UpdateLayeredWindow.assert_not_called()
        self.api.SetWindowPos.assert_not_called()
        self.assertFalse(any(item.args == (456, cw.SW_SHOWNOACTIVATE)
                             for item in self.api.ShowWindow.call_args_list))

    def test_fullscreen_hides_without_changing_user_intent(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        self.assertTrue(w._visible)
        self.assertTrue(w._fullscreen_suppressed)
        self.assertFalse(w._shown)
        self.api.ShowWindow.assert_called_once_with(456, cw.SW_HIDE)
        w.update(["latest"])
        w._paint()
        w._resubmit()
        w._submit(b"", 1, 1, 0, 0)
        w.raise_to_top()
        w.refresh_position()
        w._restore_tick()
        self.assertEqual(w.readings, ["latest"])
        self.assertTrue(w._paint_pending)
        self.assert_not_shown_or_raised()
        self.api.SetTimer.assert_called_with(456, cw.TIMER_KEEPALIVE, 500, None)

    def test_foreground_event_restores_only_after_exit(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        w._on_foreground_change(None, 3, 88, 0, 0, 0, 0)
        self.api.PostMessageW.assert_called_with(456, cw.MSG_RESTORE, 0, 0)
        w._wnd_proc(456, cw.MSG_RESTORE, 0, 0)
        self.assertEqual(w._restore_remaining, 0)
        self.probe.return_value = False
        w._wnd_proc(456, cw.MSG_RESTORE, 0, 0)
        self.assertFalse(w._fullscreen_suppressed)
        self.assertEqual(w._restore_remaining, 3)
        with patch.object(w, "_paint") as paint:
            w._restore_tick()
            paint.assert_called_once()

    def test_same_foreground_resize_is_caught_by_fallback(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        self.probe.return_value = False
        w._keepalive_tick()
        self.assertFalse(w._fullscreen_suppressed)
        self.assertEqual(w._restore_remaining, 3)

    def test_manual_hide_during_fullscreen_never_resurrects(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        w.hide()
        self.api.reset_mock()
        self.probe.return_value = False
        self.probe.reset_mock()
        w.update(["new"])
        w._keepalive_tick()
        w._restore_tick()
        w._wnd_proc(456, cw.MSG_RESTORE, 0, 0)
        w._wnd_proc(456, cw.WM_PAINT, 0, 0)
        self.assertFalse(w._visible)
        self.probe.assert_not_called()
        self.api.ShowWindow.assert_not_called()
        self.api.SetTimer.assert_not_called()
        self.assert_not_shown_or_raised()

    def test_unknown_probe_retains_fullscreen_suppression(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        self.api.reset_mock()
        self.probe.return_value = None
        w._keepalive_tick()
        w.update([])
        self.assertTrue(w._fullscreen_suppressed)
        self.assert_not_shown_or_raised()

    def test_menu_dismissal_into_fullscreen_keeps_monitoring(self):
        w = self.widget
        def tracking(*args):
            self.probe.return_value = True
            w._wnd_proc(456, cw.WM_PAINT, 0, 0)
            return 0
        self.api.TrackPopupMenu.side_effect = tracking
        w._show_menu()
        self.assertTrue(w._fullscreen_suppressed)
        self.assertFalse(w._menu_active)
        self.api.EndMenu.assert_called_once()
        self.api.DestroyMenu.assert_called_once_with(123)
        self.api.SetTimer.assert_called_with(456, cw.TIMER_KEEPALIVE, 500, None)
        self.assertEqual(w._restore_remaining, 0)
        self.assert_not_shown_or_raised()

    def test_fullscreen_appearing_during_render_cannot_show_window(self):
        w = self.widget
        w._shown = False
        self.probe.side_effect = [False, True]
        with patch.object(cw.rr, "render_panel", return_value=(b"", 1, 1)), \
             patch.object(w, "_taskbar_band", return_value=(0, 0, 48)), \
             patch.object(w, "_taskbar_color", return_value=None):
            w._paint()
        self.assertTrue(w._fullscreen_suppressed)
        self.assert_not_shown_or_raised()

    def test_destroy_while_suppressed_is_terminal(self):
        w = self.widget
        self.probe.return_value = True
        w._keepalive_tick()
        w.destroy()
        self.api.reset_mock()
        self.probe.return_value = False
        w.show()
        w.update([])
        w._keepalive_tick()
        w._restore_tick()
        self.api.ShowWindow.assert_not_called()
        self.api.SetTimer.assert_not_called()
        self.assertTrue(w._destroyed)


if __name__ == "__main__":
    unittest.main()
