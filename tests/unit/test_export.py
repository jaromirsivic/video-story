"""Tests for the pure parts of the export: frame plan, concat list, cursor track, SRT."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PilImage

from video_story.core.config import Config
from video_story.core.cursor import CursorPosition
from video_story.core.naming import FrameName
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript
from video_story.export import ExportError, ExportRequest, ExportResult, ffmpeg_export, plan, srt
from video_story.export.cursor import CURSOR_HOTSPOT, CURSOR_SIZE, cursor_image, write_cursor_png
from video_story.transcribe.ffmpeg import FfmpegNotFound

FRAMES = [FrameName(t, "screen1", "png") for t in (0, 500, 1000, 1500)]


# plan_frames ------------------------------------------------------------------


def _shown(planned: list[plan.PlannedFrame]) -> list[tuple[int, int, int]]:
    return [(p.time_ms, p.start_ms, p.duration_ms) for p in planned]


def test_plan_start_between_frames():
    planned = plan.plan_frames(FRAMES, 250, 1750)
    assert _shown(planned) == [(0, 250, 250), (500, 500, 500), (1000, 1000, 500), (1500, 1500, 250)]
    assert planned[0].filename == "0_000_screen1.png"
    assert planned[0].sidecar == "0_000_screen1.json"
    assert sum(p.duration_ms for p in planned) == 1500


def test_plan_start_before_first_frame_uses_first_frame():
    names = [FrameName(t, "screen1", "jpg") for t in (400, 900)]
    planned = plan.plan_frames(names, 0, 1000)
    assert _shown(planned) == [(400, 0, 400), (400, 400, 500), (900, 900, 100)]
    assert planned[0].filename == "0_400_screen1.jpg"


def test_plan_start_exactly_at_frame_and_end_at_frame_time():
    # A frame at end_ms would have zero duration: it is not planned.
    planned = plan.plan_frames(FRAMES, 500, 1500)
    assert _shown(planned) == [(500, 500, 500), (1000, 1000, 500)]
    # Whole recording from the first frame.
    planned = plan.plan_frames(FRAMES, 0, 2000)
    assert [p.time_ms for p in planned] == [0, 500, 1000, 1500]
    assert planned[-1].duration_ms == 500


def test_plan_after_last_frame_and_errors():
    planned = plan.plan_frames(FRAMES, 1600, 1900)
    assert _shown(planned) == [(1500, 1600, 300)]
    with pytest.raises(ExportError) as info:
        plan.plan_frames([], 0, 1000)
    assert info.value.code == "NO_FRAMES"
    with pytest.raises(ExportError) as info:
        plan.plan_frames(FRAMES, 1000, 1000)
    assert info.value.code == "INVALID_RANGE"
    with pytest.raises(ExportError) as info:
        plan.plan_frames(FRAMES, 1200, 300)
    assert info.value.code == "INVALID_RANGE"


# concat list ------------------------------------------------------------------


def test_concat_list_text_quotes_and_repeats_last(tmp_path: Path):
    frames_dir = tmp_path / "it's here" / "frames"
    frames_dir.mkdir(parents=True)
    planned = plan.plan_frames(FRAMES[:2], 250, 1000)
    text = plan.concat_list_text(frames_dir, planned)

    def quoted(name: str) -> str:
        return "'" + str(frames_dir.resolve() / name).replace("'", "'\\''") + "'"

    # Every file carries "option framerate 1000": with image2's default 25 fps time
    # base the concat demuxer would round each start time to a multiple of 40 ms.
    assert text.splitlines() == [
        f"file {quoted('0_000_screen1.png')}",
        "option framerate 1000",
        "duration 0.250",
        f"file {quoted('0_500_screen1.png')}",
        "option framerate 1000",
        "duration 0.500",
        f"file {quoted('0_500_screen1.png')}",
        "option framerate 1000",
    ]
    assert "it'\\''s here" in text and text.endswith("\n")
    with pytest.raises(ExportError):
        plan.concat_list_text(frames_dir, [])


# cursor track -----------------------------------------------------------------


def _reader(positions: dict[int, CursorPosition | None]) -> plan.CursorReader:
    return lambda frame: positions.get(frame.time_ms)


def test_output_frame_index_rounds_like_fps_filter():
    assert plan.output_frame_index(0) == 0
    assert plan.output_frame_index(250) == 6
    assert plan.output_frame_index(333) == 8  # 7.99 -> 8
    assert plan.output_frame_index(340) == 8  # 8.16 -> 8
    assert plan.output_frame_index(1000) == 24


def test_cursor_keyframes_parks_and_merges():
    planned = plan.plan_frames(FRAMES, 250, 1750)
    positions = {
        0: CursorPosition("screen1", 100, 50),
        500: CursorPosition("screen1", 100, 50),  # unchanged: merged
        1000: CursorPosition("screen2", 10, 10),  # other screen: parked
        1500: None,  # no cursor: parked (unchanged: merged)
    }
    keyframes = plan.cursor_keyframes(planned, _reader(positions), "screen1", hotspot=(1, 2))
    assert keyframes == [
        plan.CursorKeyframe(0, 99, 48),
        plan.CursorKeyframe(18, *plan.CURSOR_PARKED),
    ]
    assert plan.cursor_keyframes([], _reader({}), "screen1") == []


def test_cursor_keyframes_two_frames_on_one_output_frame():
    names = [FrameName(t, "screen1", "png") for t in (0, 10, 500)]
    planned = plan.plan_frames(names, 0, 1000)
    positions = {0: CursorPosition("screen1", 1, 1), 10: CursorPosition("screen1", 2, 2)}
    keyframes = plan.cursor_keyframes(planned, _reader(positions), "screen1")
    assert keyframes == [plan.CursorKeyframe(0, 2, 2), plan.CursorKeyframe(12, -1000, -1000)]


def _eval(expr: str, frame: int) -> int:
    """Evaluate an ``if(lt(n,N),a,b)`` tree for output frame ``frame`` (0-based) the way
    ffmpeg's overlay does: its ``n`` is 1-based (``OVERLAY_FIRST_N``)."""

    def lt(a: float, b: float) -> bool:
        return a < b

    def if_(cond: bool, a: int, b: int) -> int:
        return a if cond else b

    n = frame + plan.OVERLAY_FIRST_N
    return eval(expr.replace("if(", "if_("), {"if_": if_, "lt": lt, "n": n})  # noqa: S307


