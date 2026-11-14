"""Everything recorded about the mouse at one moment of a recording (no Qt).

A click on the mouse left of the transport opens the **Mouse** window
(:mod:`video_story.editor.mouse_details_dialog`). It follows the playhead and shows, for
that moment:

* the five buttons (held from ``down`` to ``up``, at least :data:`MIN_LIT_MS`) and the
  four wheel directions (turning from the first to the last event of a merged wheel
  record, at least :data:`MIN_LIT_MS`), like the mouse picture;
* the action in progress (click, double click, drag, scroll, Ctrl+wheel zoom, touchpad
  gesture) with its time, modifiers, the ``injected`` flag and where it started, is now
  and ended - the recording is finished, so a drag's end is known while it runs;
* the latest element under the mouse (UI Automation: at a press, the end of a drag or
  double click, the start of a scroll and where the cursor rested), how old that is,
  its parents, bounds in pixels of the screen image, the drop target and its content;
* the window of the latest mouse event and since when the user worked in it;
* the latest text copied before the moment (frame sidecars), when, its length and
  which copy of the recording it is;
* the mouse actions around the moment as timeline lines.

Built once per recording in a worker (the clipboard needs one sidecar per tick), then
every lookup is a bisection, fast enough to run on each playhead step.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from video_story.core.input_events import (
    ButtonRecord,
    ElementRecord,
    GestureRecord,
    MouseLog,
    MouseSystemParams,
    WheelRecord,
)
from video_story.core.mouse_actions import MouseAction, Point, derive_actions, path_length
from video_story.core.naming import frame_base
from video_story.core.screens import Screen
from video_story.core.timeline import format_timestamp
from video_story.host.mouse_timeline import BUTTON_NAMES, action_lines
from video_story.host.runner import FrameIndex

from .mouse_activity import MIN_LIT_MS

BUTTONS = ("left", "middle", "right", "x1", "x2")
WHEEL_DIRECTIONS = ("up", "down", "left", "right")
RECENT_BEFORE = 5  # mouse actions listed before the moment
RECENT_AFTER = 3  # and after it
NOW_MARK = "   <- now"
NONE = "-"

ACTION_NAMES = {
    "click": "CLICK",
    "drag": "DRAG",
    "scroll": "SCROLL",
    "zoom": "ZOOM-SCROLL (Ctrl+wheel)",
    "gesture": "GESTURE",
}
EVENT_NAMES = {
    "down": "press",
    "up": "release",
    "wheel": "scroll",
    "hover": "rest",
    "gesture": "gesture",
}
TARGET_NOTES = {"video_story": "in Video Story", "drawing": "on the drawing layer"}


@dataclass(frozen=True)
class ClipboardChange:
    time_ms: int
    text: str


@dataclass
class MouseMoment:
    """What the Mouse window shows at ``time_ms``: one string per text box."""

    time_ms: int
    recorded: bool
    held: dict[str, int] = field(default_factory=dict)  # button -> ms held so far
    wheel: frozenset[str] = frozenset()  # turning directions: up, down, left, right
    action: str = NONE
    action_time: str = NONE
    modifiers: str = NONE
    injected: str = NONE
    start: str = NONE
    now: str = NONE
    end: str = NONE
    scroll: str = NONE
    element_captured: str = NONE
    element_role: str = NONE
    element_name: str = NONE
    element_parents: str = NONE
    element_bounds: str = NONE
    drop_target: str = NONE
    content_label: str = "Content"
    content: str = ""
    window_title: str = NONE
    window_process: str = NONE
    window_since: str = NONE
    clipboard_changed: str = NONE
    clipboard_length: str = NONE
    clipboard_copy: str = NONE
    clipboard_text: str = ""
    recent: str = ""


def seconds_ago(delta_ms: int) -> str:
    return f"{max(0, delta_ms) / 1000:.1f} s ago"


def _modifiers(mods: Iterable[str]) -> str:
    text = "+".join(m.capitalize() for m in mods)
    return text or NONE


def _quote(text: str, limit: int = 80) -> str:
    flat = " ".join(text.split())
    return f'"{flat[:limit]}..."' if len(flat) > limit else f'"{flat}"'


def _element_label(el: ElementRecord) -> str:
    return f"{el.role} {_quote(el.name)}" if el.name else el.role


def read_clipboard_changes(index: FrameIndex) -> list[ClipboardChange]:
    """Every text copied during the recording, from the frame sidecars (one per tick)."""
    owner: dict[int, str] = {}
    for screen_id in reversed(index.screens):  # the first screen of a tick wins
        for name in index.by_screen[screen_id]:
            owner[name.time_ms] = screen_id
    changes: list[ClipboardChange] = []
    for time_ms in sorted(owner):
        sidecar = index.info_dir / f"{frame_base(time_ms, owner[time_ms])}.json"
        try:
            raw = sidecar.read_bytes()
        except OSError:
            continue
        if b'"clipboard"' not in raw:
            continue  # most sidecars: no need to parse them
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        text = data.get("clipboard") if isinstance(data, dict) else None
        if isinstance(text, str):
            changes.append(ClipboardChange(time_ms, text))
    return changes


@dataclass(frozen=True)
class _Press:
    button: str
    start: int
    end: int  # release (or the next press when its up was lost)


class MouseDetails:
    """The mouse and the clipboard of one recording, looked up by time."""

    def __init__(
        self,
        log: MouseLog | None,
        params: MouseSystemParams | None = None,
        duration_ms: int = 0,
        screens: Iterable[Screen] = (),
        clipboard: Iterable[ClipboardChange] = (),
    ) -> None:
        self.recorded = log is not None
        self.log = log or MouseLog()
        self.screens = list(screens)
        self.clipboard = sorted(clipboard, key=lambda c: c.time_ms)
        self._clip_times = [c.time_ms for c in self.clipboard]
        log = self.log
        self.actions: list[MouseAction] = derive_actions(log, params, duration_ms or None)
        self._action_starts = [a.t for a in self.actions]
        self._lines = action_lines(log, self.actions)
        self._presses = self._build_presses(log.buttons)
        self._wheels = sorted(
            (w for w in log.wheels if not w.injected and w.delta), key=lambda w: w.t
        )
        self._moves = sorted(log.moves, key=lambda m: m.t)
        self._move_times = [m.t for m in self._moves]
        # Time and kind of every event an element can belong to.
        events: dict[int, tuple[int, str]] = {}
        for rec in log.buttons:
            events[rec.seq] = (rec.t, rec.kind)
        for rec in log.wheels:
            events[rec.seq] = (rec.t, "wheel")
        for rec in log.hovers:
            events[rec.seq] = (rec.t, "hover")
        for rec in log.gestures:
            events[rec.seq] = (rec.t, "gesture")
        found: list[tuple[int, str, ElementRecord]] = []
        for ref, records in log.elements.items():
            el = records[0]
            t, kind = events.get(ref, (el.t, "event"))
            found.append((t, kind, el))
        found.sort(key=lambda f: f[0])
        self._elements = found
        self._element_times = [f[0] for f in found]
        # The window of every mouse event, for "since when".
        wins: list[tuple[int, int]] = []
        point_records: list[ButtonRecord | WheelRecord | GestureRecord] = [
            *log.buttons,
            *log.wheels,
            *log.gestures,
        ]
        for rec in point_records:
            if rec.win is not None:
                wins.append((rec.t, rec.win))
        for t, _kind, el in found:
            if el.win is not None:
                wins.append((t, el.win))
        wins.sort()
        self._wins = wins
        self._win_times = [w[0] for w in wins]
        self._win_since: list[int] = []
        for i, (t, win) in enumerate(wins):
            same = i > 0 and wins[i - 1][1] == win
            self._win_since.append(self._win_since[-1] if same else t)

    @staticmethod
    def _build_presses(buttons: Iterable[ButtonRecord]) -> list[_Press]:
        presses: list[_Press] = []
        down: dict[str, int] = {}
        for rec in sorted(buttons, key=lambda r: (r.t, r.seq)):
            if rec.injected or rec.button not in BUTTONS:
                continue
            if rec.kind == "down":
                if rec.button in down:  # its up was lost
                    presses.append(_Press(rec.button, down[rec.button], rec.t))
                down[rec.button] = rec.t
            elif rec.button in down:
                presses.append(_Press(rec.button, down.pop(rec.button), rec.t))
        for button, start in down.items():  # still held at the end
            presses.append(_Press(button, start, start))
        return presses

    # Lookups ----------------------------------------------------------------

    def _where(self, p: Point) -> str:
        return f"{p.screen or '?'} {p.x},{p.y}"

    def _local_bounds(self, bounds: tuple[int, int, int, int] | None) -> str:
        if not bounds:
            return NONE
        bx, by, bw, bh = bounds
        for screen in self.screens:
            if screen.contains(bx, by):
                x, y = screen.to_local(bx, by)
                return f"{screen.id} {x},{y} {bw}x{bh}"
        return f"desktop {bx},{by} {bw}x{bh}"

    def _action_index_at(self, time_ms: int) -> int | None:
        i = bisect.bisect_right(self._action_starts, time_ms)
        for j in range(i - 1, max(-1, i - 51), -1):  # the latest one that still runs
            a = self.actions[j]
            if time_ms < max(a.until, a.t + MIN_LIT_MS):
                return j
        return None

    def action_at(self, time_ms: int) -> MouseAction | None:
        """The action in progress (a quick one stays at least MIN_LIT_MS)."""
        j = self._action_index_at(time_ms)
        return self.actions[j] if j is not None else None

    def held_at(self, time_ms: int) -> dict[str, int]:
        held: dict[str, int] = {}
        for p in self._presses:
            if p.start <= time_ms < max(p.end, p.start + MIN_LIT_MS):
                held[p.button] = max(held.get(p.button, 0), time_ms - p.start)
        return held

    def wheel_at(self, time_ms: int) -> frozenset[str]:
        lit = set()
        for w in self._wheels:
            if w.t > time_ms:
                break
            if time_ms < max(w.end, w.t) + MIN_LIT_MS:
                if w.axis == "v":
                    lit.add("up" if w.delta > 0 else "down")
                else:
                    lit.add("right" if w.delta > 0 else "left")
        return frozenset(lit)

    def element_at(self, time_ms: int) -> tuple[int, str, ElementRecord] | None:
        i = bisect.bisect_right(self._element_times, time_ms) - 1
        return self._elements[i] if i >= 0 else None

    def clipboard_at(self, time_ms: int) -> tuple[int, ClipboardChange] | None:
        """The latest copied text at or before ``time_ms`` and its number (from 1)."""
        i = bisect.bisect_right(self._clip_times, time_ms) - 1
        return (i + 1, self.clipboard[i]) if i >= 0 else None

    def _drag_now(self, a: MouseAction, time_ms: int) -> str:
        lo = bisect.bisect_left(self._move_times, a.t)
        hi = bisect.bisect_right(self._move_times, time_ms)
        moves = self._moves[lo:hi]
        if not moves:
            return self._where(a.at)
        last = moves[-1]
        so_far = round(path_length([(a.at.gx, a.at.gy), *((m.gx, m.gy) for m in moves)]))
        return f"{last.screen or '?'} {last.x},{last.y} ({so_far} px so far)"

    def _recent(self, time_ms: int, current_index: int | None) -> str:
        if not self._lines:
            return ""
        i = bisect.bisect_right(self._action_starts, time_ms)
        stop = i if current_index is None else current_index
        first = max(0, stop - RECENT_BEFORE)
        lines = list(self._lines[first:stop])
        if current_index is not None:
            lines.append(self._lines[current_index] + NOW_MARK)
            after_from = current_index + 1
        else:
            lines.append(f"--- now {time_ms} ---")
            after_from = i
        lines.extend(self._lines[after_from : after_from + RECENT_AFTER])
        return "\n".join(lines)

    # The moment -------------------------------------------------------------

    def at(self, time_ms: int) -> MouseMoment:
        time_ms = int(time_ms)
        m = MouseMoment(time_ms=time_ms, recorded=self.recorded)
        if self.recorded:
            self._fill_mouse(m, time_ms)
        clip = self.clipboard_at(time_ms)
        if clip is None:
            m.clipboard_changed = "Nothing copied yet"
        else:
            number, change = clip
            ago = seconds_ago(time_ms - change.time_ms)
            m.clipboard_changed = f"{format_timestamp(change.time_ms)} ({ago})"
            m.clipboard_length = f"{len(change.text)} chars"
            m.clipboard_copy = f"{number} of {len(self.clipboard)}"
            m.clipboard_text = change.text
        return m

    def _fill_mouse(self, m: MouseMoment, time_ms: int) -> None:
        m.held = self.held_at(time_ms)
        m.wheel = self.wheel_at(time_ms)
        index = self._action_index_at(time_ms)
        a = self.actions[index] if index is not None else None
        if a is None:
            m.action = "Idle"
        else:
            self._fill_action(m, a, time_ms)
        found = self.element_at(time_ms)
        if found is not None:
            self._fill_element(m, *found, time_ms)
        if a is not None and a.end_ref is not None and a.end_ref != a.ref:
            end_el = self.log.element_for(a.end_ref)
            if end_el is not None and not end_el.error:
                m.drop_target = _element_label(end_el)
        i = bisect.bisect_right(self._win_times, time_ms) - 1
        if i >= 0:
            win = self.log.windows.get(self._wins[i][1])
            if win is not None:
                m.window_title = win.title or NONE
                m.window_process = win.process or NONE
            since = self._win_since[i]
            m.window_since = f"{format_timestamp(since)} ({(time_ms - since) / 1000:.1f} s)"
        m.recent = self._recent(time_ms, index)

    def _fill_action(self, m: MouseMoment, a: MouseAction, time_ms: int) -> None:
        button = BUTTON_NAMES.get(a.button or "", a.button or "")
        if a.kind in ("click", "double_click", "multi_click"):
            name = {1: "CLICK", 2: "DOUBLE-CLICK", 3: "TRIPLE-CLICK"}.get(
                a.clicks, f"MULTI-CLICK {a.clicks}"
            )
        else:
            name = ACTION_NAMES.get(a.kind, a.kind.upper())
        action = f"{name} {button}".strip()
        notes = []
        if a.held_ms:
            notes.append(f"held {a.held_ms / 1000:.1f} s")
        if a.target in TARGET_NOTES:
            notes.append(TARGET_NOTES[a.target])
        if a.unfinished:
            notes.append("still pressed at the end")
        m.action = action + (f" ({', '.join(notes)})" if notes else "")
        if a.until > a.t:
            m.action_time = (
                f"{format_timestamp(a.t)} - {format_timestamp(a.until)} ({a.duration_ms} ms)"
            )
        else:
            m.action_time = format_timestamp(a.t)
        m.modifiers = _modifiers(a.mods)
        m.injected = "yes" if a.injected else "no"
        m.start = self._where(a.at)
        if a.kind == "drag":
            m.now = self._drag_now(a, time_ms)
            end = a.end or a.at
            m.end = f"{self._where(end)} ({a.length_px} px)"
        elif a.kind in ("scroll", "zoom"):
            m.scroll = f"{a.direction} {a.notches:g} notches"
        elif a.kind == "gesture":
            what = f"{a.gesture} {a.direction or ''}".strip()
            if a.gesture == "pinch" and a.scale:
                what += f" x{a.scale:g}"
            m.scroll = f"{what}, 2 fingers (touchpad)"

    def _fill_element(self, m: MouseMoment, t: int, kind: str, el: ElementRecord, now: int) -> None:
        event = EVENT_NAMES.get(kind, kind)
        m.element_captured = f"{format_timestamp(t)} at {event} ({seconds_ago(now - t)})"
        if el.error:
            m.element_captured += f", not read: {el.error}"
            return
        m.element_role = el.role or NONE
        m.element_name = el.name or NONE
        parents = [
            f"{a.get('role')} {_quote(str(a.get('name')))}" if a.get("name") else str(a.get("role"))
            for a in el.ancestors[:3]
            if isinstance(a, dict)
        ]
        m.element_parents = " > ".join(parents) or NONE
        m.element_bounds = self._local_bounds(el.bounds)
        if el.password:
            m.content_label = "Content (password field, never read)"
            return
        text = self.log.content_of(el)
        if text is not None:
            kind_name = el.content_kind or "value"
            label = "selected" if kind_name == "selection" else kind_name
            chars = int((el.content or {}).get("chars") or len(text))
            cut = ", cut at 4096" if (el.content or {}).get("truncated") else ""
            m.content_label = f"Content ({label}, {chars} chars{cut})"
            m.content = text


def load_mouse_details(
    log: MouseLog | None,
    params: MouseSystemParams | None,
    duration_ms: int,
    screens: Iterable[Screen],
    frames_dir: Path,
    info_dir: Path,
) -> MouseDetails:
    """Build the details of a recording (reads every sidecar: run it in a worker)."""
    clipboard = read_clipboard_changes(FrameIndex(frames_dir, info_dir))
    return MouseDetails(log, params, duration_ms, screens, clipboard)


__all__ = [
    "BUTTONS",
    "NOW_MARK",
    "WHEEL_DIRECTIONS",
    "ClipboardChange",
    "MouseDetails",
    "MouseMoment",
    "load_mouse_details",
    "read_clipboard_changes",
]
