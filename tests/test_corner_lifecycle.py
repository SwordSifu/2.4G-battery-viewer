"""Offline menu/visibility regressions: no windows, HID, or configuration IO."""
import ctypes
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("corner_lifecycle_subject", ROOT / "corner_widget.py")
cw = importlib.util.module_from_spec(spec)
# Replace DLL loading even on Windows: these tests must never touch the desktop.
with patch.object(ctypes, "WinDLL", create=True, side_effect=lambda *a, **k: MagicMock()), \
     patch.object(ctypes, "WINFUNCTYPE", getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE), create=True):
    spec.loader.exec_module(cw)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.api = MagicMock()
        self.api.CreatePopupMenu.return_value = 123
        self.api.TrackPopupMenu.return_value = 0
        self.api.GetSystemMetrics.return_value = 2000
        self.api.SetTimer.return_value = 1
        self.patch = patch.object(cw, "user32", self.api)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.callbacks = [MagicMock() for _ in range(6)]
        self.widget = cw.CornerWidget(*self.callbacks, lambda: 60, MagicMock())
        self.widget.hwnd = 456
        self.widget._shown = True
        self.widget._surface = (b"", 1, 1, 1)

    def assert_no_presentation(self):
        self.api.SetWindowPos.assert_not_called()
        self.api.UpdateLayeredWindow.assert_not_called()
        self.api.ShowWindow.assert_not_called()

    def test_nested_timer_hook_refresh_and_paint_are_deferred(self):
        w = self.widget
        def nested(*args):
            self.assertTrue(w._menu_active)
            w.update(["latest"])
            w._wnd_proc(w.hwnd, cw.WM_TIMER, cw.TIMER_KEEPALIVE, 0)
            w._wnd_proc(w.hwnd, cw.WM_TIMER, cw.TIMER_RESTORE, 0)
            w._wnd_proc(w.hwnd, cw.MSG_RESTORE, 0, 0)
            w._wnd_proc(w.hwnd, cw.WM_PAINT, 0, 0)
            w._on_foreground_change(None, 3, 12, 0, 0, 0, 0)
            w._paint()
            w._resubmit()
            w._submit(b"", 1, 1, 0, 0)
            w.raise_to_top()
            w.refresh_position()
            w._show_menu()  # no recursive menu
            self.assert_no_presentation()
            self.assertEqual(w.readings, ["latest"])
            self.assertTrue(w._paint_pending)
            self.assertEqual(w._restore_remaining, 0)
            return 0
        self.api.TrackPopupMenu.side_effect = nested
        w._show_menu()
        self.assertFalse(w._menu_active)
        self.assertEqual(w._restore_remaining, 3)
        self.api.CreatePopupMenu.assert_called_once()
        self.api.DestroyMenu.assert_called_once_with(123)
        self.api.ValidateRect.assert_called_once_with(456, None)

    def test_restore_burst_is_bounded_and_uses_latest_rows(self):
        w = self.widget
        w._paint_pending = True
        with patch.object(w, "_paint") as paint, patch.object(w, "_resubmit") as submit:
            w._schedule_restore()
            w._schedule_restore()
            self.assertEqual(self.api.SetTimer.call_count, 1)
            w._wnd_proc(456, cw.WM_TIMER, cw.TIMER_RESTORE, 0)
            paint.assert_called_once()
            w._paint_pending = False
            for _ in range(5):
                w._restore_tick()
            self.assertEqual(submit.call_count, 2)
            self.assertEqual(self.api.SetWindowPos.call_count, 3)
            self.assertEqual(w._restore_remaining, 0)

    def test_hide_command_never_restores(self):
        w = self.widget
        self.api.TrackPopupMenu.return_value = 3
        w.on_hide = w.hide
        w._show_menu()
        self.api.SetTimer.assert_not_called()
        self.api.ShowWindow.assert_called_once_with(456, cw.SW_HIDE)
        self.api.reset_mock()
        w.update(["new"])
        w._keepalive_tick()
        w._restore_tick()
        w._wnd_proc(456, cw.MSG_RESTORE, 0, 0)
        w._wnd_proc(456, cw.WM_PAINT, 0, 0)
        self.assert_no_presentation()
        self.assertFalse(w._visible)

    def test_quit_command_never_restores(self):
        w = self.widget
        self.api.TrackPopupMenu.return_value = 90
        w.on_quit = w.destroy
        w._show_menu()
        w.update([])
        w.show()
        w._restore_tick()
        self.assertTrue(w._destroyed)
        self.assertIsNone(w.hwnd)
        self.api.DestroyWindow.assert_called_once_with(456)
        self.api.DestroyMenu.assert_called_once_with(123)
        self.api.SetTimer.assert_not_called()
        self.assert_no_presentation()

    def test_destroy_in_nested_loop_frees_menu_once(self):
        w = self.widget
        def nested(*args):
            w.destroy()
            return 1
        self.api.TrackPopupMenu.side_effect = nested
        w._show_menu()
        w.destroy()
        self.api.EndMenu.assert_called_once()
        self.api.DestroyMenu.assert_called_once_with(123)
        self.callbacks[0].assert_not_called()
        self.api.SetTimer.assert_not_called()

    def test_track_exception_cleans_up_and_allows_next_menu(self):
        w = self.widget
        self.api.TrackPopupMenu.side_effect = RuntimeError("test")
        with self.assertRaises(RuntimeError):
            w._show_menu()
        self.assertFalse(w._menu_active)
        self.assertIsNone(w._menu)
        self.api.DestroyMenu.assert_called_once_with(123)
        self.api.TrackPopupMenu.side_effect = None
        w._show_menu()
        self.assertEqual(self.api.DestroyMenu.call_count, 2)

    def test_build_exception_also_cleans_up(self):
        self.api.AppendMenuW.side_effect = RuntimeError("build")
        with self.assertRaises(RuntimeError):
            self.widget._show_menu()
        self.assertFalse(self.widget._menu_active)
        self.api.DestroyMenu.assert_called_once_with(123)

    def test_callback_exception_still_cleans_up(self):
        self.api.TrackPopupMenu.return_value = 1
        self.widget.on_refresh.side_effect = RuntimeError("callback")
        with self.assertRaises(RuntimeError):
            self.widget._show_menu()
        self.assertFalse(self.widget._menu_active)
        self.api.DestroyMenu.assert_called_once_with(123)

    def test_cancel_posts_null_and_schedules_not_immediate_repaint(self):
        self.widget._show_menu()
        self.api.PostMessageW.assert_called_once_with(456, 0, 0, 0)
        self.assertEqual(self.widget._restore_remaining, 3)
        self.assert_no_presentation()

    def test_failed_menu_creation_keeps_state(self):
        self.api.CreatePopupMenu.return_value = 0
        self.widget._show_menu()
        self.assertFalse(self.widget._menu_active)
        self.api.DestroyMenu.assert_not_called()
        self.api.KillTimer.assert_not_called()

    def test_idle_fallback_does_not_upload_uncovered_window(self):
        with patch.object(self.widget, "_is_covered_by_taskbar", return_value=False):
            self.widget._keepalive_tick()
        self.api.SetTimer.assert_not_called()
        self.assert_no_presentation()

    def test_foreground_hook_posts_instead_of_rendering(self):
        self.widget._on_foreground_change(None, 3, 12, 0, 0, 0, 0)
        self.api.PostMessageW.assert_called_once_with(456, cw.MSG_RESTORE, 0, 0)
        self.assert_no_presentation()

    def test_restore_timer_failure_does_not_leave_pending_burst(self):
        self.api.SetTimer.return_value = 0
        self.widget._schedule_restore()
        self.assertEqual(self.widget._restore_remaining, 0)

    def assert_native_destroy_is_terminal(self, message):
        w = self.widget
        w._wnd_proc(456, message, 0, 0)
        w.update([])
        w.show()
        w._restore_tick()
        self.assertTrue(w._destroyed)
        self.assertIsNone(w.hwnd)
        self.api.SetTimer.assert_not_called()
        self.assert_no_presentation()

    def test_native_close_is_terminal(self):
        self.assert_native_destroy_is_terminal(cw.WM_CLOSE)

    def test_external_destroy_is_terminal(self):
        self.assert_native_destroy_is_terminal(cw.WM_DESTROY)

    def test_pixel_upload_does_not_raise_or_show_and_releases_gdi(self):
        data = b"\0" * 16
        buffer = ctypes.create_string_buffer(16)
        graphics = MagicMock()
        graphics.CreateCompatibleDC.return_value = 22
        graphics.SelectObject.return_value = 33
        def dib(dc, bmi, usage, bits, section, offset):
            ctypes.cast(bits, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(buffer)
            return 44
        graphics.CreateDIBSection.side_effect = dib
        self.api.GetDC.return_value = 11
        self.api.UpdateLayeredWindow.return_value = True
        with patch.object(cw, "gdi32", graphics), \
             patch.object(self.widget, "_apply_hit_region") as region:
            self.widget._submit(data, 2, 2, 5, 6)
            self.widget._submit(data, 2, 2, 5, 6)
            region.assert_called_once_with(2, 2)
        self.api.SetWindowPos.assert_not_called()
        self.api.ShowWindow.assert_not_called()
        self.assertEqual(graphics.DeleteObject.call_count, 2)
        self.assertEqual(graphics.DeleteDC.call_count, 2)
        self.assertEqual(self.api.ReleaseDC.call_count, 2)
        self.assertEqual(buffer.raw, data)

    def test_upload_failure_still_releases_gdi(self):
        buffer = ctypes.create_string_buffer(4)
        graphics = MagicMock()
        graphics.CreateCompatibleDC.return_value = 22
        graphics.SelectObject.return_value = 33
        def dib(dc, bmi, usage, bits, section, offset):
            ctypes.cast(bits, ctypes.POINTER(ctypes.c_void_p)).contents.value = ctypes.addressof(buffer)
            return 44
        graphics.CreateDIBSection.side_effect = dib
        self.api.GetDC.return_value = 11
        self.api.UpdateLayeredWindow.return_value = False
        with patch.object(cw, "gdi32", graphics), \
             patch.object(ctypes, "get_last_error", return_value=5, create=True), \
             patch.object(ctypes, "WinError", side_effect=OSError("upload failed"), create=True):
            with self.assertRaises(OSError):
                self.widget._submit(b"\0" * 4, 1, 1, 0, 0)
        graphics.SelectObject.assert_any_call(22, 33)
        graphics.DeleteObject.assert_called_once_with(44)
        graphics.DeleteDC.assert_called_once_with(22)
        self.api.ReleaseDC.assert_called_once_with(None, 11)

    def test_failed_bitmap_creation_releases_dcs(self):
        graphics = MagicMock()
        graphics.CreateCompatibleDC.return_value = 22
        graphics.CreateDIBSection.return_value = 0
        self.api.GetDC.return_value = 11
        with patch.object(cw, "gdi32", graphics):
            self.widget._submit(b"\0" * 4, 1, 1, 0, 0)
        graphics.DeleteDC.assert_called_once_with(22)
        self.api.ReleaseDC.assert_called_once_with(None, 11)
        self.api.UpdateLayeredWindow.assert_not_called()

    def test_hidden_menu_request_is_ignored(self):
        self.widget._visible = False
        self.widget._show_menu()
        self.api.CreatePopupMenu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
