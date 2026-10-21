# -*- coding: utf-8 -*-
"""win_icon.py —— 用纯 Python 画托盘图标（不依赖 Pillow / 任何第三方库）

做法：内置一套 3x5 点阵数字，按电量数值渲染成 RGBA 位图，
再把内存中的 DIB 交给 CreateIconFromResourceEx 变成私有 HICON。
不写临时文件，不共享其他程序的图标目录；退出时销毁缓存句柄。
"""

from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

# ---- 3x5 点阵数字（每行 3 位，"1" 点亮）----
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
    "%": ("101", "001", "010", "100", "101"),
    "-": ("000", "000", "111", "000", "000"),
}

GLYPH_W, GLYPH_H = 3, 5


def render_rgba(text: str, size: int, color: tuple[int, int, int],
                plate: bool = False, plate_color=(28, 30, 34, 235)) -> bytes:
    """把文字渲染成 size×size 的 RGBA 像素（返回 size*size*4 字节，行优先）。"""
    n = len(text)
    # 选一个能放下的最大缩放倍数
    gap = 1
    scale = 1
    while True:
        w = n * GLYPH_W * (scale + 1) + (n - 1) * gap
        h = GLYPH_H * (scale + 1)
        if w > size - 2 or h > size - 2:
            break
        scale += 1

    gw, gh = GLYPH_W * scale, GLYPH_H * scale
    total_w = n * gw + (n - 1) * gap
    total_h = gh
    ox = (size - total_w) // 2
    oy = (size - total_h) // 2

    px = bytearray(size * size * 4)

    def put(x: int, y: int, rgba):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 4
            px[i:i + 4] = bytes(rgba)

    def get(x: int, y: int):
        i = (y * size + x) * 4
        return (px[i], px[i + 1], px[i + 2], px[i + 3])

    # 底衬（耳机图标用，跟鼠标图标区分开）
    if plate:
        r = max(1, size // 8)
        for y in range(size):
            for x in range(size):
                # 圆角矩形
                cx = min(x, size - 1 - x)
                cy = min(y, size - 1 - y)
                if cx + cy < r and cx < r and cy < r:
                    continue
                put(x, y, plate_color)

    # 逐字画
    for k, ch in enumerate(text):
        glyph = DIGITS.get(ch)
        if not glyph:
            continue
        gx = ox + k * (gw + gap)
        for gy, row in enumerate(glyph):
            for gxx, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        put(gx + gxx * scale + dx, oy + gy * scale + dy,
                            (*color, 255))

    # 描一圈深色轮廓，保证在浅色/深色任务栏上都看得清
    outline = (16, 17, 19, 190)
    edge = []
    for y in range(size):
        for x in range(size):
            if get(x, y)[3] == 0:
                continue
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < size and 0 <= ny < size and get(nx, ny)[3] == 0:
                    edge.append((nx, ny))
    for x, y in edge:
        if get(x, y)[3] == 0:
            put(x, y, outline)
    return bytes(px)


def build_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """把多张 RGBA 位图打包成 ICO 文件内容。"""
    entries = []
    blobs = []
    offset = 6 + 16 * len(pngs)
    for size, rgba in pngs:
        # BITMAPINFOHEADER
        bih = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                          size * size * 4, 0, 0, 0, 0)
        # XOR 位图：32bpp BGRA，自下而上
        xor = bytearray()
        for y in range(size - 1, -1, -1):
            row = rgba[y * size * 4:(y + 1) * size * 4]
            for x in range(size):
                r, g, b, a = row[x * 4:x * 4 + 4]
                # 预乘不必要，Windows 直接用 alpha
                xor += bytes((b, g, r, a))
        # AND 掩码：1bpp，每行按 4 字节对齐，全 0（靠 alpha 通道）
        row_bytes = ((size + 31) // 32) * 4
        mask = b"\x00" * (row_bytes * size)
        blob = bih + bytes(xor) + mask
        blobs.append(blob)
        entries.append(struct.pack("<BBBBHHII", size if size < 256 else 0,
                                   size if size < 256 else 0, 0, 0, 1, 32,
                                   len(blob), offset))
        offset += len(blob)
    return (struct.pack("<HHH", 0, 1, len(pngs)) + b"".join(entries)
            + b"".join(blobs))


# ---------------- HICON 缓存（仅由托盘消息线程使用） ----------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.CreateIconFromResourceEx.restype = ctypes.c_void_p
user32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.DestroyIcon.restype = wintypes.BOOL
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
SIZES = (16, 24, 32)
_icon_cache: dict[tuple, int] = {}


def make_hicon(text: str, color: tuple[int, int, int], plate: bool = False) -> int:
    """Create a non-shared HICON directly from the ICO's DIB resource bytes."""
    size = max(16, min(64, int(user32.GetSystemMetrics(49) or 16)))  # SM_CXSMICON
    key = (text, color, plate, size)
    if key in _icon_cache:
        return _icon_cache[key]
    ico = build_ico([(size, render_rgba(text, size, color, plate))])
    length, offset = struct.unpack_from("<II", ico, 14)
    resource = ctypes.create_string_buffer(ico[offset:offset + length])
    handle = user32.CreateIconFromResourceEx(resource, length, True, 0x00030000,
                                             size, size, 0)
    if handle:
        _icon_cache[key] = int(handle)
    return int(handle or 0)


def destroy_all() -> None:
    for handle in _icon_cache.values():
        if handle:
            user32.DestroyIcon(handle)
    _icon_cache.clear()


def cleanup_files() -> None:
    """Compatibility no-op: this fork never creates temporary icon files."""


if __name__ == "__main__":
    # 生成几个样例，方便肉眼检查
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_icon_preview")
    os.makedirs(out, exist_ok=True)
    for text, color, plate, name in (
        ("82", (93, 220, 138), False, "mouse82"),
        ("70", (93, 220, 138), True, "headset70"),
        ("9", (255, 107, 107), False, "mouse9"),
        ("100", (245, 196, 81), True, "headset100"),
        ("25", (245, 196, 81), False, "mouse25"),
    ):
        imgs = [(s, render_rgba(text, s, color, plate)) for s in SIZES]
        p = os.path.join(out, name + ".ico")
        with open(p, "wb") as f:
            f.write(build_ico(imgs))
        print("生成", p)
