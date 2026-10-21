# -*- coding: utf-8 -*-
"""ring_render.py —— 纯 Python 渲染彩色环状电量指示（零依赖）

输出是预乘 alpha 的 BGRA 像素，可直接喂给 UpdateLayeredWindow，
因此不需要 tkinter、不需要 PIL，也不产生任何临时文件。

一个设备一个环：
* 真实百分比 → 12 点方向起顺时针填充，颜色随剩余量变化
* 手柄只有四档 → 画成 4 段离散弧，亮几段就是第几档（不伪装成百分比）
* 读取失败   → 整圈暗色 + 中间一条横线

抗锯齿用解析覆盖率算，不做超采样：每像素常数次浮点运算。

实现要点：填充弧和轨道底色在**同一遍**循环里算出来再合成，
避免两遍绘制互相覆盖（这正是之前粗档全黑的 bug）。
"""
from __future__ import annotations

import math

COLORS = {
    "green": (93, 220, 138),
    "lime": (160, 214, 79),
    "amber": (245, 196, 81),
    "orange": (255, 159, 67),
    "red": (255, 92, 92),
    "coarse": (120, 178, 245),
    "error": (108, 112, 120),
}
TRACK = (58, 62, 70)
PLATE = (24, 26, 30)
PLATE_ALPHA = 232
SEG_GAP = 0.055          # 四段弧之间的缝（占每段的比例）

DIGITS = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "-": ("000", "000", "111", "000", "000"),
}


def ring_color(level):
    if level is None:
        return COLORS["coarse"]
    if level >= 60:
        return COLORS["green"]
    if level >= 40:
        return COLORS["lime"]
    if level >= 25:
        return COLORS["amber"]
    if level >= 10:
        return COLORS["orange"]
    return COLORS["red"]


def coarse_steps(coarse: str) -> int:
    return {"极低档": 1, "低档": 2, "中档": 3, "高档": 4}.get(coarse or "", 0)


def ring_ratio(reading) -> tuple:
    """('percent',0..1) / ('coarse',1..4) / ('error',0)"""
    if getattr(reading, "error", ""):
        return "error", 0
    level = getattr(reading, "level", None)
    if level is not None:
        return "percent", max(0.0, min(1.0, level / 100.0))
    steps = coarse_steps(getattr(reading, "coarse", ""))
    if steps:
        return "coarse", steps
    return "error", 0


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


class Canvas:
    def __init__(self, width: int, height: int):
        self.w, self.h = width, height
        self.buf = bytearray(width * height * 4)

    def _blend(self, index: int, color, alpha: float) -> None:
        if alpha <= 0.0:
            return
        if alpha > 1.0:
            alpha = 1.0
        b = self.buf
        da = b[index + 3] / 255.0
        out_a = alpha + da * (1.0 - alpha)
        if out_a <= 0.0:
            return
        for c in range(3):
            sc = color[c]
            dc = b[index + c]
            b[index + c] = int(round((sc * alpha + dc * da * (1.0 - alpha)) / out_a))
        b[index + 3] = int(round(out_a * 255.0))

    def plate(self, radius: int, color=None, alpha: float = None) -> None:
        """圆角底板。color/alpha 可覆盖，用来和任务栏同色融合。"""
        w, h, r = self.w, self.h, max(0, radius)
        base = PLATE if color is None else color
        a = PLATE_ALPHA if alpha is None else alpha
        if a <= 0:
            return
        for y in range(h):
            for x in range(w):
                dx = min(x, w - 1 - x)
                dy = min(y, h - 1 - y)
                cover = 1.0
                if dx < r and dy < r:
                    cover = _clamp01(r - math.hypot(r - dx, r - dy) + 0.5)
                if cover > 0.0:
                    self._blend((y * w + x) * 4, base, cover * (a / 255.0))

    def ring(self, cx: float, cy: float, radius: float, thickness: float,
             color, mode: str, value) -> None:
        """一遍算完填充弧与轨道，互不覆盖。"""
        r_out = radius
        r_in = radius - thickness
        x0, x1 = max(0, int(cx - r_out - 2)), min(self.w, int(cx + r_out + 3))
        y0, y1 = max(0, int(cy - r_out - 2)), min(self.h, int(cy + r_out + 3))
        tau = 2.0 * math.pi
        for y in range(y0, y1):
            dy = y + 0.5 - cy
            for x in range(x0, x1):
                dx = x + 0.5 - cx
                rr = math.hypot(dx, dy)
                if rr < r_in - 1.5 or rr > r_out + 1.5:
                    continue
                radial = _clamp01(min(r_out - rr, rr - r_in) + 0.5)
                if radial <= 0.0:
                    continue
                angle = math.atan2(dx, -dy) / tau
                if angle < 0.0:
                    angle += 1.0
                arc = tau * max(rr, 1.0)

                fill = 0.0          # 彩色填充覆盖率
                track = 1.0         # 轨道覆盖率
                if mode == "percent":
                    fill = _clamp01((value - angle) * arc + 0.5)
                    track = 1.0 - fill
                elif mode == "coarse":
                    seg_f = angle * 4.0
                    seg = min(3, int(seg_f))
                    frac = seg_f - seg
                    in_gap = frac < SEG_GAP or frac > 1.0 - SEG_GAP
                    if in_gap:
                        continue        # 段间缝隙：留空，不画任何东西
                    # 段内侧边界抗锯齿
                    edge = min(frac - SEG_GAP, (1.0 - SEG_GAP) - frac) * (arc / 4.0)
                    cov = _clamp01(edge + 0.5)
                    if seg < value:
                        fill = cov
                        track = 0.0
                    else:
                        fill = 0.0
                        track = cov
                else:
                    continue

                if fill > 0.0:
                    self._blend((y * self.w + x) * 4, color, radial * fill)
                if track > 0.0:
                    self._blend((y * self.w + x) * 4, TRACK, radial * track)

    def error_mark(self, cx: float, cy: float, scale: float) -> None:
        half = max(3.0, 6.5 * scale)
        thick = max(1.0, 1.3 * scale)
        for y in range(int(cy - thick - 2), int(cy + thick + 2)):
            cov_y = _clamp01(thick - abs(y + 0.5 - cy) + 0.5)
            if cov_y <= 0.0:
                continue
            for x in range(int(cx - half - 2), int(cx + half + 2)):
                if abs(x + 0.5 - cx) <= half:
                    self._blend((y * self.w + x) * 4, COLORS["error"], cov_y)

    def text(self, cx: float, cy: float, text: str, color, dot: int) -> None:
        if not text:
            return
        dot = max(1, int(dot))
        char_w = 3 * dot
        gap = dot
        total_w = len(text) * char_w + (len(text) - 1) * gap
        ox = cx - total_w / 2.0
        oy = cy - (5 * dot) / 2.0
        for index, ch in enumerate(text):
            glyph = DIGITS.get(ch)
            if not glyph:
                continue
            gx = ox + index * (char_w + gap)
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit != "1":
                        continue
                    for yy in range(dot):
                        for xx in range(dot):
                            px = int(gx + col * dot + xx)
                            py = int(oy + row * dot + yy)
                            if 0 <= px < self.w and 0 <= py < self.h:
                                self._blend((py * self.w + px) * 4, color, 1.0)

    def premultiplied_bgra(self) -> bytes:
        buf = self.buf
        out = bytearray(len(buf))
        for i in range(0, len(buf), 4):
            a = buf[i + 3]
            if a == 0:
                continue
            if a == 255:
                out[i], out[i + 1], out[i + 2], out[i + 3] = \
                    buf[i + 2], buf[i + 1], buf[i], 255
            else:
                out[i] = (buf[i + 2] * a) // 255
                out[i + 1] = (buf[i + 1] * a) // 255
                out[i + 2] = (buf[i] * a) // 255
                out[i + 3] = a
        return bytes(out)


