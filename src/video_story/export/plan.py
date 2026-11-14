"""Frame plan for an export: which frames cover ``[start_ms, end_ms]`` and for how long.

Pure functions without ffmpeg: they produce the text inputs of the ffmpeg run, the
concat demuxer list (``frames.txt``) and the ``overlay`` position expressions that move
the cursor pointer. The ffmpeg command itself is built in
:mod:`video_story.export.ffmpeg_export`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from video_story.core.config import EXPORT_FPS
from video_story.core.cursor import CursorPosition, cursor_from_json
from video_story.core.naming import FrameName

from . import ExportError

# Position used to hide the cursor overlay: far outside any screen image.
CURSOR_PARKED = (-1000, -1000)

# Time base given to every concat sub-file (``option framerate``): a single image read
# by image2 would otherwise get 25 fps / tbn 1/25 and the concat demuxer, which takes
# its time base from the first file, would round every ``duration`` start time to a
# multiple of 40 ms before the ``fps`` filter sees it. 1000 fps = millisecond pts.
CONCAT_FRAMERATE = 1000

# The ``overlay`` filter counts frames from 1 in ``n`` (ffmpeg 7.1: the first output
# frame evaluates with n=1), while the keyframes below use 0-based output frame indexes.
OVERLAY_FIRST_N = 1


@dataclass(frozen=True)
class PlannedFrame:
    """One source frame shown from ``start_ms`` for ``duration_ms`` (recording timeline)."""

    filename: str
    time_ms: int  # capture time of the source frame
    start_ms: int  # when it appears on the recording timeline (>= time_ms)
    duration_ms: int

    @property
    def sidecar(self) -> str:
        return f"{Path(self.filename).stem}.json"


CursorReader = Callable[[PlannedFrame], CursorPosition | None]


def plan_frames(names: list[FrameName], start_ms: int, end_ms: int) -> list[PlannedFrame]:
    """Pick the frames that cover ``[start_ms, end_ms)`` of one screen.

    ``names`` must be the sorted frames of one screen. The first planned frame is the
    one valid at ``start_ms`` (latest with ``time_ms <= start_ms``, or the first frame
    when none precede it); then every frame with ``start_ms < time_ms < end_ms`` follows.
    Each lasts until the next planned frame, the last until ``end_ms``.
    """
    if start_ms >= end_ms:
        raise ExportError(
            "INVALID_RANGE", f"start ({start_ms} ms) must be before end ({end_ms} ms)"
        )
    if not names:
        raise ExportError("NO_FRAMES", "the screen has no frames")
    first = names[0]
    for name in names:
        if name.time_ms <= start_ms:
            first = name
        else:
            break
    chosen: list[tuple[FrameName, int]] = [(first, start_ms)]
    chosen.extend((n, n.time_ms) for n in names if start_ms < n.time_ms < end_ms)
    planned: list[PlannedFrame] = []
    for index, (name, shown_at) in enumerate(chosen):
        next_at = chosen[index + 1][1] if index + 1 < len(chosen) else end_ms
        planned.append(PlannedFrame(name.filename, name.time_ms, shown_at, next_at - shown_at))
    return planned


def _concat_quote(path: Path) -> str:
    """Quote a path for the concat demuxer: single quotes, with ``'`` as ``'\\''``."""
    return "'" + str(path).replace("'", "'\\''") + "'"


def concat_list_text(frames_dir: Path, planned: list[PlannedFrame]) -> str:
    """``frames.txt`` for ``ffmpeg -f concat -safe 0`` (a slideshow with durations).

    The concat demuxer takes the ``duration`` of the last entry into account only when
    one more entry follows it, so the last file is listed once more without a duration.
    Every file gets ``option framerate 1000`` (see :data:`CONCAT_FRAMERATE`) so the
    start times are exact milliseconds and :func:`output_frame_index` holds.
    """
    if not planned:
        raise ExportError("NO_FRAMES", "nothing to export")
    base = frames_dir.resolve()
    option = f"option framerate {CONCAT_FRAMERATE}"
    lines = []
    for frame in planned:
        lines.append(f"file {_concat_quote(base / frame.filename)}")
        lines.append(option)
        lines.append(f"duration {frame.duration_ms / 1000:.3f}")
    lines.append(f"file {_concat_quote(base / planned[-1].filename)}")
    lines.append(option)
    return "\n".join(lines) + "\n"


def sidecar_cursor_reader(info_dir: Path) -> CursorReader:
    """Reader of the cursor stored in a frame's sidecar JSON (``None`` when absent)."""

    def read(frame: PlannedFrame) -> CursorPosition | None:
        try:
            data = json.loads((info_dir / frame.sidecar).read_text(encoding="utf-8"))
            return cursor_from_json(data.get("cursor"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    return read


@dataclass(frozen=True)
class CursorKeyframe:
    """Overlay position (top-left of the pointer image) from output frame ``frame`` on."""

    frame: int
    x: int
    y: int


def output_frame_index(offset_ms: int, fps: int = EXPORT_FPS) -> int:
    """0-based index of the output frame that shows time ``offset_ms`` of the clip (the
    same rounding as ffmpeg's ``fps`` filter with ``round=near`` on millisecond pts)."""
    return (max(0, offset_ms) * fps + 500) // 1000


def cursor_keyframes(
    planned: list[PlannedFrame],
    read_cursor: CursorReader,
    screen_id: str,
    hotspot: tuple[int, int] = (0, 0),
    fps: int = EXPORT_FPS,
) -> list[CursorKeyframe]:
    """Where the pointer image sits from each planned frame on (output frame indexes).

    The pointer is placed so that its hotspot is on the recorded cursor position, or
    parked at :data:`CURSOR_PARKED` (outside the picture) when the frame has no cursor
    or the cursor is on another screen. Consecutive identical positions are merged;
    the first keyframe always starts at frame 0.
    """
    if not planned:
        return []
    origin = planned[0].start_ms
    keyframes: list[CursorKeyframe] = []
    for frame in planned:
        cursor = read_cursor(frame)
        if cursor is not None and cursor.screen_id == screen_id:
            x, y = cursor.x - hotspot[0], cursor.y - hotspot[1]
        else:
            x, y = CURSOR_PARKED
        index = output_frame_index(frame.start_ms - origin, fps)
        if keyframes and keyframes[-1].frame == index:
            keyframes.pop()  # two planned frames on one output frame: the later wins
        if keyframes and (keyframes[-1].x, keyframes[-1].y) == (x, y):
            continue
        keyframes.append(CursorKeyframe(index, x, y))
    return keyframes


def _if_tree(keyframes: list[CursorKeyframe], axis: str) -> str:
    if len(keyframes) == 1:
        return str(getattr(keyframes[0], axis))
    mid = len(keyframes) // 2
    left, right = _if_tree(keyframes[:mid], axis), _if_tree(keyframes[mid:], axis)
    return f"if(lt(n,{keyframes[mid].frame + OVERLAY_FIRST_N}),{left},{right})"


def cursor_expressions(keyframes: list[CursorKeyframe]) -> tuple[str, str]:
    """``x`` and ``y`` expressions for the ``overlay`` filter (``eval=frame``).

    ffmpeg's ``overlay`` does not accept ``x``/``y`` as runtime commands (so ``sendcmd``
    cannot move it), but it re-evaluates its expressions for every frame with ``n``
    being the 1-based frame number (:data:`OVERLAY_FIRST_N`), so a keyframe at output
    frame ``F`` becomes the threshold ``lt(n,F+1)``. The keyframes form a balanced tree
    of ``if(lt(n,N),a,b)``, so a track of thousands of positions costs only ``log2(N)``
    comparisons per frame and stays far from the expression parser's recursion limits.
    """
    if not keyframes:
        keyframes = [CursorKeyframe(0, *CURSOR_PARKED)]
    return _if_tree(keyframes, "x"), _if_tree(keyframes, "y")


__all__ = [
    "CONCAT_FRAMERATE",
    "CURSOR_PARKED",
    "OVERLAY_FIRST_N",
    "CursorKeyframe",
    "CursorReader",
    "PlannedFrame",
    "concat_list_text",
    "cursor_expressions",
    "cursor_keyframes",
    "output_frame_index",
    "plan_frames",
    "sidecar_cursor_reader",
]
