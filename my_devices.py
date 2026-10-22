# -*- coding: utf-8 -*-
"""我的三设备电量 —— VXE R1 NK / 洛斐键盘 / 八位堂手柄

用法：
    python my_devices.py            打开面板（默认 60 秒自动刷新）
    python my_devices.py --once     命令行读一次后退出
    python my_devices.py --debug    控制台输出诊断信息（默认不写日志）

只有两台设备能给出真实百分比；手柄只显示系统提供的电池档位，
不把 Windows 合成的 700/1000 冒充 70%。
后台入口只有左下角三个电量环，不显示右下角通知区图标。
"""
from __future__ import annotations

import argparse
import queue
import threading
import time
import traceback

import personal_devices as pd
import tray_common as tc
import win_link

BG = "#1b1c1f"
PANEL = "#232529"
PANEL2 = "#2b2e33"
FG = "#e8e8ea"
MUTED = "#9aa0a6"
FONT = ("Microsoft YaHei UI", 10)

LEVEL_STYLE = [(60, "#5ddc8a"), (30, "#f5c451"), (0, "#ff6b6b")]


def level_color(level) -> str:
    if level is None:
        return MUTED
    for threshold, color in LEVEL_STYLE:
        if level >= threshold:
            return color
    return LEVEL_STYLE[-1][1]


def cli_once() -> int:
    worst = 0
    for reading in pd.read_all():
        if reading.ok:
            print(f"{reading.title}: {reading.display_value}   {reading.detail}")
            if reading.status:
                print(f"    状态：{reading.status}")
            if reading.extra:
                print(f"    {reading.extra}")
        else:
            worst = 1
            print(f"{reading.title}: 读取失败 —— {reading.error}")
    return worst


class DeviceCard:
    def __init__(self, parent, title: str):
        from tkinter import ttk
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=16)
        self.lab_title = ttk.Label(self.frame, text=title, style="CardTitle.TLabel")
        self.lab_title.pack(anchor="w")
        self.lab_sub = ttk.Label(self.frame, text="正在读取…", style="CardSub.TLabel")
        self.lab_sub.pack(anchor="w", pady=(2, 0))
        self.lab_level = ttk.Label(self.frame, text="--", style="CardBig.TLabel")
        self.lab_level.pack(anchor="w", pady=(10, 0))
        self.lab_status = ttk.Label(self.frame, text="", style="CardSub.TLabel")
        self.lab_status.pack(anchor="w")
        self.bar = ttk.Progressbar(self.frame, mode="determinate", maximum=100,
                                   style="Card.Horizontal.TProgressbar")
        self.bar.pack(fill="x", pady=(10, 8))
        self.lab_detail = ttk.Label(self.frame, text="", style="CardSub.TLabel",
                                    wraplength=240, justify="left")
        self.lab_detail.pack(anchor="w")
        self.lab_extra = ttk.Label(self.frame, text="", style="CardSub.TLabel",
                                   wraplength=240, justify="left")
        self.lab_extra.pack(anchor="w", pady=(4, 0))

    def update(self, reading: pd.Reading) -> None:
        self.lab_sub.configure(text=reading.subtitle)
        if reading.ok:
            self.lab_level.configure(text=reading.display_value,
                                     foreground=level_color(reading.level))
            self.lab_status.configure(text=reading.status)
            # Only a measured percentage drives the bar. A coarse category must
            # not be drawn as a precise fill level.
            if reading.level is None:
                self.bar["value"] = 0
            else:
                self.bar["value"] = reading.level
            self.lab_detail.configure(text=reading.detail)
            self.lab_extra.configure(text=reading.extra)
        else:
            self.lab_level.configure(text="--", foreground=MUTED)
            self.lab_status.configure(text="")
            self.bar["value"] = 0
            self.lab_detail.configure(text=reading.error)
            self.lab_extra.configure(text="")


