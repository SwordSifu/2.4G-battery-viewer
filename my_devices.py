# -*- coding: utf-8 -*-
"""我的三设备电量 —— VXE R1 NK / 洛斐小顺 Lite / 猎户座 X Pro

用法：
    python my_devices.py            打开 Fluent 面板（默认 60 秒自动刷新）
    python my_devices.py --once     命令行读一次后退出
    python my_devices.py --debug    控制台输出诊断信息（默认不写日志）

只有两台设备能给出真实百分比；手柄只显示系统提供的电池档位，
不把 Windows 合成的 700/1000 冒充 70%。
后台入口只有三个电量环，不显示右下角通知区图标。
"""
from __future__ import annotations

import argparse
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
    """Legacy card export retained for callers; the live UI uses FluentPanel."""
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
            # Coarse categories must never be drawn as precise percentages.
            self.bar["value"] = 0 if reading.level is None else reading.level
            self.lab_detail.configure(text=reading.detail)
            self.lab_extra.configure(text=reading.extra)
        else:
            self.lab_level.configure(text="--", foreground=MUTED)
            self.lab_status.configure(text="")
            self.bar["value"] = 0
            self.lab_detail.configure(text=reading.error)
            self.lab_extra.configure(text="")


def build_ui(*, debug=False):
    from fluent_live import build_live_ui
    return build_live_ui(debug=debug)


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
        # Crisp text and correct layout on scaled displays; the tray process
        # already opts in, so both windows share one coordinate contract.
        import ctypes
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass
    try:
        root = build_ui(debug=args.debug)
        root.mainloop()
        return 0
    except Exception:
        if args.debug:
            traceback.print_exc()
        return 1
    finally:
        tc.close_instance(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
