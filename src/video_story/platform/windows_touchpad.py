"""Precision Touchpad contacts on Windows (Raw Input), read in the mouse hook helper.

The hook helper (:mod:`video_story.platform.windows_mouse_hook`) runs :func:`reader` on a
thread of its own: it registers for the HID touchpad collection (usage page ``0x0D``,
usage ``0x05``) with ``RIDEV_INPUTSINK``, so reports arrive whichever window is in
front, and the system's own gestures keep working (no ``RIDEV_NOLEGACY``). In the window
process the reports would arrive up to 380 ms late while frames are encoded (M0).

Touchpads report "hybrid": the fingers of one scan come in several reports, only the
first carries the contact count. :class:`FrameAssembler` puts a scan back together.

Lines added to the helper's protocol:

* ``PAD <device> <x_max> <y_max>`` - a touchpad and its logical size (once, at start);
* ``T <ns> <device> <n> [<id> <x> <y>]*n`` - the fingers touching the pad in one scan
  (``n`` = 0 when the last finger lifted).

Only the parsing (:func:`parse_touch_line`) and the assembler are used by the parent;
they import on every OS.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from video_story.core.gestures import Contact

USAGE_PAGE_DIGITIZER = 0x0D
USAGE_TOUCHPAD = 0x05
USAGE_TIP_SWITCH = 0x42
USAGE_CONTACT_ID = 0x51
USAGE_CONTACT_COUNT = 0x54
USAGE_PAGE_GENERIC = 0x01
USAGE_X, USAGE_Y = 0x30, 0x31


@dataclass(frozen=True)
class PadLine:
    device: int
    width: int
    height: int


@dataclass(frozen=True)
class TouchLine:
    ns: int
    device: int
    contacts: tuple[Contact, ...]


def format_pad(device: int, width: int, height: int) -> str:
    return f"PAD {device} {width} {height}\n"


def format_touch(ns: int, device: int, contacts: list[tuple[int, int, int]]) -> str:
    flat = " ".join(f"{c} {x} {y}" for c, x, y in contacts)
    return f"T {ns} {device} {len(contacts)}" + (f" {flat}" if flat else "") + "\n"


def parse_touch_line(line: str) -> PadLine | TouchLine | None:
    parts = line.split()
    if not parts or parts[0] not in ("PAD", "T"):
        return None
    try:
        values = [int(p) for p in parts[1:]]
    except ValueError:
        return None
    if parts[0] == "PAD":
        return PadLine(*values) if len(values) == 3 else None
    if len(values) < 3:
        return None
    ns, device, count = values[:3]
    rest = values[3:]
    if count < 0 or len(rest) != 3 * count:
        return None
    contacts = tuple(Contact(rest[i], rest[i + 1], rest[i + 2]) for i in range(0, len(rest), 3))
    return TouchLine(ns, device, contacts)


class FrameAssembler:
    """Collects the contacts of one scan from hybrid reports.

    :meth:`add` takes what one report says - its contact count (0 in all but the first
    report of a scan) and the contacts it carries ``(id, x, y, tip)`` - and returns the
    fingers touching the pad once the scan is complete, else ``None``.
    """

    def __init__(self) -> None:
        self.expected = 0
        self.frame: dict[int, tuple[int, int, bool]] = {}

    def add(
        self, count: int, contacts: list[tuple[int, int, int, bool]]
    ) -> list[tuple[int, int, int]] | None:
        if count:
            self.expected, self.frame = count, {}
        if not self.expected:
            return None
        for cid, x, y, tip in contacts:
            if len(self.frame) >= self.expected:
                break
            self.frame[cid] = (x, y, tip)
        if len(self.frame) < self.expected:
            return None
        done = [(cid, x, y) for cid, (x, y, tip) in sorted(self.frame.items()) if tip]
        self.expected, self.frame = 0, {}
        return done


# The reader (inside the hook helper) -------------------------------------------------


def reader(emit: Callable[[str], bool], now_ns: Callable[[], int]) -> None:  # pragma: no cover
    """Register for touchpad reports and emit PAD / T lines until the process ends."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hid = ctypes.WinDLL("hid")
    lresult = ctypes.c_ssize_t
    wndproc_t = ctypes.WINFUNCTYPE(
        lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", wndproc_t),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", wintypes.USHORT),
            ("usUsage", wintypes.USHORT),
            ("dwFlags", wintypes.DWORD),
            ("hwndTarget", wintypes.HWND),
        ]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", wintypes.DWORD),
            ("dwSize", wintypes.DWORD),
            ("hDevice", wintypes.HANDLE),
            ("wParam", wintypes.WPARAM),
        ]

    class RAWINPUTDEVICELIST(ctypes.Structure):
        _fields_ = [("hDevice", wintypes.HANDLE), ("dwType", wintypes.DWORD)]

    class RID_DEVICE_INFO_HID(ctypes.Structure):
        _fields_ = [
            ("dwVendorId", wintypes.DWORD),
            ("dwProductId", wintypes.DWORD),
            ("dwVersionNumber", wintypes.DWORD),
            ("usUsagePage", wintypes.USHORT),
            ("usUsage", wintypes.USHORT),
        ]

    class _Info(ctypes.Union):
        _fields_ = [("hid", RID_DEVICE_INFO_HID), ("pad", ctypes.c_byte * 24)]

    class RID_DEVICE_INFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("dwType", wintypes.DWORD), ("u", _Info)]

    class HIDP_CAPS(ctypes.Structure):
        _fields_ = [
            ("Usage", ctypes.c_ushort),
            ("UsagePage", ctypes.c_ushort),
            ("InputReportByteLength", ctypes.c_ushort),
            ("OutputReportByteLength", ctypes.c_ushort),
            ("FeatureReportByteLength", ctypes.c_ushort),
            ("Reserved", ctypes.c_ushort * 17),
            ("NumberLinkCollectionNodes", ctypes.c_ushort),
            ("NumberInputButtonCaps", ctypes.c_ushort),
            ("NumberInputValueCaps", ctypes.c_ushort),
            ("NumberInputDataIndices", ctypes.c_ushort),
            ("rest", ctypes.c_ushort * 6),
        ]

    class HIDP_VALUE_CAPS(ctypes.Structure):
        _fields_ = [
            ("UsagePage", ctypes.c_ushort),
            ("ReportID", ctypes.c_ubyte),
            ("IsAlias", ctypes.c_ubyte),
            ("BitField", ctypes.c_ushort),
            ("LinkCollection", ctypes.c_ushort),
            ("LinkUsage", ctypes.c_ushort),
            ("LinkUsagePage", ctypes.c_ushort),
            ("flags", ctypes.c_ubyte * 6),
            ("BitSize", ctypes.c_ushort),
            ("ReportCount", ctypes.c_ushort),
            ("Reserved2", ctypes.c_ushort * 5),
            ("UnitsExp", ctypes.c_ulong),
            ("Units", ctypes.c_ulong),
            ("LogicalMin", ctypes.c_long),
            ("LogicalMax", ctypes.c_long),
            ("PhysicalMin", ctypes.c_long),
            ("PhysicalMax", ctypes.c_long),
            ("Usage", ctypes.c_ushort),
            ("u", ctypes.c_ushort * 7),
        ]

    if ctypes.sizeof(HIDP_VALUE_CAPS) != 72 or ctypes.sizeof(HIDP_CAPS) != 64:
        return
    hidp_ok = 0x00110000
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = lresult
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    user32.GetRawInputData.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    ]
    user32.GetRawInputDeviceInfoW.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT),
    ]
    user32.GetRawInputDeviceList.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    ]
    user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE),
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
    hid.HidP_GetValueCaps.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(HIDP_VALUE_CAPS),
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.c_void_p,
    ]
    hid.HidP_GetUsageValue.argtypes = [
        ctypes.c_int,
        ctypes.c_ushort,
        ctypes.c_ushort,
        ctypes.c_ushort,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    hid.HidP_GetUsageValue.restype = ctypes.c_long
    hid.HidP_GetUsages.argtypes = [
        ctypes.c_int,
        ctypes.c_ushort,
        ctypes.c_ushort,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    hid.HidP_GetUsages.restype = ctypes.c_long

    devices: dict[int, tuple[ctypes.Array, list[int], FrameAssembler]] = {}

    def device(handle) -> tuple[ctypes.Array, list[int], FrameAssembler] | None:
        key = int(handle or 0)
        if key in devices:
            return devices[key]
        size = wintypes.UINT()
        user32.GetRawInputDeviceInfoW(handle, 0x20000005, None, ctypes.byref(size))  # PREPARSEDDATA
        if not size.value:
            return None
        pre = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputDeviceInfoW(handle, 0x20000005, pre, ctypes.byref(size)) <= 0:
            return None
        caps = HIDP_CAPS()
        hid.HidP_GetCaps(pre, ctypes.byref(caps))
        n = ctypes.c_ushort(caps.NumberInputValueCaps)
        vcaps = (HIDP_VALUE_CAPS * max(n.value, 1))()
        hid.HidP_GetValueCaps(0, vcaps, ctypes.byref(n), pre)
        values = list(vcaps[: n.value])
        lcs = sorted(
            {
                v.LinkCollection
                for v in values
                if v.UsagePage == USAGE_PAGE_GENERIC and v.Usage == USAGE_X
            }
        )
        x_max = max(
            (
                v.LogicalMax
                for v in values
                if v.UsagePage == USAGE_PAGE_GENERIC and v.Usage == USAGE_X
            ),
            default=0,
        )
        y_max = max(
            (
                v.LogicalMax
                for v in values
                if v.UsagePage == USAGE_PAGE_GENERIC and v.Usage == USAGE_Y
            ),
            default=0,
        )
        devices[key] = (pre, lcs, FrameAssembler())
        emit(format_pad(key, int(x_max), int(y_max)))
        return devices[key]

    # Announce the touchpads now (no touch needed), so the parent knows there is one.
    count = wintypes.UINT()
    user32.GetRawInputDeviceList(None, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST))
    listed = (RAWINPUTDEVICELIST * max(count.value, 1))()
    got = user32.GetRawInputDeviceList(
        listed, ctypes.byref(count), ctypes.sizeof(RAWINPUTDEVICELIST)
    )
    if count.value and got not in (-1, 0xFFFFFFFF):
        for entry in listed[: count.value]:
            if entry.dwType != 2:  # RIM_TYPEHID
                continue
            info = RID_DEVICE_INFO(cbSize=ctypes.sizeof(RID_DEVICE_INFO))
            size = wintypes.UINT(ctypes.sizeof(info))
            user32.GetRawInputDeviceInfoW(
                entry.hDevice, 0x2000000B, ctypes.byref(info), ctypes.byref(size)
            )
            if (info.u.hid.usUsagePage, info.u.hid.usUsage) == (
                USAGE_PAGE_DIGITIZER,
                USAGE_TOUCHPAD,
            ):
                device(entry.hDevice)
    if not devices:
        return  # no precision touchpad: nothing to read

    def value(pre, page: int, lc: int, usage: int, report, length: int) -> int | None:
        out = ctypes.c_ulong()
        ok = hid.HidP_GetUsageValue(0, page, lc, usage, ctypes.byref(out), pre, report, length)
        return int(out.value) if ok == hidp_ok else None

    def tip(pre, lc: int, report, length: int) -> bool:
        usages = (ctypes.c_ushort * 16)()
        n = ctypes.c_ulong(16)
        ok = hid.HidP_GetUsages(
            0, USAGE_PAGE_DIGITIZER, lc, usages, ctypes.byref(n), pre, report, length
        )
        return ok == hidp_ok and USAGE_TIP_SWITCH in list(usages[: n.value])

    header_size = ctypes.sizeof(RAWINPUTHEADER)

    def on_input(lparam) -> None:
        stamp = now_ns()
        size = wintypes.UINT()
        user32.GetRawInputData(lparam, 0x10000003, None, ctypes.byref(size), header_size)
        buf = ctypes.create_string_buffer(size.value)
        if user32.GetRawInputData(lparam, 0x10000003, buf, ctypes.byref(size), header_size) <= 0:
            return
        header = RAWINPUTHEADER.from_buffer(buf)
        if header.dwType != 2:
            return
        dev = device(header.hDevice)
        if dev is None:
            return
        pre, lcs, assembler = dev
        size_hid = ctypes.c_ulong.from_buffer(buf, header_size).value
        reports = ctypes.c_ulong.from_buffer(buf, header_size + 4).value
        for i in range(reports):
            report = ctypes.addressof(buf) + header_size + 8 + i * size_hid
            count = value(pre, USAGE_PAGE_DIGITIZER, 0, USAGE_CONTACT_COUNT, report, size_hid) or 0
            contacts = []
            for lc in lcs:
                cid = value(pre, USAGE_PAGE_DIGITIZER, lc, USAGE_CONTACT_ID, report, size_hid)
                x = value(pre, USAGE_PAGE_GENERIC, lc, USAGE_X, report, size_hid)
                y = value(pre, USAGE_PAGE_GENERIC, lc, USAGE_Y, report, size_hid)
                if cid is not None and x is not None and y is not None:
                    contacts.append((cid, x, y, tip(pre, lc, report, size_hid)))
            frame = assembler.add(count, contacts)
            if frame is not None:
                emit(format_touch(stamp, int(header.hDevice or 0), frame))

    def proc(hwnd, msg, wparam, lparam):
        if msg == 0x00FF:  # WM_INPUT
            try:
                on_input(lparam)
            except Exception:  # noqa: BLE001 - one odd report must not end the reader
                pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    callback = wndproc_t(proc)
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=callback, hInstance=hinst, lpszClassName="VideoStoryTouchpad")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, "VideoStoryTouchpad", "", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, hinst, None
    )
    if not hwnd:
        return
    register = RAWINPUTDEVICE(USAGE_PAGE_DIGITIZER, USAGE_TOUCHPAD, 0x00000100, hwnd)  # INPUTSINK
    if not user32.RegisterRawInputDevices(ctypes.byref(register), 1, ctypes.sizeof(RAWINPUTDEVICE)):
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.DispatchMessageW(ctypes.byref(msg))


__all__ = [
    "FrameAssembler",
    "PadLine",
    "TouchLine",
    "format_pad",
    "format_touch",
    "parse_touch_line",
    "reader",
]