def build_ui():
    import tkinter as tk
    from tkinter import ttk
    import tkinter.messagebox as mb

    root = tk.Tk()
    root.title(tc.UI_TITLE)
    root.configure(bg=BG)
    root.geometry("1010x500")
    root.minsize(940, 470)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, font=FONT)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=PANEL)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG, font=FONT)
    style.configure("H1.TLabel", background=BG, foreground=FG,
                    font=("Microsoft YaHei UI", 15, "bold"))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED,
                    font=("Microsoft YaHei UI", 9))
    style.configure("CardTitle.TLabel", background=PANEL, foreground=FG,
                    font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("CardSub.TLabel", background=PANEL, foreground=MUTED,
                    font=("Microsoft YaHei UI", 9))
    style.configure("CardBig.TLabel", background=PANEL, foreground="#5ddc8a",
                    font=("Microsoft YaHei UI", 34, "bold"))
    style.configure("Card.Horizontal.TProgressbar", background="#4f8cff",
                    troughcolor=PANEL2, borderwidth=0, thickness=8)
    style.configure("TButton", background=PANEL2, foreground=FG, borderwidth=0,
                    focusthickness=0, padding=(14, 7), font=FONT)
    style.map("TButton", background=[("active", "#3a3f47"), ("disabled", "#2a2c30")],
              foreground=[("disabled", "#6b7076")])
    style.configure("Accent.TButton", background="#4f8cff", foreground="#ffffff")
    style.map("Accent.TButton", background=[("active", "#6ea1ff")])
    style.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)
    style.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2,
                    foreground=FG)

    cfg = tc.load_config()
    state = {"busy": False, "auto": True, "interval": int(cfg["interval"]),
             "after_id": None, "closing": False, "starting": False, "started_at": 0.0,
             "notice": "", "notice_until": 0.0}
    results: "queue.Queue[tuple]" = queue.Queue()
    stop_event = threading.Event()
    workers: list[threading.Thread] = []

    head = ttk.Frame(root, padding=(18, 16, 18, 8))
    head.pack(fill="x")
    ttk.Label(head, text=tc.UI_TITLE, style="H1.TLabel").pack(side="left")

    v_auto = tk.BooleanVar(value=True)
    labels = [label for _, label in tc.INTERVAL_CHOICES]
    lookup = dict((label, sec) for sec, label in tc.INTERVAL_CHOICES)
    v_interval = tk.StringVar(value=dict((sec, label) for sec, label in tc.INTERVAL_CHOICES)
                              .get(state["interval"], "1 分钟"))

    btn = ttk.Button(head, text="立即刷新", style="Accent.TButton")
    btn.pack(side="right")
    ttk.Checkbutton(head, text="自动刷新", variable=v_auto,
                    command=lambda: _on_auto()).pack(side="right", padx=(0, 12))

    body = ttk.Frame(root, padding=(18, 4, 18, 4))
    body.pack(fill="both", expand=True)
    for column in range(3):
        body.columnconfigure(column, weight=1, uniform="card")
    body.rowconfigure(0, weight=1)

    cards = {}
    for index, key in enumerate(pd.DEVICE_KEYS):
        card = DeviceCard(body, pd.DEVICE_TITLES[key])
        card.frame.grid(row=0, column=index, sticky="nsew",
                        padx=(0 if index == 0 else 8, 0 if index == 2 else 8))
        cards[key] = card

    foot = ttk.Label(root, text="正在读取…", style="Muted.TLabel", padding=(18, 4))
    foot.pack(fill="x", side="bottom")

    setbar = ttk.Frame(root, style="Panel.TFrame", padding=(14, 10))
    setbar.pack(fill="x", padx=18, pady=(2, 8))
    row_a = ttk.Frame(setbar, style="Panel.TFrame")
    row_a.pack(fill="x")
    row_b = ttk.Frame(setbar, style="Panel.TFrame")
    row_b.pack(fill="x", pady=(9, 0))

    ttk.Label(row_a, text="设置", style="CardTitle.TLabel").pack(side="left")
    ttk.Label(row_a, text="刷新间隔", style="CardSub.TLabel").pack(side="left", padx=(16, 6))
    combo = ttk.Combobox(row_a, textvariable=v_interval, width=8, state="readonly",
                         values=labels)
    combo.pack(side="left")

    v_boot = tk.BooleanVar(value=False)
    ttk.Checkbutton(row_a, text="开机自启（任务管理器可见）", variable=v_boot,
                    command=lambda: _on_boot()).pack(side="left", padx=(18, 0))

    btn_tray = ttk.Button(row_b, text="启动左下角电量环")
    btn_tray.pack(side="right")
    btn_link = ttk.Button(row_b, text="重建桌面链接", command=lambda: _on_links())
    btn_link.pack(side="right", padx=(0, 8))
    lab_tray = ttk.Label(row_b, text="", style="CardSub.TLabel")
    lab_tray.pack(side="right", padx=(0, 12))
    btn_tray.configure(command=lambda: _on_tray_toggle())

    def notify(text: str, seconds: float = 8.0) -> None:
        state["notice"] = text
        state["notice_until"] = time.time() + seconds
        _refresh_tray_state()

    def _refresh_tray_state() -> None:
        if state.get("starting"):
            return
        if state["notice"] and time.time() < state["notice_until"]:
            lab_tray.configure(text=state["notice"])
            btn_tray.configure(state="normal",
                               text="退出电量环后台" if tc.is_tray_running() else "启动左下角电量环")
            return
        running = tc.is_tray_running()
        btn_tray.configure(state="normal",
                           text="退出电量环后台" if running else "启动左下角电量环")
        lab_tray.configure(text="左下角电量环运行中：右键电量环可打开本窗口"
                                if running else "电量环后台未运行：不会后台自动刷新")

    def _reschedule() -> None:
        if state["after_id"]:
            try:
                root.after_cancel(state["after_id"])
            except Exception:
                pass
            state["after_id"] = None
        if state["auto"] and not state["closing"]:
            state["after_id"] = root.after(state["interval"] * 1000, _tick)

    def _tick() -> None:
        _refresh()
        _reschedule()

    def _on_auto() -> None:
        state["auto"] = bool(v_auto.get())
        _reschedule()

    def _on_interval(_event=None) -> None:
        seconds = lookup.get(v_interval.get())
        if seconds is None:
            return
        state["interval"] = seconds
        if not tc.save_config({"interval": seconds, "autostart": cfg.get("autostart", False)}):
            notify("保存刷新间隔失败（配置未改动）", 10)
        tc.post_to_tray(tc.MSG_RELOAD)
        _reschedule()
        _refresh()

    combo.bind("<<ComboboxSelected>>", _on_interval)

    def _on_boot() -> None:
        want = bool(v_boot.get())
        ok = tc.set_autostart(want)
        # Whatever happened, show the real state instead of pretending success.
        actual = tc.is_autostart_enabled()
        v_boot.set(actual)
        if not ok and want:
            notify("开机自启设置失败（可能被其他程序占用或权限不足）", 12)
        elif want and actual:
            notify("已加入开机自启（任务管理器 →「启动」标签可见）", 12)
        elif not want and not actual:
            notify("已取消开机自启")
        else:
            notify("开机自启状态与请求不一致，请重试", 12)
        tc.post_to_tray(tc.MSG_RELOAD)

    def _on_links() -> None:
        btn_link.configure(state="disabled", text="正在创建…")

        def work() -> None:
            try:
                results.put(("links", tc.install_desktop_links()))
            except Exception:
                traceback.print_exc()
                results.put(("links", {"folder": "", "links": {}, "errors": ["桌面链接创建异常"]}))

        thread = threading.Thread(target=work, daemon=True)
        workers.append(thread)
        thread.start()

    def _links_done(result: dict) -> None:
        btn_link.configure(state="normal", text="重建桌面链接")
        bits = []
        if result.get("folder"):
            bits.append(f"项目文件夹→{win_link.KIND_LABEL.get(result['folder'], result['folder'])}")
        for name, kind in (result.get("links") or {}).items():
            bits.append(f"{name}→{win_link.KIND_LABEL.get(kind, kind)}")
        if bits:
            notify("桌面链接已更新：" + "；".join(bits), 12)
        else:
            notify("桌面链接创建失败", 12)
        if result.get("errors"):
            mb.showwarning("桌面链接", "\n".join(result["errors"]))

    def _on_tray_toggle() -> None:
        if tc.is_tray_running():
            tc.post_to_tray(tc.MSG_QUIT)
            for delay in (600, 1500, 3000):
                root.after(delay, _refresh_tray_state)
            return
        state["starting"] = True
        state["started_at"] = time.time()
        btn_tray.configure(state="disabled", text="正在启动…")
        lab_tray.configure(text="正在启动左下角电量环…")

        def work() -> None:
            try:
                results.put(("tray", tc.start_tray_app()))
            except Exception:
                traceback.print_exc()
                results.put(("tray", False))

        thread = threading.Thread(target=work, daemon=True)
        workers.append(thread)
        thread.start()

    def _tray_done(ok: bool) -> None:
        state["starting"] = False
        _refresh_tray_state()
        if not ok:
            notify("电量环启动失败：可双击桌面「电量托盘」（原启动链接）再试", 12)

    def _apply(readings: list[pd.Reading]) -> None:
        for reading in readings:
            card = cards.get(reading.key)
            if card is not None:
                card.update(reading)
        ok = sum(1 for reading in readings if reading.ok)
        foot.configure(text=f"更新于 {time.strftime('%H:%M:%S')}   "
                            f"成功读取 {ok}/{len(readings)} 个设备"
                            + ("" if state["auto"] else "   （自动刷新已关闭）")
                            + "   手柄仅有系统电池档位，无精确百分比")
        state["busy"] = False
        btn.configure(state="normal")

    def _worker() -> None:
        try:
            readings = pd.read_all(stop_event=stop_event)
        except Exception:
            traceback.print_exc()
            readings = [pd.Reading(pd.DEVICE_TITLES[key], "", error="读取异常", key=key)
                        for key in pd.DEVICE_KEYS]
        results.put(("readings", readings))

    def _refresh() -> None:
        if state["busy"] or state["closing"]:
            return
        state["busy"] = True
        btn.configure(state="disabled")
        foot.configure(text="正在读取…")
        thread = threading.Thread(target=_worker, daemon=True)
        workers.append(thread)
        thread.start()

    btn.configure(command=_refresh)

    def _pump() -> None:
        try:
            while True:
                kind, payload = results.get_nowait()
                if kind == "readings":
                    _apply(payload)
                elif kind == "tray":
                    _tray_done(payload)
                elif kind == "links":
                    _links_done(payload)
        except queue.Empty:
            pass
        except Exception:
            traceback.print_exc()
            state["busy"] = False
            state["starting"] = False
            btn.configure(state="normal")
        if state["starting"] and time.time() - state["started_at"] > 20:
            state["starting"] = False
            _refresh_tray_state()
        _refresh_tray_state()
        if not state["closing"]:
            root.after(1000, _pump)

    def _on_close() -> None:
        if state["closing"]:
            return
        state["closing"] = True
        stop_event.set()
        if state["after_id"]:
            try:
                root.after_cancel(state["after_id"])
            except Exception:
                pass
            state["after_id"] = None
        # Do not block the Tcl thread on a stuck driver: workers are daemon
        # threads and will observe stop_event before their next I/O.
        for thread in list(workers):
            if thread.is_alive():
                thread.join(timeout=0.3)
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", _on_close)

    v_boot.set(tc.is_autostart_enabled())
    _refresh_tray_state()
    root.after(300, _refresh)
    _reschedule()
    root.after(1000, _pump)

    def raise_me() -> None:
        try:
            root.lift()
            root.focus_force()
        except Exception:
            pass
    root.after(250, raise_me)
    return root


