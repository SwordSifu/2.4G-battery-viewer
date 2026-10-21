# -*- coding: utf-8 -*-
"""Strict COMpx queries for the two personally verified receivers.

No protocol scans, Feature writes, settings, pairing or firmware commands.
The wire protocol has no nonce; all our processes serialize per HID path.
Native buffers and OVERLAPPED objects outlive actual I/O completion.
"""
from __future__ import annotations

import ctypes as c
import hashlib
import time
from ctypes import wintypes as w
from typing import Optional


class ProtocolError(ValueError):
    pass


class QueryCancelled(OSError):
    pass


IDENTITIES = {"mouse": (0x373B, 0x10C9), "keyboard": (0x05AC, 0x024F)}
REPORT_LEN = 17


def build_request(command: int, *, keyboard: bool = False) -> bytes:
    if type(command) is not int or command not in (3, 4):
        raise ValueError("Only verified online/battery queries 03/04 are allowed")
    frame = bytearray(REPORT_LEN)
    frame[0:2] = bytes((8, command))
    frame[5] = 0x80 if keyboard else 0
    frame[-1] = (0x55 - sum(frame[:-1])) & 0xFF
    return bytes(frame)


def validate_response(response: bytes, request: bytes) -> bytes:
    if len(request) != REPORT_LEN or request != build_request(request[1], keyboard=bool(request[5] & 0x80)):
        raise ProtocolError("Invalid query")
    if len(response) != REPORT_LEN:
        raise ProtocolError(f"回复长度错误：{len(response)}，应为17")
    if response[:5] != request[:5]:
        raise ProtocolError("ReportID、命令、状态或地址不匹配")
    if (sum(response) & 0xFF) != 0x55:
        raise ProtocolError("回复加和校验失败")
    if (response[5] & 0x80) != (request[5] & 0x80):
        raise ProtocolError("键盘/鼠标回复标志不匹配")
    count = response[5] & 0x7F
    minimum = 1 if request[1] == 3 else 2
    if not minimum <= count <= 10:
        raise ProtocolError("有效数据长度不足或越界")
    if request[1] == 4 and response[6] > 100:
        raise ProtocolError("电量超出0..100，拒绝钳位掩盖错误")
    return response


def matches_interface(iface, key: str) -> bool:
    if key not in IDENTITIES:
        return False
    return ((iface.vid, iface.pid) == IDENTITIES[key]
            and (iface.usage_page, iface.usage) == (0xFF02, 2)
            and (iface.input_len, iface.output_len, iface.feature_len) == (17, 17, 0)
            and "&mi_01&col05#" in iface.path.lower())


