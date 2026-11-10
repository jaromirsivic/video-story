"""Writes the mouse of a recording into ``input/mouse.jsonl`` (no OS code here).

The platform's :class:`~video_story.platform.base.MouseSource` delivers raw events on
its own thread; :class:`InputRecorder` maps them onto the screens of the recording,
tells apart clicks into other applications, into Video Story's own windows and onto
the drawing layer, merges wheel events, thins drag paths and writes the lines. A
failure here never stops the recording: the file simply ends and the manifest says why.

Element queries (what is under the mouse) follow in M3, two-finger touchpad gestures
in M5; both plug in here.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from video_story.core.cursor import locate_cursor
from video_story.core.input_events import (
    ButtonRecord,
    MouseLogWriter,
    MoveRecord,
    MoveThrottle,
    RawMouseEvent,
    StatusRecord,
    Target,
    WheelCoalescer,
    WheelRecord,
)
from video_story.core.manifest import MouseInfo
from video_story.core.screens import Screen
from video_story.core.timeline import RecordingClock
from video_story.platform.base import MouseSource

log = logging.getLogger(__name__)

HOUSEKEEPING_S = 0.1  # how often an open wheel line is closed and the file flushed


class InputRecorder:
    """Mouse events of one recording -> ``input/mouse.jsonl``."""

    def __init__(
        self,
        path: Path,
        screens: list[Screen],
        source: MouseSource,
        clock: RecordingClock,
        own_pid: int | None = None,
        drawing_windows: Callable[[], frozenset[int]] = frozenset,
    ) -> None:
        self.path = path
        self.screens = list(screens)
        self.source = source
        self.clock = clock
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.drawing_windows = drawing_windows
        self.writer = MouseLogWriter(path)
        self._wheels = WheelCoalescer()
        self._throttle = MoveThrottle()
        self._pressed: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None
        self._started = False

    # Lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        """Open the file and start the source; raises when the source cannot start
        (the file is then removed again)."""
        self.writer.open()
        self.writer.write(
            StatusRecord(
                t=max(0, self.clock.elapsed_ms()), state="started", reason=self.source.name
            )
        )
        try:
            self.source.start(self._on_event, self.clock, self._on_status)
        except Exception:
            self.writer.close()
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                log.debug("cannot remove %s", self.path, exc_info=True)
            raise
        self._started = True
        self._stop.clear()
        self._housekeeper = threading.Thread(
            target=self._housekeep, name="mouse-housekeeping", daemon=True
        )
        self._housekeeper.start()

    def stop(self) -> MouseInfo:
        """Stop the source, close the file and describe it for the manifest."""
        if self._started:
            try:
                self.source.stop()
            except Exception:  # noqa: BLE001 - never let the mouse fail a recording
                log.exception("mouse source did not stop cleanly")
        self._stop.set()
        if self._housekeeper is not None:
            self._housekeeper.join(timeout=2)
        with self._lock:
            if self.writer.is_open:
                for record in self._wheels.flush():
                    self.writer.write(record)
                self.writer.write(StatusRecord(t=max(0, self.clock.elapsed_ms()), state="stopped"))
                self.writer.close()
        self._started = False
        return self.info()

    def info(self, error: str | None = None) -> MouseInfo:
        counts = self.writer.counts
        return MouseInfo(
            platform=self.source.name,
            capabilities=self.source.capabilities,
            params=self.source.params,
            events=counts["events"],
            elements=counts["elements"],
            contents=counts["contents"],
            windows=counts["windows"],
            hook_restarts=self.source.restarts,
            error=error,
        )

    # Events ---------------------------------------------------------------------

    def _target(self, event: RawMouseEvent) -> str:
        if event.pid is None or event.pid != self.own_pid:
            return Target.APP.value
        try:
            drawing = event.window is not None and event.window in self.drawing_windows()
        except Exception:  # noqa: BLE001
            drawing = False
        return Target.DRAWING.value if drawing else Target.VIDEO_STORY.value

    def _point(self, event: RawMouseEvent) -> dict[str, object]:
        pos = locate_cursor(self.screens, event.gx, event.gy)
        return {
            "screen": pos.screen_id if pos else None,
            "x": pos.x if pos else None,
            "y": pos.y if pos else None,
            "gx": event.gx,
            "gy": event.gy,
        }

    def _on_event(self, event: RawMouseEvent) -> None:
        with self._lock:
            if not self.writer.is_open:
                return
            for record in self._wheels.due(event.t_ms):
                self.writer.write(record)
            if event.kind == "move":
                if self._pressed and self._throttle.accept(event.t_ms, event.gx, event.gy):
                    self.writer.write(MoveRecord(t=event.t_ms, **self._point(event)))  # type: ignore[arg-type]
                return
            if event.kind == "wheel":
                record = WheelRecord(
                    t=event.t_ms,
                    axis=event.axis or "v",
                    delta=event.delta,
                    mods=event.modifiers,
                    target=self._target(event),
                    injected=event.injected,
                    **self._point(event),  # type: ignore[arg-type]
                )
                for done in self._wheels.add(record):
                    self.writer.write(done)
                return
            if event.button is None:
                return
            button = str(event.button)
            if event.kind == "down":
                if not self._pressed:
                    self._throttle.reset()
                self._pressed.add(button)
            else:
                self._pressed.discard(button)
            self.writer.write(
                ButtonRecord(
                    t=event.t_ms,
                    kind=event.kind,  # type: ignore[arg-type]
                    button=button,
                    mods=event.modifiers,
                    target=self._target(event),
                    injected=event.injected,
                    **self._point(event),  # type: ignore[arg-type]
                )
            )

    def _on_status(self, state: str, reason: str) -> None:
        with self._lock:
            if self.writer.is_open:
                self.writer.write(
                    StatusRecord(t=max(0, self.clock.elapsed_ms()), state=state, reason=reason)
                )

    def _housekeep(self) -> None:
        ticks = 0
        while not self._stop.wait(HOUSEKEEPING_S):
            ticks += 1
            with self._lock:
                if not self.writer.is_open:
                    return
                for record in self._wheels.due(self.clock.elapsed_ms()):
                    self.writer.write(record)
                if ticks % 10 == 0:
                    self.writer.flush()


__all__ = ["InputRecorder"]
