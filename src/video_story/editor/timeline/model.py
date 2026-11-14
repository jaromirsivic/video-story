"""Timeline state without any Qt: duration, zoom, scroll, playhead, crop, selection.

All times are milliseconds of the recording timeline; pixels are measured inside the
track body (the area right of the track headers), whose width is ``viewport_width``.
``zoom`` is in pixels per second and is bounded by "whole recording fits the viewport"
and :data:`MAX_ZOOM_PX_PER_S`.
"""

from __future__ import annotations

MAX_ZOOM_PX_PER_S = 200.0
MIN_RANGE_MS = 1000
DEFAULT_SCREEN = "screen1"


def _clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


class TimelineModel:
    """Mutable timeline state with the invariants the view relies on."""

    def __init__(self, duration_ms: int = 0, viewport_width: int = 800) -> None:
        self.viewport_width = max(1, int(viewport_width))
        self.screens: list[str] = []
        self.selected_screen: str = DEFAULT_SCREEN
        self.playhead_ms = 0
        self.scroll_ms = 0
        self.in_ms = 0
        self.out_ms = 0
        self.duration_ms = 0
        self.zoom = 1.0
        self.keypoints: list[int] = []  # Keypoint moments (ms, ascending)
        # Where the recording was paused: (at_ms, duration_ms or None), ascending.
        self.pauses: list[tuple[int, int | None]] = []
        self.play_marker_ms: int | None = None  # where Play was last pressed (Stop returns)
        self.set_duration(duration_ms)

    # Duration and screens ---------------------------------------------------

    def set_duration(self, duration_ms: int) -> None:
        """Reset the timeline for a new recording (crop = whole, playhead = 0)."""
        self.duration_ms = max(0, int(duration_ms))
        self.playhead_ms = 0
        self.in_ms = 0
        self.out_ms = self.duration_ms
        self.keypoints = []
        self.pauses = []
        self.play_marker_ms = None
        self.fit()

    # Keypoints and play marker ---------------------------------------------

    def set_keypoints(self, keypoints: list[int]) -> None:
        """Keypoints inside the recording, sorted and unique."""
        self.keypoints = sorted({int(k) for k in keypoints if 0 <= int(k) <= self.duration_ms})

    def set_pauses(self, pauses: list[tuple[int, int | None]]) -> None:
        """Pauses inside the recording, sorted by time (one per moment)."""
        unique = {int(at): length for at, length in pauses if 0 <= int(at) <= self.duration_ms}
        self.pauses = sorted(unique.items())

    def nearest_keypoint(self, time_ms: float) -> int | None:
        if not self.keypoints:
            return None
        return min(self.keypoints, key=lambda k: abs(k - time_ms))

    def nearest_marker(self, time_ms: float) -> int | None:
        """The nearest keypoint or pause (both are magnets)."""
        markers = [*self.keypoints, *(at for at, _length in self.pauses)]
        if not markers:
            return None
        return min(markers, key=lambda k: abs(k - time_ms))

    def snap_to_keypoint(self, time_ms: int, tolerance_ms: float) -> int:
        """``time_ms`` pulled onto the nearest keypoint or pause when one is within
        ``tolerance_ms`` (the magnet of the crop handles and the playhead)."""
        nearest = self.nearest_marker(time_ms)
        if nearest is not None and abs(nearest - time_ms) <= tolerance_ms:
            return nearest
        return time_ms

    def set_play_marker(self, time_ms: int | None) -> None:
        self.play_marker_ms = None if time_ms is None else self.clamp_time(time_ms)

    def stop_position(self) -> int:
        """Where Stop lands: the play marker when it lies inside the crop, else In."""
        marker = self.play_marker_ms
        if marker is not None and self.in_ms <= marker < self.out_ms:
            return marker
        return self.in_ms

    def set_screens(self, screens: list[str]) -> None:
        self.screens = list(screens)
        if self.selected_screen not in self.screens and self.screens:
            self.selected_screen = self.screens[0]

    def select_screen(self, screen_id: str) -> bool:
        if screen_id == self.selected_screen or (self.screens and screen_id not in self.screens):
            return False
        self.selected_screen = screen_id
        return True

    @property
    def min_range_ms(self) -> int:
        """Smallest allowed crop: 1 s, or the whole recording when it is shorter."""
        return min(MIN_RANGE_MS, self.duration_ms)

    @property
    def crop_length_ms(self) -> int:
        return self.out_ms - self.in_ms

    # Zoom and scroll --------------------------------------------------------

    def min_zoom(self, width: int | None = None) -> float:
        """Zoom at which the whole recording fills ``width`` pixels."""
        width = self.viewport_width if width is None else max(1, int(width))
        if self.duration_ms <= 0:
            return MAX_ZOOM_PX_PER_S
        return min(MAX_ZOOM_PX_PER_S, width * 1000.0 / self.duration_ms)

    def max_zoom(self) -> float:
        return MAX_ZOOM_PX_PER_S

    def visible_ms(self) -> float:
        return self.viewport_width * 1000.0 / self.zoom

    def max_scroll_ms(self) -> int:
        return max(0, int(round(self.duration_ms - self.visible_ms())))

    def fit(self, width: int | None = None) -> None:
        """Show the whole recording in the viewport."""
        if width is not None:
            self.viewport_width = max(1, int(width))
        self.zoom = self.min_zoom()
        self.scroll_ms = 0

    def set_viewport_width(self, width: int) -> None:
        """The view was resized; keep the zoom unless the recording no longer fills it."""
        self.viewport_width = max(1, int(width))
        self.set_zoom(self.zoom)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = _clamp(float(zoom), self.min_zoom(), MAX_ZOOM_PX_PER_S)
        self.set_scroll(self.scroll_ms)

    def zoom_around(self, pixel: float, factor: float) -> None:
        """Multiply the zoom by ``factor`` keeping the time under ``pixel`` in place."""
        anchor_ms = self.x_to_time(pixel)
        self.zoom = _clamp(self.zoom * factor, self.min_zoom(), MAX_ZOOM_PX_PER_S)
        self.set_scroll(anchor_ms - pixel * 1000.0 / self.zoom)

    def center_on(self, time_ms: float) -> None:
        """Scroll so that ``time_ms`` sits in the middle of the viewport (as far as the
        ends of the recording allow)."""
        self.set_scroll(time_ms - self.visible_ms() / 2.0)

    def zoom_centered(self, factor: float, center_ms: float) -> None:
        """Multiply the zoom by ``factor`` and keep ``center_ms`` (the playhead) centred."""
        self.zoom = _clamp(self.zoom * factor, self.min_zoom(), MAX_ZOOM_PX_PER_S)
        self.center_on(center_ms)

    def zoom_fraction(self) -> float:
        """Zoom position on a logarithmic 0..1 scale (for the slider)."""
        lo, hi = self.min_zoom(), MAX_ZOOM_PX_PER_S
        if hi <= lo:
            return 1.0
        import math

        return _clamp(math.log(self.zoom / lo) / math.log(hi / lo), 0.0, 1.0)

    def set_zoom_fraction(self, fraction: float, center_ms: float | None = None) -> None:
        """Zoom from the slider; with ``center_ms`` that time is kept in the middle."""
        lo, hi = self.min_zoom(), MAX_ZOOM_PX_PER_S
        if hi <= lo:
            self.set_zoom(hi)
        else:
            self.set_zoom(lo * (hi / lo) ** _clamp(fraction, 0.0, 1.0))
        if center_ms is not None:
            self.center_on(center_ms)

    def set_scroll(self, scroll_ms: float) -> None:
        self.scroll_ms = int(round(_clamp(scroll_ms, 0, self.max_scroll_ms())))

    def ensure_visible(self, time_ms: int, margin_fraction: float = 0.1) -> bool:
        """Scroll so that ``time_ms`` is inside the viewport; returns True when scrolled."""
        x = self.time_to_x(time_ms)
        if 0 <= x < self.viewport_width:
            return False
        before = self.scroll_ms
        self.set_scroll(time_ms - self.visible_ms() * margin_fraction)
        return self.scroll_ms != before

    # Conversions ------------------------------------------------------------

    def time_to_x(self, time_ms: float) -> float:
        return (time_ms - self.scroll_ms) * self.zoom / 1000.0

    def x_to_time(self, x: float) -> int:
        return int(round(self.scroll_ms + x * 1000.0 / self.zoom))

    def clamp_time(self, time_ms: float) -> int:
        return int(round(_clamp(time_ms, 0, self.duration_ms)))

    # Playhead and crop ------------------------------------------------------

    def set_playhead(self, time_ms: float) -> bool:
        value = self.clamp_time(time_ms)
        if value == self.playhead_ms:
            return False
        self.playhead_ms = value
        return True

    def set_in(self, time_ms: float) -> bool:
        value = int(round(_clamp(time_ms, 0, self.out_ms - self.min_range_ms)))
        if value == self.in_ms:
            return False
        self.in_ms = value
        return True

    def set_out(self, time_ms: float) -> bool:
        value = int(round(_clamp(time_ms, self.in_ms + self.min_range_ms, self.duration_ms)))
        if value == self.out_ms:
            return False
        self.out_ms = value
        return True

    def reset_crop(self) -> bool:
        changed = self.in_ms != 0 or self.out_ms != self.duration_ms
        self.in_ms = 0
        self.out_ms = self.duration_ms
        return changed

    def inside_crop(self, time_ms: int) -> bool:
        return self.in_ms <= time_ms < self.out_ms


__all__ = ["DEFAULT_SCREEN", "MAX_ZOOM_PX_PER_S", "MIN_RANGE_MS", "TimelineModel"]
