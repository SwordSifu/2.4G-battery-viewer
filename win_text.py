"""Small, window-free system-font rasterizer; standard library only.

``rasterize_text(text, font_px, *, font_name='Segoe UI', weight=500)`` returns
an immutable ``GlyphMask(width, height, pixels, backend)``. ``pixels`` contains
one tightly packed 0..255 coverage byte per pixel, cropped to the ink bounds;
it is NOT RGB/BGRA and carries no foreground/background color. Empty ink has
size 0x0. Composite this mask in the caller's chosen color before premultiplying.

The native path lazily loads gdi32, creates its OWN memory DC and top-down 32-bit
black DIB, selects a CreateFontW font with ANTIALIASED_QUALITY (4, explicitly NOT
CLEARTYPE_QUALITY), and draws white TextOutW text. RGB intensity becomes a single
grayscale mask; GDI's undefined DIB alpha byte is ignored. No window/screen DC,
third-party font package, hardware, timer, files or process-global GDI objects.
Every selected object is restored before DeleteObject; the owned DC is deleted
on every exit. Only immutable masks (at most 128, each <=262144 bytes) are cached.
``clear_cache()`` releases these Python masks, not native resources.

Non-Windows and native failures fall back to antialiased vector-stroke digits,
+/-/percent/question mark (other non-space characters use a missing-glyph box).
That fallback intentionally is not the old 3x5 bitmap font. For bounded work the
API accepts <=32 characters, no NUL, font sizes 4..96 pixels and face names <=64
characters. Out-of-range sizes are clamped; invalid/non-finite sizes raise
ValueError. This is for short numeric UI labels, not paragraph shaping.
"""
from __future__ import annotations

import ctypes as c
import math
import os
from dataclasses import dataclass
from functools import lru_cache

ANTIALIASED_QUALITY = 4
CACHE_SIZE = 128
MAX_MASK_PIXELS = 262144


@dataclass(frozen=True)
class GlyphMask:
    width: int
    height: int
    pixels: bytes
    backend: str


# Fixed-width ABI types also keep fake-DLL tests portable off Windows.
class _SIZE(c.Structure):
    _fields_ = [("cx", c.c_int32), ("cy", c.c_int32)]


class _BITMAPINFOHEADER(c.Structure):
    _fields_ = [("biSize", c.c_uint32), ("biWidth", c.c_int32),
                ("biHeight", c.c_int32), ("biPlanes", c.c_uint16),
                ("biBitCount", c.c_uint16), ("biCompression", c.c_uint32),
                ("biSizeImage", c.c_uint32), ("biXPelsPerMeter", c.c_int32),
                ("biYPelsPerMeter", c.c_int32), ("biClrUsed", c.c_uint32),
                ("biClrImportant", c.c_uint32)]


