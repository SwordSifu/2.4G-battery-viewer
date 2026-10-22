# -*- coding: utf-8 -*-
"""Fake-only regression: the stable three rings must never create tray icons.

DLLs, device reader, corner widget, config and launchers are all replaced before
importing the subject. No native windows, hardware calls or personal config IO.
"""
import copy
import ctypes
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class FakeReading:
    def __init__(self, title, subtitle, error="", key="", level=None, coarse=""):
        self.title, self.subtitle, self.error, self.key = title, subtitle, error, key
        self.level, self.coarse = level, coarse
        self.status = self.detail = self.extra = ""
        self.updated_at = 0

    @property
    def ok(self):
        return not self.error and (self.level is not None or bool(self.coarse))

    @property
    def display_value(self):
        return str(self.level) + "%" if self.level is not None else self.coarse or "--"


class NoNotificationIconsTests(unittest.TestCase):
    def setUp(self):
        self.api, self.kernel, self.shell = MagicMock(), MagicMock(), MagicMock()
        self.api.RegisterClassExW.return_value = 1
        self.api.CreateWindowExW.return_value = 101
        self.api.GetMessageW.return_value = 0
        self.messages = {}
        def register(name):
            return self.messages.setdefault(name, 0xC000 + len(self.messages))
        self.api.RegisterWindowMessageW.side_effect = register
        self.kernel.GetModuleHandleW.return_value = 42
        self.config = {"interval": 60, "autostart": False, "corner": True, "corner_pos": None}
        self.common = types.ModuleType("tray_common")
        self.common.BASE_DIR = str(ROOT)
        self.common.APP_NAME = "fake-no-icons-app"
        self.common.MUTEX_NAME = "fake-no-icons-mutex"
        self.common.WND_CLASS = "fake-no-icons-window"
        self.common.MSG_QUIT, self.common.MSG_RELOAD = "fake-quit", "fake-reload"
        self.common.USER32, self.common.KERNEL32 = self.api, self.kernel
        self.common.load_config = MagicMock(side_effect=lambda: copy.deepcopy(self.config))
        self.common.save_config = MagicMock(return_value=True)
        self.common.start_ui = MagicMock(return_value=True)
        self.common.is_autostart_enabled = MagicMock(return_value=False)
        self.common.is_tray_running = MagicMock(return_value=False)
        self.common.acquire_instance = MagicMock(return_value=(222, True))
        self.common.close_instance = MagicMock()
        self.common.post_to_tray = MagicMock(return_value=True)
        self.devices = types.ModuleType("personal_devices")
        self.devices.DEVICE_KEYS = ("mouse", "keyboard", "gamepad")
        self.devices.DEVICE_TITLES = {"mouse": "鼠标", "keyboard": "键盘", "gamepad": "手柄"}
        self.devices.Reading = FakeReading
        self.devices.read_all = MagicMock(return_value=[
            FakeReading("鼠标", "", key="mouse", level=90),
            FakeReading("键盘", "", key="keyboard", level=100),
            FakeReading("手柄", "", key="gamepad", coarse="中档"),
        ])
        self.icons = types.ModuleType("win_icon")
        self.icons.make_hicon = MagicMock(return_value=900)
        self.icons.destroy_all = MagicMock()
        self.corner = MagicMock()
        self.corner.create.return_value = True
        self.corner.width, self.corner.height = 220, 44
        self.corner.position = None
        self.corner_module = types.ModuleType("corner_widget")
        self.corner_module.CornerWidget = MagicMock(return_value=self.corner)
        modules = {"tray_common": self.common, "personal_devices": self.devices,
                   "win_icon": self.icons, "corner_widget": self.corner_module}
        p = patch.dict(sys.modules, modules)
        p.start()
        self.addCleanup(p.stop)
        spec = importlib.util.spec_from_file_location("no_icons_subject", ROOT / "tray_app.py")
        self.subject = importlib.util.module_from_spec(spec)
        with patch.object(ctypes, "WinDLL", create=True, return_value=self.shell), \
             patch.object(ctypes, "WINFUNCTYPE", create=True,
                          new=getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)):
            spec.loader.exec_module(self.subject)
        self.app = self.subject.TrayApp()
        self.addCleanup(self.app.quit)

    def assert_no_registration(self):
        for call in self.shell.Shell_NotifyIconW.call_args_list:
            self.assertNotIn(call.args[0], (self.subject.NIM_ADD, self.subject.NIM_MODIFY))
        self.icons.make_hicon.assert_not_called()

    def run_without_reader(self):
        with patch.object(self.app, "refresh_now") as refresh:
            result = self.app.run()
        return result, refresh

    def test_startup_creates_three_ring_widget_but_no_notification_icons(self):
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 0)
        self.corner.create.assert_called_once()
        self.assertEqual([row.key for row in self.corner.update.call_args.args[0]],
                         ["mouse", "keyboard", "gamepad"])
        refresh.assert_called_once_with()
        self.assertFalse(self.app.icons_added)
        self.devices.read_all.assert_not_called()
        self.common.save_config.assert_not_called()
        self.assert_no_registration()

    def test_explicit_hidden_corner_setting_is_respected_without_config_writes(self):
        self.config["corner"] = False
        self.app.cfg = copy.deepcopy(self.config)
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 0)
        self.corner.create.assert_not_called()
        self.assertIsNone(self.app.corner)
        refresh.assert_called_once_with()
        self.common.save_config.assert_not_called()
        self.assert_no_registration()

    def test_explicit_add_update_toggle_are_hard_disabled_with_any_legacy_state(self):
        for was_added in (False, True):
            self.app.icons_added = was_added
            for _ in range(3):
                self.app.add_icons()
                self.app.update_icons()
                self.app.toggle_icons()
        self.assert_no_registration()
        # Deliberately leave no historical icons for the cleanup after this test.
        self.app.icons_added = False

    def test_low_level_icon_callback_cannot_bypass_disabled_registration(self):
        for action in (self.subject.NIM_ADD, self.subject.NIM_MODIFY):
            for uid, key, plate in self.subject.TRAY_ICONS:
                self.assertFalse(self.app._apply_icon(action, uid, key, plate))
        self.shell.Shell_NotifyIconW.assert_not_called()
        self.assert_no_registration()

    def test_refresh_result_updates_only_rings(self):
        self.app.hwnd, self.app.corner, self.app.busy = 101, self.corner, True
        self.app._work()  # Only the fake personal_devices reader can run here.
        self.app.wnd_proc(101, self.subject.MSG_REFRESH_DONE, 0, 0)
        self.assertFalse(self.app.busy)
        self.devices.read_all.assert_called_once_with(stop_event=self.app.stop_event)
        self.corner.update.assert_called_once()
        self.assertEqual(self.app.readings["gamepad"].level, None)
        self.assertEqual(self.app.readings["gamepad"].display_value, "中档")
        self.assert_no_registration()

    def test_timer_and_reload_paths_do_not_register_icons(self):
        self.app.hwnd = 101
        with patch.object(self.app, "refresh_now") as refresh:
            self.app.wnd_proc(101, self.subject.WM_TIMER, 1, 0)
            self.app.wnd_proc(101, self.app.msg_reload, 0, 0)
        refresh.assert_called_once()
        self.assert_no_registration()
        self.common.save_config.assert_not_called()

    def test_explorer_restart_never_registers_icons_even_after_legacy_added_state(self):
        self.app.hwnd, self.app.corner, self.app.icons_added = 101, self.corner, True
        for _ in range(3):
            self.app.wnd_proc(101, self.app.taskbar_created_msg, 0, 0)
            self.app.on_refresh_done()
            self.app.toggle_icons()
        self.assertFalse(self.app.icons_added)
        self.assert_no_registration()

    def test_legacy_menu_toggle_callback_is_noop(self):
        self.assertTrue(self.app.create_corner())
        callback = self.corner_module.CornerWidget.call_args.kwargs["on_toggle_tray"]
        callback()
        callback()
        self.assert_no_registration()

    def test_remove_without_legacy_icons_is_noop(self):
        self.app.hwnd = 101
        self.app.remove_icons()
        self.shell.Shell_NotifyIconW.assert_not_called()

    def test_cleanup_of_historical_icons_is_delete_only(self):
        self.app.hwnd, self.app.icons_added = 101, True
        self.app.remove_icons()
        self.assertEqual([call.args[0] for call in self.shell.Shell_NotifyIconW.call_args_list],
                         [self.subject.NIM_DELETE] * 3)
        self.assertFalse(self.app.icons_added)
        self.app.remove_icons()
        self.assertEqual(self.shell.Shell_NotifyIconW.call_count, 3)
        self.assert_no_registration()

    def test_failed_corner_creation_exits_without_fallback_timer_or_reader(self):
        self.corner.create.return_value = False
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 1)
        refresh.assert_not_called()
        self.corner.destroy.assert_called_once()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.stop_event.is_set())
        self.assertIsNone(self.app.hwnd)
        self.api.DestroyWindow.assert_called_once_with(101)
        self.api.SetTimer.assert_not_called()
        self.api.GetMessageW.assert_not_called()
        self.devices.read_all.assert_not_called()
        self.assert_no_registration()

    def test_failed_corner_cleanup_does_not_prevent_host_failure_exit(self):
        self.corner.create.return_value = False
        self.corner.destroy.side_effect = RuntimeError("fake cleanup failure")
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 1)
        refresh.assert_not_called()
        self.assertTrue(self.app.closing)
        self.assertIsNone(self.app.hwnd)
        self.api.DestroyWindow.assert_called_once_with(101)
        self.devices.read_all.assert_not_called()
        self.assert_no_registration()

    def test_corner_constructor_failure_does_not_leave_hidden_host(self):
        self.corner_module.CornerWidget.side_effect = RuntimeError("fake constructor failure")
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 1)
        refresh.assert_not_called()
        self.assertTrue(self.app.closing)
        self.assertIsNone(self.app.hwnd)
        self.api.DestroyWindow.assert_called_once_with(101)
        self.devices.read_all.assert_not_called()
        self.assert_no_registration()

    def test_first_paint_failure_destroys_corner_and_host_without_fallback(self):
        self.corner.update.side_effect = RuntimeError("fake paint failure")
        result, refresh = self.run_without_reader()
        self.assertEqual(result, 1)
        refresh.assert_not_called()
        self.corner.destroy.assert_called_once()
        self.assertIsNone(self.app.corner)
        self.assertIsNone(self.app.hwnd)
        self.api.DestroyWindow.assert_called_once_with(101)
        self.devices.read_all.assert_not_called()
        self.assert_no_registration()

    def test_main_finally_releases_mutex_after_corner_creation_failure(self):
        self.corner.create.return_value = False
        with patch.object(self.subject, "TrayApp", return_value=self.app), \
             patch.object(sys, "argv", ["tray_app.py"]):
            self.assertEqual(self.subject.main(), 1)
        self.common.close_instance.assert_called_once_with(222)
        self.assertTrue(self.app.closing)
        self.assertIsNone(self.app.hwnd)
        self.devices.read_all.assert_not_called()
        self.assert_no_registration()

    def test_main_finally_cleans_up_normal_message_loop_exit(self):
        with patch.object(self.subject, "TrayApp", return_value=self.app), \
             patch.object(self.app, "refresh_now"), \
             patch.object(sys, "argv", ["tray_app.py"]):
            self.assertEqual(self.subject.main(), 0)
        self.assertTrue(self.app.closing)
        self.corner.destroy.assert_called_once()
        self.api.DestroyWindow.assert_called_once_with(101)
        self.common.close_instance.assert_called_once_with(222)
        self.assert_no_registration()

    def test_destroy_message_still_ends_native_message_loop(self):
        self.app.wnd_proc(101, self.subject.WM_DESTROY, 0, 0)
        self.api.PostQuitMessage.assert_called_once_with(0)
        self.assert_no_registration()


if __name__ == "__main__":
    unittest.main()
