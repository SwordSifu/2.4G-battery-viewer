"""Pure preview theme/data/image helpers. No OS, configuration or device IO."""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct
import zlib

PALETTE = {
    "bg": "#10151f", "surface": "#192332", "edge": "#35455b",
    "text": "#edf5ff", "secondary": "#acb9cb", "muted": "#7d8b9e",
    "mint": "#68dfbd", "blue": "#79b8ff", "violet": "#afa7e7",
    "amber": "#f5c46f", "red": "#f0808b", "track": "#344357",
}
SCENES = (("normal", "日常"), ("low", "低电量"), ("charging", "充电 / 有线"), ("offline", "离线"))


@dataclass(frozen=True)
class PreviewReading:
    key: str
    title: str
    subtitle: str
    level: int | None = None
    coarse: str = ""
    status: str = ""
    error: str = ""
    detail: str = ""
    extra: str = ""

    @property
    def ok(self):
        return not self.error and (self.level is not None or bool(self.coarse))

    @property
    def display_value(self):
        return (f"{self.level}%" if self.level is not None else self.coarse) if self.ok else "—"


def demo_readings(scene="normal"):
    """Deliberately synthetic fixtures; never invoke a hardware provider."""
    if scene not in dict(SCENES):
        raise ValueError("unknown preview scene")
    rows = [
        PreviewReading("mouse", "VXE R1 NK", "星闪无线接收器", 80, status="放电中",
                       detail="来自已验证的接收器协议；此处数值为演示。"),
        PreviewReading("keyboard", "洛斐键盘", "2.4 GHz 无线接收器", 100, status="电量充足",
                       detail="真实应用读取接收器百分比，不使用蓝牙缓存。"),
        PreviewReading("gamepad", "八位堂手柄", "XInput 电池状态", coarse="中档", status="无线连接",
                       detail="系统仅报告四档电量；不换算为百分比。"),
    ]
    if scene == "low":
        rows[0] = PreviewReading("mouse", "VXE R1 NK", "星闪无线接收器", 21, status="建议充电")
        rows[1] = PreviewReading("keyboard", "洛斐键盘", "2.4 GHz 无线接收器", 7, status="电量偏低")
        rows[2] = PreviewReading("gamepad", "八位堂手柄", "XInput 电池状态", coarse="低档", status="电量偏低")
    elif scene == "charging":
        rows[0] = PreviewReading("mouse", "VXE R1 NK", "星闪无线接收器", 80, status="充电中")
        rows[1] = PreviewReading("keyboard", "洛斐键盘", "2.4 GHz 无线接收器", 62, status="充电指示开启")
        rows[2] = PreviewReading("gamepad", "八位堂手柄", "XInput 电池状态", coarse="有线", status="有线连接")
    elif scene == "offline":
        rows = [PreviewReading(r.key, r.title, r.subtitle, error="设备未连接或已休眠") for r in rows]
    return tuple(rows)


def rgb(color):
    if isinstance(color, str):
        return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))
    return tuple(color)


def mix(first, last, amount):
    t = max(0.0, min(1.0, amount))
    return tuple(round(a+(b-a)*t) for a, b in zip(rgb(first), rgb(last)))


def hex_color(color):
    return "#%02x%02x%02x" % tuple(color)


def state_color(reading):
    if not reading.ok:
        return PALETTE["muted"]
    if reading.level is None:
        return PALETTE["blue"]
    if reading.level < 10:
        return PALETTE["red"]
    if reading.level < 25:
        return PALETTE["amber"]
    return PALETTE["mint"]


def _chunk(kind, data):
    return struct.pack("!I", len(data))+kind+data+struct.pack("!I", zlib.crc32(kind+data)&0xffffffff)


def rgba_png(width, height, data):
    """Encode RGBA with standard-library PNG; Tk 8.6 reads PNG natively."""
    if not (1 <= width <= 8192 and 1 <= height <= 8192) or len(data) != width*height*4:
        raise ValueError("invalid RGBA image")
    raw = b"".join(b"\0"+data[y*width*4:(y+1)*width*4] for y in range(height))
    header = struct.pack("!2I5B", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n"+_chunk(b"IHDR", header)+_chunk(b"IDAT", zlib.compress(raw, 6))+_chunk(b"IEND", b"")


def gradient_png(width, height, first, last, radius=0):
    if not (1 <= width <= 8192 and 1 <= height <= 8192):
        raise ValueError("invalid gradient dimensions")
    r = max(0, min(int(radius), width//2, height//2))
    rows = []
    for y in range(height):
        color = mix(first, last, y/max(1, height-1))
        inset = 0.0
        if r:
            dy = r-(y+.5) if y < r else (y+.5)-(height-r) if y >= height-r else 0
            if dy > 0:
                inset = r-math.sqrt(max(0.0, r*r-dy*dy))
        n = int(inset)
        edge_alpha = round((1-(inset-n))*255)
        pixel = bytes((*color, 255))
        if n or inset:
            row = b"\0"*(4*n)+bytes((*color, edge_alpha))+pixel*max(0, width-2*n-2)+bytes((*color, edge_alpha))+b"\0"*(4*n)
        else:
            row = pixel*width
        rows.append(row)
    return rgba_png(width, height, b"".join(rows))


def bgra_png(data, width, height):
    """Unpremultiply renderer output for Tk's straight-alpha PNG reader."""
    if len(data) != width*height*4:
        raise ValueError("invalid BGRA buffer")
    out = bytearray(len(data))
    for i in range(0, len(data), 4):
        b, g, r, a = data[i:i+4]
        if a:
            out[i:i+4] = bytes((min(255, (r*255+a//2)//a), min(255, (g*255+a//2)//a),
                                min(255, (b*255+a//2)//a), a))
    return rgba_png(width, height, out)
