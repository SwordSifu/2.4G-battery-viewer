"""Pure-data renderer checks; native font loader is always replaced headlessly."""
import math
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fluent_ring_render as rr
import win_text as wt
from ring_render import Canvas


def reading(level=None, coarse="", error=""):
    return SimpleNamespace(level=level, coarse=coarse, error=error)


def pixel(data, width, x, y):
    index = (int(y) * width + int(x)) * 4
    return tuple(data[index:index + 4])


class GeometryTests(unittest.TestCase):
    def test_legacy_geometry_fields_and_exact_taskbar_height(self):
        geo = rr.panel_geometry(2, 43)
        self.assertEqual(set(geo), {"width", "height", "radius", "thickness", "centers", "cy", "pad", "diameter"})
        self.assertEqual((geo["width"], geo["height"]), (125, 43))
        self.assertEqual(geo, rr.panel_geometry(1, 43))
        self.assertLess(geo["thickness"], 2.5)
        self.assertLess(geo["thickness"] / geo["diameter"], 0.07)

    def test_three_rings_fit_separately_at_small_and_high_dpi_sizes(self):
        for height in (1, 8, 24, 32, 43, 64, 86, 128, 256):
            with self.subTest(height=height):
                geo = rr.panel_geometry(height_px=height)
                outer = geo["radius"] + geo["thickness"] / 2
                self.assertGreater(geo["centers"][0] - outer, 0)
                self.assertLessEqual(geo["centers"][-1] + outer, geo["width"] + 0.5)
                self.assertLessEqual(geo["cy"] + outer, height)
                self.assertGreater(geo["centers"][1] - geo["centers"][0], geo["diameter"])

    def test_default_dimensions_scale(self):
        one, two = rr.panel_geometry(), rr.panel_geometry(2)
        self.assertEqual(two["height"], 2 * one["height"])
        self.assertAlmostEqual(two["diameter"], 2 * one["diameter"])
        self.assertAlmostEqual(two["thickness"], 2 * one["thickness"])

    def test_invalid_and_unbounded_allocations_rejected(self):
        for args in ((0, 0), (-1, 0), (float("nan"), 43), (1, float("inf")), (1, 513), (1000, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                rr.panel_geometry(*args)


class DataSemanticsTests(unittest.TestCase):
    def test_percent_is_real_clamped_and_finite_only(self):
        for level, expected in ((90, 0.9), (0, 0), (100, 1), (-4, 0), (400, 1)):
            self.assertEqual(rr.ring_ratio(reading(level)), ("percent", expected))
        for level in (float("nan"), float("inf"), "bad", object()):
            self.assertEqual(rr.ring_ratio(reading(level)), ("error", 0))

    def test_four_categorical_levels_are_not_percentages(self):
        for index, coarse in enumerate(("极低档", "低档", "中档", "高档"), 1):
            self.assertEqual(rr.ring_ratio(reading(coarse=coarse)), ("coarse", index))
        for coarse in ("unknown", ["高档"], {"bad": "data"}):
            self.assertEqual(rr.ring_ratio(reading(coarse=coarse)), ("error", 0))

    def test_wired_is_not_error_and_error_wins_over_all_other_fields(self):
        self.assertEqual(rr.ring_ratio(reading(coarse="有线")), ("wired", 0))
        self.assertEqual(rr.ring_ratio(reading(90, "高档", "offline")), ("error", 0))
        self.assertEqual(rr.ring_ratio(reading(coarse="有线", error="offline")), ("error", 0))
        self.assertEqual(rr.ring_ratio(None), ("error", 0))

    def test_calm_normal_palette_warning_only_for_low_battery(self):
        for level in (25, 40, 60, 90, 100):
            self.assertEqual(rr.ring_color(level), rr.MINT)
        self.assertEqual(rr.ring_color(24), rr.AMBER)
        self.assertEqual(rr.ring_color(9), rr.RED)
        self.assertEqual(rr.ring_color(None), rr.INFO)
        self.assertEqual(rr.PLATE, (16, 21, 31))
        self.assertEqual(rr.MINT, (104, 223, 189))
        self.assertEqual(rr.INFO, (121, 184, 255))
        self.assertEqual(rr.MUTED, (123, 139, 159))


class RenderTests(unittest.TestCase):
    def setUp(self):
        wt.clear_cache()
        self.patch = mock.patch.object(wt, "_load_native", return_value=None)
        self.loader = self.patch.start()

    def tearDown(self):
        self.patch.stop()
        wt.clear_cache()

    def test_buffer_geometry_and_bgra_premultiplication(self):
        for alpha in (0, 64, 244, 255):
            with self.subTest(alpha=alpha):
                data, width, height = rr.render_panel([reading(90), reading(100), reading(coarse="中档")],
                                                     height_px=43, plate_alpha=alpha)
                self.assertEqual((width, height), (125, 43))
                self.assertEqual(len(data), width * height * 4)
                self.assertIsInstance(data, bytes)
                self.assertTrue(any(data))
                for index in range(0, len(data), 4):
                    b, g, r, a = data[index:index + 4]
                    self.assertLessEqual(max(b, g, r), a)
                    if not a:
                        self.assertEqual((b, g, r), (0, 0, 0))

    def test_plate_override_uses_rgb_input_and_bgra_output(self):
        geo = rr.panel_geometry(height_px=43)
        cx = geo["centers"][1]
        data, width, _ = rr.render_panel([reading(coarse="高档")] * 3, height_px=43,
                                         plate_color=(10, 20, 30), plate_alpha=255)
        self.assertEqual(pixel(data, width, cx, geo["cy"]), (30, 20, 10, 255))
        transparent, width, _ = rr.render_panel([], height_px=43, plate_alpha=0)
        self.assertEqual(pixel(transparent, width, 0, 0), (0, 0, 0, 0))

    def test_only_percent_readings_request_numeric_text(self):
        with mock.patch.object(rr, "_text") as text:
            rr.render_panel([reading(90), reading(100), reading(coarse="中档")], height_px=43)
        self.assertEqual([call.args[3] for call in text.call_args_list], ["90", "100"])
        self.assertTrue(all(call.args[5] == rr.TEXT for call in text.call_args_list))
        with mock.patch.object(rr, "_text") as text:
            rr.render_panel([reading(coarse="有线"), reading(error="bad"), reading(coarse="高档")])
        text.assert_not_called()

    def test_clamped_percent_label_matches_arc(self):
        with mock.patch.object(rr, "_text") as text:
            rr.render_panel([reading(500), reading(-10)], height_px=43)
        self.assertEqual([call.args[3] for call in text.call_args_list], ["100", "0"])

    def test_text_override_composites_grayscale_as_single_color(self):
        with mock.patch.object(rr, "rasterize_text", return_value=wt.GlyphMask(3, 1, b"\xff" * 3, "fake")):
            data, width, _ = rr.render_panel([reading(90)], height_px=43, plate_alpha=0,
                                             text_color=(10, 20, 30))
        geo = rr.panel_geometry(height_px=43)
        self.assertEqual(pixel(data, width, geo["centers"][0], round(geo["cy"] - 0.5)), (30, 20, 10, 255))

    def test_coarse_controller_has_four_separate_arcs_and_real_gaps(self):
        data, width, _ = rr.render_panel([reading(coarse="高档")] * 3, height_px=43, plate_alpha=0)
        geo = rr.panel_geometry(height_px=43)
        cx, cy, radius = geo["centers"][0], geo["cy"], geo["radius"]
        for angle in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
            self.assertEqual(pixel(data, width, cx + radius * math.sin(angle), cy - radius * math.cos(angle))[3], 0)
        for angle in (math.pi / 4, 3 * math.pi / 4, 5 * math.pi / 4, 7 * math.pi / 4):
            self.assertGreater(pixel(data, width, cx + radius * math.sin(angle), cy - radius * math.cos(angle))[3], 0)
        self.assertEqual(pixel(data, width, cx, cy), (0, 0, 0, 0))

    def test_error_has_visible_track_and_dash_wired_has_distinct_plug(self):
        unknown, width, _ = rr.render_panel([reading(error="offline")], height_px=43, plate_alpha=0)
        wired, _, _ = rr.render_panel([reading(coarse="有线")], height_px=43, plate_alpha=0)
        geo = rr.panel_geometry(height_px=43)
        cx, cy, radius = geo["centers"][0], geo["cy"], geo["radius"]
        self.assertGreater(pixel(unknown, width, cx, cy)[3], 0)
        self.assertGreater(pixel(unknown, width, cx, cy - radius)[3], 0)
        self.assertNotEqual(unknown, wired)
        self.assertGreater(pixel(wired, width, cx, cy - 2)[3], 0)

    def test_round_caps_extend_beyond_start_and_end_but_zero_is_empty(self):
        canvas = Canvas(41, 41)
        rr._stroke_arc(canvas, 20.5, 20.5, 12, 4, 0, math.pi / 2, rr.MINT)
        data = canvas.premultiplied_bgra()
        self.assertEqual(pixel(data, 41, 19, 8)[3], 255)  # Before start, inside round cap.
        self.assertEqual(pixel(data, 41, 17, 8)[3], 0)
        self.assertEqual(pixel(data, 41, 32, 21)[3], 255)  # After end, inside round cap.
        self.assertEqual(pixel(data, 41, 32, 23)[3], 0)
        empty = Canvas(41, 41)
        rr._stroke_arc(empty, 20.5, 20.5, 12, 4, 0, 0, rr.MINT)
        self.assertFalse(any(empty.buf))
        self.assertTrue(any(0 < data[i] < 255 for i in range(3, len(data), 4)))

    def test_full_circle_has_no_seam(self):
        canvas = Canvas(41, 41)
        rr._stroke_arc(canvas, 20.5, 20.5, 12, 2, 0, math.tau, rr.MINT)
        data = canvas.premultiplied_bgra()
        self.assertEqual(pixel(data, 41, 20, 8), pixel(data, 41, 20, 32))

    def test_missing_rows_and_iterators_are_bounded_and_deterministic(self):
        def bounded():
            yield reading(90)
            yield reading(100)
            yield reading(coarse="中档")
            raise AssertionError("renderer consumed more than three readings")
        first = rr.render_panel(bounded(), height_px=43)
        second = rr.render_panel([reading(90), reading(100), reading(coarse="中档")], height_px=43)
        self.assertEqual(first, second)
        self.assertEqual(len(rr.render_panel(None, height_px=43)[0]), 125 * 43 * 4)

    def test_native_loader_failure_uses_visible_antialiased_fallback_digits(self):
        self.loader.side_effect = OSError("no GDI")
        data, width, _ = rr.render_panel([reading(90)], height_px=43, plate_alpha=0)
        self.assertTrue(self.loader.called)
        geo = rr.panel_geometry(height_px=43)
        cx, cy = int(geo["centers"][0]), int(geo["cy"])
        center = [pixel(data, width, x, y) for x in range(cx - 10, cx + 10)
                  for y in range(cy - 8, cy + 8)]
        self.assertTrue(any(r > 170 and g > 170 and b > 170 for b, g, r, a in center))
        self.assertTrue(any(0 < a < 255 for b, g, r, a in center))

    def test_tiny_dimensions_and_bad_overrides_fail_safely(self):
        data, width, height = rr.render_panel([reading(100)], height_px=1)
        self.assertEqual(len(data), width * height * 4)
        for kwargs in ({"plate_color": (1, 2)}, {"text_color": (float("nan"), 0, 0)},
                       {"plate_alpha": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                rr.render_panel([], **kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
