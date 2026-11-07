"""Linux (X11) implementation using python-xlib. Wayland sessions are not supported."""

from __future__ import annotations

import logging
import os
from typing import Any

from video_story.core.screens import RawScreen

from .base import Platform, PlatformError, SupportStatus

log = logging.getLogger(__name__)


class LinuxPlatform(Platform):
    name = "linux"

    def __init__(self) -> None:
        self._display: Any = None

    # Connection -------------------------------------------------------------

    def _connect(self) -> Any:
        if self._display is None:
            try:
                from Xlib import display as xdisplay
            except ImportError as exc:  # pragma: no cover - dependency is platform conditional
                raise PlatformError("python-xlib is required on Linux") from exc
            try:
                self._display = xdisplay.Display()
            except Exception as exc:  # Xlib raises DisplayError subclasses
                raise PlatformError(
                    f"cannot open X display {os.environ.get('DISPLAY')!r}: {exc}"
                ) from exc
        return self._display

    def prepare_process(self) -> None:
        return None

    def support_status(self) -> SupportStatus:
        session = os.environ.get("XDG_SESSION_TYPE", "").lower()
        if session == "wayland":
            return SupportStatus(
                False,
                "Wayland sessions are not supported in this version; log in to an X11 session.",
            )
        if not os.environ.get("DISPLAY"):
            return SupportStatus(False, "DISPLAY is not set; an X11 session is required.")
        try:
            self._connect()
        except PlatformError as exc:
            return SupportStatus(False, str(exc))
        return SupportStatus(True)

    # Screens ----------------------------------------------------------------

    def list_screens(self) -> list[RawScreen]:
        display = self._connect()
        root = display.screen().root
        screens: list[RawScreen] = []
        try:
            from Xlib.ext import randr

            resources = randr.get_screen_resources(root)
            try:
                primary_output = randr.get_output_primary(root).output
            except Exception:
                primary_output = 0
            for crtc in resources.crtcs:
                info = randr.get_crtc_info(root, crtc, resources.config_timestamp)
                if not info.width or not info.height or not info.outputs:
                    continue
                name = ""
                try:
                    name = randr.get_output_info(
                        root, info.outputs[0], resources.config_timestamp
                    ).name
                except Exception:
                    pass
                screens.append(
                    RawScreen(
                        x=int(info.x),
                        y=int(info.y),
                        width=int(info.width),
                        height=int(info.height),
                        primary=primary_output in info.outputs,
                        scale=1.0,
                        name=name,
                    )
                )
        except Exception as exc:
            log.warning("RandR enumeration failed (%s); using the whole root window", exc)
        if not screens:
            geometry = root.get_geometry()
            screens.append(
                RawScreen(
                    x=0,
                    y=0,
                    width=geometry.width,
                    height=geometry.height,
                    primary=True,
                    name="root",
                )
            )
        return screens

    # Cursor -----------------------------------------------------------------

    def cursor_position(self) -> tuple[int, int] | None:
        display = self._connect()
        try:
            pointer = display.screen().root.query_pointer()
        except Exception as exc:
            log.debug("query_pointer failed: %s", exc)
            return None
        return int(pointer.root_x), int(pointer.root_y)


__all__ = ["LinuxPlatform"]
