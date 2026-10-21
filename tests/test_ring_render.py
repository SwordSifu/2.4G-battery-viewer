# -*- coding: utf-8 -*-
"""Offline tests for the ring renderer and corner-widget geometry.

No screen access, no HID, no window creation: pure function checks.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ring_render as rr


class FakeReading:
    def __init__(self, level=None, coarse="", error=""):
        self.level = level
        self.coarse = coarse
        self.error = error


class ColorTests(unittest.TestCase):
    def test_colors_get_redder_as_level_drops(self):
        self.assertEqual(rr.ring_color(100), rr.COLORS["green"])
        self.assertEqual(rr.ring_color(60), rr.COLORS["green"])
        self.assertEqual(rr.ring_color(45), rr.COLORS["lime"])
        self.assertEqual(rr.ring_color(30), rr.COLORS["amber"])
        self.assertEqual(rr.ring_color(15), rr.COLORS["orange"])
        self.assertEqual(rr.ring_color(5), rr.COLORS["red"])

    def test_none_level_is_coarse_color_not_red(self):
        self.assertEqual(rr.ring_color(None), rr.COLORS["coarse"])


class RatioTests(unittest.TestCase):
    def test_percent(self):
        self.assertEqual(rr.ring_ratio(FakeReading(level=90)), ("percent", 0.9))
        self.assertEqual(rr.ring_ratio(FakeReading(level=0)), ("percent", 0.0))

    def test_percent_clamped(self):
        self.assertEqual(rr.ring_ratio(FakeReading(level=140))[1], 1.0)
        self.assertEqual(rr.ring_ratio(FakeReading(level=-5))[1], 0.0)

    def test_coarse_maps_to_steps(self):
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="高档")), ("coarse", 4))
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="中档")), ("coarse", 3))
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="低档")), ("coarse", 2))
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="极低档")), ("coarse", 1))

    def test_error_wins_over_coarse(self):
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="中档", error="未连接")),
                         ("error", 0))

    def test_unknown_coarse_is_error(self):
        self.assertEqual(rr.ring_ratio(FakeReading(coarse="??")), ("error", 0))


class GeometryTests(unittest.TestCase):
    def test_band_height_fills_requested_pixels(self):
        geo = rr.panel_geometry(2.0, height_px=86)
        self.assertEqual(geo["height"], 86)
        self.assertGreater(geo["width"], 86)

    def test_flat_band_keeps_all_three_rings_apart(self):
        geo = rr.panel_geometry(2.0, height_px=86)
        centers = geo["centers"]
        self.assertEqual(len(centers), 3)
        self.assertLess(centers[0], centers[1])
        self.assertLess(centers[1], centers[2])
        # 环直径必须小于行高，不能互相重叠
        spacing = centers[1] - centers[0]
        self.assertGreater(spacing, geo["diameter"] * 0.95)

    def test_default_geometry_positive(self):
        geo = rr.panel_geometry(1.0)
        self.assertGreater(geo["width"], 0)
        self.assertGreater(geo["height"], 0)


class RenderTests(unittest.TestCase):
    def test_buffer_size_matches_dimensions(self):
        data, w, h = rr.render_panel([FakeReading(90), FakeReading(50),
                                      FakeReading(coarse="中档")],
                                     scale=1.0, height_px=40)
        self.assertEqual(len(data), w * h * 4)

    def test_pixels_are_premultiplied(self):
        data, w, h = rr.render_panel([FakeReading(90)], scale=1.0, height_px=40)
        for i in range(0, len(data), 4):
            b, g, r, a = data[i:i + 4]
            self.assertLessEqual(r, a)
            self.assertLessEqual(g, a)
            self.assertLessEqual(b, a)

    def test_rendered_has_visible_pixels(self):
        data, w, h = rr.render_panel([FakeReading(90), FakeReading(100),
                                      FakeReading(20)], scale=1.0, height_px=56)
        self.assertTrue(any(data[i + 3] for i in range(0, len(data), 4)))

    def test_error_ring_still_renders(self):
        data, w, h = rr.render_panel(
            [FakeReading(error="未连接"), FakeReading(error="x"),
             FakeReading(error="y")], scale=1.0, height_px=56)
        self.assertTrue(any(data[i + 3] for i in range(0, len(data), 4)))

    def test_missing_entries_pad_to_three(self):
        data, w, h = rr.render_panel([FakeReading(90)], scale=1.0, height_px=56)
        self.assertEqual(len(data), w * h * 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
