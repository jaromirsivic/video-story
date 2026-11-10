"""Operating system abstraction used by the capture layer.

Only this package may contain OS specific code. Implementations return geometry in
physical pixels of the virtual desktop so that cursor positions and screen bounds
share one coordinate system.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from video_story.core.gestures import ContactFrame, TouchpadGeometry
from video_story.core.input_events import (
    ContentRequest,
    ElementInfo,
    MouseCapabilities,
    MouseSystemParams,
    RawMouseEvent,
    WindowInfo,
)
from video_story.core.screens import RawScreen
from video_story.core.timeline import RecordingClock


class PlatformError(RuntimeError):
    """The current platform or session cannot be used for recording."""


@dataclass(frozen=True)
class SupportStatus:
    supported: bool
    reason: str = ""


class MouseSource(ABC):
    """Global mouse events (buttons, wheel, moves) while recording.

    Must never slow the system's pointer down: on Windows the hook runs in a helper
    process. ``t_ms`` of every event is on the recording clock passed to :meth:`start`.
    The sink may be called from any thread.
    """

    capabilities: MouseCapabilities = MouseCapabilities()
    params: MouseSystemParams = MouseSystemParams()
    name: str = "unknown"  # manifest "platform", e.g. "windows-ll-hook"

    @abstractmethod
    def start(
        self,
        sink: Callable[[RawMouseEvent], None],
        clock: RecordingClock,
        on_status: Callable[[str, str], None] | None = None,
    ) -> None:
        """Begin delivering events; raises :class:`PlatformError` when it cannot.

        ``on_status(state, reason)`` reports what happened to the source while recording
        (``restarted``, ``failed``) so the recorder can write it into the file."""

    @abstractmethod
    def stop(self) -> None:
        """Stop delivering events (idempotent)."""

    @abstractmethod
    def healthy(self) -> bool:
        """Whether events still arrive (the recorder restarts a source that is not)."""

    @property
    def restarts(self) -> int:
        """How often the source had to be restarted while recording."""
        return 0

    def probe(self) -> str | None:
        """Check that the source can start here (``doctor``); ``None`` when it can, else
        the reason. The default cannot tell and says so."""
        return None


class ElementInspector(ABC):
    """What is under a point: role, name, window, process and content.

    Calls block for at most ``timeout_ms`` (the recorder calls them on its own
    thread). Content is cut by the caller's rules (:func:`core.input_events.make_content`);
    the content of a password field is never read.
    """

    name: str = "unknown"  # e.g. "windows-uia"

    def prepare(self) -> None:  # noqa: B027 - optional hook
        """Get ready for queries (start helpers, load libraries); called on the thread
        that will query. Raises :class:`PlatformError` when elements cannot be read."""

    @abstractmethod
    def element_at(
        self, gx: int, gy: int, want: ContentRequest, timeout_ms: int
    ) -> ElementInfo | None:
        """The element at the virtual desktop point, or ``None`` when there is none.

        Raises :class:`TimeoutError` when the answer took longer than ``timeout_ms``."""

    @abstractmethod
    def window_info(self, window: int) -> WindowInfo | None:
        """Title and process of a top-level window (native handle)."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release OS resources (COM, D-Bus connections)."""


class TouchpadSource(ABC):
    """Contacts of a multi-touch touchpad (two-finger gestures only are derived)."""

    geometry: TouchpadGeometry = TouchpadGeometry(0, 0)

    def geometry_of(self, device: int) -> TouchpadGeometry | None:
        """Logical size of the touchpad ``device`` of a frame (``None``: unknown yet)."""
        return self.geometry if self.geometry.width > 0 else None

    @property
    def available(self) -> bool:
        """Whether a touchpad was found (a source may only know once it started)."""
        return self.geometry.width > 0

    @abstractmethod
    def start(self, sink: Callable[[ContactFrame], None], clock: RecordingClock) -> None:
        """Begin delivering frames; raises :class:`PlatformError` when it cannot."""

    @abstractmethod
    def stop(self) -> None:
        """Stop delivering frames (idempotent)."""


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

    # Mouse (implementation-plan-mouse.md); None = not available on this platform.

    def mouse_source(self) -> MouseSource | None:
        """Global mouse events, or ``None`` when this platform has none (yet)."""
        return None

    def element_inspector(self) -> ElementInspector | None:
        """Elements under the mouse, or ``None``."""
        return None

    def touchpad_source(self) -> TouchpadSource | None:
        """Touchpad contacts, or ``None`` (no precision touchpad, not supported)."""
        return None

    def describe(self) -> dict[str, object]:
        status = self.support_status()
        return {"name": self.name, "supported": status.supported, "reason": status.reason}


__all__ = [
    "ElementInspector",
    "MouseSource",
    "Platform",
    "PlatformError",
    "SupportStatus",
    "TouchpadSource",
]
