"""End-to-end export of a synthetic recording through ffmpeg, verified with ffprobe.

Skipped when ffmpeg or ffprobe is not available (GitHub runners have both on PATH);
the tests with ``FakeRuns`` need neither.
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from PIL import Image as PilImage

from video_story.core.config import Config
from video_story.core.manifest import Manifest, write_manifest
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript, write_transcript
from video_story.export import ExportError, ExportRequest, ffmpeg_export, plan
from video_story.export.cursor import CURSOR_SIZE
from video_story.export.ffmpeg_export import export_recording
from video_story.host import cli, mcp_server
from video_story.transcribe.ffmpeg import executable_name, find_ffmpeg

SESSION_ID = "20260101-120000"
SCREEN1 = (320, 200)
SCREEN2 = (160, 100)
CURSOR_AT = (100, 50)  # on screen1 for the frames at 0 and 500 ms
COLOURS = {
    "screen1": [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)],
    "screen2": [(0, 255, 255), (255, 0, 255), (255, 255, 255), (0, 0, 0)],
}
TIMES = (0, 500, 1000, 1500)


def _ffmpeg_and_ffprobe() -> tuple[Path, Path]:
    ffmpeg = find_ffmpeg(Config())
    if ffmpeg is None:
        pytest.skip("ffmpeg not available")
    candidate = ffmpeg.parent / executable_name("ffprobe")
    ffprobe = candidate if candidate.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        pytest.skip("ffprobe not available")
    return ffmpeg, Path(ffprobe)


@pytest.fixture(scope="module")
def tools() -> tuple[Path, Path]:
    return _ffmpeg_and_ffprobe()


def make_recording(
    root: Path,
    session_id: str = SESSION_ID,
    with_audio: bool = True,
    duration_ms: int = 2000,
) -> SessionPaths:
    """Two screens, frames at 0/500/1000/1500 ms in solid colours, audio and transcript.

    ``duration_ms`` longer than the last frame holds it until the end (a long encode).
    """
    paths = SessionPaths(root, session_id)
    paths.create_work_dirs()
    frames = paths.frames_dir(paths.work_dir)
    info = paths.frames_info_dir(paths.work_dir)
    screens = assign_screen_ids(
        [RawScreen(0, 0, *SCREEN1, primary=True), RawScreen(SCREEN1[0], 0, *SCREEN2)]
    )
    for index, t in enumerate(TIMES):
        if t < 1000:
            cursor = {"screen": "screen1", "x": CURSOR_AT[0], "y": CURSOR_AT[1]}
        else:
            cursor = {"screen": "screen2", "x": 10, "y": 10}
        for sid, size in (("screen1", SCREEN1), ("screen2", SCREEN2)):
            PilImage.new("RGB", size, COLOURS[sid][index]).save(
                frames / frame_filename(t, sid, "png")
            )
            (info / sidecar_filename(t, sid)).write_text(
                json.dumps({"time_ms": t, "screen": sid, "cursor": cursor}), encoding="utf-8"
            )
    write_manifest(
        paths.manifest_path(paths.work_dir),
        Manifest(
            session_id=session_id,
            started_at="x",
            finished_at="y",
            recorded_duration_ms=duration_ms,
            capture_interval_ms=500,
            image_format="png",
            jpeg_quality=None,
            screens=screens,
        ),
    )
    if with_audio:
        rate = 16000
        samples = (
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * n / rate)))
            for n in range(2 * rate)
        )
        with wave.open(str(paths.audio_path(paths.work_dir)), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(b"".join(samples))
    write_transcript(
        paths.transcript_txt_path(paths.work_dir),
        paths.transcript_json_path(paths.work_dir),
        Transcript(
            "en",
            [Segment(0, 600, "one"), Segment(600, 1200, "two"), Segment(1200, 2000, "three")],
        ),
    )
    paths.finalize()
    return paths


@pytest.fixture
def recording(tmp_path: Path) -> SessionPaths:
    return make_recording(tmp_path / "rec")


def probe(ffprobe: Path, path: Path) -> dict:
    out = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def decode_frame(ffmpeg: Path, video: Path, at_s: float, target: Path) -> PilImage.Image:
    subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-y",
            "-ss",
            f"{at_s:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "image2",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    with PilImage.open(target) as image:
        return image.convert("RGB")


def dominant_colour(image: PilImage.Image) -> tuple[int, int, int]:
    small = image.resize((1, 1), PilImage.Resampling.BOX)
    return small.getpixel((0, 0))


def _close(a: tuple[int, int, int], b: tuple[int, int, int], tolerance: int = 40) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b, strict=True))


def cursor_box_contrast(
    image: PilImage.Image,
    background: tuple[int, int, int],
    at: tuple[int, int] = CURSOR_AT,
) -> int:
    """Largest channel difference from ``background`` inside the pointer's box at ``at``."""
    width, height = CURSOR_SIZE
    best = 0
    for y in range(at[1], at[1] + height):
        for x in range(at[0], at[0] + width):
            pixel = image.getpixel((x, y))
            best = max(best, max(abs(p - b) for p, b in zip(pixel, background, strict=True)))
    return best


