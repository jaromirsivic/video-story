"""Writes the mouse of a recording into ``input/mouse.jsonl`` (no OS code here).

The platform's :class:`~video_story.platform.base.MouseSource` delivers raw events on
its own thread; :class:`InputRecorder` maps them onto the screens of the recording,
tells apart clicks into other applications, into Video Story's own windows and onto
the drawing layer, merges wheel events, thins drag paths and writes the lines.

With an :class:`~video_story.platform.base.ElementInspector` it also records what was
under the mouse (``implementation-plan-mouse.md`` ch. 6): the element at every press,
the element and the selection where a drag or a double/multi click ended, the element
where a scroll began and where the pointer came to rest (``hover``), plus the title and
process of the windows involved. Queries run on a thread of their own through a short
queue; when it is full, rest points are dropped first. Video Story's own windows and
the drawing layer are never asked about.

A failure here never stops the recording: the file simply ends and the manifest says
why. Two-finger touchpad gestures (M5) plug in here too.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from video_story.core.cursor import locate_cursor
from video_story.core.gestures import ContactFrame, GestureTracker, TouchGesture
from video_story.core.input_events import (
    ButtonRecord,
    ContentRequest,
    GestureRecord,
    HoverRecord,
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
from video_story.core.mouse_actions import SCROLL_GAP_MS
from video_story.core.screens import Screen
from video_story.core.timeline import RecordingClock
from video_story.platform.base import ElementInspector, MouseSource, TouchpadSource

log = logging.getLogger(__name__)

HOUSEKEEPING_S = 0.1  # how often an open wheel line is closed, rest points are looked for
QUERY_TIMEOUT_MS = 1500
MAX_PENDING_QUERIES = 8
HOVER_STILL_MS = 600  # the pointer stood still this long: a rest point
HOVER_GAP_MS = 500  # at least this long between two rest points


@dataclass(frozen=True)
class _Query:
    ref: int  # seq of the event the element belongs to
    gx: int
    gy: int
    want: ContentRequest
    win: int | None
    hover: bool = False


@dataclass
class _Press:
    t: int
    gx: int
    gy: int
    multi: bool  # the second (third...) press of a double / multi click
    dragging: bool = False


@dataclass(frozen=True)
class _Move:
    t: int
    gx: int
    gy: int
    target: str
    window: int | None = None


class InputRecorder:
    """Mouse events (and what was under them) of one recording -> ``input/mouse.jsonl``."""

    def __init__(
        self,
        path: Path,
        screens: list[Screen],
        source: MouseSource,
        clock: RecordingClock,
        own_pid: int | None = None,
        drawing_windows: Callable[[], frozenset[int]] = frozenset,
        inspector: ElementInspector | None = None,
        touchpad: TouchpadSource | None = None,
    ) -> None:
        self.path = path
        self.screens = list(screens)
        self.source = source
        self.clock = clock
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.drawing_windows = drawing_windows
        self.inspector = inspector
        self.touchpad = touchpad
        self._trackers: dict[int, GestureTracker] = {}
        self._touch_at: dict[int, _Move | None] = {}  # pointer when the fingers touched
        self.writer = MouseLogWriter(path)
        self._wheels = WheelCoalescer()
        self._throttle = MoveThrottle()
        self._pressed: dict[str, _Press] = {}
        self._click_downs: dict[str, tuple[int, int, int]] = {}  # button -> t, gx, gy
        self._last_wheel_t: int | None = None
        self._last_move: _Move | None = None
        self._last_query_at: tuple[int, int] | None = None
        self._last_hover_t: int | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None
        self._queries: deque[_Query] = deque()
        self._query_cond = threading.Condition()
        self._query_thread: threading.Thread | None = None
        self._elements_ok = False
        self._element_timeouts = 0
        self._dropped_queries = 0
        self._started = False

    # Lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        """Open the file and start the source; raises when the source cannot start
        (the file is then removed again). Elements are optional: when the inspector
        cannot start, only the events are recorded."""
        self.writer.open()
        self.writer.write(
            StatusRecord(
                t=max(0, self.clock.elapsed_ms()), state="started", reason=self.source.name
            )
        )
        if self.touchpad is not None:  # first: on Windows its frames come with the mouse's
            try:
                self.touchpad.start(self._on_frame, self.clock)
            except Exception as exc:  # noqa: BLE001 - events are still recorded
                log.warning("touchpad gestures are not recorded: %s", exc)
                self.touchpad = None
        try:
            self.source.start(self._on_event, self.clock, self._on_status)
        except Exception:
            if self.touchpad is not None:
                try:
                    self.touchpad.stop()
                except Exception:  # noqa: BLE001
                    log.debug("touchpad stop failed", exc_info=True)
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
        if self.inspector is not None:
            self._query_thread = threading.Thread(
                target=self._run_queries, name="mouse-elements", daemon=True
            )
            self._query_thread.start()

    def stop(self) -> MouseInfo:
        """Stop the source and the queries, close the file and describe it."""
        if self._started:
            if self.touchpad is not None:
                try:
                    self.touchpad.stop()
                except Exception:  # noqa: BLE001
                    log.exception("touchpad source did not stop cleanly")
            try:
                self.source.stop()
            except Exception:  # noqa: BLE001 - never let the mouse fail a recording
                log.exception("mouse source did not stop cleanly")
        self._stop.set()
        with self._query_cond:
            self._queries.clear()
            self._query_cond.notify_all()
        for thread in (self._housekeeper, self._query_thread):
            if thread is not None:
                thread.join(timeout=QUERY_TIMEOUT_MS / 1000 + 1)
        if self.inspector is not None:
            try:
                self.inspector.close()
            except Exception:  # noqa: BLE001
                log.exception("element inspector did not close cleanly")
        with self._lock:
            if self.writer.is_open:
                for record in self._wheels.flush():
                    self.writer.write(record)
                for device, tracker in self._trackers.items():
                    for gesture in tracker.flush():
                        self._write_gesture(device, gesture)
                self.writer.write(StatusRecord(t=max(0, self.clock.elapsed_ms()), state="stopped"))
                self.writer.close()
        self._started = False
        if self._dropped_queries:
            log.info("mouse: %d element queries dropped (queue full)", self._dropped_queries)
        return self.info()

    def info(self, error: str | None = None) -> MouseInfo:
        counts = self.writer.counts
        caps = replace(
            self.source.capabilities,
            elements=self._elements_ok,
            element_content=self._elements_ok,
            touchpad_gestures=bool(self.touchpad is not None and self.touchpad.available),
        )
        return MouseInfo(
            platform=self.source.name,
            capabilities=caps,
            params=self.source.params,
            events=counts["events"],
            elements=counts["elements"],
            contents=counts["contents"],
            windows=counts["windows"],
            element_timeouts=self._element_timeouts,
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

    def _window(self, event: RawMouseEvent, target: str) -> int | None:
        """Number of the window under the event (written once per window and title)."""
        if target != Target.APP.value or self.inspector is None or event.window is None:
            return None
        try:
            info = self.inspector.window_info(event.window)
        except Exception:  # noqa: BLE001 - only a title is lost
            log.debug("window info failed", exc_info=True)
            return None
        if info is None:
            return None
        return self.writer.window_id(event.t_ms, event.window, info)

    def _on_event(self, event: RawMouseEvent) -> None:
        with self._lock:
            if not self.writer.is_open:
                return
            for record in self._wheels.due(event.t_ms):
                self.writer.write(record)
            target = self._target(event)
            if event.kind == "move":
                self._on_move(event, target)
            elif event.kind == "wheel":
                self._on_wheel(event, target)
            elif event.button is not None:
                self._on_button(event, target)

    def _on_move(self, event: RawMouseEvent, target: str) -> None:
        self._last_move = _Move(event.t_ms, event.gx, event.gy, target, event.window)
        drag_px = self.source.params.drag_px
        for press in self._pressed.values():
            if not press.dragging and (
                abs(event.gx - press.gx) > drag_px[0] or abs(event.gy - press.gy) > drag_px[1]
            ):
                press.dragging = True
        if self._pressed and self._throttle.accept(event.t_ms, event.gx, event.gy):
            self.writer.write(MoveRecord(t=event.t_ms, **self._point(event)))  # type: ignore[arg-type]

    def _on_wheel(self, event: RawMouseEvent, target: str) -> None:
        win = self._window(event, target)
        starts = self._last_wheel_t is None or event.t_ms - self._last_wheel_t > SCROLL_GAP_MS
        self._last_wheel_t = event.t_ms
        seq = self.writer.next_seq() if starts and target == Target.APP.value else 0
        record = WheelRecord(
            seq=seq,
            t=event.t_ms,
            axis=event.axis or "v",
            delta=event.delta,
            mods=event.modifiers,
            target=target,
            win=win,
            injected=event.injected,
            **self._point(event),  # type: ignore[arg-type]
        )
        for done in self._wheels.add(record):
            self.writer.write(done)
        if seq:
            self._enqueue(_Query(seq, event.gx, event.gy, ContentRequest.NONE, win))

    def _on_button(self, event: RawMouseEvent, target: str) -> None:
        button = str(event.button)
        params = self.source.params
        win = self._window(event, target)
        record = ButtonRecord(
            t=event.t_ms,
            kind=event.kind,  # type: ignore[arg-type]
            button=button,
            mods=event.modifiers,
            target=target,
            win=win,
            injected=event.injected,
            **self._point(event),  # type: ignore[arg-type]
        )
        seq = self.writer.write(record)
        if event.kind == "down":
            if not self._pressed:
                self._throttle.reset()
            prev = self._click_downs.get(button)
            multi = (
                prev is not None
                and event.t_ms - prev[0] <= params.double_click_ms
                and abs(event.gx - prev[1]) <= params.double_click_px[0] / 2
                and abs(event.gy - prev[2]) <= params.double_click_px[1] / 2
            )
            self._pressed[button] = _Press(event.t_ms, event.gx, event.gy, multi)
            if target == Target.APP.value:
                self._enqueue(_Query(seq, event.gx, event.gy, ContentRequest.VALUE_AND_TEXT, win))
            return
        press = self._pressed.pop(button, None)
        if press is None:
            return
        dragging = press.dragging or (
            abs(event.gx - press.gx) > params.drag_px[0]
            or abs(event.gy - press.gy) > params.drag_px[1]
        )
        if dragging:
            self._click_downs.pop(button, None)
        else:
            self._click_downs[button] = (press.t, press.gx, press.gy)
        if target == Target.APP.value and (dragging or press.multi):
            # the drop target, and what the drag or the double click selected
            self._enqueue(_Query(seq, event.gx, event.gy, ContentRequest.SELECTION, win))

    def _on_status(self, state: str, reason: str) -> None:
        with self._lock:
            if self.writer.is_open:
                self.writer.write(
                    StatusRecord(t=max(0, self.clock.elapsed_ms()), state=state, reason=reason)
                )

    # Touchpad -----------------------------------------------------------------------

    def _on_frame(self, frame: ContactFrame) -> None:
        """A touchpad scan (touchpad thread): gestures are written when fingers lift."""
        with self._lock:
            if not self.writer.is_open or self.touchpad is None:
                return
            tracker = self._trackers.get(frame.device)
            if tracker is None:
                pad = self.touchpad.geometry_of(frame.device)
                if pad is None:
                    return
                tracker = self._trackers[frame.device] = GestureTracker(pad)
            if frame.contacts and frame.device not in self._touch_at:
                self._touch_at[frame.device] = self._last_move
            finished = tracker.add(frame)
            for gesture in finished:
                self._write_gesture(frame.device, gesture)
            if not frame.contacts:
                self._touch_at.pop(frame.device, None)
            elif finished:  # this frame already belongs to a new touch
                self._touch_at[frame.device] = self._last_move

    def _write_gesture(self, device: int, gesture: TouchGesture) -> None:
        """Write a finished gesture at the pointer where the fingers touched the pad
        (gestures over Video Story's own windows are left out)."""
        at = self._touch_at.get(device) or self._last_move
        if at is None or at.target != Target.APP.value:
            return
        win = None
        if at.window is not None:
            probe = RawMouseEvent(
                kind="move", t_ms=gesture.t_ms, gx=at.gx, gy=at.gy, window=at.window
            )
            win = self._window(probe, at.target)
        point = self._point(RawMouseEvent(kind="move", t_ms=gesture.t_ms, gx=at.gx, gy=at.gy))
        seq = self.writer.write(
            GestureRecord(
                t=gesture.t_ms,
                kind=gesture.kind,
                until=gesture.until_ms,
                scale=gesture.scale,
                direction=gesture.direction,
                distance=gesture.distance,
                win=win,
                **point,  # type: ignore[arg-type]
            )
        )
        self._enqueue(_Query(seq, at.gx, at.gy, ContentRequest.NONE, win))

    # Rest points --------------------------------------------------------------------

    def _check_hover(self, now: int) -> None:
        """A rest point: the pointer stood still over another application long enough,
        away from where the last element was read."""
        move = self._last_move
        if move is None or self._pressed or self.inspector is None:
            return
        if move.target != Target.APP.value or now - move.t < HOVER_STILL_MS:
            return
        if self._last_hover_t is not None and now - self._last_hover_t < HOVER_GAP_MS:
            return
        drag_px = self.source.params.drag_px
        last = self._last_query_at
        if (
            last is not None
            and abs(move.gx - last[0]) <= drag_px[0]
            and abs(move.gy - last[1]) <= drag_px[1]
        ):
            return
        self._last_hover_t = now
        point = self._point(RawMouseEvent(kind="move", t_ms=now, gx=move.gx, gy=move.gy))
        seq = self.writer.write(HoverRecord(t=now, **point))  # type: ignore[arg-type]
        self._enqueue(
            _Query(seq, move.gx, move.gy, ContentRequest.VALUE_AND_TEXT, None, hover=True)
        )

    # Element queries ----------------------------------------------------------------

    def _enqueue(self, query: _Query) -> None:
        if self.inspector is None or self._stop.is_set():
            return
        self._last_query_at = (query.gx, query.gy)
        with self._query_cond:
            if len(self._queries) >= MAX_PENDING_QUERIES:
                victim = next((q for q in self._queries if q.hover), self._queries[0])
                self._queries.remove(victim)
                self._dropped_queries += 1
            self._queries.append(query)
            self._query_cond.notify()

    def _run_queries(self) -> None:
        inspector = self.inspector
        assert inspector is not None
        try:
            inspector.prepare()
        except Exception as exc:  # noqa: BLE001 - events are still recorded
            log.warning("elements under the mouse are not recorded: %s", exc)
            self._on_status("elements_unavailable", str(exc) or type(exc).__name__)
            return
        self._elements_ok = True
        while not self._stop.is_set():
            with self._query_cond:
                while not self._queries and not self._stop.is_set():
                    self._query_cond.wait(0.5)
                if self._stop.is_set():
                    return
                query = self._queries.popleft()
            self._answer(inspector, query)

    def _answer(self, inspector: ElementInspector, query: _Query) -> None:
        started = self.clock.elapsed_ms()
        try:
            info = inspector.element_at(query.gx, query.gy, query.want, QUERY_TIMEOUT_MS)
        except TimeoutError:
            self._element_timeouts += 1
            self._write_error(query, "timeout", self.clock.elapsed_ms() - started)
            return
        except Exception as exc:  # noqa: BLE001 - one failed query must not end the rest
            log.debug("element query failed", exc_info=True)
            self._write_error(query, f"failed: {exc}"[:200], self.clock.elapsed_ms() - started)
            return
        with self._lock:
            if not self.writer.is_open:
                return
            if info is None:
                self.writer.write_element_error(self.clock.elapsed_ms(), query.ref, "none")
                return
            if info.pid is not None and info.pid == self.own_pid:
                return  # Video Story's own window after all: nothing to store
            self.writer.write_element(self.clock.elapsed_ms(), query.ref, info, win=query.win)

    def _write_error(self, query: _Query, error: str, ms: int) -> None:
        with self._lock:
            if self.writer.is_open:
                self.writer.write_element_error(self.clock.elapsed_ms(), query.ref, error, ms=ms)

    # Housekeeping -------------------------------------------------------------------

    def _housekeep(self) -> None:
        ticks = 0
        while not self._stop.wait(HOUSEKEEPING_S):
            ticks += 1
            with self._lock:
                if not self.writer.is_open:
                    return
                now = self.clock.elapsed_ms()
                for record in self._wheels.due(now):
                    self.writer.write(record)
                for device, tracker in self._trackers.items():
                    for gesture in tracker.due(now):
                        self._write_gesture(device, gesture)
                        self._touch_at.pop(device, None)
                self._check_hover(now)
                if ticks % 10 == 0:
                    self.writer.flush()


__all__ = ["InputRecorder", "QUERY_TIMEOUT_MS"]
