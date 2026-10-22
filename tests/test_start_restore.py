"""Explicit background start must request ring display, not merely find a host.

All DLLs, processes, IPC and OS integrations are mocked; no personal config IO.
"""
import ctypes
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


class StartRestoreTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("start_restore_common", Path(__file__).resolve().parents[1]/"tray_common.py")
        self.common = importlib.util.module_from_spec(spec)
        with patch.object(ctypes, "WinDLL", create=True, return_value=MagicMock()), \
             patch.dict(sys.modules, {"win_shell": types.ModuleType("win_shell")}):
            spec.loader.exec_module(self.common)
        self.running = patch.object(self.common, "is_tray_running", return_value=True)
        self.mock_running = self.running.start()
        self.addCleanup(self.running.stop)
        self.post = patch.object(self.common, "post_to_tray", return_value=True)
        self.mock_post = self.post.start()
        self.addCleanup(self.post.stop)
        self.popen = patch.object(self.common.subprocess, "Popen")
        self.mock_popen = self.popen.start()
        self.addCleanup(self.popen.stop)
        self.sleep = patch.object(self.common.time, "sleep")
        self.mock_sleep = self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.pythonw = patch.object(self.common, "pythonw_path", return_value="fake-pythonw.exe")
        self.pythonw.start()
        self.addCleanup(self.pythonw.stop)

    def test_running_host_receives_explicit_show_without_spawning(self):
        self.assertTrue(self.common.start_tray_app())
        self.mock_post.assert_called_once_with(self.common.MSG_SHOW)
        self.mock_popen.assert_not_called()
        self.mock_sleep.assert_not_called()

    def test_failed_show_delivery_is_not_reported_as_success(self):
        self.mock_post.return_value = False
        self.assertFalse(self.common.start_tray_app())
        self.mock_popen.assert_not_called()

    def test_new_host_also_receives_show_once_available(self):
        self.mock_running.side_effect = [False, True]
        self.assertTrue(self.common.start_tray_app())
        self.mock_popen.assert_called_once()
        self.mock_post.assert_called_once_with(self.common.MSG_SHOW)
        self.mock_sleep.assert_not_called()

    def test_zero_timeout_still_requests_show_if_host_is_ready(self):
        self.mock_running.side_effect = [False, True]
        self.assertTrue(self.common.start_tray_app(wait_timeout=0))
        self.mock_post.assert_called_once_with(self.common.MSG_SHOW)

    def test_no_host_and_spawn_failure_returns_false(self):
        self.mock_running.return_value = False
        self.mock_popen.side_effect = OSError("fake failure")
        self.assertFalse(self.common.start_tray_app())
        self.mock_post.assert_not_called()

    def test_start_timeout_does_not_send_to_missing_window(self):
        self.mock_running.return_value = False
        self.assertFalse(self.common.start_tray_app(wait_timeout=0))
        self.mock_post.assert_not_called()
        self.mock_popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