def test_cursor_expressions_balanced_tree():
    # A keyframe at output frame 18 switches when overlay's 1-based n reaches 19.
    assert plan.OVERLAY_FIRST_N == 1
    keyframes = [plan.CursorKeyframe(0, 5, 6), plan.CursorKeyframe(18, -1000, -1000)]
    x_expr, y_expr = plan.cursor_expressions(keyframes)
    assert x_expr == "if(lt(n,19),5,-1000)" and y_expr == "if(lt(n,19),6,-1000)"
    assert _eval(x_expr, 17) == 5 and _eval(x_expr, 18) == -1000
    assert plan.cursor_expressions([]) == ("-1000", "-1000")
    assert plan.cursor_expressions([plan.CursorKeyframe(0, 7, 8)]) == ("7", "8")
    # Many keyframes: every frame evaluates to the position of its keyframe, and the
    # nesting depth stays logarithmic.
    many = [plan.CursorKeyframe(i * 3, i, i * 2) for i in range(1000)]
    x_expr, y_expr = plan.cursor_expressions(many)
    for frame in (0, 1, 2, 3, 299, 300, 2996, 2997, 5000):
        key = max((k for k in many if k.frame <= frame), key=lambda k: k.frame)
        assert _eval(x_expr, frame) == key.x and _eval(y_expr, frame) == key.y
    assert x_expr.count("if(") == 999
    depth = max(len(prefix) - len(prefix.rstrip("(")) for prefix in [x_expr])
    assert depth <= 12
    assert "," in x_expr and "'" not in x_expr


def test_sidecar_cursor_reader(tmp_path: Path):
    (tmp_path / "0_000_screen1.json").write_text(
        '{"cursor": {"screen": "screen1", "x": 3, "y": 4}}', encoding="utf-8"
    )
    (tmp_path / "0_500_screen1.json").write_text('{"cursor": null}', encoding="utf-8")
    (tmp_path / "1_000_screen1.json").write_text("not json", encoding="utf-8")
    read = plan.sidecar_cursor_reader(tmp_path)
    assert read(plan.PlannedFrame("0_000_screen1.png", 0, 0, 1)) == CursorPosition("screen1", 3, 4)
    assert read(plan.PlannedFrame("0_500_screen1.png", 500, 500, 1)) is None
    assert read(plan.PlannedFrame("1_000_screen1.png", 1000, 1000, 1)) is None
    assert read(plan.PlannedFrame("9_000_screen1.png", 9000, 9000, 1)) is None


