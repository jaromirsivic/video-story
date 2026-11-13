"""Tests for the text timeline (host.events): speech and screen events from sidecars."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from video_story.core.crop import Crop, write_crop
from video_story.core.instructions import write_instructions
from video_story.core.manifest import Manifest, write_manifest
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript, write_transcript
from video_story.host.events import CLIPBOARD_EXCERPT_CHARS, build_timeline
from video_story.host.runner import SessionError, frames_at

INTERVAL = 500


def rect(shape_id: int | None, start: int | None, end: int | None, x2: int = 40) -> dict:
    shape: dict[str, Any] = {
        "tool": "rectangle",
        "color": "#FF0000",
        "screen": "screen1",
        "stroke_width": 4,
        "from": {"screen": "screen1", "x": 10, "y": 20},
        "to": {"screen": "screen1", "x": x2, "y": 60},
    }
    if shape_id is not None:
        shape.update(id=shape_id, started_ms=start, finished_ms=end)
    return shape


def make_recording(
    root: Path,
    ticks: dict[int, dict[str, Any]],
    duration_ms: int = 6000,
    segments: list[Segment] | None = None,
    keypoints: list[int] | None = None,
) -> SessionPaths:
    """A processed recording with two screens; ``ticks`` maps a time to sidecar extras."""
    paths = SessionPaths(root, "20260101-120000")
    paths.create_work_dirs()
    frames = paths.frames_dir(paths.work_dir)
    info = paths.frames_info_dir(paths.work_dir)
    cursor = {"screen": "screen1", "x": 1, "y": 2}
    for t in range(0, duration_ms + 1, INTERVAL):
        extra = ticks.get(t, {})
        cursor = extra.get("cursor", cursor)
        for sid in ("screen1", "screen2"):
            sidecar = {
                "time_ms": t,
                "screen": sid,
                "cursor": cursor,
                "drawings": extra.get("drawings", []),
                "drawing_now": extra.get("drawing_now"),
                "zoom": extra.get("zoom", []),
                "background": extra.get("background"),
                "grid": extra.get("grid"),
                "clipboard": extra.get("clipboard"),
            }
            (frames / frame_filename(t, sid, "png")).write_bytes(b"\x89PNG")
            (info / sidecar_filename(t, sid)).write_text(json.dumps(sidecar), encoding="utf-8")
    write_manifest(
        paths.manifest_path(paths.work_dir),
        Manifest(
            session_id=paths.session_id,
            started_at="x",
            finished_at="y",
            recorded_duration_ms=duration_ms,
            capture_interval_ms=INTERVAL,
            image_format="png",
            jpeg_quality=None,
            screens=assign_screen_ids(
                [RawScreen(0, 0, 100, 100, primary=True), RawScreen(100, 0, 50, 50)]
            ),
            keypoints=keypoints or [],
        ),
    )
    write_transcript(
        paths.transcript_txt_path(paths.work_dir),
        paths.transcript_json_path(paths.work_dir),
        Transcript(language="cs", segments=segments or []),
    )
    paths.finalize()
    return paths


def lines(result: dict[str, Any]) -> list[str]:
    return result["timeline"].splitlines()


def test_timeline_merges_speech_shapes_clipboard_cursor_and_keypoints(tmp_path: Path):
    drawn = rect(1, 1200, 1400)
    ticks = {
        1500: {"drawings": [drawn]},
        2000: {"drawings": [drawn], "clipboard": "copied text"},
        2500: {"drawings": [], "cursor": {"screen": "screen2", "x": 5, "y": 6}},
    }
    segments = [
        Segment(0, 900, "Look here."),
        Segment(1000, 2600, "Draw around this button."),
    ]
    paths = make_recording(tmp_path, ticks, segments=segments, keypoints=[1800])
    write_instructions(paths.instructions_path(paths.current_dir), "Answer in Czech.")

    result = build_timeline(paths)

    assert result["instructions"] == "Answer in Czech."
    assert result["range"] == {"start_ms": 0, "end_ms": 6000}
    assert result["counts"] == {
        "shapes": 1,
        "removed": 1,
        "restored": 0,
        "unfinished": 0,
        "clipboard": 1,
        "cursor_moves": 1,
        "zoom": 0,
        "sketch": 0,
        "grid": 0,
        "keypoints": 1,
        "pauses": 0,
        "mutes": 0,
        "speech": 2,
    }
    assert lines(result) == [
        "0 START cursor screen1 1,2",
        "0-900 SAY Look here. [cursor screen1 1,2]",
        "1000-2600 SAY Draw around this button. [cursor screen1 1,2 -> screen2 5,6]",
        "1400 DRAW #1 rectangle #FF0000 screen1 corners 10,20 40,60 "
        "(pressed 1200, released 1400; frames before 1000, after 1500)",
        "1800 KEYPOINT 1",
        '2000 CLIPBOARD "copied text"',
        "2500 REMOVED #1 (last seen 2000: Undo, Clear, Eraser or the drawing layer closed)",
        "2500 CURSOR -> screen2 5,6",
    ]


def test_timeline_follows_the_crop_and_explicit_ranges(tmp_path: Path):
    early = rect(1, 200, 400)
    late = rect(2, 4200, 4300, x2=90)
    ticks = {t: {"drawings": [early]} for t in range(500, 3000, INTERVAL)}
    ticks.update({t: {"drawings": [late]} for t in range(4500, 6001, INTERVAL)})
    segments = [Segment(100, 900, "earlier"), Segment(2100, 2900, "inside")]
    paths = make_recording(tmp_path, ticks, segments=segments)
    write_crop(paths.crop_path(paths.current_dir), Crop(2000, 4000))

    cropped = build_timeline(paths)
    assert cropped["range"] == {"start_ms": 2000, "end_ms": 4000}
    text = cropped["timeline"]
    assert "SAY earlier" not in text and "2100-2900 SAY inside" in text
    # Still on screen when the range opens: listed, marked as drawn before the range.
    assert "400 DRAW #1" in text and "(drawn before the range)" in text
    assert "3000 REMOVED #1" in text
    assert "DRAW #2" not in text  # released after the end of the range

    whole = build_timeline(paths, 0, 10**12)
    assert "4300 DRAW #2 rectangle #FF0000 screen1 corners 10,20 90,60" in whole["timeline"]
    assert "100-900 SAY earlier" in whole["timeline"]

    with pytest.raises(SessionError):
        build_timeline(paths, 3000, 1000)


def test_old_recordings_merge_a_shape_growing_while_dragged(tmp_path: Path):
    # Before shapes had ids, a rectangle was stored at every tick while dragged.
    ticks = {
        1000: {"drawings": [rect(None, None, None, x2=20)]},
        1500: {"drawings": [rect(None, None, None, x2=30)]},
        2000: {"drawings": [rect(None, None, None, x2=50)]},
        2500: {"drawings": [rect(None, None, None, x2=50)]},
        3000: {"drawings": []},
    }
    result = build_timeline(make_recording(tmp_path, ticks))
    draws = [line for line in lines(result) if " DRAW " in line]
    assert draws == [
        "2000 DRAW #1 rectangle #FF0000 screen1 corners 10,20 50,60 "
        "(seen 1000 to 2000; frames before 500, after 2000)"
    ]
    assert "3000 REMOVED #1" in result["timeline"]
    assert result["counts"]["removed"] == 1


def test_brush_zoom_and_long_clipboard(tmp_path: Path):
    brush = {
        "id": 1,
        "tool": "brush",
        "color": "#00FF00",
        "screen": "screen2",
        "started_ms": 600,
        "finished_ms": 900,
        "path": [{"screen": "screen2", "x": x, "y": y} for x, y in ((5, 9), (25, 4), (15, 30))],
    }
    zoom = {"screen": "screen2", "factor": 2.0, "x": 10, "y": 10, "width": 20, "height": 20}
    long_text = "x" * (CLIPBOARD_EXCERPT_CHARS + 50)
    ticks = {
        500: {"zoom": [zoom]},
        1000: {"zoom": [zoom], "drawings": [brush]},
        1500: {"drawings": [brush], "clipboard": long_text},
    }
    text = build_timeline(make_recording(tmp_path, ticks))["timeline"]
    assert "500 ZOOM screen2 2.0x region 10,10 20x20" in text
    assert (
        "900 DRAW #1 brush #00FF00 screen2 bbox 5,4 20x26 (3 points, path in the sidecar) "
        "(zoomed view) (pressed 600, released 900; frames before 500, after 1000)"
    ) in text
    assert "1500 ZOOM END screen2 (zoomed since 500;" in text
    assert f'1500 CLIPBOARD "{"x" * CLIPBOARD_EXCERPT_CHARS}..." ({len(long_text)} chars)' in text


def test_incomplete_transcript_is_flagged(tmp_path: Path):
    paths = make_recording(tmp_path, {})
    json_path = paths.transcript_json_path(paths.current_dir)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    data.update(complete=False, transcribed_ms=1200)
    json_path.write_text(json.dumps(data), encoding="utf-8")
    result = build_timeline(paths)
    assert result["transcript_complete"] is False
    assert "1200 ms" in result["transcript_note"]


def test_frames_without_drawings_lists_ids_only(tmp_path: Path):
    shape = rect(3, 100, 200)
    paths = make_recording(tmp_path, {500: {"drawings": [shape]}})
    full = frames_at(paths, 500, screen="all", window_ms=500)
    assert full["frames"][0]["drawings"] == [shape]
    slim = frames_at(paths, 500, screen="all", window_ms=500, include_drawings=False)
    for frame in slim["frames"] + slim["window"]["screen1"]:
        assert "drawings" not in frame
    assert slim["frames"][0]["drawing_ids"] == [3]


def shape(tool: str, shape_id: int, end: int, background: str = "screen") -> dict:
    return {
        "id": shape_id,
        "tool": tool,
        "color": "#000000",
        "screen": "screen1",
        "started_ms": end - 100,
        "finished_ms": end,
        "background": background,
        "from": {"screen": "screen1", "x": 10, "y": 20},
        "to": {"screen": "screen1", "x": 70, "y": 80},
    }


def test_ellipse_and_line_geometry(tmp_path: Path):
    ellipse, line = shape("ellipse", 1, 900), shape("line", 2, 1400)
    ticks = {1000: {"drawings": [ellipse]}, 1500: {"drawings": [ellipse, line]}}
    text = build_timeline(make_recording(tmp_path, ticks))["timeline"]
    assert "900 DRAW #1 ellipse #000000 screen1 corners 10,20 70,80 (pressed 800" in text
    assert "1400 DRAW #2 line #000000 screen1 from 10,20 to 70,80 (pressed 1300" in text


def test_shift_shapes_connected_lines_and_split_arrows(tmp_path: Path):
    square = {**shape("rectangle", 1, 900), "shift": True}
    circle = {**shape("ellipse", 2, 1400), "shift": True}
    closed = {
        **shape("brush", 3, 1900),
        "shift": True,
        "closed": True,
        "path": [{"screen": "screen1", "x": x, "y": y} for x, y in ((5, 5), (25, 5), (25, 25))],
    }
    first = {**shape("line", 4, 2400), "shift": True, "group": 1, "segment": 1}
    second = {**shape("line", 5, 2900), "shift": True, "group": 1, "segment": 2}
    shaft = {**shape("line", 6, 3400), "group": 2, "part_of": "arrow"}
    head = {**shape("arrow", 7, 3400), "group": 2, "screen": "screen2"}
    split_line = {**shape("line", 8, 3900), "group": 3}
    plain = shape("rectangle", 9, 4400)
    drawn = [square, circle, closed, first, second, shaft, head, split_line, plain]
    ticks = {
        t: {"drawings": [d for d in drawn if d["finished_ms"] < t]} for t in range(500, 6001, 500)
    }
    text = build_timeline(make_recording(tmp_path, ticks))["timeline"]
    assert "DRAW #1 rectangle #000000 screen1 corners 10,20 70,80 (square, Shift) (pressed" in text
    assert "DRAW #2 ellipse #000000 screen1 corners 10,20 70,80 (circle, Shift) (pressed" in text
    assert "path in the sidecar) (closed shape, Shift) (pressed" in text
    assert (
        "DRAW #4 line #000000 screen1 from 10,20 to 70,80 (connected lines group 1, segment 1)"
        in text
    )
    assert (
        "DRAW #5 line #000000 screen1 from 10,20 to 70,80 (connected lines group 1, segment 2)"
        in text
    )
    assert (
        "DRAW #6 line #000000 screen1 from 10,20 to 70,80 "
        "(shaft of an arrow split at a screen edge, group 2)" in text
    )
    assert (
        "DRAW #7 arrow #000000 screen2 tail 10,20 head 70,80 "
        "(head part of an arrow split at a screen edge, group 2)" in text
    )
    assert "(part of a line split at a screen edge, group 3)" in text
    assert "DRAW #9 rectangle #000000 screen1 corners 10,20 70,80 (pressed" in text


def test_sketch_on_off_and_shapes_on_the_canvas(tmp_path: Path):
    on_screen = shape("rectangle", 1, 900)
    on_canvas = shape("arrow", 2, 2400, background="sketch")
    ticks: dict[int, dict] = {500: {"background": "screen"}}
    ticks[1000] = {"background": "screen", "drawings": [on_screen]}
    for t in (1500, 2000):
        ticks[t] = {"background": "sketch", "drawings": [on_screen]}
    ticks[2500] = {"background": "sketch", "drawings": [on_screen, on_canvas]}
    ticks[3000] = {"background": "screen", "drawings": [on_screen, on_canvas]}
    ticks[3500] = {"background": "sketch", "drawings": [on_screen, on_canvas]}
    # 4000 onwards: the drawing layer is closed (no background, no drawings).
    result = build_timeline(make_recording(tmp_path, ticks))
    text = result["timeline"]
    assert (
        "1500 SKETCH on: every screen shows a plain white canvas, the user sketches on it "
        "instead of drawing on the screen" in text
    )
    assert "900 DRAW #1 rectangle #000000 screen1 corners 10,20 70,80 (pressed" in text
    assert "(on the sketch canvas)" not in next(x for x in lines(result) if "DRAW #1" in x)
    assert "2400 DRAW #2 arrow #000000 screen1 tail 10,20 head 70,80 (on the sketch canvas)" in text
    assert "3000 SKETCH off (the screens again; sketching since 1500)" in text
    assert "3500 SKETCH on" in text
    assert "4000 SKETCH off (drawing layer closed; sketching since 3500)" in text
    assert result["counts"]["sketch"] == 2

    # A range opening while sketching says so.
    cropped = build_timeline(make_recording(tmp_path / "b", ticks), 1700, 2200)
    assert (
        "1700 SKETCH on: every screen shows a plain white canvas, the user sketches on it "
        "instead of drawing on the screen (switched on before the range)" in cropped["timeline"]
    )


def test_eraser_removal_and_undo_restore(tmp_path: Path):
    a, b, c = shape("rectangle", 1, 400), shape("ellipse", 2, 600), shape("brush", 3, 800)
    c = {k: v for k, v in c.items() if k not in ("from", "to")}
    c["path"] = [{"screen": "screen1", "x": 1, "y": 1}, {"screen": "screen1", "x": 9, "y": 9}]
    ticks = {
        1000: {"drawings": [a, b, c]},
        1500: {"drawings": [c]},  # one eraser stroke took #1 and #2
        2000: {"drawings": [a, b, c]},  # Undo brought them back
        2500: {"drawings": [a, b, c]},
    }
    result = build_timeline(make_recording(tmp_path, ticks))
    text = result["timeline"]
    assert (
        "1500 REMOVED #1 #2 (last seen 1000: Undo, Clear, Eraser or the drawing layer closed)"
        in text
    )
    assert (
        "2000 RESTORED #1 #2 (gone at 1500, back by Undo of an Eraser stroke; same shapes as drawn)"
        in text
    )
    assert [x for x in lines(result) if " DRAW " in x][0].startswith("400 DRAW #1 rectangle")
    assert sum(" DRAW " in x for x in lines(result)) == 3  # restored shapes are not drawn again
    assert result["counts"]["restored"] == 2


def test_frames_report_the_background(tmp_path: Path):
    paths = make_recording(tmp_path, {1000: {"background": "sketch"}})
    found = frames_at(paths, 1000, "screen1")
    assert found["frames"][0]["background"] == "sketch"
    assert frames_at(paths, 2000, "screen1")["frames"][0]["background"] is None


def test_grid_on_off_and_snapped_shapes(tmp_path: Path):
    snapped = dict(rect(1, 1200, 1400), grid=16)
    ticks = {
        1000: {"grid": 16},
        1500: {"grid": 16, "drawings": [snapped]},
        2000: {"drawings": [snapped]},
        # A short Ctrl without a snapped shape (Ctrl+Z, ...) is left out.
        3000: {"grid": 16, "drawings": [snapped]},
        3500: {"drawings": [snapped]},
        4000: {"grid": 24, "drawings": [snapped]},
    }
    paths = make_recording(tmp_path, {**ticks, **{t: ticks[4000] for t in (4500, 5000)}})
    result = build_timeline(paths, end_ms=5000)
    text = lines(result)
    assert (
        "1000 GRID on: Ctrl held, points snap to a 16 px grid (pixels of the screen1 image; "
        "the grid shows on the sketch canvas only)"
    ) in text
    assert "2000 GRID off (Ctrl released; snapping since 1000)" in text
    assert not any(line.startswith(("3000 GRID", "3500 GRID")) for line in text)
    assert any(line.startswith("4000 GRID on") and "24 px" in line for line in text)
    draw = next(line for line in text if " DRAW #1 " in line)
    assert "(snapped to the 16 px grid, Ctrl)" in draw
    assert result["counts"]["grid"] == 2


def test_brush_closed_with_shift_during_the_stroke(tmp_path: Path):
    stroke = {
        "id": 1,
        "tool": "brush",
        "color": "#FF0000",
        "screen": "screen1",
        "stroke_width": 4,
        "started_ms": 1000,
        "finished_ms": 1900,
        "path": [{"screen": "screen1", "x": 1, "y": 1}, {"screen": "screen1", "x": 9, "y": 9}],
        "shift": True,
        "closed": True,
        "closed_ms": 1600,
    }
    paths = make_recording(tmp_path, {2000: {"drawings": [stroke]}})
    draw = next(line for line in lines(build_timeline(paths)) if " DRAW #1 " in line)
    assert "(closed shape, Shift from 1600)" in draw


def test_unfinished_shapes_are_reported(tmp_path: Path):
    now_brush = {"tool": "brush", "color": "#FF0000", "screen": "screen1", "started_ms": 1200}
    now_rect = {"tool": "rectangle", "color": "#0000FF", "screen": "screen2", "started_ms": 3100}
    done = rect(1, 2200, 2400)
    ticks = {
        # Cancelled: drawn at 1500 and 2000, gone at 2500 without a shape.
        1500: {"drawing_now": now_brush},
        2000: {"drawing_now": now_brush},
        # Finished: no DRAWING line, its DRAW line says it.
        2300: {},
        2500: {"drawing_now": {**now_brush, "started_ms": 2200}, "drawings": []},
        3000: {"drawings": [done]},
        # Cut by the end of the range.
        3500: {"drawings": [done], "drawing_now": now_rect},
        4000: {"drawings": [done], "drawing_now": now_rect},
    }
    paths = make_recording(tmp_path, ticks)
    result = build_timeline(paths, end_ms=3800)
    text = lines(result)
    assert (
        "2500 DRAWING brush #FF0000 screen1 (pressed 1200, never finished: cancelled with "
        "Esc, the drawing layer closed, or a click without size)"
    ) in text
    assert (
        "3800 DRAWING rectangle #0000FF screen2 (pressed 3100, not finished at the range end)"
        in (text)
    )
    assert not any("pressed 2200" in line and "DRAWING" in line for line in text)
    assert result["counts"]["unfinished"] == 2
