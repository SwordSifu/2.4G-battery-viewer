"""Offline live adapter tests: faked Tk, readers, threads and integration APIs.

No native windows, HID, production processes, personal config or startup IO.
"""
import builtins
import copy
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import fluent_live as live


@dataclass
class Reading:
    key: str
    title: str = "fixture"
    level: int | None = None
    coarse: str = ""
    error: str = ""
    status: str = ""
    detail: str = ""
    extra: str = ""

    @property
    def ok(self):
        return not self.error and (self.level is not None or bool(self.coarse))

    @property
    def display_value(self):
        return f"{self.level}%" if self.level is not None else self.coarse


class FakeRoot:
    def __init__(self):
        self.jobs, self.protocols, self.bindings = {}, {}, {}
        self.counter = self.destroy_count = 0
        self.after_delays = []

    def after(self, delay, callback):
        self.counter += 1
        self.after_delays.append(delay)
        self.jobs[self.counter] = (delay, callback)
        return self.counter

    def after_cancel(self, job):
        self.jobs.pop(job, None)

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def bind(self, name, callback, **kwargs):
        self.bindings[name] = callback

    def destroy(self):
        self.destroy_count += 1


class FakeThreads:
    def __init__(self):
        self.jobs = []
        self.worker = False

    def __call__(self, **kwargs):
        owner = self
        class Thread:
            def __init__(self):
                self.done = False
                self.options = kwargs
            def start(self):
                owner.jobs.append(self)
            def join(self, *args, **kwargs):
                raise AssertionError("close must not join a possibly stuck worker")
        return Thread()

    def run(self, kind):
        job = next(job for job in self.jobs if not job.done and job.options["name"] == "fluent-" + kind)
        self.worker = True
        try:
            job.options["target"]()
        finally:
            self.worker = False
            job.done = True

    def count(self, kind):
        return sum(job.options["name"] == "fluent-" + kind for job in self.jobs)


class AdapterFixture(unittest.TestCase):
    def setUp(self):
        self.root, self.threads = FakeRoot(), FakeThreads()
        self.now = 100.0
        self.cfg = {"interval": 60, "autostart": True, "corner": False, "corner_pos": [114, 620]}
        self.rows = [Reading("mouse", level=73), Reading("keyboard", level=95),
                     Reading("gamepad", coarse="中档")]
        self.pd = types.SimpleNamespace(
            empty_readings=MagicMock(side_effect=lambda message="正在读取…": [Reading(k, error=message) for k in ("mouse", "keyboard", "gamepad")]),
            read_all=MagicMock(side_effect=self.read),
        )
        self.tc = types.SimpleNamespace(
            UI_TITLE="Real production title", UI_MUTEX_NAME="fake-mutex",
            INTERVAL_CHOICES=[(30, "30 秒"), (60, "1 分钟"), (120, "2 分钟")],
            MSG_RELOAD="reload", MSG_SHOW="show", MSG_QUIT="quit",
            load_config=MagicMock(side_effect=lambda: copy.deepcopy(self.cfg)),
            save_config=MagicMock(side_effect=self.save),
            is_tray_running=MagicMock(side_effect=lambda: self.native(False)),
            is_autostart_enabled=MagicMock(side_effect=lambda: self.native(False)),
            start_tray_app=MagicMock(side_effect=lambda: self.native(True)),
            post_to_tray=MagicMock(side_effect=lambda message: self.native(True)),
            set_autostart=MagicMock(side_effect=lambda want: self.native(True)),
            install_desktop_links=MagicMock(side_effect=lambda: self.native({"folder": "shortcut", "links": {}, "errors": []})),
        )
        self.panel = MagicMock()
        for method in ("set_refreshing", "set_readings", "set_status", "set_backend_status"):
            getattr(self.panel, method).side_effect = self.ui
        self.panel.float_button.configure.side_effect = self.ui
        self.c = live.LiveController(self.root, device_api=self.pd, common_api=self.tc,
                                     thread_factory=self.threads, clock=lambda: self.now)
        self.c.start(self.panel)

    def ui(self, *args, **kwargs):
        self.assertFalse(self.threads.worker, "Tk method called from a worker")

    def native(self, result):
        self.assertTrue(self.threads.worker, "native integration called from Tk thread")
        return result

    def read(self, *, stop_event):
        self.assertTrue(self.threads.worker)
        self.assertIs(stop_event, self.c.stop_event)
        return self.rows

    def save(self, cfg):
        self.assertTrue(self.threads.worker)
        self.cfg = copy.deepcopy(cfg)
        return True

    def finish(self, kind):
        self.threads.run(kind)
        self.c._pump()

    def fire(self, name):
        job = self.c._jobs[name]
        _, callback = self.root.jobs.pop(job)
        callback()


