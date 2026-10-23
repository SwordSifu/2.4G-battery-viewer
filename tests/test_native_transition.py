"""Headless strip tests: DLLs, Tk, windows and every GDI allocation are faked."""
import ctypes as C
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import native_transition as nt


OWNER = 0x123456789 if C.sizeof(C.c_void_p) == 8 else 101
POPUP = 202


class FakeRoot:
    def __init__(self):
        self.width, self.height = 320, 240
        self.x, self.y = -500, 50
        self.viewable = True
        self.bindings = {}
        self.unbound = []
        self.after = MagicMock()
        self.attributes = MagicMock()

    def winfo_id(self):
        return 11

    def winfo_width(self):
        return self.width

    def winfo_height(self):
        return self.height

    def winfo_rootx(self):
        return self.x

    def winfo_rooty(self):
        return self.y

    def winfo_viewable(self):
        return self.viewable

    def bind(self, event, callback, add):
        assert add == "+"
        self.bindings[event] = callback
        return event + "-token"

    def unbind(self, event, token):
        self.unbound.append((event, token))
        self.bindings.pop(event, None)


class FakeDLLs:
    def __init__(self):
        self.user, self.gdi, self.kernel = MagicMock(), MagicMock(), MagicMock()
        self.alive = {11, OWNER}
        self.foreign = set()
        self.visible = {OWNER}
        self.iconic = False
        self.topmost = False
        self.destroy_success = True
        self.unregister_success = True
        self.create_success = True
        self.bitmap_success = True
        self.bits_success = True
        self.position_success = True
        self.events = []
        self.buffers = []
        self.uploads = []
        self.positions = []
        self.callback = None
        self.class_name = None
        self.user.GetAncestor.return_value = OWNER
        self.user.GetWindowThreadProcessId.side_effect = self.identify
        self.user.IsWindow.side_effect = lambda hwnd: hwnd in self.alive
        self.user.IsWindowVisible.side_effect = lambda hwnd: hwnd in self.visible
        self.user.IsIconic.side_effect = lambda hwnd: self.iconic if hwnd == OWNER else False
        self.user.GetWindowLongPtrW.side_effect = lambda *args: nt.WS_EX_TOPMOST if self.topmost else 0
        self.user.RegisterClassExW.side_effect = self.register
        self.user.UnregisterClassW.side_effect = self.unregister
        self.user.CreateWindowExW.side_effect = self.create
        self.user.DestroyWindow.side_effect = self.destroy
        self.user.ShowWindow.side_effect = self.show
        self.user.SetWindowPos.side_effect = self.position
        self.user.ValidateRect.return_value = 1
        self.user.DefWindowProcW.return_value = 7
        self.user.GetDC.return_value = 11
        self.user.ReleaseDC.side_effect = self.cleanup("release")
        self.gdi.CreateCompatibleDC.return_value = 22
        self.gdi.CreateDIBSection.side_effect = self.dib
        self.gdi.SelectObject.side_effect = self.select
        self.gdi.DeleteDC.side_effect = self.cleanup("delete_dc")
        self.gdi.DeleteObject.side_effect = self.cleanup("delete_bitmap")
        self.user.UpdateLayeredWindow.side_effect = self.upload
        self.user.UpdateLayeredWindow.return_value = 1
        self.kernel.GetModuleHandleW.return_value = 303
        self.loader = MagicMock(side_effect=lambda name, **kwargs: {
            "user32": self.user, "gdi32": self.gdi, "kernel32": self.kernel}[name])

    def identify(self, hwnd, pointer):
        self.events.append(("pid", hwnd))
        C.cast(pointer, C.POINTER(C.c_uint32)).contents.value = (
            os.getpid() + 1 if hwnd in self.foreign else os.getpid())
        return 88 if hwnd in self.alive else 0

    def register(self, pointer):
        record = C.cast(pointer, C.POINTER(nt.WNDCLASSEXW)).contents
        assert record.cbSize == C.sizeof(nt.WNDCLASSEXW)
        assert not record.hIcon and not record.hbrBackground
        self.callback = record.lpfnWndProc
        self.class_name = record.lpszClassName
        self.events.append(("register", self.class_name))
        return 1

    def unregister(self, name, instance):
        self.events.append(("unregister", name))
        return self.unregister_success

    def create(self, *args):
        self.events.append(("create", args[8]))
        if not self.create_success:
            return 0
        self.alive.add(POPUP)
        return POPUP

    def destroy(self, hwnd):
        self.events.append(("destroy", hwnd))
        if self.destroy_success:
            self.callback(hwnd, nt.WM_NCDESTROY, 0, 0)
            self.alive.discard(hwnd)
            self.visible.discard(hwnd)
        return self.destroy_success

    def show(self, hwnd, how):
        was_visible = hwnd in self.visible
        if how == nt.SW_HIDE:
            self.visible.discard(hwnd)
        else:
            # Simulate the implicit show Z-order request: the WndProc must
            # suppress its raising, despite SW_SHOWNOACTIVATE not activating.
            position = nt.WINDOWPOS(hwnd, None, 0, 0, 0, 0, 0)
            self.callback(hwnd, nt.WM_WINDOWPOSCHANGING, 0, C.addressof(position))
            self.positions.append(("show", position.flags))
            self.visible.add(hwnd)
        return was_visible

    def position(self, hwnd, insert_after, x, y, width, height, flags):
        position = nt.WINDOWPOS(hwnd, insert_after, x, y, width, height, flags)
        self.callback(hwnd, nt.WM_WINDOWPOSCHANGING, 0, C.addressof(position))
        self.positions.append(("position", position.flags, insert_after))
        return self.position_success

    def dib(self, dc, pointer, usage, bits, section, offset):
        info = C.cast(pointer, C.POINTER(nt.BITMAPINFO)).contents.bmiHeader
        self.dib_info = (info.biSize, info.biWidth, info.biHeight, info.biPlanes,
                         info.biBitCount, info.biCompression, info.biSizeImage)
        if not self.bitmap_success:
            return 0
        if self.bits_success:
            buffer = C.create_string_buffer(info.biSizeImage)
            self.buffers.append(buffer)
            C.cast(bits, C.POINTER(C.c_void_p)).contents.value = C.addressof(buffer)
        return 44

    def select(self, dc, bitmap):
        self.events.append(("select", bitmap))
        return 33 if bitmap == 44 else 44

    def cleanup(self, name):
        def call(*args):
            self.events.append((name, *args))
            return 1
        return call

    def upload(self, hwnd, dc, destination, size, memory, source, key, blend, flags):
        dest = C.cast(destination, C.POINTER(nt.POINT)).contents
        dimensions = C.cast(size, C.POINTER(nt.SIZE)).contents
        src = C.cast(source, C.POINTER(nt.POINT)).contents
        alpha = C.cast(blend, C.POINTER(nt.BLENDFUNCTION)).contents
        self.uploads.append((hwnd, dest.x, dest.y, dimensions.cx, dimensions.cy,
                             src.x, src.y, key, alpha.BlendOp, alpha.BlendFlags,
                             alpha.SourceConstantAlpha, alpha.AlphaFormat, flags))
        return self.user.UpdateLayeredWindow.return_value


