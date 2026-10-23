"""Headless preview contracts. No Tk root, native windows, configuration or HID."""
import builtins
from dataclasses import FrozenInstanceError
import importlib.util
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch
import zlib

import fluent_theme as theme
import ui_preview


def decode_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    at, compressed, size = 8, b"", None
    while at < len(data):
        length = struct.unpack("!I", data[at:at+4])[0]
        kind, content = data[at+4:at+8], data[at+8:at+8+length]
        crc = struct.unpack("!I", data[at+8+length:at+12+length])[0]
        assert crc == zlib.crc32(kind+content)&0xffffffff
        if kind == b"IHDR": size = struct.unpack("!2I", content[:8])
        elif kind == b"IDAT": compressed += content
        at += 12+length
    width, height = size
    raw = zlib.decompress(compressed)
    rows = []
    for y in range(height):
        start = y*(width*4+1)
        assert raw[start] == 0
        rows.append(raw[start+1:start+1+width*4])
    return width, height, b"".join(rows)


class ThemeTests(unittest.TestCase):
    def test_fixtures_are_immutable_and_controller_has_no_fake_percent(self):
        for key, _ in theme.SCENES:
            rows = theme.demo_readings(key)
            self.assertEqual(len(rows), 3)
            self.assertEqual([r.key for r in rows], ["mouse", "keyboard", "gamepad"])
            self.assertIsNone(rows[2].level)
            self.assertNotIn("%", rows[2].display_value)
            with self.assertRaises(FrozenInstanceError): rows[0].level = 1

    def test_ring_and_panel_use_identical_status_palette_and_thresholds(self):
        import fluent_ring_render as rings
        for level in (0, 9, 10, 24, 25, 29, 60, 100):
            row = theme.PreviewReading("mouse", "fixture", "", level=level)
            self.assertEqual(theme.rgb(theme.state_color(row)), rings.ring_color(level))
        self.assertEqual(theme.rgb(theme.PALETTE["blue"]), rings.INFO)

    def test_offline_wins_over_values_and_unknown_scene_rejected(self):
        self.assertTrue(all(not r.ok and r.display_value == "—" for r in theme.demo_readings("offline")))
        with self.assertRaises(ValueError): theme.demo_readings("anything")

    def test_low_colors_and_wired_not_low_battery(self):
        mouse, keyboard, controller = theme.demo_readings("low")
        self.assertEqual(theme.state_color(mouse), theme.PALETTE["amber"])
        self.assertEqual(theme.state_color(keyboard), theme.PALETTE["red"])
        self.assertEqual(theme.demo_readings("charging")[2].coarse, "有线")

    def test_png_round_trip_crc_and_vertical_endpoints(self):
        width, height, pixels = decode_png(theme.gradient_png(4, 3, "#102030", "#405060"))
        self.assertEqual((width, height), (4, 3))
        self.assertEqual(pixels[:4], bytes((16, 32, 48, 255)))
        self.assertEqual(pixels[-4:], bytes((64, 80, 96, 255)))

    def test_rounded_gradient_has_transparent_corners_not_whole_window_alpha(self):
        _, _, pixels = decode_png(theme.gradient_png(20, 12, "#102030", "#203040", 6))
        self.assertEqual(pixels[3], 0)
        self.assertEqual(pixels[(6*20+10)*4+3], 255)
        self.assertTrue(any(0 < pixels[i] < 255 for i in range(3, len(pixels), 4)))

    def test_bgra_unpremultiply_preserves_coverage_and_color_order(self):
        _, _, pixels = decode_png(theme.bgra_png(bytes((0, 0, 64, 128, 2, 3, 4, 255)), 2, 1))
        self.assertEqual(pixels, bytes((128, 0, 0, 128, 4, 3, 2, 255)))

    def test_invalid_sizes_and_buffers_rejected(self):
        for dimensions in ((0, 1), (1, 0), (8193, 1)):
            with self.assertRaises(ValueError): theme.gradient_png(*dimensions, "#000000", "#ffffff")
        with self.assertRaises(ValueError): theme.rgba_png(2, 1, b"x")
        with self.assertRaises(ValueError): theme.bgra_png(b"x", 1, 1)

    def test_importing_preview_does_not_import_or_invoke_production_integrations(self):
        original = builtins.__import__
        forbidden = {"personal_devices", "personal_hid", "tray_common", "win_shell"}
        def checked(name, *args, **kwargs):
            if name.split(".")[0] in forbidden: raise AssertionError("production import: "+name)
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("isolated_preview_import", Path(ui_preview.__file__))
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, "__import__", side_effect=checked), patch.object(ui_preview.ctypes, "WinDLL", create=True) as loader:
            spec.loader.exec_module(module)
        loader.assert_not_called()


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = MagicMock()
        self.controller = ui_preview.PreviewController(self.root)
        self.controller.panel = MagicMock()
        self.corner = MagicMock()
        self.controller.corner = self.corner

    def test_hide_and_show_reuse_window_and_update_only_when_explicit(self):
        self.corner._visible = True
        self.assertFalse(self.controller.toggle_float())
        self.corner.hide.assert_called_once()
        self.corner.destroy.assert_not_called()
        self.controller.panel.set_float_visible.assert_called_with(False)
        self.corner._visible = False
        self.assertTrue(self.controller.toggle_float())
        self.corner.show.assert_called_once()

    def test_scene_update_never_shows_or_recreates_hidden_float(self):
        self.corner._visible = False
        rows = theme.demo_readings("offline")
        self.controller.update_rows(rows)
        self.corner.update.assert_called_once_with(rows)
        self.corner.show.assert_not_called()

    def test_local_interval_and_lock_do_not_persist_or_use_production_callbacks(self):
        self.controller.set_interval(120)
        self.assertEqual(self.controller.interval, 120)
        self.corner.locked = True
        self.controller.toggle_lock()
        self.corner.set_locked.assert_called_once_with(False)
        self.controller.toggle_lock(keep=True)
        self.assertEqual(self.corner.set_locked.call_count, 1)

    def test_close_is_idempotent_and_terminal(self):
        self.controller.close()
        self.controller.close()
        self.corner.destroy.assert_called_once()
        self.root.destroy.assert_called_once()
        self.assertFalse(self.controller.toggle_float())

    def test_root_closes_even_if_preview_float_cleanup_fails(self):
        self.corner.destroy.side_effect = RuntimeError("fake cleanup failure")
        with self.assertRaises(RuntimeError): self.controller.close()
        self.root.destroy.assert_called_once()

    def test_invalid_auto_close_rejected_before_any_window_creation(self):
        for value in ("nan", "inf", "-1", "0", "601"):
            with self.subTest(value=value), patch.object(sys, "argv", ["ui_preview.py", "--close-after", value]), \
                 patch.object(ui_preview, "build_preview") as build, patch("sys.stderr"):
                with self.assertRaises(SystemExit): ui_preview.main()
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
