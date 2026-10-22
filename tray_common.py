# -*- coding: utf-8 -*-
"""Personal three-device fork: configuration, owned shortcuts and window IPC.

Importing this module does not read hardware, start processes or install anything.
Only explicit set_autostart/install_desktop_links calls change system entries.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes

import win_shell

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "personal_tray_config.json")
APP_NAME = "PersonalThreeDeviceBattery"
WND_CLASS = "PersonalThreeDeviceBattery.Tray.v1"
MUTEX_NAME = "Local\\PersonalThreeDeviceBattery.Tray.v1"
UI_MUTEX_NAME = "Local\\PersonalThreeDeviceBattery.Panel.v1"
UI_TITLE = "我的三设备电量 · Personal"
MSG_QUIT = "PersonalThreeDeviceBattery.Quit.v1"
MSG_RELOAD = "PersonalThreeDeviceBattery.Reload.v1"
MSG_SHOW = "PersonalThreeDeviceBattery.Show.v1"
DEFAULT_CONFIG = {"interval": 60, "autostart": False,
                  "corner": True, "corner_pos": None}
INTERVAL_CHOICES = [(30, "30 秒"), (60, "1 分钟"), (120, "2 分钟"),
                    (300, "5 分钟"), (600, "10 分钟"), (1800, "30 分钟")]
STARTUP_LNK_NAME = "我的三设备电量-Personal-托盘.lnk"
DESKTOP_LINK_NAME = "我的三设备电量-Personal-项目.lnk"
LAUNCHER_NAMES = ["我的设备电量.bat", "电量托盘.bat"]
DESKTOP_SHORTCUTS = {"我的三设备电量-Personal-面板.lnk": "my_devices.py",
                     "我的三设备电量-Personal-托盘.lnk": "tray_app.py"}
STARTUP_APPROVED_FOLDER = (r"Software\Microsoft\Windows\CurrentVersion"
                           r"\Explorer\StartupApproved\StartupFolder")
USER32 = ctypes.WinDLL("user32", use_last_error=True)
KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
USER32.FindWindowW.restype = ctypes.c_void_p
USER32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
USER32.PostMessageW.restype = wintypes.BOOL
USER32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                               ctypes.c_size_t, ctypes.c_ssize_t]
USER32.RegisterWindowMessageW.restype = wintypes.UINT
USER32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
USER32.ShowWindow.restype = wintypes.BOOL
USER32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
USER32.SetForegroundWindow.restype = wintypes.BOOL
USER32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
KERNEL32.CreateMutexW.restype = ctypes.c_void_p
KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
KERNEL32.CloseHandle.restype = wintypes.BOOL
KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]


def validate_config(data) -> dict:
    clean = dict(DEFAULT_CONFIG)
    if isinstance(data, dict):
        value = data.get("interval")
        if type(value) is int and 30 <= value <= 1800:
            clean["interval"] = value
        if type(data.get("autostart")) is bool:
            clean["autostart"] = data["autostart"]
        if type(data.get("corner")) is bool:
            clean["corner"] = data["corner"]
        pos = data.get("corner_pos")
        # Saved corner-indicator position; reject anything that is not a
        # plain pair of ints, so a corrupt config cannot place it off-screen.
        if (isinstance(pos, list) and len(pos) == 2
                and all(type(v) is int and -20000 < v < 20000 for v in pos)):
            clean["corner_pos"] = [pos[0], pos[1]]
    return clean


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as stream:
            return validate_config(json.load(stream))
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> bool:
    """Atomic replacement: invalid values default, failures leave prior file intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=os.path.dirname(CONFIG_PATH),
                                         prefix=".personal-config-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = stream.name
            json.dump(validate_config(cfg), stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG_PATH)
        temporary = None
        return True
    except OSError:
        return False
    finally:
        if temporary is not None:
            # This exact absolute path was allocated by NamedTemporaryFile above.
            try:
                os.unlink(temporary)
            except OSError:
                pass


def pythonw_path() -> str:
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(candidate):
        raise OSError("未找到 pythonw.exe；请使用完整 Windows Python 安装")
    return candidate


def tray_script() -> str:
    return os.path.join(BASE_DIR, "tray_app.py")


def _arguments(script: str) -> str:
    return subprocess.list2cmdline([script])


def autostart_command() -> str:
    return subprocess.list2cmdline([pythonw_path(), tray_script()])


def startup_dir() -> str:
    return win_shell.startup_dir()


def desktop_dir() -> str:
    return win_shell.desktop_dir()


def startup_lnk() -> str:
    return os.path.join(startup_dir(), STARTUP_LNK_NAME)


def _same_path(left: str, right: str) -> bool:
    return bool(left and right and os.path.normcase(os.path.abspath(left)) ==
                os.path.normcase(os.path.abspath(right)))


def _owned_shortcut(path: str, target: str, arguments: str) -> bool:
    """Refuse symlinks and foreign/invalid ordinary files, even with our name."""
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    got = win_shell.read_shortcut(path)
    return bool(got and _same_path(got.get("target", ""), target) and
                got.get("arguments", "") == arguments and
                _same_path(got.get("workdir", ""), BASE_DIR))