class _Overlapped(c.Structure):
    _fields_ = [("Internal", c.c_size_t), ("InternalHigh", c.c_size_t),
                ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", c.c_void_p)]


class _Caps(c.Structure):
    _fields_ = [("usage", w.WORD), ("page", w.WORD), ("input", w.WORD),
                ("output", w.WORD), ("feature", w.WORD),
                ("reserved", w.WORD * 17), ("counts", w.WORD * 10)]


class _Attrs(c.Structure):
    _fields_ = [("size", w.ULONG), ("vid", w.USHORT),
                ("pid", w.USHORT), ("version", w.USHORT)]


class _Win32:
    def __init__(self):
        k = c.WinDLL("kernel32", use_last_error=True)
        h = c.WinDLL("hid", use_last_error=True)
        p, u = c.c_void_p, w.DWORD
        def bind(lib, name, args, result):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
            return fn
        self.open = bind(k, "CreateFileW", [w.LPCWSTR, u, u, p, u, u, p], p)
        self.close = bind(k, "CloseHandle", [p], w.BOOL)
        self.event = bind(k, "CreateEventW", [p, w.BOOL, w.BOOL, w.LPCWSTR], p)
        self.wait = bind(k, "WaitForSingleObject", [p, u], u)
        self.read = bind(k, "ReadFile", [p, p, u, c.POINTER(u), c.POINTER(_Overlapped)], w.BOOL)
        self.write = bind(k, "WriteFile", [p, p, u, c.POINTER(u), c.POINTER(_Overlapped)], w.BOOL)
        self.result = bind(k, "GetOverlappedResult", [p, c.POINTER(_Overlapped), c.POINTER(u), w.BOOL], w.BOOL)
        self.cancel = bind(k, "CancelIoEx", [p, c.POINTER(_Overlapped)], w.BOOL)
        self.mutex = bind(k, "CreateMutexW", [p, w.BOOL, w.LPCWSTR], p)
        self.release = bind(k, "ReleaseMutex", [p], w.BOOL)
        self.attrs = bind(h, "HidD_GetAttributes", [p, c.POINTER(_Attrs)], w.BOOL)
        self.prep = bind(h, "HidD_GetPreparsedData", [p, c.POINTER(p)], w.BOOL)
        self.free = bind(h, "HidD_FreePreparsedData", [p], w.BOOL)
        self.caps = bind(h, "HidP_GetCaps", [p, c.POINTER(_Caps)], c.c_long)


class HidChannel:
    """One in-flight query; independent, overlapped read and write handles."""
    def __init__(self, iface, key: str, stop_event=None, timeout: float = 0.65):
        if not matches_interface(iface, key):
            raise ProtocolError("设备身份、接口或报告长度不匹配，未发送任何命令")
        self.iface, self.key = iface, key
        self.stop_event = stop_event
        self.timeout = max(0.1, min(float(timeout), 2.0))
        self.api = None
        self.handles = []
        self.lock = None
        self.owns_lock = False
        self.reader = self.writer = None

    def _cancelled(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _open(self, access, flags=0):
        h = self.api.open(self.iface.path, access, 3, None, 3, flags, None)
        if not h or h == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        self.handles.append(h)
        return h

    def __enter__(self):
        self.api = _Win32()
        try:
            name = "Local\\SwordSifuBatteryHid_" + hashlib.sha256(self.iface.path.lower().encode()).hexdigest()[:24]
            self.lock = self.api.mutex(None, False, name)
            if not self.lock:
                raise c.WinError(c.get_last_error())
            deadline = time.monotonic() + 1.5
            while True:
                if self._cancelled():
                    raise QueryCancelled("读取已取消")
                state = self.api.wait(self.lock, 50)
                if state in (0, 0x80):
                    self.owns_lock = True
                    break
                if state != 258:
                    raise c.WinError(c.get_last_error())
                if time.monotonic() >= deadline:
                    raise TimeoutError("接收器正由另一个本工具实例读取")
            check = self._open(0)
            attrs, data = _Attrs(), c.c_void_p()
            attrs.size = c.sizeof(attrs)
            if not self.api.attrs(check, c.byref(attrs)) or (attrs.vid, attrs.pid) != IDENTITIES[self.key]:
                raise ProtocolError("打开后设备身份复核失败")
            try:
                caps = _Caps()
                if not self.api.prep(check, c.byref(data)) or self.api.caps(data, c.byref(caps)) != 0x110000:
                    raise ProtocolError("无法复核HID能力表")
                if (caps.page, caps.usage, caps.input, caps.output, caps.feature) != (0xFF02, 2, 17, 17, 0):
                    raise ProtocolError("打开后报告描述符复核失败")
            finally:
                if data:
                    self.api.free(data)
            self.reader = self._open(0x80000000, 0x40000000)
            self.writer = self._open(0x40000000, 0x40000000)
            return self
        except BaseException:
            self.close()
            raise

    def _io(self, fn, handle, buf, length: int, seconds: float) -> Optional[int]:
        if self._cancelled():
            raise QueryCancelled("读取已取消")
        ev = self.api.event(None, True, False, None)
        if not ev:
            raise c.WinError(c.get_last_error())
        ov, transferred = _Overlapped(), w.DWORD()
        ov.hEvent = ev
        pending = False
        try:
            if fn(handle, buf, length, c.byref(transferred), c.byref(ov)):
                return int(transferred.value)
            err = c.get_last_error()
            if err != 997:
                raise c.WinError(err)
            pending = True
            end = time.monotonic() + seconds
            while True:
                if self._cancelled():
                    raise QueryCancelled("读取已取消")
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return None
                state = self.api.wait(ev, max(1, min(50, int(remaining * 1000))))
                if state == 0:
                    if not self.api.result(handle, c.byref(ov), c.byref(transferred), False):
                        err = c.get_last_error()
                        # A failed result also signifies completion (e.g. disconnected).
                        if err != 996:
                            pending = False
                        raise c.WinError(err)
                    pending = False
                    return int(transferred.value)
                if state != 258:
                    raise c.WinError(c.get_last_error())
        finally:
            if pending:
                self.api.cancel(handle, c.byref(ov))
                # CancelIoEx is only a request. Do not release ov/buf/event
                # until the kernel has completed the pending operation.
                self.api.result(handle, c.byref(ov), c.byref(transferred), True)
            self.api.close(ev)

    def drain(self) -> None:
        """Discard stale replies left in the input queue by an earlier query.

        A slow or cancelled previous exchange can leave a late frame pending;
        reading it as the answer to the next command would look like a
        protocol violation. Bounded, never blocking the caller for long.
        """
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            buf = c.create_string_buffer(REPORT_LEN)
            if self._io(self.api.read, self.reader, buf, REPORT_LEN, 0.02) is None:
                return

    def exchange(self, command: int, attempts: int = 1) -> bytes:
        request = build_request(command, keyboard=self.key == "keyboard")
        last_error = "设备没有回复（可能休眠、离线或被厂商软件占用）"
        for attempt in range(max(1, attempts)):
            if attempt:
                # The receiver answers the first query almost immediately but
                # throttles rapid repeats, so retries back off progressively.
                time.sleep(0.35 * attempt)
            self.drain()
            data = c.create_string_buffer(request, REPORT_LEN)
            if self._io(self.api.write, self.writer, data, REPORT_LEN, self.timeout) != REPORT_LEN:
                raise TimeoutError("查询写入未完成")
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                buf = c.create_string_buffer(REPORT_LEN)
                count = self._io(self.api.read, self.reader, buf, REPORT_LEN,
                                 max(0.001, deadline - time.monotonic()))
                if count is None:
                    break
                candidate = buf.raw[:count]
                # Ignore late frames belonging to a previous command instead
                # of failing the current query on the first mismatch.
                if candidate[:2] != request[:2]:
                    last_error = "读到上一条查询的残留回复，已忽略"
                    continue
                try:
                    return validate_response(candidate, request)
                except ProtocolError as exc:
                    last_error = str(exc)
        raise TimeoutError(last_error)

    def close(self):
        if self.api is None:
            return
        for handle in reversed(self.handles):
            self.api.close(handle)
        self.handles.clear()
        if self.lock:
            if self.owns_lock:
                self.api.release(self.lock)
            self.api.close(self.lock)
        self.lock = None
        self.owns_lock = False

    def __exit__(self, *_):
        self.close()
