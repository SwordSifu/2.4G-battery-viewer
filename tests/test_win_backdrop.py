"""Offline backdrop contract tests: no actual DLL loading, Tk or desktop access."""
import ctypes as C
from dataclasses import FrozenInstanceError
import os
import unittest
from unittest.mock import MagicMock, patch

import win_backdrop as wb


class BackdropTests(unittest.TestCase):
    def setUp(self):
        self.root = MagicMock()
        self.api = MagicMock()
        self.api.hwnd.return_value = 1234
        self.api.high_contrast.return_value = False
        self.api.transparency_enabled.return_value = True
        self.api.composition.return_value = (0, True)
        self.api.set_attribute.return_value = 0
        self.api.extend_frame.return_value = 0
        for context in (patch.object(wb.sys, "platform", "win32"),
                        patch.object(wb, "_windows_build", return_value=26300),
                        patch.object(wb, "_load_native", return_value=self.api)):
            context.start()
            self.addCleanup(context.stop)

    def apply(self, **kwargs):
        return wb.apply_backdrop(self.root, **kwargs)

    def assert_solid(self, result, reason):
        self.assertEqual(result.applied, "solid")
        self.assertIn(reason, result.reason)
        self.assertEqual(result.client_background, "#10151f")
        self.assertFalse(result.frame_extended)
        self.assertFalse(result.client_translucency_verified)
        self.root.configure.assert_called_with(background="#10151f")

    def assert_reset(self):
        self.api.set_attribute.assert_any_call(1234, 38, 1)
        self.api.extend_frame.assert_called_with(1234, 0)

    def test_acrylic_uses_only_documented_attributes_and_full_extension(self):
        result = self.apply()
        self.assertEqual(result.requested, "acrylic")
        self.assertEqual(result.applied, "acrylic")
        self.assertTrue(result.native_accepted)
        self.assertTrue(result.frame_extended)
        self.assertEqual(result.client_background, "#000000")
        self.assertEqual([call.args for call in self.api.set_attribute.call_args_list],
                         [(1234, 20, 1), (1234, 33, 2), (1234, 38, 3)])
        self.api.extend_frame.assert_called_once_with(1234, -1)
        self.root.configure.assert_called_with(background="#000000")
        self.assertFalse(result.client_translucency_verified)
        self.assertIn("client-unverified", result.reason)
        self.assertTrue(all(status.succeeded for status in result.api_status))
        self.root.after.assert_not_called()
        self.root.attributes.assert_not_called()

    def test_selective_margins_preserve_ordinary_client_region(self):
        result = self.apply(frame_margins=(32, 32, 224, 32))
        self.assertTrue(result.frame_extended)
        self.api.extend_frame.assert_called_once_with(1234, (32, 32, 224, 32))
        self.assertEqual(result.api_status[-1].value, (32, 32, 224, 32))

    def test_invalid_margins_rejected_before_native_access(self):
        for margins in ((1, 2), (-1, 1, 1, 1), (True, 1, 1, 1), (1, 1, 1, 99999), "1234"):
            with self.subTest(margins=margins), self.assertRaises(ValueError):
                self.apply(frame_margins=margins)
        self.api.hwnd.assert_not_called()

    def test_solid_kind_is_an_explicit_safe_choice(self):
        result = self.apply(kind="solid")
        self.assert_solid(result, "forced-solid")
        self.assertEqual(result.requested, "solid")
        self.api.high_contrast.assert_not_called()

    def test_mica_is_type_two_and_is_not_claimed_to_be_glass(self):
        result = self.apply(kind="mica")
        self.api.set_attribute.assert_any_call(1234, 38, 2)
        self.assertEqual(result.applied, "mica")
        self.assertFalse(result.client_translucency_verified)

    def test_unknown_kind_is_rejected_before_native_calls(self):
        with self.assertRaises(ValueError):
            self.apply(kind="accent")
        self.api.hwnd.assert_not_called()

    def test_result_and_nested_api_records_are_immutable(self):
        result = self.apply()
        with self.assertRaises(FrozenInstanceError):
            result.applied = "glass"
        with self.assertRaises(FrozenInstanceError):
            result.api_status[0].hresult = -1
        self.assertIsInstance(result.api_status, tuple)

    def test_non_windows_does_not_load_dlls(self):
        with patch.object(wb.sys, "platform", "linux"), patch.object(wb, "_load_native") as load:
            self.assert_solid(self.apply(), "unsupported-platform")
            load.assert_not_called()

    def test_dll_load_failure_is_solid(self):
        with patch.object(wb, "_load_native", side_effect=OSError("no dwm")):
            self.assert_solid(self.apply(), "native-unavailable")
        self.api.set_attribute.assert_not_called()

    def test_foreign_or_invalid_hwnd_never_receives_native_writes(self):
        self.api.hwnd.side_effect = OSError("refusing HWND not owned by this process")
        self.assert_solid(self.apply(), "refusing HWND")
        self.api.set_attribute.assert_not_called()
        self.api.extend_frame.assert_not_called()

    def test_force_solid_resets_without_querying_accessibility(self):
        result = self.apply(force_solid=True)
        self.assert_solid(result, "forced-solid")
        self.assertFalse(result.native_accepted)
        self.assert_reset()
        self.api.high_contrast.assert_not_called()

    def test_solid_chrome_is_cosmetic_and_never_requests_transparency(self):
        result = wb.style_chrome(self.root)
        self.assertEqual(len(result), 2)
        self.assertEqual([call.args for call in self.api.set_attribute.call_args_list], [(1234, 20, 1), (1234, 33, 2)])
        self.api.extend_frame.assert_not_called()
        self.api.transparency_enabled.assert_not_called()

    def test_chrome_respects_high_contrast_and_foreign_window_guard(self):
        self.api.high_contrast.return_value = True
        self.assertEqual(wb.style_chrome(self.root), ())
        self.api.set_attribute.assert_not_called()
        self.api.hwnd.side_effect = OSError("foreign window")
        self.assertEqual(wb.style_chrome(self.root), ())
        self.api.set_attribute.assert_not_called()

    def test_restore_is_repeatable_solid_reset(self):
        for _ in range(2):
            self.assert_solid(wb.restore_backdrop(self.root), "forced-solid")
        self.assert_reset()

    def test_unsupported_build_never_requests_material(self):
        with patch.object(wb, "_windows_build", return_value=22000):
            self.assert_solid(self.apply(), "unsupported-build")
        self.assertEqual([call.args for call in self.api.set_attribute.call_args_list], [(1234, 38, 1)])
        self.assert_reset()

    def test_build_22621_is_supported(self):
        with patch.object(wb, "_windows_build", return_value=22621):
            self.assertEqual(self.apply().applied, "acrylic")

    def test_high_contrast_does_not_override_caption_or_corners(self):
        self.api.high_contrast.return_value = True
        self.assert_solid(self.apply(), "high-contrast-enabled")
        self.api.transparency_enabled.assert_not_called()
        self.assertEqual([call.args for call in self.api.set_attribute.call_args_list], [(1234, 38, 1)])
        self.assert_reset()

    def test_disabled_transparency_is_solid(self):
        self.api.transparency_enabled.return_value = False
        self.assert_solid(self.apply(), "transparency-disabled")
        self.api.composition.assert_not_called()
        self.assert_reset()

    def test_preference_query_errors_fail_closed(self):
        for name in ("high_contrast", "transparency_enabled", "composition"):
            with self.subTest(name=name):
                method = getattr(self.api, name)
                method.side_effect = OSError("query failed")
                self.assert_solid(self.apply(), "preference-query-failed")
                self.assert_reset()
                method.side_effect = None

    def test_composition_false_or_failed_hresult_is_solid(self):
        for result in ((0, False), (-1, True), (0x80004005, True)):
            self.api.composition.return_value = result
            outcome = self.apply()
            self.assert_solid(outcome, "composition-unavailable")
            self.assertEqual(outcome.api_status[0].api, "DwmIsCompositionEnabled")
            self.assert_reset()

    def test_any_required_attribute_failure_falls_back_and_records_hresult(self):
        for failed, reason in ((20, "dark-caption"), (33, "corner"), (38, "system-backdrop")):
            with self.subTest(attribute=failed):
                self.api.set_attribute.side_effect = lambda hwnd, attr, value: (
                    0x80070057 if attr == failed and not (attr == 38 and value == 1) else 0)
                result = self.apply()
                self.assert_solid(result, reason)
                self.assertFalse(result.native_accepted)
                failed_status = [s for s in result.api_status if s.attribute == failed and not s.succeeded]
                self.assertEqual(failed_status[0].hresult, -2147024809)
                self.assert_reset()

    def test_frame_extension_failure_rolls_back_but_preserves_acceptance_fact(self):
        self.api.extend_frame.side_effect = lambda hwnd, value: -1 if value == -1 else 0
        result = self.apply()
        self.assert_solid(result, "extended-frame-api-failed")
        self.assertTrue(result.native_accepted)
        self.assert_reset()

    def test_native_call_exception_is_reported_and_falls_back(self):
        self.api.set_attribute.side_effect = OSError("missing API")
        result = self.apply()
        self.assert_solid(result, "dark-caption-api-failed")
        self.assertIsNone(result.api_status[1].hresult)
        self.assertIn("missing API", result.api_status[1].error)
        self.assert_reset()

    def test_failed_rollback_still_recommends_opaque_client_and_reports_failure(self):
        self.api.set_attribute.return_value = -1
        self.api.extend_frame.return_value = -2
        result = self.apply(force_solid=True)
        self.assert_solid(result, "forced-solid")
        self.assertEqual([s.hresult for s in result.api_status], [-1, -2])
        self.assertIn("native-rollback-incomplete", result.reason)
        self.assertIn("not-verified", result.reason)

    def test_root_paint_failure_does_not_leave_extended_black_policy(self):
        self.root.configure.side_effect = RuntimeError("destroyed")
        result = self.apply()
        self.assertEqual(result.applied, "solid")
        self.assertEqual(result.client_background, "#10151f")
        self.assertIn("root-background-unavailable", result.reason)
        self.assert_reset()

    def test_hresult_success_includes_nonnegative_success_codes(self):
        self.api.set_attribute.return_value = 1
        self.api.extend_frame.return_value = 1
        self.assertEqual(self.apply().applied, "acrylic")


