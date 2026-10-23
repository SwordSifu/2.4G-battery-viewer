"""Pure-data, opt-in Fluent three-ring renderer (no window or hardware access).

Drop-in renderer interface for CornerWidget:
    render_panel(readings, scale=1, height_px=0, plate_color=None,
                 plate_alpha=None, text_color=None) -> (BGRA_bytes, width, height)
    panel_geometry(scale=1, height_px=0) -> legacy geometry fields

Colors are RGB inputs; output is premultiplied BGRA for UpdateLayeredWindow.
Geometry radius is the stroke CENTERLINE radius and diameter includes the whole
stroke. Arc caps and vector marks use analytic coverage. Numeric labels use
win_text's grayscale system-font masks, composited as white by default. No
animation, worker, screen capture, configuration or startup side effects.

Exact battery measurements get numbers and a continuous arc. Coarse controller
levels get ONLY four separated arcs: never a fabricated percent/number. Wired
controllers get four subdued arcs plus a small line plug, not an error. Unknown
readings get a muted track and dash. Tiny identity decorations are deliberately
omitted: at 43 px they compete with the only important information, the digits.
"""
from __future__ import annotations

import itertools
import math

from ring_render import Canvas  # Existing compositing primitive; never changed.
from win_text import rasterize_text

PLATE = (16, 21, 31)       # #10151f
PLATE_ALPHA = 244
MINT = (104, 223, 189)     # #68dfbd
INFO = (121, 184, 255)     # #79b8ff
MUTED = (123, 139, 159)    # #7b8b9f
TEXT = (245, 248, 252)
TRACK = (43, 56, 73)
AMBER = (245, 196, 111)
RED = (240, 128, 139)
COLORS = {"mint": MINT, "info": INFO, "muted": MUTED, "amber": AMBER, "red": RED}
SEGMENT_GAP = 0.29  # Radians between centerlines; rounded caps leave a real gap.


def ring_color(level):
    """Reserve amber/red for actual low battery; normal levels stay calm mint."""
    if level is None:
        return INFO
    if level < 10:
        return RED
    if level < 25:
        return AMBER
    return MINT


def coarse_steps(coarse: str) -> int:
    if not isinstance(coarse, str):
        return 0
    return {"极低档": 1, "低档": 2, "中档": 3, "高档": 4}.get(coarse or "", 0)


def ring_ratio(reading) -> tuple:
    """Return ('percent', 0..1), ('coarse', 1..4), ('wired', 0), or ('error', 0)."""
    if reading is None or getattr(reading, "error", ""):
        return "error", 0
    level = getattr(reading, "level", None)
    if level is not None:
        try:
            number = float(level)
        except (ValueError, TypeError, OverflowError):
            return "error", 0
        if not math.isfinite(number):
            return "error", 0
        return "percent", max(0.0, min(1.0, number / 100.0))
    coarse = getattr(reading, "coarse", "")
    if coarse in ("有线", "有线连接", "wired"):
        return "wired", 0
    steps = coarse_steps(coarse)
    return ("coarse", steps) if steps else ("error", 0)


def panel_geometry(scale: float = 1.0, height_px: int = 0) -> dict:
    """Legacy fields, compact spacing, exact requested positive pixel height.

    An explicit height wins over scale, as with the original renderer. Default
    height is 64*scale. Heights are bounded at 512 to avoid unbounded allocations;
    invalid/non-finite inputs raise ValueError instead of producing broken DIBs.
    """
    try:
        requested, factor = float(height_px), float(scale)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid panel dimensions") from exc
    if not math.isfinite(requested) or not math.isfinite(factor):
        raise ValueError("panel dimensions must be finite")
    if requested > 0:
        height = max(1, int(round(requested)))
    else:
        if factor <= 0:
            raise ValueError("scale must be positive")
        height = max(1, int(round(64 * factor)))
    if height > 512:
        raise ValueError("panel height must not exceed 512 pixels")
    pad = min(height * 0.2, max(1.0, height * 0.075))
    diameter = height - 2 * pad
    gap = min(diameter * 0.25, max(2.0, diameter * 0.12))
    thickness = min(diameter * 0.22, max(1.4, diameter * 0.06))
    width = max(1, int(round(3 * diameter + 2 * gap + 2 * pad)))
    return {"width": width, "height": height,
            "radius": (diameter - thickness) / 2.0, "thickness": thickness,
            "centers": [pad + diameter / 2.0 + i * (diameter + gap) for i in range(3)],
            "cy": height / 2.0, "pad": pad, "diameter": diameter}


