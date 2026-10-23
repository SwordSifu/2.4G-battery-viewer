"""Independent Fluent preview; no production host/config/hardware integration.

python ui_preview.py
python ui_preview.py --material solid --scene low
The floating preview appears ABOVE the taskbar, not over the deployed rings.
"""
from __future__ import annotations

import argparse
import ctypes
import math
import os
import sys

from fluent_theme import SCENES


class PreviewController:
    def __init__(self, root):
        self.root = root
        self.panel = None
        self.corner = None
        self.closed = False
        self.interval = 60  # Preview-only menu selection, never persisted.

    def update_rows(self, rows):
        if self.corner is not None:
            self.corner.update(rows)

    def hide_float(self):
        if self.corner is not None:
            self.corner.hide()
        if self.panel is not None:
            self.panel.set_float_visible(False)

    def toggle_float(self):
        if self.closed: return False
        if self.corner is None:
            import corner_widget as cw
            import fluent_ring_render as renderer
            from types import SimpleNamespace
            def translucent_render(readings, **options):
                # Layered-ring alpha is real per-pixel transparency, independent
                # of DWM panel materials. Keep crisp text over a quiet dark disc.
                options.update(plate_color=(16, 21, 31), plate_alpha=184, text_color=(245, 248, 252))
                return renderer.render_panel(readings, **options)
            preview_renderer = SimpleNamespace(render_panel=translucent_render, panel_geometry=renderer.panel_geometry)
            class PreviewCorner(cw.CornerWidget):
                WND_CLASS = f"PersonalBattery.FluentPreview.Corner.{os.getpid()}.{id(self)}"
                def _taskbar_band(self):
                    x, y, height = super()._taskbar_band()
                    # Real layered-window preview, never cover production rings.
                    return x+12, y-height-max(12, height//5), height
                def _is_covered_by_taskbar(self):
                    # This preview sits ABOVE, not inside, the taskbar. Shell
                    # Z-order alone cannot cover it; do not upload idle bursts.
                    return False
            corner = PreviewCorner(on_refresh=self.panel.refresh, on_open_panel=self.foreground,
                on_toggle_tray=lambda: None, on_hide=self.hide_float, on_quit=self.close,
                on_toggle_lock=self.toggle_lock, interval_getter=lambda: self.interval,
                on_interval_change=self.set_interval, locked=True, renderer=preview_renderer)
            if not corner.create():
                raise OSError("无法创建电量环预览窗口")
            self.corner = corner
            corner.update(self.panel.rows)
        elif self.corner._visible:
            self.hide_float()
            return False
        else:
            self.corner.update(self.panel.rows)
            self.corner.show()
        return True

    def toggle_lock(self, keep=False):
        if self.corner is not None and not keep:
            self.corner.set_locked(not self.corner.locked)

    def set_interval(self, value):
        self.interval = value

    def foreground(self):
        self.root.deiconify()
        self.root.lift()

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            if self.corner is not None:
                self.corner.destroy()
                self.corner = None
        finally:
            self.root.destroy()


def build_preview(*, material="acrylic", scene="normal", floating=False):
    if sys.platform != "win32":
        raise OSError("此原生视觉预览需要 Windows；纯渲染测试可离线运行")
    # Same coordinate contract as deployed corner; no double DPI scaling.
    ctypes.WinDLL("user32").SetProcessDPIAware()
    import tkinter as tk
    from fluent_panel import FluentPanel
    root = tk.Tk()
    controller = PreviewController(root)
    try:
        panel = FluentPanel(root, material=material, scene=scene,
                            on_scene=controller.update_rows, on_float=controller.toggle_float)
        controller.panel = panel
        root.protocol("WM_DELETE_WINDOW", controller.close)
        root.bind("<Escape>", lambda event: controller.close())
        if floating:
            panel.set_float_visible(controller.toggle_float())
        return root, panel, controller
    except BaseException:
        controller.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--material", choices=("acrylic", "mica", "solid"), default="acrylic")
    parser.add_argument("--scene", choices=tuple(key for key, _ in SCENES), default="normal")
    parser.add_argument("--floating", action="store_true", help="同时显示任务栏上方的独立电量环预览")
    parser.add_argument("--close-after", type=float, help="测试用：指定秒数后关闭，仅0到600秒")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.close_after is not None and (not math.isfinite(args.close_after) or not 0 < args.close_after <= 600):
        parser.error("--close-after 必须为0到600之间的有限秒数")
    root, panel, controller = build_preview(material=args.material, scene=args.scene, floating=args.floating)
    if args.debug:
        print(panel.backdrop, flush=True)
    if args.close_after:
        root.after(round(args.close_after*1000), controller.close)
    root.mainloop()
    controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