class LiveTests(AdapterFixture):
    def test_start_schedules_bounded_pump_without_native_or_startup_actions(self):
        self.assertIn(250, self.root.after_delays)
        self.assertEqual(self.root.jobs[self.c._jobs["auto"]][0], 60000)
        for method in (self.tc.is_tray_running, self.tc.is_autostart_enabled,
                       self.tc.start_tray_app, self.tc.set_autostart,
                       self.tc.install_desktop_links, self.tc.post_to_tray, self.pd.read_all):
            method.assert_not_called()

    def test_one_reader_and_ui_thread_only_results_preserve_coarse_value(self):
        self.fire("initial")
        self.c.refresh()
        self.c.refresh()
        self.assertEqual(self.threads.count("readings"), 1)
        self.threads.run("readings")
        self.panel.set_readings.assert_not_called()
        self.assertTrue(self.c.busy)
        self.c._pump()
        self.assertFalse(self.c.busy)
        self.panel.set_readings.assert_called_once_with(self.rows)
        self.assertIsNone(self.panel.set_readings.call_args.args[0][2].level)
        self.assertNotIn("%", self.rows[2].display_value)
        self.panel.set_refreshing.assert_called_with(False)
        self.assertIn("3/3", self.c.status_text)

    def test_reader_failure_replaces_stale_values_with_honest_errors_and_no_console(self):
        self.pd.read_all.side_effect = RuntimeError("fake driver failure")
        self.c.refresh()
        with patch.object(live.traceback, "print_exc") as diagnostic:
            self.finish("readings")
        diagnostic.assert_not_called()
        rows = self.panel.set_readings.call_args.args[0]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(not r.ok and r.level is None for r in rows))
        self.assertTrue(self.c.status_error)
        self.assertFalse(self.c.busy)
        self.c.refresh()
        self.assertEqual(self.threads.count("readings"), 2)

    def test_debug_worker_failure_prints_diagnostics_only_when_requested(self):
        self.c.debug = True
        self.pd.read_all.side_effect = RuntimeError("fake failure")
        self.c.refresh()
        with patch.object(live.traceback, "print_exc") as diagnostic:
            self.finish("readings")
        diagnostic.assert_called_once()

    def test_auto_off_cancels_and_manual_refresh_remains_available(self):
        self.c.set_auto(False)
        self.assertNotIn("auto", self.c._jobs)
        self.c.refresh()
        self.finish("readings")
        self.assertNotIn("auto", self.c._jobs)
        self.c.set_auto(True)
        self.fire("auto")
        self.assertTrue(self.c.busy)
        self.assertIn("auto", self.c._jobs)

    def test_interval_saves_fresh_full_config_preserving_host_changes(self):
        self.cfg.update(autostart=False, corner=True, corner_pos=[877, 555])
        self.c.set_interval(120)
        self.tc.save_config.assert_not_called()
        self.finish("interval")
        self.tc.save_config.assert_called_once_with({"interval": 120, "autostart": False,
                                                   "corner": True, "corner_pos": [877, 555]})
        self.tc.post_to_tray.assert_called_once_with("reload")
        self.assertEqual(self.c.interval, 120)
        self.assertEqual(self.root.jobs[self.c._jobs["auto"]][0], 120000)
        self.assertTrue(self.c.busy)

    def test_failed_interval_save_keeps_actual_interval_and_does_not_reload(self):
        self.tc.save_config.side_effect = lambda cfg: self.native(False)
        self.c.set_interval(120)
        self.c.set_interval(30)
        self.finish("interval")
        self.assertEqual(self.c.interval, 60)
        self.assertFalse(self.c.interval_busy)
        self.assertTrue(self.c.status_error)
        self.tc.post_to_tray.assert_not_called()
        self.assertEqual(self.threads.count("interval"), 1)

    def test_saved_interval_survives_reload_delivery_exception(self):
        self.tc.post_to_tray.side_effect = OSError("fake IPC unavailable")
        self.c.set_interval(120)
        self.finish("interval")
        self.assertEqual(self.cfg["interval"], 120)
        self.assertEqual(self.c.interval, 120)
        self.assertFalse(self.c.interval_busy)
        self.assertFalse(self.c.status_error)
        self.assertIn("已保存", self.c.status_text)

    def test_invalid_interval_rejected_before_config_or_worker_changes(self):
        for value in (True, 0, 29, 1801, "60"):
            with self.assertRaises(ValueError): self.c.set_interval(value)
        self.assertEqual(self.threads.count("interval"), 0)
        self.tc.save_config.assert_not_called()

    def test_start_is_explicit_worker_start_show_and_never_quit_or_hide(self):
        self.tc.is_tray_running.side_effect = lambda: self.native(True)
        self.c.start_host()
        self.c.start_host()
        self.tc.start_tray_app.assert_not_called()
        self.panel.float_button.configure.assert_called_with(state="disabled")
        self.finish("host")
        self.tc.start_tray_app.assert_called_once_with()
        self.tc.post_to_tray.assert_not_called()
        self.assertTrue(self.c.backend_running)
        self.assertIn("请求显示", self.c.status_text)
        self.panel.float_button.configure.assert_called_with(state="normal")
        self.c.start_host()
        self.assertEqual(self.threads.count("host"), 2)

    def test_start_failure_never_claims_running_or_visible(self):
        self.tc.start_tray_app.side_effect = lambda: self.native(False)
        self.c.start_host()
        self.finish("host")
        self.assertFalse(self.c.backend_running)
        self.assertTrue(self.c.status_error)
        self.assertIn("失败", self.c.status_text)

    def test_quit_is_explicit_and_waits_for_observed_real_exit(self):
        self.c.backend_running = True
        self.c.quit_host()
        self.tc.post_to_tray.assert_not_called()
        self.finish("quit")
        self.tc.post_to_tray.assert_called_once_with("quit")
        self.assertTrue(self.c.backend_running)
        self.assertIn("等待", self.c._last_backend)
        self.finish("observe")
        self.assertFalse(self.c.backend_running)
        self.assertIn("未运行", self.c._last_backend)

    def test_autostart_failure_reflects_real_state_and_has_no_config_write(self):
        self.tc.set_autostart.side_effect = lambda want: self.native(False)
        self.c.set_autostart(True)
        self.c.set_autostart(True)
        self.finish("autostart")
        self.tc.set_autostart.assert_called_once_with(True)
        self.tc.post_to_tray.assert_called_once_with("reload")
        self.assertFalse(self.c.autostart)
        self.assertTrue(self.c.status_error)
        self.assertFalse(self.c.boot_busy)
        self.tc.save_config.assert_not_called()

    def test_successful_autostart_survives_reload_exception(self):
        self.tc.is_autostart_enabled.side_effect = lambda: self.native(True)
        self.tc.post_to_tray.side_effect = OSError("fake IPC unavailable")
        self.c.set_autostart(True)
        self.finish("autostart")
        self.assertTrue(self.c.autostart)
        self.assertFalse(self.c.boot_busy)
        self.assertFalse(self.c.status_error)

    def test_autostart_success_and_mismatch_both_use_actual_state(self):
        self.tc.is_autostart_enabled.side_effect = lambda: self.native(True)
        self.c.set_autostart(True)
        self.finish("autostart")
        self.assertTrue(self.c.autostart)
        self.assertFalse(self.c.status_error)
        self.c.set_autostart(False)
        self.finish("autostart")
        self.assertTrue(self.c.autostart)
        self.assertTrue(self.c.status_error)

    def test_link_action_is_async_single_flight_and_surfaces_partial_errors(self):
        self.tc.install_desktop_links.side_effect = lambda: self.native({"folder": "shortcut", "links": {}, "errors": ["foreign shortcut untouched"]})
        self.c.install_links()
        self.c.install_links()
        self.tc.install_desktop_links.assert_not_called()
        self.finish("links")
        self.assertEqual(self.threads.count("links"), 1)
        self.assertFalse(self.c.links_busy)
        self.assertIn("foreign shortcut untouched", self.c.status_text)
        self.assertTrue(self.c.status_error)

    def test_backend_observation_is_worker_only_single_flight_and_throttled(self):
        self.c._pump()
        self.c._pump()
        self.assertEqual(self.threads.count("observe"), 1)
        self.tc.is_tray_running.assert_not_called()
        self.finish("observe")
        self.c._pump()
        self.assertEqual(self.threads.count("observe"), 1)
        self.now += 5
        self.c._pump()
        self.assertEqual(self.threads.count("observe"), 2)
        self.tc.start_tray_app.assert_not_called()
        self.tc.set_autostart.assert_not_called()

    def test_stale_backend_observation_cannot_override_explicit_host_action(self):
        self.c._pump()
        self.threads.run("observe")  # Snapshot false; deliver AFTER host result.
        stale = self.c.results.get_nowait()
        self.tc.is_tray_running.side_effect = lambda: self.native(True)
        self.c.start_host()
        self.threads.run("host")
        self.c.results.put(stale)
        self.c._pump()
        self.assertTrue(self.c.backend_running)
        self.assertIn("运行中", self.c._last_backend)

    def test_stale_failed_observation_does_not_erase_new_host_state(self):
        self.tc.is_tray_running.side_effect = OSError("fake status failure")
        self.c._pump()
        self.threads.run("observe")
        self.tc.is_tray_running.side_effect = lambda: self.native(True)
        self.c.start_host()
        self.threads.run("host")
        self.c._pump()
        self.assertTrue(self.c.backend_running)
        self.assertIn("运行中", self.c._last_backend)

    def test_action_exceptions_release_controls_and_report_no_false_success(self):
        self.tc.start_tray_app.side_effect = RuntimeError("fake start failure")
        self.c.start_host()
        self.finish("host")
        self.assertIsNone(self.c.host_action)
        self.assertIsNone(self.c.backend_running)
        self.assertTrue(self.c.status_error)
        self.tc.set_autostart.side_effect = RuntimeError("fake shortcut failure")
        self.c.set_autostart(True)
        self.finish("autostart")
        self.assertFalse(self.c.boot_busy)
        self.assertIsNone(self.c.autostart)
        self.assertTrue(self.c.status_error)
        self.tc.install_desktop_links.side_effect = RuntimeError("fake link failure")
        self.c.install_links()
        self.finish("links")
        self.assertFalse(self.c.links_busy)
        self.assertTrue(self.c.status_error)
        self.tc.post_to_tray.side_effect = RuntimeError("fake quit failure")
        self.c.quit_host()
        self.finish("quit")
        self.assertIsNone(self.c.host_action)
        self.assertTrue(self.c.status_error)

    def test_pump_work_is_bounded_and_unchanged_backend_does_not_repaint(self):
        self.c._render_backend()
        calls = self.panel.set_backend_status.call_count
        button_calls = self.panel.float_button.configure.call_count
        for _ in range(33):
            self.c.results.put(("links", True, {"folder": "shortcut", "errors": []}))
        self.c._pump()
        self.assertEqual(self.c.results.qsize(), 1)
        self.assertEqual(self.panel.set_backend_status.call_count, calls)
        self.assertEqual(self.panel.float_button.configure.call_count, button_calls)

    def test_notice_expiry_restores_latest_read_status(self):
        self.c._notice("operation notice")
        self.c.refresh()
        self.finish("readings")
        self.assertEqual(self.c.status_text, "operation notice")
        self.now += 11
        self.c._pump()
        self.assertIn("3/3", self.c.status_text)

    def test_close_cancels_all_jobs_sets_stop_and_never_joins_or_quits_host(self):
        self.c.refresh()  # Deliberately never run/finish the fake reader.
        self.c.install_links()
        self.c.close()
        self.c.close()
        self.assertTrue(self.c.stop_event.is_set())
        self.assertEqual(self.root.jobs, {})
        self.assertEqual(self.root.destroy_count, 1)
        self.tc.post_to_tray.assert_not_called()
        self.assertTrue(all(job.options["daemon"] for job in self.threads.jobs))
        self.threads.run("readings")
        self.assertTrue(self.c.results.empty())
        self.c.refresh()
        self.c.start_host()
        self.c.quit_host()
        self.c.install_links()
        self.c.open_settings()
        self.c._pump()
        self.assertEqual(self.root.jobs, {})

    def test_destroy_event_only_closes_on_root_and_does_not_destroy_twice(self):
        self.c._destroyed(types.SimpleNamespace(widget=object()))
        self.assertFalse(self.c.closed)
        self.c._destroyed(types.SimpleNamespace(widget=self.root))
        self.assertTrue(self.c.closed)
        self.assertEqual(self.root.destroy_count, 0)
        self.assertEqual(self.root.jobs, {})

    def test_thread_start_failure_releases_busy_state_via_queue(self):
        self.c._thread = MagicMock(side_effect=RuntimeError("fake thread failure"))
        self.c.refresh()
        self.c._pump()
        self.assertFalse(self.c.busy)
        self.assertTrue(self.c.status_error)

    def test_builder_injects_only_real_rows_title_and_callbacks_without_side_effects(self):
        root, factory = FakeRoot(), MagicMock(return_value=self.panel)
        result = live.build_live_ui(root_factory=lambda: root, panel_factory=factory,
                                    device_api=self.pd, common_api=self.tc, thread_factory=self.threads)
        self.assertIs(result, root)
        args, kw = factory.call_args
        self.assertEqual(args, (root,))
        self.assertIs(kw["preview"], False)
        self.assertEqual(kw["title"], self.tc.UI_TITLE)
        self.assertTrue(all(not row.ok for row in kw["rows"]))
        self.assertEqual(kw["on_refresh"], root.live_controller.refresh)
        self.assertEqual(kw["on_float"], root.live_controller.start_host)
        self.assertEqual(kw["on_settings"], root.live_controller.open_settings)
        self.pd.read_all.assert_not_called()
        self.tc.set_autostart.assert_not_called()
        self.tc.install_desktop_links.assert_not_called()

    def test_failed_builder_cleans_up_root_and_reraises(self):
        root = FakeRoot()
        with self.assertRaisesRegex(RuntimeError, "fake panel"):
            live.build_live_ui(root_factory=lambda: root, panel_factory=MagicMock(side_effect=RuntimeError("fake panel")),
                               device_api=self.pd, common_api=self.tc)
        self.assertEqual(root.destroy_count, 1)

    def test_import_does_not_import_tk_or_production_apis(self):
        original = builtins.__import__
        forbidden = {"tkinter", "tray_common", "personal_devices", "personal_hid", "win_shell"}
        def checked(name, *args, **kwargs):
            if name.split(".")[0] in forbidden:
                raise AssertionError("unexpected import: " + name)
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("isolated_fluent_live", Path(live.__file__))
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=checked):
            spec.loader.exec_module(module)

    def test_settings_popup_reuses_window_and_never_mutates_on_open(self):
        dialog = MagicMock()
        with patch.object(live, "SettingsDialog", return_value=dialog) as factory:
            self.c.open_settings()
            self.c.open_settings()
        factory.assert_called_once_with(self.c)
        dialog.window.lift.assert_called_once()
        self.tc.set_autostart.assert_not_called()
        self.tc.save_config.assert_not_called()
        self.tc.start_tray_app.assert_not_called()
        self.tc.install_desktop_links.assert_not_called()


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.options, self.bindings, self.protocols = kwargs, {}, {}
    def pack(self, **kwargs): pass
    def configure(self, **kwargs): self.options.update(kwargs)
    def bind(self, key, callback, **kwargs): self.bindings[key] = callback
    def protocol(self, key, callback): self.protocols[key] = callback
    def title(self, *args): pass
    def transient(self, *args): pass
    def geometry(self, *args): pass
    def minsize(self, *args): pass
    def update_idletasks(self): pass
    def destroy(self):
        if "<Destroy>" in self.bindings:
            self.bindings["<Destroy>"](types.SimpleNamespace(widget=self))
    def invoke(self): self.options["command"]()