def decode_all_frames(ffmpeg: Path, video: Path, size: tuple[int, int]) -> list[PilImage.Image]:
    """Every frame of ``video`` as RGB images (raw decode, no seeking)."""
    raw = subprocess.run(
        [str(ffmpeg), "-v", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        check=True,
        capture_output=True,
    ).stdout
    stride = size[0] * size[1] * 3
    assert len(raw) % stride == 0
    return [
        PilImage.frombytes("RGB", size, raw[i : i + stride]) for i in range(0, len(raw), stride)
    ]


# Frames whose offsets are not multiples of 40 ms (the concat demuxer's default 1/25 s
# time base): without "option framerate 1000" their start times would be rounded.
OFFSET_TIMES = (0, 100, 300)
OFFSET_COLOURS = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
OFFSET_CURSORS = [(20, 20), (150, 20), (250, 120)]  # distinct, non-overlapping boxes


def make_offset_recording(root: Path) -> SessionPaths:
    """One screen, frames at 0/100/300 ms held until 1000 ms, cursor moving per frame."""
    paths = SessionPaths(root, SESSION_ID)
    paths.create_work_dirs()
    frames = paths.frames_dir(paths.work_dir)
    info = paths.frames_info_dir(paths.work_dir)
    screens = assign_screen_ids([RawScreen(0, 0, *SCREEN1, primary=True)])
    for t, colour, (cx, cy) in zip(OFFSET_TIMES, OFFSET_COLOURS, OFFSET_CURSORS, strict=True):
        PilImage.new("RGB", SCREEN1, colour).save(frames / frame_filename(t, "screen1", "png"))
        (info / sidecar_filename(t, "screen1")).write_text(
            json.dumps(
                {
                    "time_ms": t,
                    "screen": "screen1",
                    "cursor": {"screen": "screen1", "x": cx, "y": cy},
                }
            ),
            encoding="utf-8",
        )
    write_manifest(
        paths.manifest_path(paths.work_dir),
        Manifest(
            session_id=SESSION_ID,
            started_at="x",
            finished_at="y",
            recorded_duration_ms=1000,
            capture_interval_ms=100,
            image_format="png",
            jpeg_quality=None,
            screens=screens,
        ),
    )
    paths.finalize()
    return paths


# export_recording -------------------------------------------------------------


def test_export_screen1_range(tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path):
    ffmpeg, ffprobe = tools
    output = tmp_path / "out" / "clip.mp4"
    reports: list[tuple[float, str]] = []
    request = ExportRequest(recording, output, screen_id="screen1", start_ms=250, end_ms=1750)
    result = export_recording(
        request,
        Config(recording_folder=recording.recording_folder),
        progress=lambda f, t: reports.append((f, t)),
    )

    assert result.output == output.resolve() and output.is_file()
    assert result.frames_used == 4 and result.duration_ms == 1500
    assert (result.width, result.height, result.fps) == (320, 200, 24)
    assert result.command[0] == str(ffmpeg) and result.ffmpeg == str(ffmpeg)
    assert reports[0][0] == 0.0 and reports[-1] == (1.0, "done")
    assert all(0.0 <= f <= 1.0 for f, _ in reports)

    info = probe(ffprobe, output)
    video = [s for s in info["streams"] if s["codec_type"] == "video"]
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(video) == 1 and video[0]["codec_name"] == "h264"
    assert video[0]["r_frame_rate"] == "24/1"
    assert (video[0]["width"], video[0]["height"]) == (320, 200)
    assert len(audio) == 1 and audio[0]["codec_name"] == "aac"
    assert abs(float(info["format"]["duration"]) - 1.5) <= 0.2

    first = decode_frame(ffmpeg, output, 0.1, tmp_path / "f01.png")
    assert first.size == (320, 200)
    assert _close(dominant_colour(first), COLOURS["screen1"][0])  # frame at 0 ms (red)
    assert cursor_box_contrast(first, COLOURS["screen1"][0]) > 100  # pointer drawn

    later = decode_frame(ffmpeg, output, 1.0, tmp_path / "f10.png")
    assert _close(dominant_colour(later), COLOURS["screen1"][2])  # 1250 ms -> frame at 1000 (blue)
    assert cursor_box_contrast(later, COLOURS["screen1"][2]) < 40  # cursor on screen2: hidden

    assert result.srt == output.with_suffix(".srt")
    assert result.srt.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:00,350\none\n"
        "\n2\n00:00:00,350 --> 00:00:00,950\ntwo\n"
        "\n3\n00:00:00,950 --> 00:00:01,500\nthree\n"
    )


