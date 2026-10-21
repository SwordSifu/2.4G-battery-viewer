# -*- coding: utf-8 -*-
"""One shared reader for this user's R1 NK, Lofree keyboard and 8BitDo pad.

Runtime dependencies: Python standard library + system Win32 DLLs only.

Evidence status (2026-10-21):
* Mouse  373B:10C9 FF02:2 17/17 -> real 90%, discharging (sum-checksum reply)
* Keyboard 05AC:024F FF02:2 17/17 -> real 100%, charging indicator off
* Gamepad 2DC8:2030 -> XInput documented API only gives 4 coarse levels.
  The controller exposes no verified exact percentage, so we never invent one.

Do not add speculative protocol scans: unknown devices stay unknown.
"""
from __future__ import annotations

import ctypes as c
import time
from ctypes import wintypes as w
from dataclasses import dataclass
from typing import Optional

from personal_hid import HidChannel, ProtocolError, matches_interface

DEVICE_TITLES = {"mouse": "VXE R1 NK 鼠标", "keyboard": "洛斐键盘", "gamepad": "八位堂手柄"}
DEVICE_KEYS = tuple(DEVICE_TITLES)


@dataclass
class Reading:
    title: str
    subtitle: str
    level: Optional[int] = None
    status: str = ""
    detail: str = ""
    error: str = ""
    extra: str = ""
    key: str = ""
    coarse: str = ""
    updated_at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error and (self.level is not None or bool(self.coarse))

    @property
    def display_value(self) -> str:
        """Only a measured percentage becomes a percentage string."""
        if not self.ok:
            return "--"
        return f"{self.level}%" if self.level is not None else self.coarse


def empty_readings(message: str = "正在读取…") -> list[Reading]:
    return [Reading(title, "", error=message, key=key)
            for key, title in DEVICE_TITLES.items()]


# --------------------------------------------------------------------------
# COMpx receivers (mouse / keyboard)
# --------------------------------------------------------------------------
def read_compx(key: str, interfaces, stop_event=None) -> Reading:
    if key not in ("mouse", "keyboard"):
        raise ValueError("unsupported personal HID key")
    reading = Reading(DEVICE_TITLES[key], "接收器未连接", key=key)
    candidates = [item for item in interfaces if matches_interface(item, key)]
    if not candidates:
        reading.error = "未找到已验证的接收器接口（请确认接收器已插上）"
        return reading
    if len(candidates) != 1:
        reading.error = "发现多个同型号接收器，拒绝随便选一个"
        return reading

    iface = candidates[0]
    reading.subtitle = f"{iface.vid:04X}:{iface.pid:04X} · {iface.product or '无线接收器'}"
    reading.detail = "COMpx 接收器电量查询（加和校验）"
    try:
        with HidChannel(iface, key, stop_event=stop_event) as channel:
            # Battery first: it is the primary datum and the command the
            # vendor UI itself issues on demand. The online query is only an
            # extra annotation and must never consume the battery attempt.
            response = channel.exchange(4, attempts=3)
            online_status = None
            if key == "keyboard":
                # Extra metadata on the keyboard costs another throttled
                # exchange, so it is skipped: the percentage is what matters.
                pass
            else:
                try:
                    online_status = channel.exchange(3, attempts=2)[6]
                except (OSError, ValueError):
                    online_status = None
    except (OSError, ValueError) as exc:
        reading.error = f"读取失败：{exc}"
        return reading

    reading.level = response[6]
    raw_charge = response[7]
    if key == "keyboard":
        # Only the indicator bit is proven for this model; naming further
        # charging sub-states would be a guess.
        reading.status = "充电指示开启" if raw_charge else "充电指示关闭"
    else:
        reading.status = {0: "放电中", 1: "充电中"}.get(raw_charge, "充电状态未知")
    reading.extra = ("来自当前接收器回复，不是蓝牙缓存" if key == "keyboard"
                     else "星闪接收器；非电压估算")
    if online_status is not None and online_status != 1:
        reading.extra += f"；在线标志={online_status}（设备可能已休眠）"
    if key == "mouse" and raw_charge not in (0, 1):
        reading.extra += f"；原始充电字段={raw_charge}"
    reading.updated_at = time.time()
    return reading


# --------------------------------------------------------------------------
# 8BitDo gamepad via documented XInput API (no vtable guessing)
# --------------------------------------------------------------------------
XINPUT_DLLS = ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll")
BATTERY_TYPES = {1: "有线", 2: "碱性电池", 3: "镍氢电池"}
BATTERY_LEVELS = {1: "低档", 2: "中档", 3: "高档"}


class _XInputBattery(c.Structure):
    _fields_ = [("type", c.c_ubyte), ("level", c.c_ubyte)]


def _load_xinput():
    for name in XINPUT_DLLS:
        try:
            dll = c.WinDLL(name)
            fn = dll.XInputGetBatteryInformation
        except (OSError, AttributeError):
            continue
        fn.argtypes = [w.DWORD, c.c_ubyte, c.POINTER(_XInputBattery)]
        fn.restype = w.DWORD
        return fn
    raise OSError("系统没有可用的 XInput DLL")


def read_gamepad(stop_event=None) -> Reading:
    """XInput only reports 4 levels; never convert that into a percentage."""
    reading = Reading(DEVICE_TITLES["gamepad"], "2DC8:2030 · Ultimate 3mode Xbox",
                      key="gamepad", detail="XInput 电池档位（非百分比）",
                      extra="手柄不提供精确百分比；Windows 合成的 700/1000 不是 70%")
    try:
        if stop_event is not None and stop_event.is_set():
            raise OSError("读取已取消")
        fn = _load_xinput()
        found = []
        for slot in range(4):
            battery = _XInputBattery()
            if fn(slot, 0, c.byref(battery)) == 0:
                found.append((slot, battery))
        if not found:
            raise OSError("手柄未连接（无线手柄休眠后需按一下键；有线也需插上）")
        if len(found) > 1:
            raise OSError("检测到多个XInput手柄，无法确认是哪一台")
        _slot, battery = found[0]
        kind = BATTERY_TYPES.get(battery.type)
        level = BATTERY_LEVELS.get(battery.level)
        if kind == "有线":
            reading.status = "有线连接"
            reading.coarse = "有线"
            reading.extra = "有线模式通常不提供电池读数"
        elif level:
            reading.status = kind or "电池"
            reading.coarse = level
            reading.extra = f"{kind}；这只是四档区间，不是精确百分比"
        else:
            raise OSError(f"未知的XInput电池档位（type={battery.type}, level={battery.level}）")
        reading.updated_at = time.time()
    except (OSError, ValueError) as exc:
        reading.error = str(exc)
    return reading


def read_all(stop_event=None) -> list[Reading]:
    """The only normal read path; fixed order keeps UI/tray mapping stable."""
    try:
        import hid_win
        interfaces = hid_win.enumerate_hid()
    except Exception as exc:
        rows = [Reading(DEVICE_TITLES[key], "", error=f"无法枚举HID：{exc}", key=key)
                for key in ("mouse", "keyboard")]
    else:
        rows = [read_compx(key, interfaces, stop_event=stop_event)
                for key in ("mouse", "keyboard")]
    rows.append(read_gamepad(stop_event=stop_event))
    return rows