def main() -> int:
    parser = argparse.ArgumentParser(description="我的三设备电量")
    parser.add_argument("--once", action="store_true", help="命令行读一次就退出")
    parser.add_argument("--debug", action="store_true", help="保持控制台输出")
    parser.add_argument("--install-links", action="store_true", help="显式创建桌面链接")
    parser.add_argument("--autostart", choices=["on", "off", "status"],
                        help="开机自启：on 打开 / off 关闭 / status 查看")
    args = parser.parse_args()

    if args.install_links:
        result = tc.install_desktop_links()
        print("桌面链接：")
        print("  项目文件夹 ->", win_link.KIND_LABEL.get(result["folder"], result["folder"] or "创建失败"))
        for name, kind in result["links"].items():
            print(f"  {name} -> {win_link.KIND_LABEL.get(kind, kind)}")
        for message in result["errors"]:
            print("  注意：", message)
        return 0 if not result["errors"] else 1

    if args.autostart:
        if args.autostart == "status":
            status = tc.autostart_status()
            print("开机自启：", "已启用" if status["enabled"] else "未启用")
            print("  快捷方式：", status["shortcut"],
                  "(存在)" if status["shortcut_exists"] else "(不存在)")
            print("  任务管理器状态：", status["taskmanager_state"])
            print("  目标：", status["target"])
        else:
            ok = tc.set_autostart(args.autostart == "on")
            print("已" + ("加入" if args.autostart == "on" else "取消") + "开机自启",
                  "" if ok else "（失败）")
            return 0 if ok else 1
        return 0

    if args.once:
        return cli_once()

    mutex, first = tc.acquire_instance(tc.UI_MUTEX_NAME)
    if not first:
        tc.close_instance(mutex)
        if not tc.foreground_ui():
            print("界面已经在运行了")
        return 0
    try:
        root = build_ui()
        root.mainloop()
        return 0
    except Exception:
        traceback.print_exc()
        return 1
    finally:
        tc.close_instance(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
