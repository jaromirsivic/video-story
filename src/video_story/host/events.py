"""Text timeline of a recording: speech and screen events in one compact list.

Agents read this before any screenshot. It merges the transcript segments with what
the frame sidecars say happened on screen - shapes drawn, removed and restored, shapes
started but never finished, the white sketch canvas switched on and off, Ctrl held to
snap to the grid, text copied, the cursor moving to another
screen, zoomed screens - the keypoints of the manifest
and, when it was recorded, what the user did with the mouse in other applications
(:mod:`video_story.host.mouse_timeline`), all on the recording timeline (milliseconds
since the start). Every event
names the frame times worth fetching (``frames --at <ms>``), so the agent opens only
the few images the words and the geometry cannot answer.

Drawings, background, cursor, zoom and clipboard are the same in every sidecar of a
tick, so one sidecar per tick is read (the first screen that has a frame then).
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from typing import Any

from video_story.core.crop import Crop, read_crop
from video_story.core.instructions import read_instructions
from video_story.core.naming import frame_base
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, read_transcript, transcript_status
from video_story.host.mouse_timeline import LEGEND as MOUSE_LEGEND
from video_story.host.mouse_timeline import MOUSE_MODES, mouse_detail, mouse_events
from video_story.host.runner import FrameIndex, SessionError, archive_info

CLIPBOARD_EXCERPT_CHARS = 200
# Ctrl held this briefly without a snapped shape is a shortcut (Ctrl+Z, ...), not snapping.
GRID_MIN_MS = 1000

LEGEND = (
    "One event per line, times in ms since the recording start (use them with "
    "`frames --at <ms>`). SAY: speech with the cursor at its start and end. "
    "DRAW #id: a finished shape (pressed..released) with geometry in pixels of that "
    "screen's image and the frames before and after it; (on the sketch canvas): drawn "
    "on the white canvas, not on the screen. (square, Shift) / (circle, Shift): drawn "
    "holding Shift on purpose; (closed shape, Shift): a brush stroke whose end joins its "
    "start - usually the user circles an area meaning 'this' (Shift from <ms>: closed only "
    "then, in the middle of the stroke); (snapped to the N px grid, Ctrl): its points lie "
    "on the grid on purpose; (connected lines group g, "
    "segment n): lines drawn one after another with Shift, each starting where the "
    "previous one ended - one route or outline; (... split at a screen edge, group g): one "
    "line or arrow that crosses screens, stored as one piece per screen (the arrow's head "
    "is on the piece with tool arrow). Connected lines get one DRAW line per segment as "
    "soon as the click ends it, so they interleave with the speech. DRAWING: a shape "
    "pressed in the range that is not finished there (cut by the range end, or cancelled). "
    "REMOVED: shapes gone (Undo, Clear, the "
    "Eraser or the drawing layer closed). RESTORED: shapes back (Undo of an Eraser "
    "stroke). SKETCH on / off: every screen shows a plain white canvas instead of the "
    "screen - the user sketches an idea there. GRID on / off: Ctrl held while drawing, "
    "points snap to a grid (shown on the sketch canvas only). "
    "CLIPBOARD: text the user copied. CURSOR: the "
    "cursor moved to another screen. ZOOM: a screen shown magnified (coordinates are "
    "then in pixels of the zoomed image). KEYPOINT: a moment the user marked. "
    "Arrows: (double-headed, Shift) has a head at both ends, given as ends A B. "
    "PAUSE: the user paused the recording there; the paused time is cut out (nothing "
    "was recorded meanwhile, the timeline simply continues). MUTE / UNMUTE mic or "
    "computer: the user muted that audio track - it holds silence until UNMUTE, so no "
    "speech there is intended, not a fault."
)


def _duration_text(ms: int) -> str:
    """``42 s`` or ``3 min 05 s`` for a pause."""
    seconds = max(0, round(ms / 1000))
    if seconds < 60:
        return f"{seconds} s"
    return f"{seconds // 60} min {seconds % 60:02d} s"


def _pause_events(manifest: dict[str, Any], window: Crop, counts: dict[str, int]) -> list[_Event]:
    """``PAUSE`` lines for the pauses of the manifest inside ``window``."""
    events: list[_Event] = []
    for pause in manifest.get("pauses") or []:
        if not isinstance(pause, dict):
            continue
        at_ms, duration = pause.get("at_ms"), pause.get("duration_ms")
        if not isinstance(at_ms, int) or not window.start_ms <= at_ms <= window.end_ms:
            continue
        length = _duration_text(duration) if isinstance(duration, int) else "?"
        events.append(
            _Event(
                at_ms,
                6,
                f"{at_ms} PAUSE {length} (the user paused the recording here; the "
                f"{length} are cut out, nothing was recorded meanwhile)",
            )
        )
        counts["pauses"] = counts.get("pauses", 0) + 1
    counts.setdefault("pauses", 0)
    return events


MUTE_TRACKS = {"mic": "mic", "computer": "computer"}


def _mute_events(manifest: dict[str, Any], window: Crop, counts: dict[str, int]) -> list[_Event]:
    """``MUTE`` / ``UNMUTE`` lines for the muted intervals of the manifest in ``window``.

    An interval that began before the range shows as muted at the range start."""
    events: list[_Event] = []
    for mute in manifest.get("mutes") or []:
        if not isinstance(mute, dict) or mute.get("track") not in MUTE_TRACKS:
            continue
        track = MUTE_TRACKS[str(mute["track"])]
        start, end = mute.get("from_ms"), mute.get("to_ms")
        if not isinstance(start, int):
            continue
        if not isinstance(end, int):
            end = window.end_ms
        if end < window.start_ms or start > window.end_ms:
            continue
        if start < window.start_ms:
            text = f"{window.start_ms} MUTE {track} (muted since {start})"
            events.append(_Event(window.start_ms, 6, text))
        elif start == 0:
            events.append(_Event(0, 6, f"0 MUTE {track} (muted from the start)"))
        else:
            events.append(_Event(start, 6, f"{start} MUTE {track}"))
        if end <= window.end_ms and end < int(manifest.get("recorded_duration_ms") or 0):
            events.append(_Event(end, 6, f"{end} UNMUTE {track}"))
        counts["mutes"] = counts.get("mutes", 0) + 1
    counts.setdefault("mutes", 0)
    return events


@dataclass
class _Shape:
    label: int
    data: dict[str, Any]  # the latest geometry
    first_seen_ms: int  # first tick showing it
    last_change_ms: int  # tick of the latest geometry (old recordings grew while dragged)

    @property
    def exact(self) -> bool:
        return isinstance(self.data.get("finished_ms"), int)


@dataclass
class _Event:
    time_ms: int
    order: int  # tie breaker: speech before the screen events of the same moment
    text: str


@dataclass
class _Ticks:
    """Sidecars of the ticks around a range, read once (one screen per tick)."""

    all_times: list[int]
    times: list[int] = field(default_factory=list)
    data: list[dict[str, Any]] = field(default_factory=list)

    def at(self, time_ms: int) -> dict[str, Any] | None:
        """Sidecar of the latest loaded tick at or before ``time_ms`` (else the first)."""
        if not self.times:
            return None
        pos = bisect.bisect_right(self.times, time_ms) - 1
        return self.data[max(pos, 0)]

    def before(self, time_ms: int) -> int | None:
        """Time of the last tick strictly before ``time_ms`` (any tick of the recording)."""
        pos = bisect.bisect_left(self.all_times, time_ms) - 1
        return self.all_times[pos] if pos >= 0 else None

    def after(self, time_ms: int) -> int | None:
        """Time of the first tick strictly after ``time_ms`` (any tick of the recording)."""
        pos = bisect.bisect_right(self.all_times, time_ms)
        return self.all_times[pos] if pos < len(self.all_times) else None

    def frames_around(self, start_ms: int, end_ms: int) -> str:
        """``frames before X, after Y`` for an action from ``start_ms`` to ``end_ms``."""
        before, after = self.before(start_ms), self.after(end_ms)
        parts = ([f"before {before}"] if before is not None else []) + (
            [f"after {after}"] if after is not None else []
        )
        return "frames " + ", ".join(parts) if parts else "no frames around"


def _load_ticks(index: FrameIndex, start_ms: int, end_ms: int) -> _Ticks:
    if not index.screens:
        raise SessionError("NO_FRAMES", f"no frames in {index.info_dir}")
    owner: dict[int, str] = {}
    for screen_id in reversed(index.screens):  # the first screen wins
        for name in index.by_screen[screen_id]:
            owner[name.time_ms] = screen_id
    all_times = sorted(owner)
    ticks = _Ticks(all_times=all_times)
    # From the last tick at or before the start (the state the range opens with) to
    # the first tick after the end (where a shape released just before the end shows).
    first = max(bisect.bisect_right(all_times, start_ms) - 1, 0)
    last = min(bisect.bisect_right(all_times, end_ms), len(all_times) - 1)
    for time_ms in all_times[first : last + 1]:
        sidecar = index.info_dir / f"{frame_base(time_ms, owner[time_ms])}.json"
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            ticks.times.append(time_ms)
            ticks.data.append(data)
    return ticks


# Formatting -------------------------------------------------------------------


def _point(p: Any) -> str:
    if isinstance(p, dict):
        return f"{p.get('x')},{p.get('y')}"
    return "?"


def _cursor(sidecar: dict[str, Any] | None) -> str:
    cursor = sidecar.get("cursor") if sidecar else None
    if not isinstance(cursor, dict):
        return "?"
    return f"{cursor.get('screen')} {_point(cursor)}"


def _geometry(shape: dict[str, Any]) -> str:
    tool = shape.get("tool")
    if tool == "brush":
        path = shape.get("path") if isinstance(shape.get("path"), list) else []
        xs = [p.get("x", 0) for p in path if isinstance(p, dict)]
        ys = [p.get("y", 0) for p in path if isinstance(p, dict)]
        if not xs:
            return "empty path"
        return (
            f"bbox {min(xs)},{min(ys)} {max(xs) - min(xs)}x{max(ys) - min(ys)} "
            f"({len(xs)} points, path in the sidecar)"
        )
    if tool == "arrow":
        heads = shape.get("heads")
        if heads == "both":
            return f"ends {_point(shape.get('from'))} {_point(shape.get('to'))}"
        if heads == "start":  # the first piece of a split double-headed arrow
            return f"head {_point(shape.get('from'))} to {_point(shape.get('to'))}"
        return f"tail {_point(shape.get('from'))} head {_point(shape.get('to'))}"
    if tool == "line":
        return f"from {_point(shape.get('from'))} to {_point(shape.get('to'))}"
    # rectangle, ellipse (its bounding box)
    return f"corners {_point(shape.get('from'))} {_point(shape.get('to'))}"


def _shape_note(shape: dict[str, Any]) -> str:
    """What Shift, Ctrl and splitting at screen edges made of a shape ("" for a plain one)."""
    tool = shape.get("tool")
    notes: list[str] = []
    if shape.get("shift"):
        kind = {
            "rectangle": "square",
            "ellipse": "circle",
            "brush": "closed shape",
            "arrow": "double-headed",
        }.get(str(tool))
        if kind == "closed shape" and isinstance(shape.get("closed_ms"), int):
            notes.append(f"closed shape, Shift from {shape['closed_ms']}")
        elif kind:
            notes.append(f"{kind}, Shift")
    grid = shape.get("grid")
    if isinstance(grid, int):
        notes.append(f"snapped to the {grid} px grid, Ctrl")
    group, segment = shape.get("group"), shape.get("segment")
    if isinstance(group, int):
        if isinstance(segment, int):
            notes.append(f"connected lines group {group}, segment {segment}")
        elif shape.get("part_of") == "arrow":
            notes.append(f"shaft of an arrow split at a screen edge, group {group}")
        elif tool == "arrow":
            part = "start head part" if shape.get("heads") == "start" else "head part"
            notes.append(f"{part} of an arrow split at a screen edge, group {group}")
        else:
            notes.append(f"part of a line split at a screen edge, group {group}")
    return "".join(f" ({note})" for note in notes)


def _zoom_text(entry: dict[str, Any]) -> str:
    return (
        f"{entry.get('screen')} {entry.get('factor')}x region "
        f"{entry.get('x')},{entry.get('y')} {entry.get('width')}x{entry.get('height')}"
    )


def _excerpt(text: str) -> str:
    flat = json.dumps(text, ensure_ascii=False)
    if len(text) <= CLIPBOARD_EXCERPT_CHARS:
        return flat
    cut = json.dumps(text[:CLIPBOARD_EXCERPT_CHARS], ensure_ascii=False)
    return f'{cut[:-1]}..." ({len(text)} chars)'


# Events -----------------------------------------------------------------------


def _speech_events(
    segments: list[Segment], crop: Crop, ticks: _Ticks, over=lambda _s, _e: None
) -> list[_Event]:
    events = []
    for seg in segments:
        start = max(seg.start_ms, crop.start_ms)
        end = min(seg.end_ms, crop.end_ms)
        first, last = _cursor(ticks.at(start)), _cursor(ticks.at(end))
        cursor = first if first == last else f"{first} -> {last}"
        rested = over(start, end)
        if rested:
            cursor += f" {rested}"
        events.append(
            _Event(
                seg.start_ms,
                0,
                f"{seg.start_ms}-{seg.end_ms} SAY {seg.text.strip()} [cursor {cursor}]",
            )
        )
    return events


def _shape_key(shape: dict[str, Any]) -> tuple[str, Any]:
    shape_id = shape.get("id")
    if isinstance(shape_id, int) and shape_id > 0:
        return ("id", shape_id)
    # Recordings made before shapes had ids: the shape itself is its identity.
    return ("sig", json.dumps(shape, sort_keys=True))


def _anchor(shape: dict[str, Any]) -> Any:
    path = shape.get("path")
    if isinstance(path, list):
        return path[0] if path else None
    return shape.get("from")


def _same_stroke(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """``new`` is ``old`` dragged further (recordings from before shapes had ids)."""
    return all(old.get(k) == new.get(k) for k in ("tool", "color", "screen")) and (
        _anchor(old) == _anchor(new)
    )


_Changes = list[tuple[int, int, list[int]]]  # (time, previous tick, labels)


def _track_shapes(ticks: _Ticks) -> tuple[list[_Shape], _Changes, _Changes]:
    """Every shape seen in the loaded ticks, the removals and the restorations.

    Removals and restorations are ``(time, previous tick, labels)``. Shapes with an
    ``id`` are exact; one that comes back with the same id after it was gone was
    restored (Undo of an eraser stroke). Older recordings stored a shape while it was
    being dragged: a shape that replaces one of the same tool, color, screen and
    starting point within one tick is the same shape growing, not a new one.
    """
    shapes: list[_Shape] = []
    known: dict[tuple[str, Any], _Shape] = {}
    present: dict[tuple[str, Any], _Shape] = {}
    removals: _Changes = []
    restorations: _Changes = []
    next_label = 0
    previous_ms: int | None = None
    for time_ms, sidecar in zip(ticks.times, ticks.data, strict=True):
        current = {_shape_key(s): s for s in sidecar.get("drawings") or [] if isinstance(s, dict)}
        gone = [k for k in present if k not in current]
        continued: set[tuple[str, Any]] = set()
        now: dict[tuple[str, Any], _Shape] = {}
        restored: list[int] = []
        for key, shape in current.items():
            record = present.get(key) or (known.get(key) if key[0] == "id" else None)
            if record is not None and key not in present:
                restored.append(record.label)
            if record is None and key[0] == "sig":
                for old in gone:
                    if old not in continued and _same_stroke(present[old].data, shape):
                        record = present[old]
                        continued.add(old)
                        record.data = shape
                        record.last_change_ms = time_ms
                        break
            if record is None:
                if key[0] == "id":
                    label = key[1]
                    next_label = max(next_label, label)
                else:
                    next_label += 1
                    label = next_label
                record = _Shape(label, shape, time_ms, time_ms)
                shapes.append(record)
            known[key] = record
            now[key] = record
        removed = [present[k].label for k in gone if k not in continued]
        if removed and previous_ms is not None:
            removals.append((time_ms, previous_ms, removed))
        if restored and previous_ms is not None:
            restorations.append((time_ms, previous_ms, sorted(restored)))
        present = now
        previous_ms = time_ms
    return shapes, removals, restorations


def _shape_events(ticks: _Ticks, window: Crop) -> tuple[list[_Event], dict[str, int]]:
    shapes, removals, restorations = _track_shapes(ticks)
    events: list[_Event] = []
    count = 0
    for shape in shapes:
        data = shape.data
        if shape.exact:
            released = data["finished_ms"]
            pressed = data.get("started_ms")
            pressed = pressed if isinstance(pressed, int) else released
            times = f"pressed {pressed}, released {released}"
            after = shape.first_seen_ms
        else:
            pressed = shape.first_seen_ms
            released = shape.last_change_ms
            times = f"seen {pressed}" if pressed == released else f"seen {pressed} to {released}"
            after = released
        if released > window.end_ms:
            continue
        before = ticks.before(pressed)
        frames = f"before {before}, after {after}" if before is not None else f"after {after}"
        state = ticks.at(after) or {}
        zoomed = {z.get("screen") for z in state.get("zoom") or [] if isinstance(z, dict)}
        zoom_note = " (zoomed view)" if data.get("screen") in zoomed else ""
        if data.get("background") == "sketch":
            zoom_note += " (on the sketch canvas)"
        early = " (drawn before the range)" if released < window.start_ms else ""
        events.append(
            _Event(
                released,
                1,
                f"{released} DRAW #{shape.label} {data.get('tool')} {data.get('color')} "
                f"{data.get('screen')} {_geometry(data)}{_shape_note(data)}{zoom_note} "
                f"({times}; frames {frames}){early}",
            )
        )
        count += 1
    removed_count = 0
    for time_ms, last_seen, labels in removals:
        if window.start_ms <= time_ms <= window.end_ms:
            ids = " ".join(f"#{label}" for label in labels)
            events.append(
                _Event(
                    time_ms,
                    2,
                    f"{time_ms} REMOVED {ids} (last seen {last_seen}: Undo, Clear, Eraser "
                    "or the drawing layer closed)",
                )
            )
            removed_count += len(labels)
    restored_count = 0
    for time_ms, last_seen, labels in restorations:
        if window.start_ms <= time_ms <= window.end_ms:
            ids = " ".join(f"#{label}" for label in labels)
            events.append(
                _Event(
                    time_ms,
                    2,
                    f"{time_ms} RESTORED {ids} (gone at {last_seen}, back by Undo of an "
                    "Eraser stroke; same shapes as drawn)",
                )
            )
            restored_count += len(labels)
    unfinished = _unfinished_events(ticks, window, shapes)
    events.extend(unfinished)
    return events, {
        "shapes": count,
        "removed": removed_count,
        "restored": restored_count,
        "unfinished": len(unfinished),
    }


def _unfinished_events(ticks: _Ticks, window: Crop, shapes: list[_Shape]) -> list[_Event]:
    """``DRAWING`` lines: shapes the sidecars show being drawn (``drawing_now``) in the
    range that were never finished there - cut by the end of the range, or cancelled
    (Esc, the drawing layer closed, a click without size)."""
    finished: dict[int, int] = {}  # started_ms -> finished_ms of the finished shapes
    for shape in shapes:
        started, done = shape.data.get("started_ms"), shape.data.get("finished_ms")
        if isinstance(started, int) and isinstance(done, int):
            finished[started] = min(done, finished.get(started, done))
    events: list[_Event] = []
    current: dict[str, Any] | None = None
    seen_in_range = False

    def close(at: int | None) -> None:
        if current is None or not seen_in_range:
            return
        started = current["started_ms"]
        what = (
            f"DRAWING {current.get('tool')} {current.get('color')} {current.get('screen')} "
            f"(pressed {started}"
        )
        done = finished.get(started)
        if done is not None and done <= window.end_ms:
            return  # finished: its DRAW line says everything
        if done is not None:
            text = f"{what}, released {done} after the range end)"
            events.append(_Event(window.end_ms, 1, f"{window.end_ms} {text}"))
        elif at is None or at > window.end_ms:
            text = f"{what}, not finished at the range end)"
            events.append(_Event(window.end_ms, 1, f"{window.end_ms} {text}"))
        else:
            text = (
                f"{what}, never finished: cancelled with Esc, the drawing layer closed, "
                "or a click without size)"
            )
            events.append(_Event(at, 1, f"{at} {text}"))

    for time_ms, sidecar in zip(ticks.times, ticks.data, strict=True):
        now = sidecar.get("drawing_now")
        started = now.get("started_ms") if isinstance(now, dict) else None
        if not isinstance(started, int):
            now = None
        if current is not None and (now is None or now["started_ms"] != current["started_ms"]):
            close(time_ms)
            current = None
        if now is not None and current is None:
            current, seen_in_range = now, False
        if current is not None and window.start_ms <= time_ms <= window.end_ms:
            seen_in_range = True
    close(None)
    return events


def _grid_events(ticks: _Ticks, window: Crop) -> list[_Event]:
    """``GRID on`` / ``GRID off``: Ctrl held while drawing (sidecar ``grid``, the spacing
    in pixels of that screen's image). A short Ctrl (under ``GRID_MIN_MS``) without a
    shape snapped meanwhile is a keyboard shortcut and left out."""
    snapped: list[int] = sorted(
        {
            shape["finished_ms"]
            for sidecar in ticks.data
            for shape in sidecar.get("drawings") or []
            if isinstance(shape, dict)
            and isinstance(shape.get("grid"), int)
            and isinstance(shape.get("finished_ms"), int)
        }
    )
    intervals: list[tuple[int, int | None, int, str]] = []  # (on, off, px, screen)
    on: tuple[int, int, str] | None = None
    for time_ms, sidecar in zip(ticks.times, ticks.data, strict=True):
        grid = sidecar.get("grid")
        if isinstance(grid, int) and on is None:
            on = (time_ms, grid, str(sidecar.get("screen")))
        elif not isinstance(grid, int) and on is not None:
            intervals.append((on[0], time_ms, on[1], on[2]))
            on = None
    if on is not None:
        intervals.append((on[0], None, on[1], on[2]))
    events: list[_Event] = []
    for start, end, px, screen in intervals:
        stop = end if end is not None else ticks.times[-1]
        used = any(start <= t <= stop for t in snapped)
        if end is not None and end - start < GRID_MIN_MS and not used:
            continue
        at = max(start, window.start_ms)
        if at <= window.end_ms and (end is None or end > window.start_ms):
            early = " (held before the range)" if start < window.start_ms else ""
            events.append(
                _Event(
                    at,
                    1,
                    f"{at} GRID on: Ctrl held, points snap to a {px} px grid (pixels of the "
                    f"{screen} image; the grid shows on the sketch canvas only){early}",
                )
            )
        if end is not None and window.start_ms <= end <= window.end_ms:
            events.append(_Event(end, 1, f"{end} GRID off (Ctrl released; snapping since {start})"))
    return events


def _screen_events(ticks: _Ticks, window: Crop) -> tuple[list[_Event], dict[str, int]]:
    """Shapes, sketch canvas, clipboard, cursor screen changes and zoom in ``window``."""
    events, counts = _shape_events(ticks, window)
    counts.update({"clipboard": 0, "cursor_moves": 0, "zoom": 0, "sketch": 0})
    grid_events = _grid_events(ticks, window)
    events.extend(grid_events)
    counts["grid"] = sum(1 for e in grid_events if " GRID on" in e.text)
    sketching = False
    sketch_since: int | None = None
    cursor_screen: str | None = None
    zoomed: dict[str, dict[str, Any]] = {}
    zoom_since: dict[str, int] = {}
    opening = True  # the first loaded tick is the state the range starts with

    def in_range(time_ms: int) -> bool:
        return window.start_ms <= time_ms <= window.end_ms

    for time_ms, sidecar in zip(ticks.times, ticks.data, strict=True):
        background = sidecar.get("background")
        if background == "sketch" and not sketching:
            at = max(time_ms, window.start_ms)
            if at <= window.end_ms:
                early = " (switched on before the range)" if opening and time_ms < at else ""
                events.append(
                    _Event(
                        at,
                        1,
                        f"{at} SKETCH on: every screen shows a plain white canvas, the user "
                        f"sketches on it instead of drawing on the screen{early}",
                    )
                )
                counts["sketch"] += 1
            sketching, sketch_since = True, time_ms
        elif background != "sketch" and sketching:
            if in_range(time_ms):
                how = "the screens again" if background == "screen" else "drawing layer closed"
                events.append(
                    _Event(
                        time_ms,
                        1,
                        f"{time_ms} SKETCH off ({how}; sketching since {sketch_since})",
                    )
                )
            sketching = False
        clip = sidecar.get("clipboard")
        if isinstance(clip, str) and not opening and in_range(time_ms):
            events.append(_Event(time_ms, 3, f"{time_ms} CLIPBOARD {_excerpt(clip)}"))
            counts["clipboard"] += 1
        cursor = sidecar.get("cursor") if isinstance(sidecar.get("cursor"), dict) else None
        screen = cursor.get("screen") if cursor else None
        if screen is not None and screen != cursor_screen:
            if opening:
                start = window.start_ms
                events.append(_Event(start, -1, f"{start} START cursor {_cursor(sidecar)}"))
            elif in_range(time_ms):
                events.append(_Event(time_ms, 4, f"{time_ms} CURSOR -> {_cursor(sidecar)}"))
                counts["cursor_moves"] += 1
            cursor_screen = screen
        zoom_now = {
            str(z.get("screen")): z for z in sidecar.get("zoom") or [] if isinstance(z, dict)
        }
        for sid, entry in zoom_now.items():
            if sid not in zoomed:
                at = max(time_ms, window.start_ms)
                if at <= window.end_ms:
                    events.append(_Event(at, 5, f"{at} ZOOM {_zoom_text(entry)}"))
                    counts["zoom"] += 1
                zoom_since[sid] = time_ms
        for sid, entry in zoomed.items():
            if sid not in zoom_now and in_range(time_ms):
                events.append(
                    _Event(
                        time_ms,
                        5,
                        f"{time_ms} ZOOM END {sid} (zoomed since {zoom_since.get(sid)}; "
                        f"last {_zoom_text(entry)})",
                    )
                )
        zoomed = zoom_now
        opening = False
    return events, counts


def build_timeline(
    paths: SessionPaths,
    start_ms: int | None = None,
    end_ms: int | None = None,
    mouse: str = "auto",
) -> dict[str, Any]:
    """Merged text timeline of a processed recording.

    The range defaults to the user's crop (the whole recording when uncropped);
    ``start_ms`` / ``end_ms`` override either end. ``mouse``: ``auto`` (every action, or
    scrolls merged when there are very many), ``full``, ``summary`` or ``off``.
    """
    if mouse not in MOUSE_MODES:
        raise SessionError("USAGE", f"unknown mouse mode {mouse!r}; use {', '.join(MOUSE_MODES)}")
    base = paths.current_dir
    index = FrameIndex.of(paths, base)
    if not index.screens:
        raise SessionError("NO_FRAMES", f"no frames in {base}")
    manifest: dict[str, Any] = {}
    manifest_path = paths.manifest_path(base)
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
    duration_ms = int(manifest.get("recorded_duration_ms") or 0)
    crop = read_crop(paths.crop_path(base), duration_ms)
    window = Crop(
        start_ms=crop.start_ms if start_ms is None else max(0, start_ms),
        end_ms=crop.end_ms if end_ms is None else end_ms,
    )
    if window.end_ms < window.start_ms:
        raise SessionError("USAGE", f"end_ms {window.end_ms} is before start_ms {window.start_ms}")

    ticks = _load_ticks(index, window.start_ms, window.end_ms)
    events, counts = _screen_events(ticks, window)
    pointer = mouse_events(paths, manifest, window, ticks.frames_around, mouse)
    for line in pointer.lines:
        events.append(_Event(line.time_ms, 3, line.text))
    counts.update(pointer.counts)

    status = transcript_status(paths.transcript_json_path(base))
    segments: list[Segment] = []
    language = None
    if status.exists:
        try:
            transcript = read_transcript(paths.transcript_json_path(base))
        except (OSError, ValueError, KeyError):
            transcript = None
        if transcript is not None:
            language = transcript.language
            segments = transcript.segments_in_range(window.start_ms, window.end_ms)
    events.extend(_speech_events(segments, window, ticks, pointer.over))

    keypoints = [k for k in manifest.get("keypoints") or [] if isinstance(k, dict)]
    for kp in keypoints:
        time_ms = kp.get("time_ms")
        if isinstance(time_ms, int) and window.start_ms <= time_ms <= window.end_ms:
            events.append(_Event(time_ms, 6, f"{time_ms} KEYPOINT {kp.get('index')}"))
            counts["keypoints"] = counts.get("keypoints", 0) + 1
    counts.setdefault("keypoints", 0)
    events.extend(_pause_events(manifest, window, counts))
    events.extend(_mute_events(manifest, window, counts))
    counts["speech"] = len(segments)

    events.sort(key=lambda e: (e.time_ms, e.order))
    screens = [
        {
            "id": s.get("id"),
            "primary": bool(s.get("primary")),
            **(s.get("image_size") or {}),
        }
        for s in manifest.get("screens") or []
        if isinstance(s, dict)
    ]
    result: dict[str, Any] = {
        "session_id": paths.session_id,
        "range": window.to_dict(),
        "crop": crop.to_dict(),
        "duration_ms": duration_ms,
        "instructions": read_instructions(paths.instructions_path(base)),
        "language": language,
        "transcript_complete": status.complete,
        "screens": screens,
        "capture_interval_ms": manifest.get("capture_interval_ms"),
        "counts": counts,
        "legend": LEGEND + (" " + MOUSE_LEGEND if pointer.recorded else ""),
        "mouse": {"recorded": pointer.recorded, "mode": pointer.mode},
        "timeline": "\n".join(e.text for e in events) + ("\n" if events else ""),
    }
    if pointer.note:
        result["mouse_note"] = pointer.note
    if not status.complete:
        result["transcript_note"] = (
            f"The transcript is incomplete (segments reach {status.transcribed_ms} ms); "
            "run `video-story transcribe` first."
        )
    archive = archive_info(paths, base)
    if archive is not None:
        result["archive"] = archive
    return result


def build_mouse_detail(
    paths: SessionPaths,
    start_ms: int | None = None,
    end_ms: int | None = None,
    at_ms: int | None = None,
    max_path_points: int | None = None,
) -> dict[str, Any]:
    """Everything recorded about the mouse actions of the cropped range (or
    ``start_ms``..``end_ms``), or of the one action at ``at_ms``. ``max_path_points``
    thins the path of a long drag (output that is not written to a file)."""
    base = paths.current_dir
    manifest_path = paths.manifest_path(base)
    if not manifest_path.exists():
        raise SessionError("NOT_FOUND", f"no manifest.json in {base}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionError("NOT_FOUND", f"cannot read {manifest_path}: {exc}") from exc
    duration_ms = int(manifest.get("recorded_duration_ms") or 0)
    crop = read_crop(paths.crop_path(base), duration_ms)
    window = Crop(
        start_ms=crop.start_ms if start_ms is None else max(0, start_ms),
        end_ms=crop.end_ms if end_ms is None else end_ms,
    )
    if window.end_ms < window.start_ms:
        raise SessionError("USAGE", f"end_ms {window.end_ms} is before start_ms {window.start_ms}")
    detail = mouse_detail(paths, manifest, window, at_ms, max_path_points)
    return {"session_id": paths.session_id, **detail}


def mouse_records(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """The actions and rest points of :func:`build_mouse_detail` as one list in time
    order (a rest point is marked ``kind: "rest"``; an action comes first at the same
    time). One record is one line of the ``mouse`` file and one unit of an MCP page."""
    records = [(a.get("t", 0), 0, a) for a in detail.get("actions") or []]
    records += [(r.get("t", 0), 1, {"kind": "rest", **r}) for r in detail.get("rest_points") or []]
    records.sort(key=lambda item: item[:2])  # stable: the order within a kind stays
    return [record for _t, _rank, record in records]


def mouse_record_lines(records: list[dict[str, Any]]) -> list[str]:
    """``records`` as JSON Lines (``"t": <ms>`` is searchable as written)."""
    return [json.dumps(record, ensure_ascii=False, default=str) for record in records]


__all__ = [
    "CLIPBOARD_EXCERPT_CHARS",
    "LEGEND",
    "build_mouse_detail",
    "build_timeline",
    "mouse_record_lines",
    "mouse_records",
]