# cursor image -----------------------------------------------------------------


def test_cursor_image_is_deterministic(tmp_path: Path):
    image = cursor_image()
    assert image.size == CURSOR_SIZE and image.mode == "RGBA"
    assert image.getpixel(CURSOR_HOTSPOT)[3] == 255  # the tip is opaque
    assert image.getpixel((23, 0)) == (0, 0, 0, 0)  # top-right corner transparent
    assert image.getpixel((3, 10)) == (255, 255, 255, 255)  # white fill inside
    first = write_cursor_png(tmp_path / "a" / "cursor.png").read_bytes()
    second = write_cursor_png(tmp_path / "b.png").read_bytes()
    assert first == second
    with PilImage.open(tmp_path / "b.png") as reloaded:
        assert reloaded.size == CURSOR_SIZE


# srt --------------------------------------------------------------------------


def test_srt_timestamp():
    assert srt.srt_timestamp(0) == "00:00:00,000"
    assert srt.srt_timestamp(3_723_456) == "01:02:03,456"
    assert srt.srt_timestamp(-5) == "00:00:00,000"


def test_srt_text_clips_shifts_numbers(tmp_path: Path):
    transcript = Transcript(
        "en",
        [
            Segment(0, 600, "one"),
            Segment(600, 1200, "two"),
            Segment(1200, 2000, "three"),
            Segment(1300, 1400, "   "),  # empty text: dropped
            Segment(1750, 1900, "after"),  # outside: dropped
        ],
    )
    text = srt.srt_text(transcript, 250, 1750)
    assert text == (
        "1\n00:00:00,000 --> 00:00:00,350\none\n"
        "\n2\n00:00:00,350 --> 00:00:00,950\ntwo\n"
        "\n3\n00:00:00,950 --> 00:00:01,500\nthree\n"
    )
    assert "\r" not in text
    # Segments touching the range only at its edges are not included.
    assert srt.srt_text(transcript, 600, 1200) == "1\n00:00:00,000 --> 00:00:00,600\ntwo\n"
    assert srt.srt_text(Transcript(None, []), 0, 1000) == ""
    path = srt.write_srt(tmp_path / "sub" / "clip.srt", transcript, 250, 1750)
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    assert raw.decode("utf-8") == text


# export_recording without ffmpeg ---------------------------------------------


def test_export_requires_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def missing(config: Config) -> Path:
        raise FfmpegNotFound("nope")

    monkeypatch.setattr(ffmpeg_export, "ensure_ffmpeg", missing)
    request = ExportRequest(SessionPaths(tmp_path, "20260101-120000"), tmp_path / "x.mp4")
    with pytest.raises(ExportError) as info:
        ffmpeg_export.export_recording(request, Config(recording_folder=tmp_path))
    assert info.value.code == "FFMPEG_MISSING" and "nope" in str(info.value)


def test_export_missing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ffmpeg_export, "ensure_ffmpeg", lambda config: tmp_path / "ffmpeg")
    request = ExportRequest(SessionPaths(tmp_path, "20260101-120000"), tmp_path / "x.mp4")
    with pytest.raises(ExportError) as info:
        ffmpeg_export.export_recording(request, Config(recording_folder=tmp_path))
    assert info.value.code == "NO_MANIFEST"


