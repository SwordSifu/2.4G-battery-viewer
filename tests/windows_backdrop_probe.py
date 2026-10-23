"""Opt-in measured CLIENT-pixel probe using ONLY temporary owned Tk windows.

    python tests/windows_backdrop_probe.py --run

Uses already-installed Pillow for test screenshots only; never installs it.
No app imports, hardware/config/startup access, other-process HWND mutation,
whole-window alpha, color keys, undocumented APIs or asynchronous timers.
Dark/bright/dark background control is checked on-screen. Both bare Tk and
black Canvas gaps are sampled well INSIDE the client, plus a painted-card control.
A normal Canvas fill may have zero GDI alpha and add RGB to an extended frame;
the card control is not assumed to be truly opaque.
Acrylic response is evidence of backdrop visibility, not proof of blur quality.
Mica intentionally samples wallpaper, so no response to an underlying window
is not by itself evidence that Mica failed. HRESULTs alone prove neither effect.
All temporary windows are destroyed in finally, even if the probe fails.
"""
import argparse
import ctypes as C
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import win_backdrop as wb


def _settle(root, dwm):
    # Bounded synchronous paint/compose samples, not a persistent timer/event loop.
    for _ in range(6):
        root.update()
        dwm.DwmFlush()
        time.sleep(0.08)
    root.update()
    dwm.DwmFlush()


def _mean(image, x, y):
    from PIL import ImageStat
    return tuple(round(value, 2) for value in
                 ImageStat.Stat(image.crop((x - 8, y - 8, x + 8, y + 8))).mean[:3])