def test_export_without_cursor_srt_and_audio(tools: tuple[Path, Path], tmp_path: Path):
    ffmpeg, ffprobe = tools
    recording = make_recording(tmp_path / "rec", with_audio=False)
    output = tmp_path / "plain.mp4"
    request = ExportRequest(
        recording,
        output,
        screen_id="screen2",
        start_ms=0,
        end_ms=None,
        draw_cursor=False,
        write_srt=False,
    )
    result = export_recording(request, Config(recording_folder=recording.recording_folder))
    assert result.srt is None and not output.with_suffix(".srt").exists()
    assert result.end_ms == 2000 and result.duration_ms == 2000 and result.frames_used == 4
    assert (result.width, result.height) == (160, 100)
    info = probe(ffprobe, output)
    assert [s["codec_type"] for s in info["streams"]] == ["video"]
    assert abs(float(info["format"]["duration"]) - 2.0) <= 0.2
    assert "cursor.png" not in result.command and "-c:a" not in result.command

    # screen1 without the pointer: the cursor box stays the background colour.
    output2 = tmp_path / "nocursor.mp4"
    export_recording(
        ExportRequest(recording, output2, start_ms=250, end_ms=1750, draw_cursor=False),
        Config(recording_folder=recording.recording_folder),
    )
    first = decode_frame(ffmpeg, output2, 0.1, tmp_path / "nc.png")
    assert cursor_box_contrast(first, COLOURS["screen1"][0]) < 40


def test_export_clamps_end_and_validates(
    tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path
):
    cfg = Config(recording_folder=recording.recording_folder)
    result = export_recording(
        ExportRequest(
            recording, tmp_path / "clamped.mp4", start_ms=1500, end_ms=99_000, draw_cursor=False
        ),
        cfg,
    )
    assert result.end_ms == 2000 and result.frames_used == 1
    with pytest.raises(ExportError) as info:
        export_recording(ExportRequest(recording, tmp_path / "x.mp4", screen_id="screen9"), cfg)
    assert info.value.code == "UNKNOWN_SCREEN"
    with pytest.raises(ExportError) as info:
        export_recording(ExportRequest(recording, tmp_path / "x.mp4", start_ms=2000), cfg)
    assert info.value.code == "INVALID_RANGE"
    with pytest.raises(ExportError) as info:
        export_recording(ExportRequest(recording, tmp_path / "x.mp4", start_ms=-1), cfg)
    assert info.value.code == "INVALID_RANGE"
    assert not (tmp_path / "x.mp4").exists()