class FakeVariable:
    def __init__(self, master=None, value=None): self.value = value
    def set(self, value): self.value = value
    def get(self): return self.value


class SettingsTests(AdapterFixture):
    def test_dark_settings_controls_use_real_state_and_explicit_commands(self):
        tk = types.ModuleType("tkinter")
        tk.Toplevel, tk.StringVar, tk.BooleanVar = FakeWidget, FakeVariable, FakeVariable
        ttk = types.ModuleType("tkinter.ttk")
        for name in ("Frame", "Label", "Checkbutton", "Button", "Combobox"):
            setattr(ttk, name, FakeWidget)
        ttk.Style = MagicMock()
        tk.ttk = ttk
        chrome = types.ModuleType("win_backdrop")
        chrome.style_chrome = MagicMock()
        self.panel.s = lambda value: value
        self.panel.font.side_effect = lambda *args: ("fake-font", args)
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.ttk": ttk, "win_backdrop": chrome}):
            self.c.open_settings()
        view = self.c.settings
        chrome.style_chrome.assert_called_once_with(view.window)
        self.assertEqual(view.boot.options["state"], "disabled")
        self.assertEqual(view.quit.options["state"], "disabled")
        self.tc.set_autostart.assert_not_called()
        self.c.autostart, self.c.backend_running = True, True
        self.c._render_backend()
        view.sync()
        self.assertTrue(view.boot_var.get())
        self.assertEqual(view.boot.options["state"], "normal")
        self.assertEqual(view.quit.options["state"], "normal")
        view.boot_var.set(False)
        view.boot.invoke()
        self.tc.set_autostart.assert_not_called()
        self.finish("autostart")
        self.assertFalse(view.boot_var.get())
        view.interval_var.set("2 分钟")
        view.combo.bindings["<<ComboboxSelected>>"](None)
        self.assertEqual(view.combo.options["state"], "disabled")
        self.finish("interval")
        self.assertEqual(view.interval_var.get(), "2 分钟")
        view.show.invoke()
        self.assertEqual(self.c.host_action, "start")
        self.assertEqual(view.show.options["state"], "disabled")
        view.close()
        self.assertIsNone(self.c.settings)