def _delta(a, b):
    return round(sum(abs(x - y) for x, y in zip(a, b)) / 3, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--selective", action="store_true", help="extend only header/gutters; verify ordinary card colors")
    parser.add_argument("--summary", action="store_true", help="print compact measured evidence")
    args = parser.parse_args()
    if not args.run:
        parser.error("temporary real desktop windows require --run")
    if sys.platform != "win32":
        parser.error("this probe is Windows-only")
    try:
        import tkinter as tk
        from PIL import ImageGrab
    except ImportError as error:
        parser.error("probe requires existing Tk/Pillow; do not install: " + str(error))
    native = wb._load_native()
    u, dwm = native.user, native.dwm
    u.SetProcessDPIAware()
    u.GetForegroundWindow.restype = C.c_void_p
    u.GetForegroundWindow.argtypes = []
    u.SetForegroundWindow.argtypes = [C.c_void_p]
    u.SetForegroundWindow.restype = C.c_int32
    dwm.DwmFlush.argtypes = []
    dwm.DwmFlush.restype = C.c_int32
    class Point(C.Structure):
        _fields_ = [("x", C.c_int32), ("y", C.c_int32)]
    u.WindowFromPoint.argtypes = [Point]
    u.WindowFromPoint.restype = C.c_void_p
    u.IsChild.argtypes = [C.c_void_p, C.c_void_p]
    u.IsChild.restype = C.c_int32
    u.GetCursorPos.argtypes = [C.POINTER(Point)]
    u.SetCursorPos.argtypes = [C.c_int32, C.c_int32]
    u.mouse_event.argtypes = [C.c_uint32, C.c_uint32, C.c_uint32, C.c_uint32, C.c_size_t]
    old_cursor = Point()
    u.GetCursorPos(C.byref(old_cursor))
    old_foreground = u.GetForegroundWindow()
    background = target = None
    report = {"build": wb._windows_build(), "records": [], "temporary_windows_destroyed": False}
    try:
        background = tk.Tk()
        background.title("Temporary backdrop pixel-control background")
        background.overrideredirect(True)
        background.geometry("720x480+80+80")
        background.configure(background="#ffffff")
        background.attributes("-topmost", True)
        # Separate Tk interpreter/root: the control must not be an owned popup
        # which DWM may exclude from its owner's backdrop sample.
        target = tk.Tk()
        target.title("Temporary Tk CLIENT backdrop probe")
        target.geometry("420x260+220+180")
        target.resizable(False, False)
        target.attributes("-topmost", True)
        background.update()
        target.lift()
        target.focus_force()
        target.update()
        target_hwnd = native.hwnd(target)
        report["owned_target_hwnd"] = target_hwnd
        for mode in ("bare-tk", "canvas"):
            for child in target.winfo_children():
                child.destroy()
            if mode == "canvas":
                surface = tk.Canvas(target, background="#000000", highlightthickness=0, borderwidth=0)
                surface.pack(fill="both", expand=True)
                surface.create_rectangle(220, 30, 390, 230, fill=wb.SOLID_BACKGROUND, outline="")
            else:
                surface = None
                card = tk.Frame(target, background=wb.SOLID_BACKGROUND, borderwidth=0)
                card.place(x=220, y=30, width=170, height=200)
            for kind, force_solid in (("acrylic", True), ("acrylic", False), ("mica", False)):
                result = wb.apply_backdrop(target, kind=kind, force_solid=force_solid,
                    frame_margins=(16, 16, 60, 16) if args.selective else None)
                if surface is not None:
                    surface.configure(background=result.client_background)
                target.lift()
                target.focus_force()
                _settle(background, dwm)
                samples = []
                for color in ("#000000", "#ffffff", "#000000"):
                    background.configure(background=color)
                    target.lift()
                    target.focus_force()
                    activation_accepted = bool(u.SetForegroundWindow(target_hwnd))
                    if u.GetForegroundWindow() != target_hwnd:
                        # Give this OWN blank test window a normal user-input
                        # foreground grant, without changing Windows lock policy.
                        point = Point(target.winfo_rootx()+60, target.winfo_rooty()+120)
                        hit = u.WindowFromPoint(point)
                        assert hit == target_hwnd or u.IsChild(target_hwnd, hit), "unsafe probe click target"
                        u.SetCursorPos(point.x, point.y)
                        u.mouse_event(2, 0, 0, 0, 0)
                        u.mouse_event(4, 0, 0, 0, 0)
                        target.update()
                    u.SetCursorPos(target.winfo_rootx()+80, target.winfo_rooty()+210)
                    _settle(background, dwm)
                    left, top = target.winfo_rootx(), target.winfo_rooty()
                    image = ImageGrab.grab(bbox=(left, top, left + 420, top + 260))
                    bx, by = background.winfo_rootx(), background.winfo_rooty()
                    control = ImageGrab.grab(bbox=(bx + 16, by + 16, bx + 48, by + 48))
                    samples.append({"background": color,
                                    "client_gap_rgb": _mean(image, 60, 26 if args.selective else 120),
                                    "painted_card_rgb": _mean(image, 300, 120),
                                    "exposed_background_rgb": _mean(control, 16, 16),
                                    "own_window_activation_accepted": activation_accepted,
                                    "target_is_foreground": u.GetForegroundWindow() == target_hwnd})
                gap_delta = _delta(samples[0]["client_gap_rgb"], samples[1]["client_gap_rgb"])
                card_delta = _delta(samples[0]["painted_card_rgb"], samples[1]["painted_card_rgb"])
                background_delta = _delta(samples[0]["exposed_background_rgb"], samples[1]["exposed_background_rgb"])
                repeat_delta = _delta(samples[0]["client_gap_rgb"], samples[2]["client_gap_rgb"])
                controlled = background_delta > 220 and all(s["target_is_foreground"] for s in samples)
                responding = controlled and gap_delta >= 5 and repeat_delta <= 3
                report["records"].append({"surface": mode, "kind": kind, "forced_solid": force_solid,
                                          "result": asdict(result), "samples": samples,
                                          "client_gap_delta": gap_delta, "painted_card_delta": card_delta,
                                          "background_control_delta": background_delta,
                                          "repeat_dark_delta": repeat_delta,
                                          "background_control_valid": background_delta > 220,
                                          "control_valid": controlled,
                                          "client_responds_to_background": responding,
                                          "interpretation": (
                                              "controlled client response observed" if responding else
                                              "activation/control unavailable; client glass unverified" if not controlled else
                                              "no controlled client response; never infer glass from API success")})
        wb.restore_backdrop(target)
    finally:
        try:
            if target is not None:
                target.destroy()
        finally:
            if background is not None:
                background.destroy()
            report["temporary_windows_destroyed"] = True
            u.SetCursorPos(old_cursor.x, old_cursor.y)
            if old_foreground:
                u.SetForegroundWindow(old_foreground)
    if args.summary:
        report["records"] = [{"surface": r["surface"], "kind": r["kind"], "solid": r["forced_solid"],
            "controlled": r["control_valid"], "gap_delta": r["client_gap_delta"],
            "responds": r["client_responds_to_background"],
            "gap_colors": [s["client_gap_rgb"] for s in r["samples"]],
            "card_colors": [s["painted_card_rgb"] for s in r["samples"]],
            "hresults": [s["hresult"] for s in r["result"]["api_status"]]} for r in report["records"]]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