def test_export_frame_timing_and_cursor_switch(tools: tuple[Path, Path], tmp_path: Path):
    """Each source first appears on the output frame ``output_frame_index`` predicts and
    the pointer moves on exactly that frame (overlay's ``n`` is 1-based)."""
    ffmpeg, _ = tools
    recording = make_offset_recording(tmp_path / "rec")
    output = tmp_path / "timing.mp4"
    result = export_recording(
        ExportRequest(recording, output, write_srt=False),
        Config(recording_folder=recording.recording_folder, export_preset="ultrafast"),
    )
    assert result.duration_ms == 1000 and result.frames_used == 3

    frames = decode_all_frames(ffmpeg, output, SCREEN1)
    assert len(frames) == 24
    shown: list[int] = []
    pointer: list[int | None] = []
    for image in frames:
        colour = image.getpixel((SCREEN1[0] - 8, SCREEN1[1] - 8))  # away from all pointers
        [source] = [i for i, c in enumerate(OFFSET_COLOURS) if _close(colour, c)]
        shown.append(source)
        visible = [
            i
            for i, at in enumerate(OFFSET_CURSORS)
            if cursor_box_contrast(image, OFFSET_COLOURS[source], at) > 100
        ]
        pointer.append(visible[0] if len(visible) == 1 else None)

    expected_first = {i: plan.output_frame_index(t) for i, t in enumerate(OFFSET_TIMES)}
    assert expected_first == {0: 0, 1: 2, 2: 7}  # 100 ms -> 2.4 -> 2, 300 ms -> 7.2 -> 7
    assert {source: shown.index(source) for source in range(3)} == expected_first
    assert shown == sorted(shown)  # each source shown once, in order
    assert pointer == shown  # the pointer switches on the same output frame


