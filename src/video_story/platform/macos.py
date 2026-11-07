"""macOS placeholder. Support is planned; recording is refused with a clear message."""

from __future__ import annotations

from video_story.core.screens import RawScreen

from .base import Platform, PlatformError, SupportStatus

_REASON = "macOS support is planned for a later version."


class MacOSPlatform(Platform):
    name = "macos"

    def prepare_process(self) -> None:
        return None

    def support_status(self) -> SupportStatus:
        return SupportStatus(False, _REASON)

    def list_screens(self) -> list[RawScreen]:
        raise PlatformError(_REASON)

    def cursor_position(self) -> tuple[int, int] | None:
        return None


__all__ = ["MacOSPlatform"]
