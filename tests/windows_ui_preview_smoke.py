"""Opt-in real UI acceptance using synthetic preview data, no production IO.

Pillow is used only for requested screenshot artifacts, never by the application.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ui_preview import build_preview


class Rect(C.Structure):
    _fields_ = [(name, W.LONG) for name in ("left", "top", "right", "bottom")]


class Point(C.Structure):
    _fields_ = [("x", W.LONG), ("y", W.LONG)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--idle-seconds", type=float, default=0)
    parser.add_argument("--idle-background", action="store_true", help="restore user's foreground during the idle interval")
    args = parser.parse_args()
    if not args.run:
        parser.error("real preview windows require --run")
    if not 0 <= args.idle_seconds <= 120:
        parser.error("idle duration must be bounded at 120 seconds")
    if args.out:
        from PIL import ImageGrab
        output = args.out.resolve()
        output.mkdir(parents=True, exist_ok=True)
    root, panel, controller = build_preview(material="acrylic", scene="normal")
    u = C.WinDLL("user32", use_last_error=True)
    k = C.WinDLL("kernel32", use_last_error=True)
    u.GetAncestor.argtypes, u.GetAncestor.restype = [C.c_void_p, W.UINT], C.c_void_p
    u.GetWindowRect.argtypes = [C.c_void_p, C.POINTER(Rect)]
    u.GetCursorPos.argtypes = [C.POINTER(Point)]
    u.WindowFromPoint.argtypes, u.WindowFromPoint.restype = [Point], C.c_void_p
    u.IsChild.argtypes = [C.c_void_p, C.c_void_p]
    u.SetForegroundWindow.argtypes = [C.c_void_p]
    u.GetForegroundWindow.restype = C.c_void_p
    u.IsWindowVisible.argtypes = [C.c_void_p]
    u.GetGuiResources.argtypes, u.GetGuiResources.restype = [C.c_void_p, W.DWORD], W.DWORD
    k.GetCurrentProcess.restype = C.c_void_p
    old_foreground = u.GetForegroundWindow()
    old_cursor = Point()
    u.GetCursorPos(C.byref(old_cursor))
    get_style = u.GetWindowLongPtrW if C.sizeof(C.c_void_p) == 8 else u.GetWindowLongW
    get_style.argtypes, get_style.restype = [C.c_void_p, C.c_int], C.c_ssize_t
    def pump(duration=.35):
        deadline = time.monotonic()+duration
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.015)
    def capture(name):
        if not args.out: return
        root.update_idletasks()
        rect = Rect()
        assert u.GetWindowRect(hwnd, C.byref(rect))
        ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom)).save(output/name)
    def resources():
        return {"gdi": u.GetGuiResources(k.GetCurrentProcess(), 0), "user": u.GetGuiResources(k.GetCurrentProcess(), 1)}
    def foreground():
        root.lift()
        root.update()
        u.SetForegroundWindow(hwnd)
        if u.GetForegroundWindow() != hwnd:
            # Click only our own blank preview header, never production UI.
            point = Point(root.winfo_rootx()+panel.s(450), root.winfo_rooty()+panel.s(95))
            hit = u.WindowFromPoint(point)
            assert hit == hwnd or u.IsChild(hwnd, hit), "unsafe preview activation"
            u.SetCursorPos(point.x, point.y)
            u.mouse_event(2, 0, 0, 0, 0)
            u.mouse_event(4, 0, 0, 0, 0)
        u.SetCursorPos(root.winfo_rootx()+panel.s(450), root.winfo_rooty()+panel.s(500))
        pump()
        assert u.GetForegroundWindow() == hwnd, "foreground preview not established"
    report = {"real_hardware": False, "personal_config_touched": False, "artifacts": []}
    try:
        root.update()
        hwnd = u.GetAncestor(root.winfo_id(), 2)
        assert hwnd
        foreground()
        assert float(root.attributes("-alpha")) == 1
        assert not root.attributes("-transparentcolor")
        assert not get_style(hwnd, -20) & 0x80000, "panel must not use whole-window layered alpha"
        report["backdrop"] = asdict(panel.backdrop)
        for scene in ("normal", "low", "charging", "offline"):
            panel.scene_buttons[scene].invoke()
            pump()
            assert panel.scene == scene
            assert all(r.error for r in panel.rows) if scene == "offline" else panel.rows[2].level is None
            capture(f"{scene}-acrylic.png")
            report["artifacts"].append(f"{scene}-acrylic.png")
        panel.scene_buttons["normal"].invoke()
        for choice, name in (("Mica · 云母", "normal-mica.png"), ("深色实底", "normal-solid.png")):
            panel.material_choice.set(choice)
            panel.material_combo.event_generate("<<ComboboxSelected>>")
            pump()
            if choice == "深色实底":
                assert panel.backdrop.applied == "solid" and panel.client_bg == "#10151f"
            capture(name)
            report["artifacts"].append(name)
        root.geometry(f"{panel.s(940)}x{panel.s(620)}")
        pump(.5)
        controls = [panel.refresh_button, panel.details_button, panel.float_button, panel.material_combo, *panel.scene_buttons.values()]
        for control in controls:
            assert control.winfo_ismapped()
            assert control.winfo_x() >= 0 and control.winfo_x()+control.winfo_width() <= root.winfo_width()+2
            assert control.winfo_y() >= 0 and control.winfo_y()+control.winfo_height() <= root.winfo_height()+2
        capture("minimum-solid.png")
        panel.float_button.invoke()
        pump()
        corner = controller.corner
        assert corner is not None and corner.hwnd and corner._visible
        data, w, h, _ = corner._surface
        assert len(data) == w*h*4
        assert any(0 < data[i] < 255 for i in range(3, len(data), 4))
        assert u.IsWindowVisible(corner.hwnd)
        original_corner_hwnd = corner.hwnd
        panel.float_button.invoke()
        pump(.2)
        assert not corner._visible and not u.IsWindowVisible(corner.hwnd)
        panel.scene_buttons["charging"].invoke()
        pump(.2)
        assert not u.IsWindowVisible(corner.hwnd), "scene refresh resurrected hidden preview"
        panel.float_button.invoke()
        pump(.2)
        assert corner.hwnd == original_corner_hwnd and u.IsWindowVisible(corner.hwnd)
        panel.scene_buttons["normal"].invoke()
        panel.details_button.invoke()
        pump(.2)
        assert panel._details.winfo_exists()
        panel._details.destroy()
        panel._details = None
        panel.material_choice.set("Acrylic · 磨砂")
        panel.material_combo.event_generate("<<ComboboxSelected>>")
        root.geometry(f"{panel.s(1100)}x{panel.s(670)}")
        pump(.5)
        foreground()
        def cycles(times):
            for _ in range(times):
                for key in ("low", "offline", "charging", "normal"):
                    panel.scene_buttons[key].invoke()
                    root.update()
        cycles(4)              # Bounded caches warm to their steady state.
        before = resources()
        cycles(4)
        after = resources()
        assert after["gdi"] <= before["gdi"]+1, ("GDI growth at steady state", before, after)
        assert after["user"] <= before["user"]+1, ("USER growth at steady state", before, after)
        report["scene_cycles"] = {"before": before, "after": after}
        if args.idle_seconds:
            if args.idle_background:
                root.lower()
                if old_foreground:
                    u.SetForegroundWindow(old_foreground)
            pump(.5)
            cpu, started = time.process_time(), time.monotonic()
            root.after(round(args.idle_seconds*1000), root.quit)
            root.mainloop()
            elapsed, consumed = time.monotonic()-started, time.process_time()-cpu
            report["idle"] = {"seconds": round(elapsed, 2), "cpu_seconds": round(consumed, 5), "resources": resources(),
                "background": args.idle_background, "completed": elapsed >= args.idle_seconds*.95, "preview_closed": controller.closed}
            if elapsed < args.idle_seconds*.95:
                report["idle"]["note"] = "interrupted; this is NOT an accepted idle benchmark"
        report["result"] = "PASS"
    finally:
        controller.close()
        u.SetCursorPos(old_cursor.x, old_cursor.y)
        if old_foreground: u.SetForegroundWindow(old_foreground)
    print(json.dumps(report, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