def _startup_approved():
    """Read ONLY our StartupFolder entry. Unknown format is not called enabled."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_FOLDER,
                            0, winreg.KEY_READ) as key:
            data, _ = winreg.QueryValueEx(key, STARTUP_LNK_NAME)
        if isinstance(data, bytes) and len(data) == 12:
            if data[0] == 2:
                return True
            if data[0] == 3:
                return False
        return False
    except FileNotFoundError:
        return None  # Windows has not recorded it: startup-folder default.
    except OSError:
        return False


def is_autostart_enabled() -> bool:
    try:
        return (_owned_shortcut(startup_lnk(), pythonw_path(),
                                _arguments(tray_script())) and
                _startup_approved() is not False)
    except OSError:
        return False


def _create_owned_shortcut(path: str, target: str, arguments: str = "") -> bool:
    if os.path.lexists(path):
        # Idempotent, never overwrite a file whose ownership cannot be established.
        return _owned_shortcut(path, target, arguments)
    return bool(win_shell.create_shortcut(path, target, arguments=arguments,
                                         workdir=BASE_DIR,
                                         description="Personal 三设备电量") and
                _owned_shortcut(path, target, arguments))


def set_autostart(enabled: bool) -> bool:
    """Explicit user action only. No Run/StartupApproved writes or migrations."""
    if type(enabled) is not bool:
        return False
    path = startup_lnk()
    try:
        target, arguments = pythonw_path(), _arguments(tray_script())
        if enabled:
            return (_create_owned_shortcut(path, target, arguments) and
                    is_autostart_enabled())
        if not os.path.lexists(path):
            return True
        if not _owned_shortcut(path, target, arguments):
            return False
        # Verified exact owned shortcut path, not a caller-supplied deletion target.
        return bool(win_shell.remove_shortcut(path) and not os.path.lexists(path))
    except OSError:
        return False


def autostart_status() -> dict:
    path = startup_lnk()
    approved = _startup_approved()
    try:
        target = autostart_command()
    except OSError as exc:
        target = str(exc)
    return {"enabled": is_autostart_enabled(), "shortcut": path,
            "shortcut_exists": os.path.lexists(path), "target": target,
            "method": "本 fork 启动文件夹快捷方式（不写注册表）",
            "taskmanager_state": ("已被任务管理器禁用/状态不可确认" if approved is False
                                  else "已启用" if approved else "未记录（默认启用）")}


def install_desktop_links() -> dict:
    """Explicit creation of independent .lnk names; never replace user files."""
    result = {"folder": "", "links": {}, "errors": []}
    try:
        folder = desktop_dir()
        if _create_owned_shortcut(os.path.join(folder, DESKTOP_LINK_NAME), BASE_DIR):
            result["folder"] = "shortcut"
        else:
            result["errors"].append("项目快捷方式已被其他文件占用或创建失败")
        for name, script in DESKTOP_SHORTCUTS.items():
            if _create_owned_shortcut(os.path.join(folder, name), pythonw_path(),
                                      _arguments(os.path.join(BASE_DIR, script))):
                result["links"][name] = "shortcut"
            else:
                result["errors"].append(f"{name} 已被其他文件占用或创建失败")
    except OSError as exc:
        result["errors"].append(str(exc))
    return result


def acquire_instance(name: str) -> tuple[int, bool]:
    ctypes.set_last_error(0)
    handle = KERNEL32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "创建单实例锁失败")
    return int(handle), ctypes.get_last_error() != 183


def close_instance(handle: int) -> None:
    if handle:
        KERNEL32.CloseHandle(handle)


def tray_hwnd() -> int:
    return int(USER32.FindWindowW(WND_CLASS, None) or 0)


def ui_hwnd() -> int:
    return int(USER32.FindWindowW(None, UI_TITLE) or 0)


def is_tray_running() -> bool:
    return bool(tray_hwnd())


def post_to_tray(name: str) -> bool:
    hwnd = tray_hwnd()
    return bool(hwnd and USER32.PostMessageW(
        hwnd, USER32.RegisterWindowMessageW(name), 0, 0))


def foreground_ui() -> bool:
    hwnd = ui_hwnd()
    if not hwnd:
        return False
    USER32.ShowWindow(hwnd, 9)
    USER32.SetForegroundWindow(hwnd)
    return True


def start_ui() -> bool:
    if foreground_ui():
        return True
    try:
        subprocess.Popen([pythonw_path(), os.path.join(BASE_DIR, "my_devices.py")],
                         cwd=BASE_DIR, creationflags=0x08000000)
        return True  # Panel's own mutex closes any simultaneous launch race.
    except OSError:
        return False


def start_tray_app(wait_timeout: float = 6.0) -> bool:
    """Explicit start/show request, not a passive running-state check."""
    if is_tray_running():
        return post_to_tray(MSG_SHOW)
    try:
        subprocess.Popen([pythonw_path(), tray_script()], cwd=BASE_DIR,
                         creationflags=0x08000000)
    except OSError:
        return False
    deadline = time.monotonic() + max(0, wait_timeout)
    # Bounded startup check, not an idle polling loop. Never terminate a process.
    while time.monotonic() < deadline:
        if is_tray_running():
            return post_to_tray(MSG_SHOW)
        time.sleep(0.1)
    return bool(is_tray_running() and post_to_tray(MSG_SHOW))