def panel_geometry(scale: float = 1.0, height_px: int = 0) -> dict:
    """面板几何。

    height_px 给定时按该像素高度铺满（用于贴进任务栏），
    否则按 scale 推导一个默认尺寸。
    """
    if height_px and height_px > 0:
        pad = max(2.0, height_px * 0.06)
        d = max(8.0, height_px - 2 * pad)
        gap = max(2.0, d * 0.16)
        width = 3 * d + 2 * gap + 2 * pad
        thickness = max(2.0, d * 0.11)
        return {"width": int(round(width)), "height": int(round(height_px)),
                "radius": d / 2 - thickness / 2, "thickness": thickness,
                "centers": [pad + d / 2 + i * (d + gap) for i in range(3)],
                "cy": height_px / 2.0, "pad": pad, "diameter": d}
    d = 60 * scale
    gap = 8 * scale
    pad = 7 * scale
    width = 3 * d + 2 * gap + 2 * pad
    height = d + 2 * pad
    return {"width": int(round(width)), "height": int(round(height)),
            "radius": d / 2 - 1, "thickness": 6 * scale,
            "centers": [pad + d / 2 + i * (d + gap) for i in range(3)],
            "cy": height / 2.0, "pad": pad, "diameter": d}


def render_panel(readings, scale: float = 1.0, height_px: int = 0,
                 plate_color=None, plate_alpha=None, text_color=None) -> tuple:
    """渲染三设备环状面板，返回 (预乘BGRA, width, height)。

    plate_color / plate_alpha / text_color 留空则用深色默认主题；
    贴任务栏时由调用方传入采样到的任务栏颜色，让底板与任务栏融为一体。
    """
    geo = panel_geometry(scale, height_px)
    canvas = Canvas(geo["width"], geo["height"])
    canvas.plate(radius=max(2, int(round(geo["height"] * 0.22))),
                 color=plate_color, alpha=plate_alpha)
    rows = list(readings)[:3]
    while len(rows) < 3:
        rows.append(None)
    dot = max(2, int(round(geo["diameter"] / 24.0)))
    for index, reading in enumerate(rows):
        cx, cy = geo["centers"][index], geo["cy"]
        if reading is None:
            mode, value, color = "error", 0, COLORS["error"]
        else:
            mode, value = ring_ratio(reading)
            color = (COLORS["error"] if mode == "error"
                     else COLORS["coarse"] if mode == "coarse"
                     else ring_color(getattr(reading, "level", None)))
        canvas.ring(cx, cy, geo["radius"], geo["thickness"], color, mode, value)
        label_color = color if text_color is None else text_color
        if mode == "percent":
            canvas.text(cx, cy, str(int(round(getattr(reading, "level", 0)))),
                        label_color, dot)
        elif mode == "coarse":
            canvas.text(cx, cy, str(int(value)), label_color, dot)
        else:
            canvas.error_mark(cx, cy, geo["diameter"] / 60.0)
    return canvas.premultiplied_bgra(), geo["width"], geo["height"]
