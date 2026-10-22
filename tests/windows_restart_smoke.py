"""Opt-in native background start/hide/exit/restart regression.

Real separate processes/windows/IPC, isolated temporary configuration/namespaces,
synthetic readings and a controlled fullscreen predicate. No device queries,
personal config or startup changes. Real geometry has its own fullscreen smoke.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tray_common as tc


def configure(folder):
    token = folder.name
    tc.CONFIG_PATH = str(folder / "restart-config.json")
    tc.WND_CLASS = "PersonalBattery.RestartSmoke."+token
    tc.MUTEX_NAME = "Local\\"+tc.WND_CLASS
    tc.MSG_SHOW = tc.WND_CLASS+".Show"
    tc.MSG_QUIT = tc.WND_CLASS+".Quit"
    tc.MSG_RELOAD = tc.WND_CLASS+".Reload"


def host(folder):
    configure(folder)
    import corner_widget as cw
    import tray_app as app
    state = {"fullscreen": False}
    class ControlledCorner(cw.CornerWidget):
        WND_CLASS = tc.WND_CLASS+".Corner"
        def __init__(self, *args, **kwargs):
            kwargs["fullscreen_probe"] = lambda hwnd: state["fullscreen"]
            super().__init__(*args, **kwargs)
    cw.CornerWidget = ControlledCorner
    # Ordinary application data flow, but NEVER execute a device provider.
    app.pd.read_all = lambda stop_event=None: [
        app.pd.Reading(app.pd.DEVICE_TITLES[key], "synthetic restart test", key=key,
                       level=80 if key != "gamepad" else None,
                       coarse="中档" if key == "gamepad" else "")
        for key in app.pd.DEVICE_KEYS]
    messages = {name: tc.USER32.RegisterWindowMessageW(tc.WND_CLASS+"."+name)
                for name in ("HideTest", "FullscreenOnTest", "FullscreenOffTest")}
    original = app.TrayApp.wnd_proc
    def wnd_proc(self, hwnd, message, wp, lp):
        if message == messages["HideTest"]:
            self.hide_corner()
            return 0
        if message in (messages["FullscreenOnTest"], messages["FullscreenOffTest"]):
            state["fullscreen"] = message == messages["FullscreenOnTest"]
            if self.corner:
                self.corner._keepalive_tick()
            return 0
        return original(self, hwnd, message, wp, lp)
    app.TrayApp.wnd_proc = wnd_proc
    sys.argv = ["tray_app.py"]
    return app.main()


def wait_for(predicate, description, seconds=6):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError(description)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--host", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.host:
        return host(args.host.resolve())
    if not args.run:
        parser.error("native windows/process interaction requires --run")
    u = tc.USER32
    u.IsWindowVisible.argtypes = [C.c_void_p]
    u.IsWindowVisible.restype = W.BOOL
    u.GetWindowThreadProcessId.argtypes = [C.c_void_p, C.POINTER(W.DWORD)]
    with tempfile.TemporaryDirectory(prefix="personal-battery-restart-") as directory:
        folder = Path(directory).resolve()  # Exact owned temporary cleanup target.
        configure(folder)
        path = Path(tc.CONFIG_PATH)
        initial = {"interval": 60, "autostart": False, "corner": False, "corner_pos": [233, 1697]}
        path.write_text(json.dumps(initial), encoding="utf-8")
        expected = dict(initial, corner=True)
        processes = []
        def launch():
            process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--host", str(folder)], cwd=str(ROOT))
            processes.append(process)
            return process
        def ring():
            return u.FindWindowW(tc.WND_CLASS+".Corner", None)
        def visible():
            hwnd = ring()
            return bool(hwnd and u.IsWindowVisible(hwnd))
        def send(name):
            assert tc.post_to_tray(tc.WND_CLASS+"."+name)
        try:
            first = launch()
            wait_for(visible, "legacy corner=false prevented initial ring creation")
            pid = W.DWORD()
            u.GetWindowThreadProcessId(ring(), C.byref(pid))
            assert pid.value == first.pid
            wait_for(lambda: json.loads(path.read_text(encoding="utf-8")) == expected, "legacy flag not migrated cleanly")
            original_ring = ring()
            original_host = tc.tray_hwnd()
            send("HideTest")
            wait_for(lambda: not visible(), "session hide did not hide actual window")
            before = path.read_bytes()
            assert tc.post_to_tray(tc.MSG_RELOAD)
            u.PostMessageW(original_host, 0x0113, 1, 0)  # Actual refresh route, synthetic provider.
            time.sleep(.7)
            assert not visible() and path.read_bytes() == before
            duplicate = launch()  # Actual main() single-instance path, not a test-only show API.
            assert duplicate.wait(timeout=6) == 0
            wait_for(visible, "repeated ordinary launch did not restore hidden rings")
            assert ring() == original_ring and tc.tray_hwnd() == original_host
            send("HideTest")
            wait_for(lambda: not visible(), "second hide failed")
            assert tc.start_tray_app()  # Existing-host helper must send MSG_SHOW too.
            wait_for(visible, "panel start helper did not restore hidden rings")
            print("PASS legacy flag recovery; session hide stays hidden through refresh/reload; repeat launch/helper restore same process", flush=True)
            send("FullscreenOnTest")
            wait_for(lambda: not visible(), "controlled fullscreen did not suppress ring")
            assert tc.post_to_tray(tc.MSG_SHOW)
            time.sleep(.7)
            assert not visible(), "explicit show bypassed fullscreen suppression"
            send("FullscreenOffTest")
            wait_for(visible, "fullscreen exit did not restore explicitly enabled ring")
            send("HideTest")
            wait_for(lambda: not visible(), "hide before fullscreen transition failed")
            send("FullscreenOnTest")
            send("FullscreenOffTest")
            time.sleep(.7)
            assert not visible(), "fullscreen exit resurrected session-hidden rings"
            assert path.read_bytes() == before
            print("PASS controlled fullscreen still suppresses explicit show; exiting it never resurrects session hide", flush=True)
            assert tc.post_to_tray(tc.MSG_QUIT)
            assert first.wait(timeout=8) == 0
            wait_for(lambda: not tc.is_tray_running(), "first host did not exit")
            assert json.loads(path.read_text(encoding="utf-8")) == expected
            second = launch()
            wait_for(visible, "hide/quit/new-process restart did not restore ring")
            pid = W.DWORD()
            u.GetWindowThreadProcessId(ring(), C.byref(pid))
            assert pid.value == second.pid and second.pid != first.pid
            assert tc.post_to_tray(tc.MSG_QUIT)
            assert second.wait(timeout=8) == 0
            wait_for(lambda: not tc.is_tray_running(), "second host did not exit")
            assert json.loads(path.read_text(encoding="utf-8")) == expected
            print(json.dumps({"result": "PASS", "host_pids": [first.pid, second.pid], "personal_config_touched": False,
                              "real_hardware": False, "fullscreen_predicate": "controlled (not real geometry)"}), flush=True)
        finally:
            if tc.is_tray_running():
                tc.post_to_tray(tc.MSG_QUIT)
            for process in processes:
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()  # Only child processes created and retained above.
                    process.wait(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
