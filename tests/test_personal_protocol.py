# -*- coding: utf-8 -*-
"""Offline tests for the frozen personal protocol layer.

No hardware access: every case here uses synthesized frames, so the suite is
safe to run while the real devices are unplugged.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import personal_hid as ph
import personal_devices as pdv


def _sum_ok(frame: bytearray) -> bytes:
    frame[-1] = (0x55 - sum(frame[:-1])) & 0xFF
    return bytes(frame)


class RequestTests(unittest.TestCase):
    def test_mouse_battery_request_is_frozen(self):
        self.assertEqual(ph.build_request(4).hex(" "),
                         "08 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 49")

    def test_keyboard_uses_flag_at_byte5(self):
        self.assertEqual(ph.build_request(4, keyboard=True).hex(" "),
                         "08 04 00 00 00 80 00 00 00 00 00 00 00 00 00 00 c9")

    def test_online_command(self):
        self.assertEqual(ph.build_request(3)[1], 3)
        self.assertEqual(ph.build_request(3, keyboard=True)[5], 0x80)

    def test_rejects_unverified_commands(self):
        for command in (0, 1, 2, 5, 0x81, 0xFF):
            with self.assertRaises(ValueError):
                ph.build_request(command)
        for bad in (True, 4.0, "4"):
            with self.assertRaises(ValueError):
                ph.build_request(bad)


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.req = ph.build_request(4)
        self.kreq = ph.build_request(4, keyboard=True)

    def _reply(self, command, count, value, charge=0, byte5=0x00, byte0=0x08):
        frame = bytearray(17)
        frame[0] = byte0
        frame[1] = command
        frame[5] = byte5
        frame[6] = value
        frame[7] = charge
        return _sum_ok(frame)

    def test_accepts_real_mouse_reply(self):
        raw = bytes.fromhex("08 04 00 00 00 02 5a 00 0f b3 00 00 00 00 00 00 2b")
        self.assertEqual(ph.validate_response(raw, self.req)[6], 90)

    def test_accepts_real_keyboard_reply(self):
        raw = bytes.fromhex("08 04 00 00 00 84 64 00 10 bc 00 00 00 00 00 00 95")
        self.assertEqual(ph.validate_response(raw, self.kreq)[6], 100)

    def test_rejects_wrong_length(self):
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(bytes([8, 4, 0]), self.req)

    def test_rejects_header_mismatch(self):
        raw = bytearray(self._reply(4, 2, 50))
        raw[1] = 3
        _sum_ok(raw)
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(bytes(raw), self.req)

    def test_rejects_bad_checksum(self):
        raw = bytearray(self._reply(4, 2, 50))
        raw[-1] = (raw[-1] + 1) & 0xFF
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(bytes(raw), self.req)

    def test_rejects_out_of_range_percentage(self):
        for value in (101, 200, 255):
            with self.assertRaises(ph.ProtocolError):
                ph.validate_response(self._reply(4, 2, value), self.req)

    def test_rejects_zero_length_payload(self):
        raw = bytearray(self._reply(4, 2, 50))
        raw[5] = 0
        _sum_ok(raw)
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(bytes(raw), self.req)

    def test_rejects_keyboard_flag_mismatch(self):
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(self._reply(4, 2, 50, byte5=0x00), self.kreq)

    def test_rejects_online_as_battery(self):
        # command 03 reply must not satisfy a 04 request
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(self._reply(3, 2, 1), self.req)

    def test_rejects_short_valid_length_for_battery(self):
        raw = bytearray(self._reply(4, 2, 50))
        raw[5] = 0x01
        _sum_ok(raw)
        with self.assertRaises(ph.ProtocolError):
            ph.validate_response(bytes(raw), self.req)


class IdentityTests(unittest.TestCase):
    class FakeIface:
        def __init__(self, vid, pid, page, usage, inp, out, feat, path):
            self.vid, self.pid = vid, pid
            self.usage_page, self.usage = page, usage
            self.input_len, self.output_len, self.feature_len = inp, out, feat
            self.path = path

    def _ok(self, **over):
        values = dict(vid=0x373B, pid=0x10C9, page=0xFF02, usage=2,
                      inp=17, out=17, feat=0,
                      path=r"\\?\hid#vid_373b&pid_10c9&mi_01&col05#7&1&0&0004#{guid}")
        values.update(over)
        return self.FakeIface(**values)

    def test_matches_exact_identity(self):
        self.assertTrue(ph.matches_interface(self._ok(), "mouse"))

    def test_rejects_other_usage_page(self):
        self.assertFalse(ph.matches_interface(self._ok(page=0xFF04), "mouse"))

    def test_rejects_wrong_report_lengths(self):
        self.assertFalse(ph.matches_interface(self._ok(out=20), "mouse"))
        self.assertFalse(ph.matches_interface(self._ok(feat=8), "mouse"))

    def test_rejects_other_collection(self):
        self.assertFalse(ph.matches_interface(
            self._ok(path=r"\\?\hid#vid_373b&pid_10c9&mi_01&col04#x"), "mouse"))

    def test_keyboard_identity(self):
        kb = self._ok(vid=0x05AC, pid=0x024F,
                      path=r"\\?\hid#vid_05ac&pid_024f&mi_01&col05#7&2&0&0004#{guid}")
        self.assertTrue(ph.matches_interface(kb, "keyboard"))
        self.assertFalse(ph.matches_interface(kb, "mouse"))

    def test_unknown_key(self):
        self.assertFalse(ph.matches_interface(self._ok(), "headset"))


class ReadingTests(unittest.TestCase):
    def test_percentage_display(self):
        r = pdv.Reading("m", "s", level=90, key="mouse")
        self.assertTrue(r.ok)
        self.assertEqual(r.display_value, "90%")

    def test_coarse_is_ok_but_not_percentage(self):
        r = pdv.Reading("g", "s", coarse="中档", key="gamepad")
        self.assertTrue(r.ok)
        self.assertEqual(r.display_value, "中档")
        self.assertIsNone(r.level)

    def test_error_is_not_ok(self):
        r = pdv.Reading("g", "s", error="未连接", coarse="中档", key="gamepad")
        self.assertFalse(r.ok)
        self.assertEqual(r.display_value, "--")

    def test_empty_readings_shape(self):
        rows = pdv.empty_readings()
        self.assertEqual([r.key for r in rows], ["mouse", "keyboard", "gamepad"])
        self.assertTrue(all(not r.ok for r in rows))


class CompxSelectionTests(unittest.TestCase):
    class FakeIface:
        def __init__(self, vid, pid, page, usage, inp, out, feat, path, product="x"):
            self.vid, self.pid = vid, pid
            self.usage_page, self.usage = page, usage
            self.input_len, self.output_len, self.feature_len = inp, out, feat
            self.path = path
            self.product = product

    def _iface(self):
        return self.FakeIface(
            0x373B, 0x10C9, 0xFF02, 2, 17, 17, 0,
            r"\\?\hid#vid_373b&pid_10c9&mi_01&col05#7&1&0&0004#{guid}")

    def test_missing_receiver_reports_error_without_io(self):
        reading = pdv.read_compx("mouse", [])
        self.assertFalse(reading.ok)
        self.assertIn("未找到", reading.error)

    def test_duplicate_receivers_refused(self):
        reading = pdv.read_compx("mouse", [self._iface(), self._iface()])
        self.assertFalse(reading.ok)
        self.assertIn("多个", reading.error)

    def test_keyboard_ignores_mouse_interface(self):
        reading = pdv.read_compx("keyboard", [self._iface()])
        self.assertFalse(reading.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