class BufferTests(unittest.TestCase):
    def test_smoothstep_endpoints_uniform_rows_and_premultiplication(self):
        data = nt.transition_bgra(3, 5, (24, 37, 58))
        self.assertIsInstance(data, bytes)
        self.assertEqual(len(data), 60)
        self.assertEqual(data[:12], b"\0" * 12)
        self.assertEqual(data[-12:], bytes((58, 37, 24, 255)) * 3)
        self.assertEqual([data[y * 12 + 3] for y in range(5)], [0, 40, 128, 215, 255])
        for y in range(5):
            row = data[y * 12:(y + 1) * 12]
            self.assertEqual(row, row[:4] * 3)
            b, g, r, a = row[:4]
            self.assertEqual((b, g, r), tuple((v * a + 127) // 255 for v in (58, 37, 24)))
            self.assertTrue(all(value <= a for value in (b, g, r)))

    def test_opacity_is_monotone_and_eases_at_both_ends(self):
        alpha = nt.transition_bgra(1, 33)[3::4]
        self.assertEqual(list(alpha), sorted(alpha))
        self.assertLess(alpha[1] - alpha[0], alpha[16] - alpha[15])
        self.assertLess(alpha[-1] - alpha[-2], alpha[16] - alpha[15])
        for first, last in zip(alpha, reversed(alpha)):
            self.assertLessEqual(abs(first + last - 255), 1)

    def test_float_color_is_finite_bounded_and_rounded(self):
        self.assertEqual(nt.transition_bgra(2, 3, [23.6, 37.0, 58.4]),
                         nt.transition_bgra(2, 3, (24, 37, 58)))
        self.assertEqual(nt.transition_bgra(1, 2, (0, 255, 0))[-4:], bytes((0, 255, 0, 255)))

    def test_invalid_dimensions_are_rejected(self):
        for width, height in ((0, 2), (-1, 2), (8193, 2), (1, 0), (1, -1), (1, 513),
                              (True, 2), (2, False), (2.0, 2), (2, 2.0), (None, 2),
                              (float("inf"), 2), (2, float("nan"))):
            with self.subTest(width=width, height=height), self.assertRaises(ValueError):
                nt.transition_bgra(width, height)

    def test_invalid_colors_are_rejected(self):
        for color in (None, (), (1, 2), (1, 2, 3, 4), "123", (True, 2, 3),
                      (float("nan"), 1, 2), (1, float("inf"), 2), (1, 2, -1),
                      (256, 1, 2), (1, "2", 3), (1, complex(2, 1), 3)):
            with self.subTest(color=color), self.assertRaises(ValueError):
                nt.transition_bgra(1, 2, color)

    def test_dimension_limits_and_degenerate_single_row(self):
        self.assertEqual(len(nt.transition_bgra(8192, 2)), 8192 * 2 * 4)
        self.assertEqual(len(nt.transition_bgra(1, 512)), 512 * 4)
        self.assertEqual(nt.transition_bgra(1, 1), bytes((58, 37, 24, 255)))

    def test_import_and_construction_never_load_dlls(self):
        spec = importlib.util.spec_from_file_location("transition_import_probe", Path(nt.__file__))
        module = importlib.util.module_from_spec(spec)
        with patch.object(C, "WinDLL", create=True) as load:
            spec.loader.exec_module(module)
            module.TransitionStrip(FakeRoot())
            load.assert_not_called()

    def test_win32_structures_have_fixed_width_and_pointer_sized_fields(self):
        self.assertEqual(C.sizeof(nt.POINT), 8)
        self.assertEqual(C.sizeof(nt.SIZE), 8)
        self.assertEqual(C.sizeof(nt.BITMAPINFOHEADER), 40)
        self.assertEqual(C.sizeof(nt.BLENDFUNCTION), 4)
        self.assertEqual(C.sizeof(nt.WNDCLASSEXW), 80 if C.sizeof(C.c_void_p) == 8 else 48)
        self.assertEqual(C.sizeof(nt.WINDOWPOS), 40 if C.sizeof(C.c_void_p) == 8 else 28)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.root = FakeRoot()
        self.dll = FakeDLLs()
        self.platform = patch.object(nt.sys, "platform", "win32")
        self.loading = patch.object(C, "WinDLL", self.dll.loader, create=True)
        self.platform.start()
        self.loading.start()
        self.strip = nt.TransitionStrip(self.root)
        self.addCleanup(self.platform.stop)
        self.addCleanup(self.loading.stop)
        self.addCleanup(self.clean)

    def clean(self):
        self.dll.foreign.clear()
        self.dll.destroy_success = self.dll.unregister_success = True
        self.dll.user.DestroyWindow.side_effect = self.dll.destroy
        self.dll.user.UnregisterClassW.side_effect = self.dll.unregister
        self.dll.user.IsWindow.side_effect = lambda hwnd: hwnd in self.dll.alive
        self.dll.user.GetWindowThreadProcessId.side_effect = self.dll.identify
        self.strip.destroy()

    def present(self, **kwargs):
        return self.strip.present(84, 108, **kwargs)

    def assert_no_window_writes(self):
        for name in ("RegisterClassExW", "CreateWindowExW", "UpdateLayeredWindow",
                     "SetWindowPos", "ShowWindow", "DestroyWindow", "UnregisterClassW"):
            getattr(self.dll.user, name).assert_not_called()

    def test_lazy_typed_owned_popup_uses_root_client_origin(self):
        self.dll.loader.assert_not_called()
        self.assertTrue(self.present(side_margin_px=5))
        self.assertEqual([c.args[0] for c in self.dll.loader.call_args_list],
                         ["user32", "gdi32", "kernel32"])
        self.assertEqual(self.dll.user.GetAncestor.call_args.args, (11, nt.GA_ROOT))
        self.assertLess(self.dll.events.index(("pid", OWNER)),
                        next(i for i, event in enumerate(self.dll.events) if event[0] == "register"))
        create = self.dll.user.CreateWindowExW.call_args.args
        self.assertEqual(create[0], nt.WS_EX_LAYERED | nt.WS_EX_TOOLWINDOW |
                         nt.WS_EX_NOACTIVATE | nt.WS_EX_TRANSPARENT)
        self.assertFalse(create[0] & nt.WS_EX_TOPMOST)
        self.assertEqual((create[2], create[3], create[8]), ("", nt.WS_POPUP, OWNER))
        self.assertEqual(self.dll.uploads[0],
                         (POPUP, -495, 134, 310, 24, 0, 0, 0, 0, 0, 255, nt.AC_SRC_ALPHA, nt.ULW_ALPHA))
        self.assertEqual(self.dll.dib_info, (40, 310, -24, 1, 32, 0, 310 * 24 * 4))
        self.assertEqual(self.dll.buffers[0].raw, nt.transition_bgra(310, 24))
        self.dll.user.ShowWindow.assert_called_once_with(POPUP, nt.SW_SHOWNOACTIVATE)
        self.assertIs(self.dll.user.CreateWindowExW.restype, C.c_void_p)
        self.assertIs(self.dll.user.DefWindowProcW.restype, C.c_ssize_t)
        self.assertIs(self.dll.gdi.SelectObject.restype, C.c_void_p)
        self.assertEqual(len(self.dll.user.UpdateLayeredWindow.argtypes), 9)
        self.root.attributes.assert_not_called()
        self.root.after.assert_not_called()

    def test_owner_relative_z_only_and_show_never_implicitly_raises(self):
        self.assertTrue(self.present())
        call = self.dll.user.SetWindowPos.call_args.args
        self.assertEqual(call[1], OWNER)
        self.assertTrue(call[-1] & nt.SWP_NOOWNERZORDER)
        self.assertTrue(call[-1] & nt.SWP_NOACTIVATE)
        show_flags = [row[1] for row in self.dll.positions if row[0] == "show"]
        self.assertTrue(show_flags)
        self.assertTrue(all(flags & nt.SWP_NOZORDER and flags & nt.SWP_NOACTIVATE for flags in show_flags))
        self.dll.user.SetForegroundWindow.assert_not_called()
        self.dll.user.BringWindowToTop.assert_not_called()

    def test_same_surface_and_position_only_moves_do_not_upload(self):
        with patch.object(nt, "transition_bgra", wraps=nt.transition_bgra) as pixels:
            self.assertTrue(self.present())
            self.assertTrue(self.present())
            self.assertEqual(self.dll.user.SetWindowPos.call_count, 1)
            self.root.x, self.root.y = -700, 100
            self.assertTrue(self.present())
            self.assertTrue(self.strip.present(85, 109))
            self.assertEqual(pixels.call_count, 1)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)
        self.assertEqual(self.dll.user.CreateWindowExW.call_count, 1)
        move = self.dll.user.SetWindowPos.call_args.args
        self.assertEqual(move[:6], (POPUP, None, -700, 185, 0, 0))
        self.assertTrue(move[-1] & nt.SWP_NOZORDER)
        self.assertTrue(move[-1] & nt.SWP_NOSIZE)
        self.assertTrue(move[-1] & nt.SWP_NOACTIVATE)
        self.assertEqual(self.dll.gdi.CreateDIBSection.call_count, 1)

    def test_resize_and_color_replace_single_entry_but_never_create_second_window(self):
        with patch.object(nt, "transition_bgra", wraps=nt.transition_bgra) as pixels:
            self.assertTrue(self.present())
            original = self.strip._pixels
            self.root.width += 2
            self.assertTrue(self.present())
            self.assertIsNot(original, self.strip._pixels)
            self.assertTrue(self.present(color_rgb=(40, 50, 60)))
            self.assertTrue(self.present(color_rgb=(40.1, 50.1, 60.1)))
            self.assertEqual(pixels.call_count, 3)
        self.assertEqual(self.strip._pixel_key, (322, 24, (40, 50, 60)))
        self.assertIsInstance(self.strip._pixels, bytes)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 3)
        self.assertEqual(self.dll.user.CreateWindowExW.call_count, 1)
        self.assertEqual(self.dll.gdi.DeleteDC.call_count, 3)
        self.assertEqual(self.dll.gdi.DeleteObject.call_count, 3)
        self.assertEqual(self.dll.user.ReleaseDC.call_count, 3)

    def test_hide_and_reshow_reuse_native_pixels_and_are_idempotent(self):
        self.assertTrue(self.present())
        self.assertTrue(self.strip.hide())
        self.assertTrue(self.strip.hide())
        self.assertEqual(self.dll.user.ShowWindow.call_count, 2)
        self.assertTrue(self.present())
        self.assertEqual(self.dll.user.ShowWindow.call_count, 3)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)
        self.assertEqual(self.dll.user.SetWindowPos.call_count, 1)

    def test_unviewable_root_does_not_even_load_dlls(self):
        self.root.viewable = False
        self.assertFalse(self.present())
        self.dll.loader.assert_not_called()
        self.assert_no_window_writes()

    def test_unviewable_iconic_or_hidden_root_hides_existing_strip(self):
        self.assertTrue(self.present())
        for state in ("unviewable", "iconic", "hidden"):
            with self.subTest(state=state):
                self.root.viewable = state != "unviewable"
                self.dll.iconic = state == "iconic"
                if state == "hidden":
                    self.dll.visible.discard(OWNER)
                self.assertFalse(self.present())
                self.assertNotIn(POPUP, self.dll.visible)
                self.root.viewable = True
                self.dll.iconic = False
                self.dll.visible.add(OWNER)
                self.assertTrue(self.present())
        self.assertEqual(self.dll.user.CreateWindowExW.call_count, 1)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)

    def test_iconic_hidden_and_topmost_owners_never_create_a_strip(self):
        for state in ("iconic", "hidden", "topmost"):
            with self.subTest(state=state):
                self.dll.iconic = state == "iconic"
                self.dll.topmost = state == "topmost"
                if state == "hidden":
                    self.dll.visible.discard(OWNER)
                else:
                    self.dll.visible.add(OWNER)
                self.assertFalse(self.present())
                self.assert_no_window_writes()

    def test_foreign_owner_or_missing_wrapper_never_receives_native_writes(self):
        self.dll.foreign.add(OWNER)
        self.assertFalse(self.present())
        self.assert_no_window_writes()
        self.dll.foreign.clear()
        self.dll.user.GetAncestor.return_value = None
        self.assertFalse(self.present())
        self.assert_no_window_writes()

    def test_failed_thread_pid_query_is_not_an_ownership_success(self):
        self.dll.user.GetWindowThreadProcessId.side_effect = lambda *args: 0
        self.assertFalse(self.present())
        self.assert_no_window_writes()

    def test_owner_change_or_foreign_owner_after_creation_only_hides_own_popup(self):
        self.assertTrue(self.present())
        self.dll.foreign.add(OWNER)
        self.assertFalse(self.present())
        self.dll.user.ShowWindow.assert_called_with(POPUP, nt.SW_HIDE)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)
        self.assertEqual(self.dll.user.SetWindowPos.call_count, 1)
        self.dll.foreign.clear()
        self.dll.alive.add(909)
        self.dll.user.GetAncestor.return_value = 909
        self.assertFalse(self.present())
        self.assertEqual(self.dll.user.CreateWindowExW.call_count, 1)

    def test_foreign_popup_handle_is_never_uploaded_shown_moved_or_destroyed(self):
        self.dll.foreign.add(POPUP)
        self.assertFalse(self.present())
        self.dll.user.UpdateLayeredWindow.assert_not_called()
        self.dll.user.ShowWindow.assert_not_called()
        self.dll.user.SetWindowPos.assert_not_called()
        self.assertFalse(self.strip.destroy())
        self.dll.user.DestroyWindow.assert_not_called()
        self.dll.user.UnregisterClassW.assert_not_called()
        self.assertIs(nt._LIVE_CLASSES[self.strip._class_name], self.strip)

    def test_invalid_geometry_or_color_rejected_before_native_load(self):
        cases = ((-1, 10, 0, nt.DEFAULT_COLOR), (84, 84, 0, nt.DEFAULT_COLOR),
                 (84, 241, 0, nt.DEFAULT_COLOR), (84, 108, -1, nt.DEFAULT_COLOR),
                 (84, 108, 160, nt.DEFAULT_COLOR), (True, 108, 0, nt.DEFAULT_COLOR),
                 (84.0, 108, 0, nt.DEFAULT_COLOR), (84, 108, 0.5, nt.DEFAULT_COLOR),
                 (84, 108, 0, (1, float("nan"), 2)))
        for top, bottom, margin, color in cases:
            with self.subTest(top=top, bottom=bottom, margin=margin, color=color):
                self.assertFalse(self.strip.present(top, bottom, color, margin))
        self.root.width = 8193
        self.assertFalse(self.present())
        self.root.width, self.root.height = 300, 1000
        self.assertFalse(self.strip.present(0, 513))
        self.root.height, self.root.x = 240, 2 ** 31
        self.assertFalse(self.present())
        self.dll.loader.assert_not_called()
        self.assert_no_window_writes()

    def test_invalid_update_hides_previous_good_surface(self):
        self.assertTrue(self.present())
        self.assertFalse(self.strip.present(108, 84))
        self.dll.user.ShowWindow.assert_called_with(POPUP, nt.SW_HIDE)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)

    def test_non_windows_methods_never_load_dlls(self):
        with patch.object(nt.sys, "platform", "linux"):
            self.assertFalse(self.present())
            self.assertFalse(self.strip.hide())
            self.assertFalse(self.strip.destroy())
        self.dll.loader.assert_not_called()
        self.assertFalse(self.root.bindings)

    def test_dll_load_failure_returns_false_without_window_mutation(self):
        self.dll.loader.side_effect = OSError("not available")
        self.assertFalse(self.present())
        self.assert_no_window_writes()

    def test_paint_acknowledges_only_no_upload_timer_hook_or_idle_loop(self):
        self.assertTrue(self.present())
        for _ in range(50):
            self.assertEqual(self.strip._wnd_proc(POPUP, nt.WM_PAINT, 0, 0), 0)
        self.assertEqual(self.dll.user.ValidateRect.call_count, 50)
        self.dll.user.ValidateRect.assert_called_with(POPUP, None)
        self.assertEqual(self.strip._wnd_proc(POPUP, nt.WM_NCHITTEST, 0, 0), nt.HTTRANSPARENT)
        self.assertEqual(self.strip._wnd_proc(POPUP, nt.WM_MOUSEACTIVATE, 0, 0), nt.MA_NOACTIVATE)
        self.assertEqual(self.strip._wnd_proc(POPUP, nt.WM_ERASEBKGND, 0, 0), 1)
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)
        self.assertEqual(self.dll.user.SetWindowPos.call_count, 1)
        self.dll.user.SetTimer.assert_not_called()
        self.dll.user.SetWinEventHook.assert_not_called()
        self.root.after.assert_not_called()

    def test_callbacks_contain_native_exceptions(self):
        self.assertTrue(self.present())
        self.dll.user.ValidateRect.side_effect = OSError("gone")
        self.assertEqual(self.strip._wnd_proc(POPUP, nt.WM_PAINT, 0, 0), 0)
        self.dll.user.DefWindowProcW.side_effect = OSError("gone")
        self.assertEqual(self.strip._wnd_proc(POPUP, 123456, 0, 0), 0)

    def test_root_unmap_and_destroy_bindings_ignore_child_events(self):
        self.assertTrue(self.present())
        unmap = self.root.bindings["<Unmap>"]
        destroy = self.root.bindings["<Destroy>"]
        unmap(SimpleNamespace(widget=object()))
        destroy(SimpleNamespace(widget=object()))
        self.assertTrue(self.strip._shown)
        unmap(SimpleNamespace(widget=self.root))
        self.assertFalse(self.strip._shown)
        self.assertTrue(self.present())
        destroy(SimpleNamespace(widget=self.root))
        self.assertTrue(self.strip._closed)
        self.assertFalse(self.root.bindings)
        self.assertEqual(len(self.root.unbound), 2)
        self.assertFalse(self.present())

    def test_destroy_then_unregister_retains_callback_until_both_complete(self):
        self.assertTrue(self.present())
        callback, name = self.strip._callback, self.strip._class_name
        original_destroy, original_unregister = self.dll.destroy, self.dll.unregister

        def destroy(hwnd):
            self.assertIs(self.strip._callback, callback)
            self.assertIs(nt._LIVE_CLASSES[name], self.strip)
            return original_destroy(hwnd)

        def unregister(class_name, instance):
            self.assertIsNone(self.strip._hwnd)
            self.assertIs(self.strip._callback, callback)
            self.assertIs(nt._LIVE_CLASSES[name], self.strip)
            return original_unregister(class_name, instance)

        self.dll.user.DestroyWindow.side_effect = destroy
        self.dll.user.UnregisterClassW.side_effect = unregister
        self.assertTrue(self.strip.destroy())
        self.assertTrue(self.strip.destroy())
        self.dll.user.DestroyWindow.assert_called_once_with(POPUP)
        self.dll.user.UnregisterClassW.assert_called_once_with(name, 303)
        self.assertNotIn(name, nt._LIVE_CLASSES)
        self.assertIsNone(self.strip._callback)
        self.assertIsNone(self.strip._pixels)
        self.assertFalse(self.present())

    def test_destroy_failure_retains_class_callback_and_can_retry(self):
        self.assertTrue(self.present())
        name, callback = self.strip._class_name, self.strip._callback
        self.dll.destroy_success = False
        self.assertFalse(self.strip.destroy())
        self.assertIs(self.strip._callback, callback)
        self.assertIs(nt._LIVE_CLASSES[name], self.strip)
        self.dll.user.UnregisterClassW.assert_not_called()
        self.assertFalse(self.present())
        self.dll.destroy_success = True
        self.assertTrue(self.strip.destroy())
        self.assertNotIn(name, nt._LIVE_CLASSES)

    def test_unregister_failure_retains_callback_but_never_destroys_window_twice(self):
        self.assertTrue(self.present())
        name, callback = self.strip._class_name, self.strip._callback
        self.dll.unregister_success = False
        self.assertFalse(self.strip.destroy())
        self.assertIs(self.strip._callback, callback)
        self.assertIsNone(self.strip._hwnd)
        self.assertIs(nt._LIVE_CLASSES[name], self.strip)
        self.dll.unregister_success = True
        self.assertTrue(self.strip.destroy())
        self.dll.user.DestroyWindow.assert_called_once_with(POPUP)
        self.assertNotIn(name, nt._LIVE_CLASSES)

    def test_external_native_destruction_is_terminal_and_class_is_cleaned_later(self):
        self.assertTrue(self.present())
        name = self.strip._class_name
        self.dll.destroy(POPUP)
        self.assertIsNone(self.strip._hwnd)
        self.assertFalse(self.present())
        self.assertIn(name, nt._LIVE_CLASSES)
        self.assertTrue(self.strip.destroy())
        self.dll.user.DestroyWindow.assert_not_called()
        self.assertNotIn(name, nt._LIVE_CLASSES)

    def test_owner_already_removed_popup_does_not_require_a_live_root(self):
        self.assertTrue(self.present())
        self.dll.alive.discard(OWNER)
        self.dll.alive.discard(POPUP)
        self.assertTrue(self.strip.destroy())
        self.dll.user.DestroyWindow.assert_not_called()
        self.dll.user.UnregisterClassW.assert_called_once()

    def test_window_creation_failure_unregisters_and_releases_callback(self):
        self.dll.create_success = False
        self.assertFalse(self.present())
        self.dll.user.UnregisterClassW.assert_called_once()
        self.dll.user.DestroyWindow.assert_not_called()
        self.assertFalse(self.strip._registered)
        self.assertIsNone(self.strip._callback)
        self.assertNotIn(self.dll.class_name, nt._LIVE_CLASSES)

    def test_registration_failure_never_creates_or_unregisters_unowned_class(self):
        self.dll.user.RegisterClassExW.side_effect = None
        self.dll.user.RegisterClassExW.return_value = 0
        self.assertFalse(self.present())
        self.dll.user.CreateWindowExW.assert_not_called()
        self.dll.user.UnregisterClassW.assert_not_called()
        self.assertIsNone(self.strip._callback)

    def test_creation_exception_unregisters_class(self):
        self.dll.user.CreateWindowExW.side_effect = OSError("create failed")
        self.assertFalse(self.present())
        self.dll.user.UnregisterClassW.assert_called_once()
        self.assertIsNone(self.strip._callback)

    def test_position_failure_hides_and_can_retry_without_reupload(self):
        self.assertTrue(self.present())
        self.root.x += 1
        self.dll.position_success = False
        self.assertFalse(self.present())
        self.assertNotIn(POPUP, self.dll.visible)
        self.dll.position_success = True
        self.assertTrue(self.present())
        self.assertEqual(self.dll.user.UpdateLayeredWindow.call_count, 1)

    def test_gdi_success_restores_selection_then_frees_every_resource(self):
        self.assertTrue(self.present())
        cleanup = [event for event in self.dll.events
                   if event[0] in ("select", "delete_dc", "delete_bitmap", "release")]
        self.assertEqual(cleanup, [("select", 44), ("select", 33),
                                   ("delete_dc", 22), ("delete_bitmap", 44), ("release", None, 11)])

    def test_missing_screen_dc_never_allocates_other_resources(self):
        self.dll.user.GetDC.return_value = 0
        self.assertFalse(self.present())
        self.dll.gdi.CreateCompatibleDC.assert_not_called()
        self.dll.gdi.DeleteDC.assert_not_called()
        self.dll.gdi.DeleteObject.assert_not_called()
        self.dll.user.ReleaseDC.assert_not_called()

    def test_memory_dc_failure_still_releases_screen_dc(self):
        self.dll.gdi.CreateCompatibleDC.return_value = 0
        self.assertFalse(self.present())
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.dll.gdi.CreateDIBSection.assert_not_called()
        self.dll.gdi.DeleteDC.assert_not_called()
        self.dll.gdi.DeleteObject.assert_not_called()

    def test_bitmap_failure_deletes_dc_and_releases_screen(self):
        self.dll.bitmap_success = False
        self.assertFalse(self.present())
        self.dll.gdi.DeleteDC.assert_called_once_with(22)
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.dll.gdi.DeleteObject.assert_not_called()
        self.dll.gdi.SelectObject.assert_not_called()

    def test_null_dib_bits_still_delete_created_bitmap(self):
        self.dll.bits_success = False
        self.assertFalse(self.present())
        self.dll.gdi.DeleteDC.assert_called_once_with(22)
        self.dll.gdi.DeleteObject.assert_called_once_with(44)
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.dll.gdi.SelectObject.assert_not_called()

    def test_select_failure_deletes_dc_before_bitmap(self):
        for result in (0, nt.HGDI_ERROR):
            with self.subTest(result=result):
                self.dll.events.clear()
                self.dll.gdi.SelectObject.side_effect = None
                self.dll.gdi.SelectObject.return_value = result
                self.assertFalse(self.present())
                events = [e[0] for e in self.dll.events]
                self.assertLess(events.index("delete_dc"), events.index("delete_bitmap"))
                self.assertIn("release", events)
        self.dll.user.UpdateLayeredWindow.assert_not_called()

    def test_memmove_exception_restores_selection_and_frees_every_resource(self):
        with patch.object(C, "memmove", side_effect=OSError("copy failed")):
            self.assertFalse(self.present())
        self.dll.gdi.SelectObject.assert_any_call(22, 33)
        self.dll.gdi.DeleteDC.assert_called_once_with(22)
        self.dll.gdi.DeleteObject.assert_called_once_with(44)
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.dll.user.UpdateLayeredWindow.assert_not_called()

    def test_upload_failure_hides_old_strip_reuses_cache_and_releases_gdi(self):
        self.assertTrue(self.present())
        self.dll.user.UpdateLayeredWindow.return_value = 0
        with patch.object(nt, "transition_bgra", wraps=nt.transition_bgra) as pixels:
            self.assertFalse(self.present(color_rgb=(12, 34, 56)))
            self.assertIsNone(self.strip._uploaded_key)
            self.assertNotIn(POPUP, self.dll.visible)
            self.dll.user.UpdateLayeredWindow.return_value = 1
            self.assertTrue(self.present(color_rgb=(12, 34, 56)))
            self.assertEqual(pixels.call_count, 1)
        self.assertEqual(self.dll.gdi.DeleteDC.call_count, 3)
        self.assertEqual(self.dll.gdi.DeleteObject.call_count, 3)
        self.assertEqual(self.dll.user.ReleaseDC.call_count, 3)

    def test_upload_exception_still_restores_and_releases(self):
        self.dll.user.UpdateLayeredWindow.side_effect = OSError("ULW failed")
        self.assertFalse(self.present())
        self.dll.gdi.SelectObject.assert_any_call(22, 33)
        self.dll.gdi.DeleteDC.assert_called_once_with(22)
        self.dll.gdi.DeleteObject.assert_called_once_with(44)
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)

    def test_restore_failure_or_exception_does_not_skip_dc_bitmap_or_release(self):
        for failure in (0, OSError("restore failed")):
            with self.subTest(failure=failure):
                self.dll.gdi.DeleteDC.reset_mock()
                self.dll.gdi.DeleteObject.reset_mock()
                self.dll.user.ReleaseDC.reset_mock()

                def select(dc, bitmap):
                    if bitmap == 44:
                        return 33
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                self.dll.gdi.SelectObject.side_effect = select
                self.assertFalse(self.present())
                self.dll.gdi.DeleteDC.assert_called_once_with(22)
                self.dll.gdi.DeleteObject.assert_called_once_with(44)
                self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.dll.user.ShowWindow.assert_not_called()

    def test_cleanup_exceptions_do_not_skip_other_owned_resources(self):
        self.dll.gdi.DeleteDC.side_effect = OSError("DC cleanup failed")
        self.dll.gdi.DeleteObject.side_effect = OSError("bitmap cleanup failed")
        self.assertFalse(self.present())
        self.dll.gdi.SelectObject.assert_any_call(22, 33)
        self.dll.gdi.DeleteDC.assert_called_once_with(22)
        self.dll.gdi.DeleteObject.assert_called_once_with(44)
        self.dll.user.ReleaseDC.assert_called_once_with(None, 11)
        self.assertIsNone(self.strip._uploaded_key)


if __name__ == "__main__":
    unittest.main()