class OwnedWindowTests(unittest.TestCase):
    def setUp(self):
        # Bypass real WinDLL construction; these tests run offline everywhere.
        self.native = object.__new__(wb._WindowsAPI)
        self.native.user = MagicMock()
        self.native.user.GetAncestor.return_value = 777
        self.root = MagicMock()
        self.root.winfo_id.return_value = 555

    def process(self, pid):
        def identify(hwnd, out):
            C.cast(out, C.POINTER(C.c_uint32)).contents.value = pid
            return 99
        self.native.user.GetWindowThreadProcessId.side_effect = identify

    def test_resolves_tk_wrapper_and_checks_process_before_returning(self):
        self.process(os.getpid())
        self.assertEqual(self.native.hwnd(self.root), 777)
        self.native.user.GetAncestor.assert_called_once_with(555, 2)
        self.root.update_idletasks.assert_called_once()

    def test_refuses_production_or_any_foreign_process(self):
        self.process(os.getpid() + 1)
        with self.assertRaisesRegex(OSError, "not owned"):
            self.native.hwnd(self.root)

    def test_refuses_missing_wrapper(self):
        self.native.user.GetAncestor.return_value = None
        with self.assertRaisesRegex(OSError, "unavailable"):
            self.native.hwnd(self.root)


if __name__ == "__main__":
    unittest.main()
