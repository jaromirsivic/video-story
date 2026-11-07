"""Windows implementation (ctypes, no extra dependencies)."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from video_story.core.screens import RawScreen

from .base import Platform, SupportStatus

log = logging.getLogger(__name__)

MONITORINFOF_PRIMARY = 0x1
MDT_EFFECTIVE_DPI = 0
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
WDA_EXCLUDEFROMCAPTURE = 0x11


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


class WindowsPlatform(Platform):
    name = "windows"

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._shcore = getattr(ctypes.windll, "shcore", None)
        self._prepared = False

    def prepare_process(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        try:
            if self._user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            ):
                return
        except (AttributeError, OSError):
            pass
        try:
            if self._shcore is not None and self._shcore.SetProcessDpiAwareness(2) == 0:
                return
        except (AttributeError, OSError):
            pass
        try:
            self._user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            log.warning("Could not set DPI awareness; cursor mapping may be off on scaled screens")

    def support_status(self) -> SupportStatus:
        return SupportStatus(True)

    def _monitor_scale(self, hmonitor: int) -> float:
        if self._shcore is None:
            return 1.0
        dpi_x = wintypes.UINT()
        dpi_y = wintypes.UINT()
        try:
            if (
                self._shcore.GetDpiForMonitor(
                    hmonitor, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
                )
                == 0
            ):
                return round(dpi_x.value / 96.0, 3)
        except (AttributeError, OSError):
            pass
        return 1.0

    def list_screens(self) -> list[RawScreen]:
        self.prepare_process()
        screens: list[RawScreen] = []

        def _callback(hmonitor, _hdc, _rect, _lparam):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if self._user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                rect = info.rcMonitor
                screens.append(
                    RawScreen(
                        x=rect.left,
                        y=rect.top,
                        width=rect.right - rect.left,
                        height=rect.bottom - rect.top,
                        primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                        scale=self._monitor_scale(hmonitor),
                        name=info.szDevice,
                    )
                )
            return True

        if not self._user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_callback), 0):
            raise OSError("EnumDisplayMonitors failed")
        return screens

    def cursor_position(self) -> tuple[int, int] | None:
        point = wintypes.POINT()
        if self._user32.GetCursorPos(ctypes.byref(point)):
            return point.x, point.y
        return None

    def exclude_window_from_capture(self, window_handle: int) -> bool:
        try:
            return bool(
                self._user32.SetWindowDisplayAffinity(
                    wintypes.HWND(window_handle), WDA_EXCLUDEFROMCAPTURE
                )
            )
        except (AttributeError, OSError):
            return False


__all__ = ["WindowsPlatform"]
