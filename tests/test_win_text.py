"""Headless glyph tests: fake GDI only, no window, screen DC or real DLL load."""
import ctypes as c
import importlib.util
import os
import sys
import unittest
from dataclasses import FrozenInstanceError
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import win_text as wt


class FakeGDI:
    """Own fake DIB storage and enforce GDI's no-delete-while-selected rule."""
    def __init__(self, fail=""):
        self.fail = fail
        self.events = []
        self.live = set()
        self.dcs = {}
        self.font_args = None
        self.info = None
        self.buffer = None
        self.text_color = self.background = None

    def step(self, name):
        self.events.append(name)
        return self.fail != name

    def CreateCompatibleDC(self, parent):
        assert parent is None  # Never a screen/window DC.
        if not self.step("dc"):
            return 0
        self.dcs[20] = {"font": 11, "bitmap": 12}
        return 20

    def CreateFontW(self, *args):
        self.font_args = args
        if not self.step("font"):
            return 0
        self.live.add(21)
        return 21

    def SelectObject(self, dc, obj):
        kind = "font" if obj in (11, 21) else "bitmap"
        name = ("select_" if obj in (21, 22) else "restore_") + kind
        if not self.step(name):
            return c.c_void_p(-1).value
        previous = self.dcs[dc][kind]
        self.dcs[dc][kind] = obj
        return previous

    def GetTextExtentPoint32W(self, dc, text, count, output):
        if not self.step("extent"):
            return 0
        size = c.cast(output, c.POINTER(wt._SIZE)).contents
        size.cx, size.cy = (100000, 12) if self.fail == "huge_extent" else (9, 12)
        if self.fail == "invalid_extent":
            size.cx = -1
        return 1

    def CreateDIBSection(self, dc, info, usage, bits, section, offset):
        if not self.step("dib"):
            return 0
        self.info = wt._BITMAPINFO.from_buffer_copy(c.string_at(info, c.sizeof(wt._BITMAPINFO)))
        self.width, self.height = self.info.bmiHeader.biWidth, -self.info.bmiHeader.biHeight
        self.buffer = c.create_string_buffer(self.width * self.height * 4)
        self.live.add(22)
        if self.fail != "null_bits":
            c.cast(bits, c.POINTER(c.c_void_p))[0] = c.addressof(self.buffer)
        return 22

    def SetTextColor(self, dc, color):
        self.text_color = color
        return 0 if self.step("text_color") else 0xFFFFFFFF

    def SetBkColor(self, dc, color):
        self.background = color
        return 0xFFFFFF if self.step("background") else 0xFFFFFFFF

    def SetBkMode(self, dc, mode):
        assert mode == 2
        return 1 if self.step("background_mode") else 0

    def TextOutW(self, dc, x, y, text, count):
        if not self.step("draw"):
            return 0
        assert not any(self.buffer.raw), "DIB must start black"
        if self.fail == "empty_ink":
            return 1
        for px, py, bgra in ((x, y, (20, 80, 140, 255)),
                             (x + 1, y + 1, (255, 255, 255, 0)),
                             (x + 2, y, (0, 0, 0, 255))):
            pos = (py * self.width + px) * 4
            for channel, value in enumerate(bgra):
                self.buffer[pos + channel] = bytes([value])
        return 1

    def GdiFlush(self):
        return 1 if self.step("flush") else 0

    def DeleteObject(self, obj):
        self.step("delete_bitmap" if obj == 22 else "delete_font")
        assert all(obj not in selected.values() for selected in self.dcs.values())
        self.live.remove(obj)
        return 1

    def DeleteDC(self, dc):
        self.step("delete_dc")
        del self.dcs[dc]
        return 1


