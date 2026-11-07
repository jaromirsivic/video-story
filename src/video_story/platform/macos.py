"""macOS implementation using CoreGraphics through ctypes (no extra dependencies).

Coordinates: Quartz Display Services report display bounds and the mouse location in
*points* of one global space whose origin is the top-left corner of the main display,
with y growing downwards. Captured images (mss) are in pixels, so every screen carries
a ``pixel_ratio`` (2.0 on Retina displays) that maps points to image pixels.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging

from video_story.core.screens import RawScreen

from .base import Platform, PlatformError, SupportStatus

log = logging.getLogger(__name__)

MAX_DISPLAYS = 32


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _load_core_graphics() -> ctypes.CDLL:
    path = ctypes.util.find_library("CoreGraphics") or (
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
    )
    cg = ctypes.cdll.LoadLibrary(path)
    cg.CGGetActiveDisplayList.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    cg.CGGetActiveDisplayList.restype = ctypes.c_int32
    cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayIsMain.argtypes = [ctypes.c_uint32]
    cg.CGDisplayIsMain.restype = ctypes.c_uint32
    cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = CGPoint
    return cg


def _load_core_foundation() -> ctypes.CDLL:
    path = ctypes.util.find_library("CoreFoundation") or (
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    cf = ctypes.cdll.LoadLibrary(path)
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    cf.CFRelease.restype = None
    return cf


class MacOSPlatform(Platform):
    name = "macos"

    def __init__(self) -> None:
        self._cg: ctypes.CDLL | None = None
        self._cf: ctypes.CDLL | None = None

    def _libs(self) -> tuple[ctypes.CDLL, ctypes.CDLL]:
        if self._cg is None or self._cf is None:
            try:
                self._cg = _load_core_graphics()
                self._cf = _load_core_foundation()
            except OSError as exc:  # pragma: no cover - only on a broken macOS install
                raise PlatformError(f"cannot load CoreGraphics: {exc}") from exc
        return self._cg, self._cf

    def prepare_process(self) -> None:
        return None

    # Permissions -------------------------------------------------------------

    def screen_capture_allowed(self) -> bool | None:
        """Screen Recording permission (macOS 10.15+); ``None`` when unknown."""
        cg, _ = self._libs()
        try:
            preflight = cg.CGPreflightScreenCaptureAccess
        except AttributeError:
            return None
        preflight.restype = ctypes.c_bool
        preflight.argtypes = []
        return bool(preflight())

    def request_screen_capture(self) -> bool:
        cg, _ = self._libs()
        try:
            request = cg.CGRequestScreenCaptureAccess
        except AttributeError:
            return False
        request.restype = ctypes.c_bool
        request.argtypes = []
        return bool(request())

    def support_status(self) -> SupportStatus:
        try:
            self._libs()
        except PlatformError as exc:
            return SupportStatus(False, str(exc))
        allowed = self.screen_capture_allowed()
        if allowed is False:
            self.request_screen_capture()
            return SupportStatus(
                False,
                "Screen Recording permission is required: System Settings > Privacy & Security > "
                "Screen Recording, enable the terminal or host application, then start again.",
            )
        return SupportStatus(True)

    # Screens ------------------------------------------------------------------

    def list_screens(self) -> list[RawScreen]:
        cg, _ = self._libs()
        ids = (ctypes.c_uint32 * MAX_DISPLAYS)()
        count = ctypes.c_uint32(0)
        if cg.CGGetActiveDisplayList(MAX_DISPLAYS, ids, ctypes.byref(count)) != 0:
            raise PlatformError("CGGetActiveDisplayList failed")
        screens: list[RawScreen] = []
        for index in range(count.value):
            display = ids[index]
            bounds = cg.CGDisplayBounds(display)
            width = int(round(bounds.size.width))
            height = int(round(bounds.size.height))
            if width <= 0 or height <= 0:
                continue
            pixels_wide = int(cg.CGDisplayPixelsWide(display))
            ratio = round(pixels_wide / width, 3) if pixels_wide and width else 1.0
            screens.append(
                RawScreen(
                    x=int(round(bounds.origin.x)),
                    y=int(round(bounds.origin.y)),
                    width=width,
                    height=height,
                    primary=bool(cg.CGDisplayIsMain(display)),
                    scale=ratio,
                    name=f"display{display}",
                    pixel_ratio=ratio,
                )
            )
        if not screens:
            raise PlatformError("no active displays reported by CoreGraphics")
        return screens

    # Cursor -------------------------------------------------------------------

    def cursor_position(self) -> tuple[int, int] | None:
        cg, cf = self._libs()
        event = cg.CGEventCreate(None)
        if not event:
            return None
        try:
            point = cg.CGEventGetLocation(event)
        finally:
            cf.CFRelease(event)
        return int(round(point.x)), int(round(point.y))


__all__ = ["MacOSPlatform"]