def _clamp(value):
    return max(0.0, min(1.0, value))


def _rgb(value, default):
    if value is None:
        return default
    try:
        if len(value) != 3:
            raise ValueError("expected an RGB triple")
        return tuple(max(0, min(255, int(component))) for component in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("expected a finite RGB triple") from exc


def _stroke_arc(canvas, cx, cy, radius, thickness, start, sweep, color, opacity=1.0):
    """AA signed distance to a circular centerline, including round end caps.

    Angles start at 12 o'clock and increase clockwise. Zero sweep paints no
    cap/dot; full sweep closes exactly, with no seam at 100 percent.
    """
    if sweep <= 0 or opacity <= 0:
        return
    half = thickness / 2.0
    outer = radius + half + 1
    x0, x1 = max(0, math.floor(cx - outer)), min(canvas.w, math.ceil(cx + outer))
    y0, y1 = max(0, math.floor(cy - outer)), min(canvas.h, math.ceil(cy + outer))
    full = sweep >= math.tau - 1e-10
    start %= math.tau
    end = start + sweep
    sx, sy = radius * math.sin(start), -radius * math.cos(start)
    ex, ey = radius * math.sin(end), -radius * math.cos(end)
    for y in range(y0, y1):
        dy = y + 0.5 - cy
        for x in range(x0, x1):
            dx = x + 0.5 - cx
            radial = math.hypot(dx, dy)
            if abs(radial - radius) > half + 0.5:
                continue
            angle = (math.atan2(dx, -dy) - start) % math.tau
            if full or angle <= sweep:
                distance = abs(radial - radius)
            else:
                distance = min(math.hypot(dx - sx, dy - sy), math.hypot(dx - ex, dy - ey))
            cover = _clamp(half + 0.5 - distance) * opacity
            if cover:
                canvas._blend((y * canvas.w + x) * 4, color, cover)


def _stroke_line(canvas, ax, ay, bx, by, thickness, color, opacity=1.0):
    radius = thickness / 2.0
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    for y in range(max(0, math.floor(min(ay, by) - radius - 1)),
                   min(canvas.h, math.ceil(max(ay, by) + radius + 1))):
        for x in range(max(0, math.floor(min(ax, bx) - radius - 1)),
                       min(canvas.w, math.ceil(max(ax, bx) + radius + 1))):
            t = _clamp(((x + 0.5 - ax) * vx + (y + 0.5 - ay) * vy) / length2) if length2 else 0
            distance = math.hypot(x + 0.5 - ax - t * vx, y + 0.5 - ay - t * vy)
            cover = _clamp(radius + 0.5 - distance) * opacity
            if cover:
                canvas._blend((y * canvas.w + x) * 4, color, cover)


def _plug(canvas, cx, cy, diameter, color):
    unit = max(0.35, diameter / 36.55)
    thick = max(0.75, unit * 0.95)
    # Short, horizontal line plug: two pins, open body, cable. No text glyph or
    # platform symbol dependency, visibly different from the unknown-state dash.
    paths = [[(-2, -2.3), (1.5, -2.3), (1.5, 2.3), (-2, 2.3), (-2, -2.3)],
             [(-4, -1.2), (-2, -1.2)], [(-4, 1.2), (-2, 1.2)], [(1.5, 0), (4.5, 0)]]
    for path in paths:
        for a, b in zip(path, path[1:]):
            _stroke_line(canvas, cx + a[0] * unit, cy + a[1] * unit,
                         cx + b[0] * unit, cy + b[1] * unit, thick, color)


def _text(canvas, cx, cy, label, diameter, color):
    size = max(4, min(96, int(round(diameter * 0.43))))
    mask = rasterize_text(label, size)
    # Fit ink, not nominal cell metrics; 100 stays inside the ring at 43px.
    max_width, max_height = diameter * 0.70, diameter * 0.55
    if mask.width > max_width or mask.height > max_height:
        ratio = min(max_width / max(mask.width, 1), max_height / max(mask.height, 1))
        size = max(4, int(math.floor(size * ratio)))
        mask = rasterize_text(label, size)
    left, top = int(round(cx - mask.width / 2.0)), int(round(cy - mask.height / 2.0))
    for y in range(mask.height):
        py = top + y
        if not 0 <= py < canvas.h:
            continue
        for x in range(mask.width):
            px = left + x
            if 0 <= px < canvas.w:
                alpha = mask.pixels[y * mask.width + x] / 255.0
                if alpha:
                    canvas._blend((py * canvas.w + px) * 4, color, alpha)


def render_panel(readings, scale: float = 1.0, height_px: int = 0,
                 plate_color=None, plate_alpha=None, text_color=None) -> tuple:
    """Render up to three readings; return immutable premultiplied BGRA + size.

    Missing readings are unknown, not zero. Inputs only need level/coarse/error
    attributes; no device classes are imported. plate_alpha is 0..255; RGB and
    alpha overrides are clamped. Fully transparent pixels are always zero BGRA.
    """
    geo = panel_geometry(scale, height_px)
    canvas = Canvas(geo["width"], geo["height"])
    plate = _rgb(plate_color, PLATE)
    label_color = _rgb(text_color, TEXT)
    try:
        alpha = PLATE_ALPHA if plate_alpha is None else max(0, min(255, int(plate_alpha)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid plate alpha") from exc
    canvas.plate(radius=max(1, int(round(geo["height"] * 0.20))), color=plate, alpha=alpha)
    # The idle track must sit quietly on light AND dark taskbars; a fixed dark
    # slate reads as a heavy black ring on the light Windows 11 taskbar.
    track = tuple(int(round(p + (t - p) * 0.24)) for p, t in zip(plate, TRACK))
    rows = list(itertools.islice(readings if readings is not None else (), 3))
    rows.extend([None] * (3 - len(rows)))
    radius, thick, diameter = geo["radius"], geo["thickness"], geo["diameter"]
    for index, reading in enumerate(rows):
        cx, cy = geo["centers"][index], geo["cy"]
        mode, value = ring_ratio(reading)
        if mode in ("coarse", "wired"):
            color = (RED if value == 1 else AMBER if value == 2 else INFO) if mode == "coarse" else INFO
            for segment in range(4):
                lit = mode == "coarse" and segment < value
                _stroke_arc(canvas, cx, cy, radius, thick,
                            segment * math.pi / 2 + SEGMENT_GAP / 2,
                            math.pi / 2 - SEGMENT_GAP, color if lit else track)
            if mode == "wired":
                _plug(canvas, cx, cy, diameter, INFO)
        else:
            _stroke_arc(canvas, cx, cy, radius, thick, 0, math.tau, track)
            if mode == "percent":
                level = value * 100
                _stroke_arc(canvas, cx, cy, radius, thick, 0, value * math.tau, ring_color(level))
                # Warning hues must survive a light-taskbar dark-text override.
                ink = ring_color(level) if level < 25 else label_color
                _text(canvas, cx, cy, str(int(round(level))), diameter, ink)
            else:
                half = max(1.0, diameter * 0.10)
                _stroke_line(canvas, cx - half, cy, cx + half, cy,
                             max(0.7, diameter * 0.035), MUTED)
    return canvas.premultiplied_bgra(), geo["width"], geo["height"]
