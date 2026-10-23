"""Production adapter for FluentPanel; importing it performs no OS/device IO.

Tk owns every widget and timer. Daemon workers only call the existing reader or
explicit integration APIs and enqueue results; closing never waits on a driver.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback


class LiveController:
    PUMP_MS = 250
    OBSERVE_SECONDS = 5.0

    def __init__(self, root, *, device_api, common_api, debug=False,
                 thread_factory=None, clock=None):
        self.root, self.pd, self.tc = root, device_api, common_api
        self.debug = debug
        self._thread = thread_factory or threading.Thread
        self._clock = clock or time.monotonic
        self.results = queue.Queue()
        self.stop_event = threading.Event()
        self.panel = self.settings = None
        self.closed = self.busy = self.interval_busy = self.boot_busy = False
        self.links_busy = self.observe_busy = False
        self.host_action = None
        self.backend_running = self.autostart = None
        self.auto = True
        self.interval = int(self.tc.load_config()["interval"])
        self._jobs = {}
        self._epoch = 0
        self._next_observe = 0.0
        self._quit_until = 0.0
        self._notice_until = 0.0
        self.status_text, self.status_error = "正在读取…", False
        self._reading_status, self._reading_error = self.status_text, False
        self._last_status = self._last_backend = self._last_float_state = None
        self._observe_epoch = None

    def start(self, panel):
        self.panel = panel
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Destroy>", self._destroyed, add=True)
        self._publish_status()
        self._render_backend()
        self._schedule("initial", 300, self.refresh)
        self._schedule("pump", self.PUMP_MS, self._pump)
        self._reschedule()

    def _schedule(self, name, delay, callback):
        self._cancel(name)
        if self.closed:
            return
        def run():
            self._jobs.pop(name, None)
            if not self.closed:
                callback()
        self._jobs[name] = self.root.after(delay, run)

    def _cancel(self, name):
        job = self._jobs.pop(name, None)
        if job is not None:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass  # The window may already be tearing down.

    def _launch(self, kind, work):
        def run():
            try:
                result = (kind, True, work())
            except Exception as exc:
                if self.debug:
                    traceback.print_exc()
                result = (kind, False, exc)
            if not self.stop_event.is_set():
                self.results.put(result)
        try:
            self._thread(target=run, daemon=True, name="fluent-" + kind).start()
        except Exception as exc:
            if self.debug:
                traceback.print_exc()
            self.results.put((kind, False, exc))

    def refresh(self):
        if self.closed or self.busy:
            return
        self._cancel("initial")
        self.busy = True
        self.panel.set_refreshing(True)
        self._reading_status, self._reading_error = "正在读取…", False
        self._publish_status()
        self._launch("readings", lambda: self.pd.read_all(stop_event=self.stop_event))

    def _reschedule(self):
        self._cancel("auto")
        if self.auto and not self.closed:
            self._schedule("auto", self.interval * 1000, self._auto_tick)

    def _auto_tick(self):
        self.refresh()
        self._reschedule()

    def set_auto(self, enabled):
        if self.closed:
            return
        self.auto = bool(enabled)
        self._reschedule()
        self._sync_settings()

    def _reload_host(self):
        # Persistence/shortcut success must not be misreported if IPC fails.
        # A missing host is normal; it will load the saved interval on start.
        try:
            self.tc.post_to_tray(self.tc.MSG_RELOAD)
        except Exception:
            if self.debug:
                traceback.print_exc()

    def set_interval(self, seconds):
        if self.closed or self.interval_busy:
            return
        if type(seconds) is not int or not 30 <= seconds <= 1800:
            raise ValueError("invalid refresh interval")
        self.interval_busy = True
        self._sync_settings()
        def work():
            # Never save a stale/partial snapshot: retain corner position,
            # visibility and autostart, including changes from the host.
            cfg = self.tc.load_config()
            cfg["interval"] = seconds
            saved = self.tc.save_config(cfg)
            if saved:
                self._reload_host()
            return saved, seconds
        self._launch("interval", work)

    def set_autostart(self, enabled):
        if self.closed or self.boot_busy:
            return
        want = bool(enabled)
        self.boot_busy = True
        self._epoch += 1
        self._sync_settings()
        def work():
            ok = self.tc.set_autostart(want)
            actual = self.tc.is_autostart_enabled()
            self._reload_host()
            return ok, actual, want
        self._launch("autostart", work)

    def start_host(self):
        """Explicit start/show only; never toggle a running host to hidden."""
        if self.closed or self.host_action:
            return
        self.host_action = "start"
        self._epoch += 1
        self._quit_until = 0.0
        self._render_backend()
        def work():
            ok = self.tc.start_tray_app()
            return ok, self.tc.is_tray_running()
        self._launch("host", work)

    def quit_host(self):
        if self.closed or self.host_action:
            return
        self.host_action = "quit"
        self._epoch += 1
        self._render_backend()
        self._launch("quit", lambda: self.tc.post_to_tray(self.tc.MSG_QUIT))

    def install_links(self):
        if self.closed or self.links_busy:
            return
        self.links_busy = True
        self._sync_settings()
        self._launch("links", self.tc.install_desktop_links)

    def _observe(self):
        if self.observe_busy or self.host_action or self.boot_busy:
            return
        self.observe_busy = True
        epoch = self._observe_epoch = self._epoch
        self._next_observe = self._clock() + self.OBSERVE_SECONDS
        self._launch("observe", lambda: (epoch, self.tc.is_tray_running(),
                                        self.tc.is_autostart_enabled()))

    def _complete(self, kind, success, value):
        if kind == "readings":
            self.busy = False
            self.panel.set_refreshing(False)
            rows = value if success else self.pd.empty_readings("读取异常；可使用 --debug 查看诊断")
            self.panel.set_readings(rows)
            ok = sum(bool(row.ok) for row in rows)
            self._reading_status = (f"更新于 {time.strftime('%H:%M:%S')} · 成功读取 {ok}/{len(rows)} 个设备"
                                    " · 手柄仅显示系统电池档位，非百分比")
            self._reading_error = ok < len(rows)
        elif kind == "interval":
            self.interval_busy = False
            if success and value[0]:
                self.interval = value[1]
                self._reschedule()
                self.refresh()
                self._notice("刷新间隔已保存")
            else:
                self._notice("保存刷新间隔失败，请重试", error=True)
        elif kind == "autostart":
            self.boot_busy = False
            self._next_observe = 0.0
            if success:
                ok, self.autostart, want = value
                if ok and self.autostart == want:
                    self._notice("已启用开机自启（任务管理器可见）" if want else "已取消开机自启")
                else:
                    self._notice("开机自启设置未生效；已显示实际状态", error=True)
            else:
                self.autostart = None
                self._notice("开机自启设置异常；正在核对实际状态", error=True)
        elif kind == "host":
            self.host_action = None
            self._next_observe = 0.0
            if success:
                ok, self.backend_running = value
                if ok and self.backend_running:
                    self._notice("已请求显示电量环（全屏时仍可能临时隐藏）")
                else:
                    self._notice("电量环启动 / 显示失败，请重试", error=True)
            else:
                self.backend_running = None
                self._notice("电量环启动异常，请重试", error=True)
        elif kind == "quit":
            self.host_action = None
            self._next_observe = 0.0
            if success and value:
                self._quit_until = self._clock() + 10
                self._notice("已请求退出电量环后台；正在确认状态")
            else:
                self._notice("未能发送退出请求；后台可能已退出", error=True)
        elif kind == "links":
            self.links_busy = False
            if success:
                errors = value.get("errors") or []
                if errors:
                    self._notice("桌面链接未全部创建：" + "；".join(errors), error=True)
                elif value.get("folder") or value.get("links"):
                    self._notice("桌面链接已更新（不覆盖其他文件）")
                else:
                    self._notice("桌面链接创建失败", error=True)
            else:
                self._notice("桌面链接创建异常", error=True)
        elif kind == "observe":
            self.observe_busy = False
            if success and value[0] == self._epoch:
                _, self.backend_running, self.autostart = value
                if not self.backend_running:
                    self._quit_until = 0.0
            elif success:
                self._next_observe = 0.0  # A user action superseded this snapshot.
            elif self._observe_epoch == self._epoch:
                self.backend_running = self.autostart = None
            else:
                self._next_observe = 0.0
        self._publish_status()
        self._render_backend()
        self._sync_settings()

    def _pump(self):
        if self.closed:
            return
        # Bound work per turn, even if many asynchronous actions finish together.
        for _ in range(32):
            try:
                result = self.results.get_nowait()
            except queue.Empty:
                break
            self._complete(*result)
        if self._clock() >= self._next_observe:
            self._observe()
        self._publish_status()
        self._render_backend()
        self._schedule("pump", self.PUMP_MS, self._pump)

    def _notice(self, text, error=False):
        self.status_text, self.status_error = text, error
        self._notice_until = self._clock() + 10
        self._publish_status()
        self._sync_settings()

    def _publish_status(self):
        if self._clock() >= self._notice_until:
            self.status_text, self.status_error = self._reading_status, self._reading_error
        status = self.status_text, self.status_error
        if self.panel is not None and status != self._last_status:
            self.panel.set_status(status[0], error=status[1])
            self._last_status = status

    def _render_backend(self):
        if self.host_action == "start":
            text = "正在启动 / 显示电量环…"
        elif self.host_action == "quit":
            text = "正在请求退出电量环后台…"
        elif self.backend_running is None:
            text = "电量环后台状态待确认"
        elif not self.backend_running:
            text = "电量环后台未运行 · 可手动启动"
        elif self._clock() < self._quit_until:
            text = "已请求退出后台，等待结束…"
        else:
            text = "电量环后台运行中 · 可请求显示"
        if self.panel is not None:
            if text != self._last_backend:
                self.panel.set_backend_status(text)
                self._last_backend = text
            state = "disabled" if self.host_action else "normal"
            if state != self._last_float_state:
                self.panel.float_button.configure(state=state)
                self._last_float_state = state
        self._sync_settings()

    def _sync_settings(self):
        if self.settings is not None:
            self.settings.sync()

    def open_settings(self):
        if self.closed:
            return
        if self.settings is None:
            self.settings = SettingsDialog(self)
        else:
            self.settings.window.lift()
            self.settings.window.focus_set()
        self._next_observe = 0.0
        self.settings.sync()

    def _destroyed(self, event):
        if event.widget is self.root:
            self.close(destroy=False)

    def close(self, destroy=True):
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        for name in list(self._jobs):
            self._cancel(name)
        # Daemon workers may be stuck in a driver/COM call. Do not join them or
        # touch Tk from their completion path, and do not quit the shared host.
        if destroy:
            self.root.destroy()


class SettingsDialog:
    """Dark, non-modal settings; mutations only follow an explicit UI action."""
    def __init__(self, controller):
        import tkinter as tk
        from tkinter import ttk
        from fluent_theme import PALETTE as P
        from win_backdrop import style_chrome

        self.controller = c = controller
        p, s = c.panel, c.panel.s
        self.window = win = tk.Toplevel(c.root)
        win.title("设置 · " + c.tc.UI_TITLE)
        win.configure(bg=P["bg"])
        win.transient(c.root)
        win.geometry(f"{s(590)}x{s(550)}")
        win.minsize(s(560), s(530))
        st = ttk.Style(win)
        st.configure("Live.TFrame", background=P["bg"])
        st.configure("Live.TLabel", background=P["bg"], foreground=P["text"], font=p.font(13))
        st.configure("Live.Muted.TLabel", background=P["bg"], foreground=P["secondary"], font=p.font(11))
        st.configure("Live.TCheckbutton", background=P["bg"], foreground=P["text"], font=p.font(13),
                     indicatorbackground=P["surface"], indicatorforeground=P["mint"])
        st.map("Live.TCheckbutton", background=[("active", P["bg"])],
               foreground=[("disabled", P["muted"])])
        st.configure("Live.TButton", background="#253346", foreground=P["text"],
                     borderwidth=0, padding=(s(12), s(8)), font=p.font(12))
        st.map("Live.TButton", background=[("active", "#35465d")], foreground=[("disabled", P["muted"])])
        st.configure("Live.TCombobox", fieldbackground="#223045", background="#223045",
                     foreground=P["text"], arrowcolor=P["secondary"], padding=s(7), font=p.font(12))
        st.map("Live.TCombobox", fieldbackground=[("readonly", "#223045")],
               foreground=[("readonly", P["text"]), ("disabled", P["muted"])])
        body = ttk.Frame(win, padding=s(24), style="Live.TFrame")
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="刷新与后台", style="Live.TLabel", font=p.font(21, True)).pack(anchor="w")
        self.auto_var = tk.BooleanVar(win, value=c.auto)
        ttk.Checkbutton(body, text="自动刷新面板", variable=self.auto_var, style="Live.TCheckbutton",
                        command=lambda: c.set_auto(self.auto_var.get())).pack(anchor="w", pady=(s(20), s(12)))
        row = ttk.Frame(body, style="Live.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="刷新间隔", style="Live.TLabel").pack(side="left", padx=(0, s(18)))
        self.interval_var = tk.StringVar(win)
        self.lookup = {label: seconds for seconds, label in c.tc.INTERVAL_CHOICES}
        self.combo = ttk.Combobox(row, textvariable=self.interval_var, values=list(self.lookup),
                                  state="readonly", width=15, style="Live.TCombobox")
        self.combo.pack(side="left")
        self.combo.bind("<<ComboboxSelected>>", lambda event: c.set_interval(self.lookup[self.interval_var.get()]))
        self.boot_var = tk.BooleanVar(win, value=False)
        self.boot = ttk.Checkbutton(body, text="开机启动电量环（任务管理器可见）", variable=self.boot_var,
                                   style="Live.TCheckbutton", command=lambda: c.set_autostart(self.boot_var.get()))
        self.boot.pack(anchor="w", pady=(s(23), s(10)))
        ttk.Label(body, text="仅操作本项目拥有的启动快捷方式；不会自动修复自启。",
                  style="Live.Muted.TLabel").pack(anchor="w")
        self.backend_var = tk.StringVar(win)
        ttk.Label(body, textvariable=self.backend_var, style="Live.Muted.TLabel").pack(anchor="w", pady=(s(24), s(10)))
        actions = ttk.Frame(body, style="Live.TFrame")
        actions.pack(fill="x")
        self.show = ttk.Button(actions, text="启动 / 显示电量环", command=c.start_host, style="Live.TButton")
        self.show.pack(side="left")
        self.quit = ttk.Button(actions, text="退出电量环后台", command=c.quit_host, style="Live.TButton")
        self.quit.pack(side="left", padx=s(10))
        self.links = ttk.Button(body, text="创建桌面链接", command=c.install_links, style="Live.TButton")
        self.links.pack(anchor="w", pady=(s(16), s(8)))
        ttk.Label(body, text="关闭面板不退出后台；显示请求不会切换为隐藏。",
                  style="Live.Muted.TLabel").pack(anchor="w")
        self.notice = ttk.Label(body, text="", style="Live.Muted.TLabel", wraplength=s(525), justify="left")
        self.notice.pack(fill="x", pady=(s(18), s(8)))
        ttk.Button(body, text="完成", command=self.close, style="Live.TButton").pack(side="bottom", anchor="e")
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda event: self.close())
        win.bind("<Destroy>", self._destroyed, add=True)
        win.update_idletasks()
        style_chrome(win)

    def sync(self):
        c = self.controller
        self.auto_var.set(c.auto)
        self.interval_var.set(dict(c.tc.INTERVAL_CHOICES).get(c.interval, f"{c.interval} 秒"))
        self.combo.configure(state="disabled" if c.interval_busy else "readonly")
        self.boot_var.set(bool(c.autostart))
        self.boot.configure(state="disabled" if c.boot_busy or c.autostart is None else "normal")
        self.show.configure(state="disabled" if c.host_action else "normal")
        self.quit.configure(state="normal" if c.backend_running and not c.host_action else "disabled")
        self.links.configure(state="disabled" if c.links_busy else "normal",
                             text="正在创建…" if c.links_busy else "创建桌面链接")
        self.backend_var.set(c._last_backend or "后台状态待确认")
        self.notice.configure(text=c.status_text, foreground="#f0808b" if c.status_error else "#acb9cb")

    def _destroyed(self, event):
        if event.widget is self.window and self.controller.settings is self:
            self.controller.settings = None

    def close(self):
        self.controller.settings = None
        self.window.destroy()


def build_live_ui(*, debug=False, root_factory=None, panel_factory=None,
                  device_api=None, common_api=None, thread_factory=None):
    """Return the legacy Tk root; injectable dependencies keep tests offline."""
    if device_api is None:
        import personal_devices as device_api
    if common_api is None:
        import tray_common as common_api
    if root_factory is None:
        from tkinter import Tk
        root_factory = Tk
    if panel_factory is None:
        from fluent_panel import FluentPanel
        panel_factory = FluentPanel
    root = root_factory()
    try:
        controller = LiveController(root, device_api=device_api, common_api=common_api,
                                    debug=debug, thread_factory=thread_factory)
        panel = panel_factory(root, preview=False, rows=device_api.empty_readings(),
                              title=common_api.UI_TITLE, on_refresh=controller.refresh,
                              on_float=controller.start_host, on_settings=controller.open_settings)
        controller.start(panel)
        # Keep an explicit lifetime owner and a useful seam for offline tests.
        root.live_controller = controller
        return root
    except Exception:
        root.destroy()
        raise