def test_export_cancel_keeps_previous_output(
    tools: tuple[Path, Path], tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    # A long encode (the last frame is held for ten minutes) that is cancelled once ffmpeg
    # has opened the partial file; an earlier export at the output path must survive.
    recording = make_recording(tmp_path / "rec", duration_ms=600_000)
    output = tmp_path / "cancelled" / "clip.mp4"
    output.parent.mkdir()
    output.write_bytes(b"previous export")
    output.with_suffix(".srt").write_text("previous subtitles", encoding="utf-8")
    partial = output.with_name("clip.part.mp4")
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return partial.exists()

    caplog.set_level("INFO", logger="video_story.export.ffmpeg_export")
    with pytest.raises(ExportError) as info:
        export_recording(
            ExportRequest(recording, output),
            Config(recording_folder=recording.recording_folder, export_preset="ultrafast"),
            cancel=cancel,
        )
    assert info.value.code == "CANCELLED" and calls >= 1
    assert not partial.exists()
    assert output.read_bytes() == b"previous export"
    assert output.with_suffix(".srt").read_text(encoding="utf-8") == "previous subtitles"
    assert "export cancelled" in caplog.text


def test_export_exception_in_progress_callback_removes_partial(
    tools: tuple[Path, Path], tmp_path: Path
):
    recording = make_recording(tmp_path / "rec", duration_ms=600_000)
    output = tmp_path / "boom.mp4"
    output.write_bytes(b"previous export")
    partial = output.with_name("boom.part.mp4")

    class Boom(Exception):
        pass

    def progress(fraction: float, text: str) -> None:
        if fraction > 0.0 or partial.exists():
            raise Boom

    with pytest.raises(Boom):
        export_recording(
            ExportRequest(recording, output),
            Config(recording_folder=recording.recording_folder, export_preset="ultrafast"),
            progress=progress,
        )
    assert not partial.exists()
    assert output.read_bytes() == b"previous export"


def test_export_ffmpeg_failure_keeps_previous_output(
    tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path
):
    # A frame file that is not an image makes ffmpeg fail: the error carries its stderr.
    frames = recording.frames_dir()
    (frames / frame_filename(0, "screen1", "png")).write_bytes(b"not a png")
    output = tmp_path / "broken.mp4"
    output.write_bytes(b"previous export")
    with pytest.raises(ExportError) as info:
        export_recording(
            ExportRequest(recording, output, end_ms=400),
            Config(recording_folder=recording.recording_folder),
        )
    assert info.value.code == "FFMPEG_FAILED" and "ffmpeg exited" in str(info.value)
    assert not output.with_name("broken.part.mp4").exists()
    assert output.read_bytes() == b"previous export"
    assert not output.with_suffix(".srt").exists()


def test_export_appends_mp4_suffix(
    tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path
):
    cfg = Config(recording_folder=recording.recording_folder, export_preset="ultrafast")
    result = export_recording(
        ExportRequest(recording, tmp_path / "demo", end_ms=300, draw_cursor=False), cfg
    )
    assert result.output == (tmp_path / "demo.mp4").resolve() and result.output.is_file()
    assert result.srt == (tmp_path / "demo.srt").resolve() and result.srt.is_file()
    assert not (tmp_path / "demo").exists()
    result = export_recording(
        ExportRequest(recording, tmp_path / "my.recording", end_ms=300, draw_cursor=False), cfg
    )
    assert result.output == (tmp_path / "my.recording.mp4").resolve()
    assert result.srt == (tmp_path / "my.recording.srt").resolve()
    # An upper-case suffix is kept (and ffmpeg still writes an MP4 container).
    result = export_recording(
        ExportRequest(recording, tmp_path / "Upper.MP4", end_ms=300, draw_cursor=False), cfg
    )
    assert result.output == (tmp_path / "Upper.MP4").resolve() and result.output.is_file()
    assert not list(tmp_path.glob("*.part.*"))


def test_export_removes_stale_srt(
    tools: tuple[Path, Path],
    recording: SessionPaths,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    cfg = Config(recording_folder=recording.recording_folder, export_preset="ultrafast")
    output = tmp_path / "again.mp4"
    srt = output.with_suffix(".srt")
    first = export_recording(ExportRequest(recording, output, end_ms=300, draw_cursor=False), cfg)
    assert first.srt == srt.resolve() and srt.is_file()

    caplog.set_level("INFO", logger="video_story.export.ffmpeg_export")
    again = export_recording(
        ExportRequest(recording, output, end_ms=300, draw_cursor=False, write_srt=False), cfg
    )
    assert again.srt is None and not srt.exists() and output.is_file()
    assert "stale subtitles" in caplog.text

    # The same when the transcript is missing.
    export_recording(ExportRequest(recording, output, end_ms=300, draw_cursor=False), cfg)
    assert srt.is_file()
    recording.transcript_json_path().unlink()
    missing = export_recording(ExportRequest(recording, output, end_ms=300, draw_cursor=False), cfg)
    assert missing.srt is None and not srt.exists()


def test_export_output_under_existing_file(
    tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path
):
    (tmp_path / "afile").write_bytes(b"x")
    with pytest.raises(ExportError) as info:
        export_recording(
            ExportRequest(recording, tmp_path / "afile" / "clip.mp4", end_ms=300),
            Config(recording_folder=recording.recording_folder),
        )
    assert info.value.code == "OUTPUT_ERROR" and "afile" in str(info.value)


# A video cut short ------------------------------------------------------------


def test_export_leaves_out_a_frame_without_picture(
    tools: tuple[Path, Path],
    recording: SessionPaths,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """A zero-byte frame used to end the video where it stood (ffmpeg takes a file it
    cannot open for the end of the input and still exits with 0)."""
    ffmpeg, ffprobe = tools
    (recording.frames_dir() / frame_filename(1000, "screen1", "png")).write_bytes(b"")
    output = tmp_path / "gap.mp4"
    caplog.set_level("WARNING", logger="video_story.export.ffmpeg_export")
    result = export_recording(
        ExportRequest(recording, output, draw_cursor=False),
        Config(recording_folder=recording.recording_folder, export_preset="ultrafast"),
    )
    assert result.frames_used == 3 and result.duration_ms == 2000
    assert "1 frame(s) of screen1 have no picture" in caplog.text
    assert "1_000_screen1.png" in caplog.text
    info = probe(ffprobe, output)
    [video] = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert abs(float(video["duration"]) - 2.0) <= 0.1  # the video stream, not the container
    assert len(decode_all_frames(ffmpeg, output, SCREEN1)) == 48
    # The frame before the gap stays until the next good one.
    during = decode_frame(ffmpeg, output, 1.25, tmp_path / "gap.png")
    assert _close(dominant_colour(during), COLOURS["screen1"][1])
    after = decode_frame(ffmpeg, output, 1.75, tmp_path / "after.png")
    assert _close(dominant_colour(after), COLOURS["screen1"][3])


@pytest.mark.parametrize("ffmpeg_says_so", [True, False])
def test_export_fails_when_a_frame_cannot_be_read_during_the_encode(
    tools: tuple[Path, Path],
    recording: SessionPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ffmpeg_says_so: bool,
):
    """A frame that goes away after the plan was made (locked by an antivirus, a network
    folder that dropped out): ffmpeg exits with 0, audio 2.0 s, video 1.0 s. With
    ``ffmpeg_says_so`` its error line gives it away; without (other ffmpeg versions word
    it differently) the frame count of the written file does."""
    victim = recording.frames_dir() / frame_filename(1000, "screen1", "png")
    real_list = ffmpeg_export.concat_list_text

    def list_then_lose_a_frame(frames_dir, planned):
        text = real_list(frames_dir, planned)
        victim.unlink()
        return text

    monkeypatch.setattr(ffmpeg_export, "concat_list_text", list_then_lose_a_frame)
    if not ffmpeg_says_so:
        monkeypatch.setattr(ffmpeg_export, "INPUT_ERROR_MARKER", "<never logged>")
    output = tmp_path / "cut.mp4"
    output.write_bytes(b"previous export")
    with pytest.raises(ExportError) as info:
        export_recording(
            ExportRequest(recording, output, draw_cursor=False),
            Config(recording_folder=recording.recording_folder, export_preset="ultrafast"),
        )
    assert info.value.code == "OUTPUT_TRUNCATED"
    counted = "holds 24 frames (1.0 s) instead of 2.0 s" in str(info.value)
    assert counted or (ffmpeg_says_so and "1_000_screen1.png" in str(info.value))
    assert not output.with_name("cut.part.mp4").exists()
    assert output.read_bytes() == b"previous export"
    assert not output.with_suffix(".srt").exists()


def test_verify_video_counts_the_frames_of_the_video_stream(
    tools: tuple[Path, Path], recording: SessionPaths, tmp_path: Path
):
    cfg = Config(recording_folder=recording.recording_folder)  # default preset: B-frames
    output = tmp_path / "whole.mp4"
    export_recording(ExportRequest(recording, output), cfg)
    assert ffmpeg_export.verify_video(output, 2000, cfg) == 48
    assert ffmpeg_export.verify_video(output, 2100, cfg) == 48  # within the tolerance
    with pytest.raises(ExportError) as info:
        ffmpeg_export.verify_video(output, 4000, cfg)  # the audio could be that long
    assert info.value.code == "OUTPUT_TRUNCATED" and "48 frames" in str(info.value)

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    with pytest.raises(ExportError) as info:
        ffmpeg_export.verify_video(broken, 2000, cfg)
    assert info.value.code == "OUTPUT_UNVERIFIED"
    with pytest.raises(ExportError) as info:
        ffmpeg_export.verify_video(recording.audio_path(), 2000, cfg)  # no video stream
    assert info.value.code == "OUTPUT_UNVERIFIED"
    with pytest.raises(ExportError) as info:
        ffmpeg_export.verify_video(output, 2000, cfg, cancel=lambda: True)
    assert info.value.code == "CANCELLED"


# The same without ffmpeg: its runs are replaced by what they would report.


class FakeRuns:
    """Stands in for ``_run_ffmpeg``: the encode writes the partial file and reports
    ``encoded`` (an input error line or ``None``), the probe reports ``frames``."""

    def __init__(self, frames: int | None, input_error: str | None = None) -> None:
        self.frames = frames
        self.input_error = input_error
        self.probed: list[Path] = []

    def __call__(self, cmd, cwd, duration_ms, progress, cancel):
        run = SimpleNamespace(returncode=0, frames=None, input_error=None, stderr_tail=[])
        if cmd[-3:] == ["-f", "null", "-"]:
            self.probed.append(Path(cmd[cmd.index("-i") + 1]))
            assert self.probed[-1].read_bytes() == b"new video"
            run.frames = self.frames
        else:
            Path(cmd[-1]).write_bytes(b"new video")
            run.input_error = self.input_error
        return run


def _export_with(runs: FakeRuns, recording: SessionPaths, output: Path, monkeypatch) -> None:
    monkeypatch.setattr(ffmpeg_export, "ensure_ffmpeg", lambda config: Path("ffmpeg"))
    monkeypatch.setattr(ffmpeg_export, "_run_ffmpeg", runs)
    output.write_bytes(b"previous export")
    export_recording(
        ExportRequest(recording, output, write_srt=False),
        Config(recording_folder=recording.recording_folder),
    )


def test_export_checks_the_written_video_before_it_replaces_the_output(
    recording: SessionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = tmp_path / "clip.mp4"
    partial = output.with_name("clip.part.mp4")

    # 2000 ms at 24 fps are 48 frames; 45 is the least that passes.
    for frames in (48, 45):
        runs = FakeRuns(frames)
        _export_with(runs, recording, output, monkeypatch)
        assert runs.probed == [partial]  # the partial file, before it got the name
        assert output.read_bytes() == b"new video" and not partial.exists()

    # ffmpeg exited with 0, but the video stream stops half way.
    for frames in (44, 24, 0):
        with pytest.raises(ExportError) as info:
            _export_with(FakeRuns(frames), recording, output, monkeypatch)
        assert info.value.code == "OUTPUT_TRUNCATED"
        assert f"holds {frames} frames" in str(info.value)
        assert output.read_bytes() == b"previous export" and not partial.exists()

    # The file cannot be read back at all.
    with pytest.raises(ExportError) as info:
        _export_with(FakeRuns(None), recording, output, monkeypatch)
    assert info.value.code == "OUTPUT_UNVERIFIED"
    assert output.read_bytes() == b"previous export" and not partial.exists()

    # ffmpeg said that it could not read an input: no need to count.
    runs = FakeRuns(48, input_error="Impossible to open 'x.png'\nError during demuxing: nope")
    with pytest.raises(ExportError) as info:
        _export_with(runs, recording, output, monkeypatch)
    assert info.value.code == "OUTPUT_TRUNCATED" and "Impossible to open" in str(info.value)
    assert runs.probed == []
    assert output.read_bytes() == b"previous export" and not partial.exists()


def test_export_with_only_empty_frames(
    recording: SessionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for t in TIMES:
        (recording.frames_dir() / frame_filename(t, "screen1", "png")).write_bytes(b"")
    with pytest.raises(ExportError) as info:
        _export_with(FakeRuns(48), recording, tmp_path / "none.mp4", monkeypatch)
    assert info.value.code == "NO_FRAMES" and "empty" in str(info.value)


# CLI and MCP ------------------------------------------------------------------


@pytest.fixture
def config_file(recording: SessionPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {"recording_folder": str(recording.recording_folder), "export_preset": "ultrafast"}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIDEO_STORY_CONFIG", str(path))
    return path


def test_cli_export(
    tools: tuple[Path, Path], recording: SessionPaths, config_file: Path, tmp_path: Path, capsys
):
    code = cli.main(
        [
            "--config",
            str(config_file),
            "export",
            "--session",
            recording.session_id,
            "--start-ms",
            "250",
            "--end-ms",
            "1750",
            "--output",
            str(tmp_path / "cli" / "clip.mp4"),
            "--no-cursor",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    data = json.loads(captured.out)
    assert data["ok"] is True and data["duration_ms"] == 1500 and data["frames_used"] == 4
    assert Path(data["output"]).is_file() and Path(data["srt"]).is_file()

    # Default output path and error mapping.
    code = cli.main(
        [
            "--config",
            str(config_file),
            "export",
            "--session",
            recording.session_id,
            "--screen",
            "screen7",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert json.loads(captured.err)["code"] == "UNKNOWN_SCREEN"
    args = cli.build_parser().parse_args(
        [
            "--config",
            str(config_file),
            "export",
            "--session",
            recording.session_id,
            "--end-ms",
            "500",
            "--no-srt",
        ]
    )
    result = cli.cmd_export(args)
    expected = recording.recording_folder / f"{recording.session_id}_screen1.mp4"
    assert Path(result["output"]) == expected.resolve() and result["srt"] is None
    assert expected.is_file()


def test_mcp_export_tool(
    tools: tuple[Path, Path], recording: SessionPaths, config_file: Path, tmp_path: Path
):
    reports: list[tuple[float, str]] = []
    result = mcp_server.tool_export(
        recording.session_id,
        output=str(tmp_path / "mcp" / "clip.mp4"),
        start_ms=250,
        end_ms=1750,
        draw_cursor=False,
        config_file=config_file,
        progress=lambda f, t: reports.append((f, t)),
    )
    assert result["ok"] is True and result["duration_ms"] == 1500
    assert Path(result["output"]).is_file() and Path(result["srt"]).is_file()
    assert reports and reports[-1][0] == 1.0

    default = mcp_server.tool_export(
        recording.session_id, end_ms=300, write_srt=False, config_file=config_file
    )
    assert default["ok"] is True
    assert (
        Path(default["output"])
        == (recording.recording_folder / f"{SESSION_ID}_screen1.mp4").resolve()
    )

    # A relative output is resolved against the recording folder, ".mp4" is appended.
    relative = mcp_server.tool_export(
        recording.session_id,
        output="sub/clip",
        end_ms=300,
        draw_cursor=False,
        config_file=config_file,
    )
    assert relative["ok"] is True
    assert Path(relative["output"]) == (recording.recording_folder / "sub" / "clip.mp4").resolve()
    assert Path(relative["output"]).is_file()

    # An output path under an existing file is reported, not raised.
    (recording.recording_folder / "afile").write_bytes(b"x")
    blocked = mcp_server.tool_export(
        recording.session_id, output="afile/clip.mp4", end_ms=300, config_file=config_file
    )
    assert blocked["ok"] is False and blocked["code"] == "OUTPUT_ERROR"
    assert "afile" in blocked["message"]

    bad = mcp_server.tool_export(recording.session_id, screen="screen9", config_file=config_file)
    assert bad["ok"] is False and bad["code"] == "UNKNOWN_SCREEN"
    unknown = mcp_server.tool_export("20990101-000000", config_file=config_file)
    assert unknown["ok"] is False and unknown["code"] == "NO_MANIFEST"

    server = mcp_server.create_server(config_file)
    called = anyio.run(
        server.call_tool,
        "video_story_export",
        {
            "session_id": recording.session_id,
            "end_ms": 300,
            "draw_cursor": False,
            "output": str(tmp_path / "srv.mp4"),
        },
    )
    assert called.structured_content["ok"] is True
    assert Path(called.structured_content["output"]).is_file()
