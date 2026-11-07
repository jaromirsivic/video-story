"""Operating system abstraction used by the capture layer.

Only this package may contain OS specific code. Implementations return geometry in
physical pixels of the virtual desktop so that cursor positions and screen bounds
share one coordinate system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from video_story.core.screens import RawScreen


class PlatformError(RuntimeError):
    """The current platform or session cannot be used for recording."""


@dataclass(frozen=True)
class SupportStatus:
    supported: bool
    reason: str = ""


class Platform(ABC):
    """Per-OS services needed by Video Story."""

    name: str = "unknown"

    @abstractmethod
    def prepare_process(self) -> None:
        """One-time process setup (for example DPI awareness). Must run before any UI."""

    @abstractmethod
    def support_status(self) -> SupportStatus:
        """Whether screen and cursor capture can work in this session."""

    @abstractmethod
    def list_screens(self) -> list[RawScreen]:
        """All connected screens with physical pixel bounds in the virtual desktop."""

    @abstractmethod
    def cursor_position(self) -> tuple[int, int] | None:
        """Global cursor position in physical pixels, or ``None`` if unavailable."""

    def exclude_window_from_capture(self, window_handle: int) -> bool:  # pragma: no cover
        """Best effort: hide a window from screen capture. Returns True when applied."""
        return False

    def describe(self) -> dict[str, object]:
        status = self.support_status()
        return {"name": self.name, "supported": status.supported, "reason": status.reason}


__all__ = ["Platform", "PlatformError", "SupportStatus"]