class LegacyEntryTests(unittest.TestCase):
    def setUp(self):
        self.pd = types.ModuleType("personal_devices")
        self.pd.read_all = MagicMock(return_value=[Reading("gamepad", coarse="中档")])
        self.tc = types.ModuleType("tray_common")
        self.tc.UI_MUTEX_NAME = "fake-ui-mutex"
        self.tc.acquire_instance = MagicMock(return_value=(123, True))
        self.tc.close_instance = MagicMock()
        self.tc.foreground_ui = MagicMock(return_value=True)
        self.tc.install_desktop_links = MagicMock(return_value={"folder": "shortcut", "links": {}, "errors": []})
        self.tc.set_autostart = MagicMock(return_value=True)
        self.tc.autostart_status = MagicMock(return_value={"enabled": False, "shortcut": "fake.lnk", "shortcut_exists": False,
                                                          "taskmanager_state": "fake status", "target": "fake target"})
        link = types.ModuleType("win_link")
        link.KIND_LABEL = {"shortcut": "快捷方式"}
        spec = importlib.util.spec_from_file_location("isolated_my_devices", Path(live.__file__).with_name("my_devices.py"))
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"personal_devices": self.pd, "tray_common": self.tc, "win_link": link}):
            spec.loader.exec_module(self.module)

    def test_build_ui_delegates_debug_without_using_hardware(self):
        root = object()
        with patch.object(live, "build_live_ui", return_value=root) as build:
            self.assertIs(self.module.build_ui(debug=True), root)
        build.assert_called_once_with(debug=True)
        self.pd.read_all.assert_not_called()

    def test_once_preserves_coarse_cli_and_bypasses_ui_mutex(self):
        with patch.object(sys, "argv", ["my_devices.py", "--once"]), patch("builtins.print") as output:
            self.assertEqual(self.module.main(), 0)
        self.assertIn("中档", output.call_args_list[0].args[0])
        self.assertNotIn("%", output.call_args_list[0].args[0])
        self.tc.acquire_instance.assert_not_called()

    def test_main_keeps_ui_mutex_and_debug_forwarding(self):
        root = MagicMock()
        with patch.object(sys, "argv", ["my_devices.py", "--debug"]), patch.object(self.module, "build_ui", return_value=root) as build:
            self.assertEqual(self.module.main(), 0)
        build.assert_called_once_with(debug=True)
        root.mainloop.assert_called_once()
        self.tc.acquire_instance.assert_called_once_with("fake-ui-mutex")
        self.tc.close_instance.assert_called_once_with(123)

    def test_existing_ui_foregrounds_without_building_another_root(self):
        self.tc.acquire_instance.return_value = 124, False
        with patch.object(sys, "argv", ["my_devices.py"]), patch.object(self.module, "build_ui") as build:
            self.assertEqual(self.module.main(), 0)
        build.assert_not_called()
        self.tc.foreground_ui.assert_called_once()
        self.tc.close_instance.assert_called_once_with(124)

    def test_explicit_link_and_autostart_cli_paths_remain_compatible(self):
        for args in (["--install-links"], ["--autostart", "on"], ["--autostart", "off"], ["--autostart", "status"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["my_devices.py", *args]), patch("builtins.print"):
                self.assertEqual(self.module.main(), 0)
        self.tc.install_desktop_links.assert_called_once()
        self.assertEqual(self.tc.set_autostart.call_args_list[0].args, (True,))
        self.assertEqual(self.tc.set_autostart.call_args_list[1].args, (False,))
        self.tc.autostart_status.assert_called_once()
        self.tc.acquire_instance.assert_not_called()


if __name__ == "__main__":
    unittest.main()