class NativeRasterTests(unittest.TestCase):
    def setUp(self):
        wt.clear_cache()
        self.native = FakeGDI()
        self.patch = mock.patch.object(wt, "_load_native", return_value=self.native)
        self.loader = self.patch.start()

    def tearDown(self):
        self.patch.stop()
        wt.clear_cache()

    def test_black_top_down_dib_grayscale_mask_ignores_alpha(self):
        mask = wt.rasterize_text("90", 16)
        self.assertEqual(mask, wt.GlyphMask(2, 2, bytes([80, 0, 0, 255]), "gdi"))
        self.assertEqual(self.native.font_args[0], -16)
        self.assertEqual(self.native.font_args[4], 500)
        self.assertEqual(self.native.font_args[11], wt.ANTIALIASED_QUALITY)
        self.assertEqual(self.native.font_args[-1], "Segoe UI")
        header = self.native.info.bmiHeader
        self.assertEqual(header.biBitCount, 32)
        self.assertEqual(header.biSize, 40)
        self.assertLess(header.biHeight, 0)
        self.assertEqual((self.native.text_color, self.native.background), (0xFFFFFF, 0))
        self.assertEqual(self.native.live, set())
        self.assertEqual(self.native.dcs, {})

    def test_objects_restored_before_delete_in_reverse_selection_order(self):
        wt.rasterize_text("90", 16)
        self.assertEqual(self.native.events[-5:],
                         ["restore_bitmap", "restore_font", "delete_bitmap", "delete_font", "delete_dc"])

    def test_each_allocation_and_drawing_failure_cleans_owned_resources(self):
        stages = ("dc", "font", "select_font", "extent", "huge_extent", "invalid_extent", "dib", "null_bits",
                  "select_bitmap", "text_color", "background", "background_mode", "draw", "flush", "empty_ink")
        for stage in stages:
            with self.subTest(stage=stage):
                wt.clear_cache()
                native = FakeGDI(stage)
                self.loader.return_value = native
                mask = wt.rasterize_text("90", 16)
                self.assertEqual(mask.backend, "fallback")
                self.assertTrue(mask.pixels)
                self.assertEqual(native.live, set())
                self.assertEqual(native.dcs, {})

    def test_restore_failure_destroys_dc_before_deleting_selected_objects(self):
        self.native.fail = "restore_bitmap"
        wt.rasterize_text("90", 16)
        self.assertEqual(self.native.live, set())
        self.assertEqual(self.native.dcs, {})
        self.assertLess(self.native.events.index("delete_dc"), self.native.events.index("delete_bitmap"))

    def test_exception_mid_draw_still_restores_and_deletes(self):
        with mock.patch.object(self.native, "TextOutW", side_effect=OSError("draw interrupted")):
            result = wt.rasterize_text("1", 16)
        self.assertEqual(result.backend, "fallback")
        self.assertEqual(self.native.live, set())
        self.assertEqual(self.native.dcs, {})

    def test_cache_has_no_native_handles_and_reuses_immutable_mask(self):
        first = wt.rasterize_text("90", 16)
        second = wt.rasterize_text("90", 16)
        self.assertIs(first, second)
        self.assertEqual(self.loader.call_count, 1)
        self.assertEqual(self.native.events.count("font"), 1)
        self.assertEqual(self.native.live, set())
        with self.assertRaises(FrozenInstanceError):
            first.width = 300
        self.assertIsInstance(first.pixels, bytes)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        wt.clear_cache()
        self.real_loader = wt._load_native
        self.patch = mock.patch.object(wt, "_load_native", return_value=None)
        self.loader = self.patch.start()

    def tearDown(self):
        self.patch.stop()
        wt.clear_cache()

    def test_pure_python_vector_fallback_has_grayscale_antialiasing(self):
        for text in ("0", "90", "100", "123456789", "-+%?", "X"):
            with self.subTest(text=text):
                mask = wt.rasterize_text(text, 17)
                self.assertEqual(mask.backend, "fallback")
                self.assertEqual(len(mask.pixels), mask.width * mask.height)
                self.assertTrue(any(mask.pixels))
                self.assertTrue(any(0 < px < 255 for px in mask.pixels))

    def test_loader_failure_is_cosmetic_and_falls_back(self):
        self.loader.side_effect = OSError("DLL unavailable")
        mask = wt.rasterize_text("90", 16)
        self.assertEqual(mask.backend, "fallback")
        self.assertGreater(mask.width, 0)

    def test_cache_is_bounded_and_clearable(self):
        for number in range(140):
            wt.rasterize_text(str(number), 14)
        info = wt.cache_info()
        self.assertEqual((info.maxsize, info.currsize), (128, 128))
        wt.clear_cache()
        self.assertEqual(wt.cache_info().currsize, 0)

    def test_empty_ink_skips_native_loader(self):
        for text in ("", " ", "\t"):
            self.assertEqual(wt.rasterize_text(text, 16), wt.GlyphMask(0, 0, b"", "empty"))
        self.loader.assert_not_called()

    def test_size_is_clamped_and_mask_work_is_bounded(self):
        self.assertEqual(wt.rasterize_text("90", 10000), wt.rasterize_text("90", 96))
        self.assertEqual(wt.rasterize_text("90", -100), wt.rasterize_text("90", 4))
        mask = wt.rasterize_text("8" * 32, 96)
        self.assertLessEqual(len(mask.pixels), wt.MAX_MASK_PIXELS)

    def test_invalid_inputs_fail_before_native_loader(self):
        for args in (("x" * 33, 16), ("a\0b", 16), ("90", float("nan")),
                     ("90", float("inf")), (None, 16), ("90", None)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                wt.rasterize_text(*args)
        with self.assertRaises(ValueError):
            wt.rasterize_text("90", 16, font_name="x" * 65)
        self.loader.assert_not_called()

    def test_import_does_not_load_windll(self):
        name = "_win_text_lazy_import_test"
        spec = importlib.util.spec_from_file_location(name, wt.__file__)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            with mock.patch.object(c, "WinDLL", create=True, side_effect=AssertionError("eager DLL load")) as loader:
                spec.loader.exec_module(module)
                loader.assert_not_called()
        finally:
            del sys.modules[name]

    def test_non_windows_loader_is_safe_without_ctypes_windll(self):
        # Exercise a fresh undecorated real loader under a simulated non-Windows
        # host. The patch from setUp means no platform DLL can accidentally load.
        with mock.patch.object(wt.os, "name", "posix"):
            self.assertIsNone(self.real_loader.__wrapped__())


if __name__ == "__main__":
    unittest.main(verbosity=2)