def test_filter_graph_and_command(tmp_path: Path):
    assert ffmpeg_export.filter_graph(None) == (
        "[0:v]fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p[vout]"
    )
    with_cursor = ffmpeg_export.filter_graph(("if(lt(n,6),1,2)", "3"))
    assert with_cursor == (
        "[0:v]fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2[v0];"
        "[v0][1:v]overlay=x='if(lt(n,6),1,2)':y='3':eval=frame:format=auto,"
        "format=yuv420p[vout]"
    )
    cfg = Config(recording_folder=tmp_path, export_preset="fast", export_crf=20)
    cmd = ffmpeg_export.build_command(
        Path("ffmpeg"), cfg, tmp_path / "o.mp4", 1500, 250, tmp_path / "a.wav", True, with_cursor
    )
    assert cmd[cmd.index("-ss") + 1] == "0.250"
    assert cmd[cmd.index("-map") + 1] == "[vout]" and "2:a" in cmd
    assert cmd[cmd.index("-preset") + 1] == "fast" and cmd[cmd.index("-crf") + 1] == "20"
    assert cmd[cmd.index("-filter_complex") + 1] == with_cursor
    assert cmd.count("-t") == 2 and cmd[-1] == str(tmp_path / "o.mp4")
    assert cmd[-3:-1] == ["-f", "mp4"]  # the container does not depend on the name
    silent = ffmpeg_export.build_command(
        Path("ffmpeg"), cfg, tmp_path / "o.mp4", 1500, 0, None, False, "g", "filters.txt"
    )
    assert "-c:a" not in silent and "cursor.png" not in silent
    assert silent[silent.index("-filter_complex_script") + 1] == "filters.txt"
    assert ffmpeg_export._parse_progress_line("out_time_us=1500000\n") == 1500
    assert ffmpeg_export._parse_progress_line("out_time_ms=N/A") is None
    assert ffmpeg_export._parse_progress_line("progress=end") is None
    assert ffmpeg_export._parse_progress_frames("frame=144\n") == 144
    assert ffmpeg_export._parse_progress_frames("frame=N/A") is None
    assert ffmpeg_export._parse_progress_frames("drop_frames=3") is None


def test_probe_command_and_minimum_frames(tmp_path: Path):
    cmd = ffmpeg_export.probe_command(Path("ffmpeg"), tmp_path / "o.mp4")
    assert cmd[0] == "ffmpeg" and cmd[cmd.index("-i") + 1] == str(tmp_path / "o.mp4")
    assert cmd[cmd.index("-map") + 1] == "0:v:0"  # the video stream only, not the audio
    assert cmd[cmd.index("-c") + 1] == "copy" and cmd[-3:] == ["-f", "null", "-"]
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"
    # 24 fps; up to VERIFY_TOLERANCE_FRAMES (3) may be missing, never all of them.
    assert ffmpeg_export.minimum_frames(6000) == 141
    assert ffmpeg_export.minimum_frames(1000) == 21
    assert ffmpeg_export.minimum_frames(1010) == 22  # 24.24 frames round up to 25
    assert ffmpeg_export.minimum_frames(100) == 1 and ffmpeg_export.minimum_frames(0) == 1


def test_frames_with_picture_leaves_out_empty_and_missing_files(tmp_path: Path):
    (tmp_path / FRAMES[0].filename).write_bytes(b"picture")
    (tmp_path / FRAMES[1].filename).write_bytes(b"")  # the recorder died while writing
    (tmp_path / FRAMES[3].filename).write_bytes(b"picture")  # FRAMES[2] is not there
    usable, empty = ffmpeg_export.frames_with_picture(tmp_path, FRAMES)
    assert usable == [FRAMES[0], FRAMES[3]] and empty == [FRAMES[1], FRAMES[2]]
    # The plan of what is left shows the frame before each gap longer.
    planned = plan.plan_frames(usable, 0, 2000)
    assert _shown(planned) == [(0, 0, 1500), (1500, 1500, 500)]


def test_normalise_output_and_partial_name(tmp_path: Path):
    normalise = ffmpeg_export.normalise_output
    assert normalise(tmp_path / "clip.mp4") == (tmp_path / "clip.mp4").resolve()
    assert normalise(tmp_path / "Clip.MP4") == (tmp_path / "Clip.MP4").resolve()
    assert normalise(tmp_path / "demo") == (tmp_path / "demo.mp4").resolve()
    assert normalise(tmp_path / "my.recording") == (tmp_path / "my.recording.mp4").resolve()
    assert normalise(tmp_path / "a.mp4.bak") == (tmp_path / "a.mp4.bak.mp4").resolve()
    partial = ffmpeg_export.partial_output(tmp_path / "out" / "clip.mp4")
    assert partial == tmp_path / "out" / "clip.part.mp4"


def test_export_result_to_dict(tmp_path: Path):
    result = ExportResult(
        output=tmp_path / "o.mp4",
        srt=None,
        screen_id="screen1",
        start_ms=0,
        end_ms=10,
        duration_ms=10,
        width=2,
        height=2,
        fps=24,
        frames_used=1,
        ffmpeg="ffmpeg",
        command=["ffmpeg"],
    )
    data = result.to_dict()
    assert data["srt"] is None and data["screen"] == "screen1" and "command" not in data