class _BITMAPINFO(c.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", c.c_uint32 * 1)]


class _NativeGDI:
    def __init__(self, dll):
        signatures = {
            "CreateCompatibleDC": ([c.c_void_p], c.c_void_p),
            "CreateFontW": ([c.c_int] * 5 + [c.c_uint32] * 8 + [c.c_wchar_p], c.c_void_p),
            "SelectObject": ([c.c_void_p, c.c_void_p], c.c_void_p),
            "GetTextExtentPoint32W": ([c.c_void_p, c.c_wchar_p, c.c_int, c.POINTER(_SIZE)], c.c_int),
            "CreateDIBSection": ([c.c_void_p, c.POINTER(_BITMAPINFO), c.c_uint32,
                                  c.POINTER(c.c_void_p), c.c_void_p, c.c_uint32], c.c_void_p),
            "SetTextColor": ([c.c_void_p, c.c_uint32], c.c_uint32),
            "SetBkColor": ([c.c_void_p, c.c_uint32], c.c_uint32),
            "SetBkMode": ([c.c_void_p, c.c_int], c.c_int),
            "TextOutW": ([c.c_void_p, c.c_int, c.c_int, c.c_wchar_p, c.c_int], c.c_int),
            "GdiFlush": ([], c.c_int),
            "DeleteObject": ([c.c_void_p], c.c_int),
            "DeleteDC": ([c.c_void_p], c.c_int),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(dll, name)
            fn.argtypes, fn.restype = args, result
            setattr(self, name, fn)


@lru_cache(maxsize=1)
def _load_native():
    """Lazy replaceable loader: no WinDLL call occurs merely by importing us."""
    if os.name != "nt":
        return None
    return _NativeGDI(c.WinDLL("gdi32", use_last_error=True))


def _valid_handle(value):
    if isinstance(value, c.c_void_p):
        value = value.value
    return value not in (None, 0, -1, c.c_void_p(-1).value)


def _trim(width, height, pixels, backend):
    xs, ys = [], []
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        ink = [x for x, coverage in enumerate(row) if coverage]
        if ink:
            xs.extend((ink[0], ink[-1]))
            ys.append(y)
    if not ys:
        return GlyphMask(0, 0, b"", backend)
    left, right, top, bottom = min(xs), max(xs) + 1, ys[0], ys[-1] + 1
    cropped = b"".join(bytes(pixels[y * width + left:y * width + right])
                       for y in range(top, bottom))
    return GlyphMask(right - left, bottom - top, cropped, backend)


def _cleanup(native, dc, font, old_font, bitmap, old_bitmap):
    # If restoring an object itself fails, destroy the DC BEFORE attempting to
    # delete owned objects: DeleteObject must never target a selected object.
    restored = True
    for previous in (old_bitmap, old_font):
        if previous is not None:
            try:
                restored = _valid_handle(native.SelectObject(dc, previous)) and restored
            except Exception:
                restored = False
    dc_deleted = False
    if not restored:
        try:
            dc_deleted = bool(native.DeleteDC(dc))
        except Exception:
            pass
    # If even DeleteDC failed, leaking is safer than deleting a selected object.
    if restored or dc_deleted:
        for owned in (bitmap, font):
            if owned is not None:
                try:
                    native.DeleteObject(owned)
                except Exception:
                    pass
    if not dc_deleted:
        try:
            native.DeleteDC(dc)
        except Exception:
            pass


def _native_mask(native, text, font_px, font_name, weight):
    dc = native.CreateCompatibleDC(None)
    if not _valid_handle(dc):
        raise OSError("CreateCompatibleDC failed")
    font = bitmap = old_font = old_bitmap = None
    try:
        created = native.CreateFontW(-font_px, 0, 0, 0, weight, 0, 0, 0,
                                     1, 0, 0, ANTIALIASED_QUALITY, 0, font_name)
        if not _valid_handle(created):
            raise OSError("CreateFontW failed")
        font = created
        previous = native.SelectObject(dc, font)
        if not _valid_handle(previous):
            raise OSError("SelectObject(font) failed")
        old_font = previous
        size = _SIZE()
        count = len(text.encode("utf-16-le")) // 2
        if not native.GetTextExtentPoint32W(dc, text, count, c.byref(size)):
            raise OSError("GetTextExtentPoint32W failed")
        if size.cx <= 0 or size.cy <= 0:
            raise OSError("invalid native text extent")
        pad = max(3, font_px // 4)  # Room for side bearings and AA overshoot.
        width, height = size.cx + 2 * pad, size.cy + 2 * pad
        if not (0 < width <= 4096 and 0 < height <= 256
                and width * height <= MAX_MASK_PIXELS):
            raise OSError("unreasonable native text extent")
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(
            c.sizeof(_BITMAPINFOHEADER), width, -height, 1, 32, 0,
            width * height * 4, 0, 0, 0, 0)
        bits = c.c_void_p()
        created = native.CreateDIBSection(dc, c.byref(info), 0, c.byref(bits), None, 0)
        # A bitmap with a null bits pointer still belongs to us and needs cleanup.
        if _valid_handle(created):
            bitmap = created
        if bitmap is None or not bits.value:
            raise OSError("CreateDIBSection failed")
        previous = native.SelectObject(dc, bitmap)
        if not _valid_handle(previous):
            raise OSError("SelectObject(bitmap) failed")
        old_bitmap = previous
        c.memset(bits, 0, width * height * 4)
        if native.SetTextColor(dc, 0xFFFFFF) == 0xFFFFFFFF:
            raise OSError("SetTextColor failed")
        if native.SetBkColor(dc, 0) == 0xFFFFFFFF or not native.SetBkMode(dc, 2):
            raise OSError("SetBkColor/SetBkMode failed")
        if not native.TextOutW(dc, pad, pad, text, count):
            raise OSError("TextOutW failed")
        if not native.GdiFlush():
            raise OSError("GdiFlush failed")
        raw = c.string_at(bits, width * height * 4)
        pixels = bytes((raw[i] + raw[i + 1] + raw[i + 2] + 1) // 3
                       for i in range(0, len(raw), 4))
        result = _trim(width, height, pixels, "gdi")
        if text.strip() and not result.pixels:
            raise OSError("native glyph raster was empty")
        return result
    finally:
        _cleanup(native, dc, font, old_font, bitmap, old_bitmap)


def _curve(start, control, end, steps=10):
    return [((1 - t) ** 2 * start[0] + 2 * t * (1 - t) * control[0] + t * t * end[0],
             (1 - t) ** 2 * start[1] + 2 * t * (1 - t) * control[1] + t * t * end[1])
            for t in (i / steps for i in range(steps + 1))]


def _paths(ch):
    # Continuous polylines/quadratic curves in a 10x16 cap-height coordinate
    # system; no bitmap cells. Each segment is rasterized as an AA capsule.
    q = _curve
    if ch == "0":
        return [[(5 + 3.5 * math.cos(t), 8 + 7 * math.sin(t))
                 for t in (i * math.tau / 40 for i in range(41))]]
    if ch == "1":
        return [[(2.5, 4), (5.5, 1), (5.5, 15)], [(2.5, 15), (8.5, 15)]]
    if ch == "2":
        return [q((1.5, 4), (3, -1), (7, 1.5)) + q((7, 1.5), (11, 4.5), (6, 9))
                + [(1.5, 15), (9, 15)]]
    if ch == "3":
        return [q((1.5, 2), (9, -1), (8.5, 5)) + q((8.5, 5), (8, 8), (4.5, 8))
                + q((4.5, 8), (10, 7), (9, 12)) + q((9, 12), (8, 17), (1.5, 14))]
    if ch == "4":
        return [[(7.5, 15), (7.5, 1), (1, 10.5), (10, 10.5)]]
    if ch == "5":
        return [[(9, 1), (2, 1), (1.5, 7.5)]
                + q((1.5, 7.5), (10, 4), (9, 11.5))
                + q((9, 11.5), (8, 17), (1.5, 14))]
    if ch == "6":
        return [q((8, 1), (1, 2), (1.5, 11)) + q((1.5, 11), (2, 17), (7, 14.5))
                + q((7, 14.5), (12, 10), (7, 7.5))
                + q((7, 7.5), (3, 5.5), (1.5, 10))]
    if ch == "7":
        return [[(1, 1), (9.5, 1), (3.5, 15)]]
    if ch == "8":
        return [q((5, 8), (-1, 5), (2, 2)) + q((2, 2), (5, -1), (8.5, 2))
                + q((8.5, 2), (11, 5), (5, 8))
                + q((5, 8), (-1, 11), (2, 14)) + q((2, 14), (5, 17), (8.5, 14))
                + q((8.5, 14), (12, 10), (5, 8))]
    if ch == "9":
        return [q((2, 15), (9, 14), (8.5, 5)) + q((8.5, 5), (8, -1), (3, 1.5))
                + q((3, 1.5), (-2, 6), (3, 8.5))
                + q((3, 8.5), (7, 10.5), (8.5, 6))]
    if ch in ("-", "−"):
        return [[(2, 8), (8, 8)]]
    if ch == "+":
        return [[(1, 8), (9, 8)], [(5, 4), (5, 12)]]
    if ch == "%":
        circles = [[(x + 1.7 * math.cos(t), y + 1.7 * math.sin(t))
                    for t in (i * math.tau / 16 for i in range(17))]
                   for x, y in ((2, 3), (8, 13))]
        return [[(1, 15), (9, 1)]] + circles
    if ch == "?":
        return [q((1.5, 3), (5, -1), (8, 2)) + q((8, 2), (11, 5), (5, 9))
                + [(5, 10.5)], [(5, 14), (5, 14.1)]]
    if ch.isspace():
        return []
    return [[(1, 1), (9, 1), (9, 15), (1, 15), (1, 1)]]


def _fallback_mask(text, font_px):
    unit = font_px / 21.0
    width, height = max(1, math.ceil((len(text) * 12 + 2) * unit) + 4), math.ceil(18 * unit) + 4
    pixels = bytearray(width * height)
    radius = max(0.45, unit * 0.7)
    for index, ch in enumerate(text):
        for path in _paths(ch):
            points = [(2 + (index * 12 + x) * unit, 2 + y * unit) for x, y in path]
            for (ax, ay), (bx, by) in zip(points, points[1:]):
                vx, vy = bx - ax, by - ay
                length2 = vx * vx + vy * vy
                for y in range(max(0, math.floor(min(ay, by) - radius - 1)),
                               min(height, math.ceil(max(ay, by) + radius + 1))):
                    for x in range(max(0, math.floor(min(ax, bx) - radius - 1)),
                                   min(width, math.ceil(max(ax, bx) + radius + 1))):
                        t = max(0.0, min(1.0, ((x + 0.5 - ax) * vx + (y + 0.5 - ay) * vy)
                                             / length2)) if length2 else 0.0
                        distance = math.hypot(x + 0.5 - ax - t * vx, y + 0.5 - ay - t * vy)
                        coverage = int(round(255 * max(0.0, min(1.0, radius + 0.5 - distance))))
                        pos = y * width + x
                        pixels[pos] = max(pixels[pos], coverage)
    return _trim(width, height, pixels, "fallback")


@lru_cache(maxsize=CACHE_SIZE)
def _rasterize_cached(text, font_px, font_name, weight):
    try:
        native = _load_native()
        if native is not None:
            return _native_mask(native, text, font_px, font_name, weight)
    except Exception:
        # Rasterization is cosmetic. The data renderer must survive unavailable
        # DLLs, font/bitmap/DC allocation failures, and failures mid-draw.
        pass
    return _fallback_mask(text, font_px)


def rasterize_text(text: str, font_px: float, *, font_name: str = "Segoe UI",
                   weight: int = 500) -> GlyphMask:
    """Return a cached tightly packed grayscale ink mask, never a GDI handle."""
    if not isinstance(text, str) or len(text) > 32 or "\0" in text:
        raise ValueError("text must be a string of at most 32 characters without NUL")
    if not isinstance(font_name, str) or not font_name or len(font_name) > 64 or "\0" in font_name:
        raise ValueError("invalid font face name")
    try:
        size = float(font_px)
        weight = int(weight)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid font size or weight") from exc
    if not math.isfinite(size):
        raise ValueError("font size must be finite")
    size = max(4, min(96, int(round(size))))
    weight = max(100, min(900, weight))
    if not text or not text.strip():
        return GlyphMask(0, 0, b"", "empty")
    return _rasterize_cached(text, size, font_name, weight)


def clear_cache() -> None:
    """Clear only the bounded Python glyph cache; no native handles are retained."""
    _rasterize_cached.cache_clear()


def cache_info():
    """Return the standard lru_cache hit/miss/maxsize/currsize statistics."""
    return _rasterize_cached.cache_info()
