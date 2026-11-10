"""Build and run the ffmpeg command that renders a recording to MP4 (+ SRT).

The video is a slideshow of the screen's frames fed through the concat demuxer, turned
into constant 24 fps, with the cursor pointer drawn by an ``overlay`` filter whose
``x``/``y`` expressions follow the recorded cursor per frame (see
:func:`video_story.export.plan.cursor_expressions`). Audio is the recording's WAV
cropped to the range and padded to the exact video length. Progress comes from
``-progress pipe:1``; a cancel check is polled while ffmpeg runs.
"""

from __future__ import annotations

import collections
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from video_story.core.config import EXPORT_FPS, Config
from video_story.core.manifest import read_manifest
from video_story.core.screens import find_screen
from video_story.core.transcript import read_transcript
from video_story.host.runner import FrameIndex
from video_story.transcribe.ffmpeg import FfmpegNotFound, _creation_flags, ensure_ffmpeg

from . import CancelCheck, ExportError, ExportRequest, ExportResult, ProgressCallback
from .cursor import CURSOR_HOTSPOT, write_cursor_png
from .plan import (
    PlannedFrame,
    concat_list_text,
    cursor_expressions,
    cursor_keyframes,
    plan_frames,
    sidecar_cursor_reader,
)
from .srt import write_srt

log = logging.getLogger(__name__)

CANCEL_POLL_S = 0.2
STDERR_TAIL_LINES = 30
FRAMES_LIST_NAME = "frames.txt"
CURSOR_PNG_NAME = "cursor.png"
FILTER_SCRIPT_NAME = "filters.txt"
OUTPUT_SUFFIX = ".mp4"
# ffmpeg renders into ``<stem>.part.mp4`` next to the output; the file is moved onto
# the final name only when the encode succeeded, so a cancelled or failed export never
# destroys an earlier MP4 at the same path and no half-written file carries its name.
PARTIAL_INFIX = ".part"
# Longer filter graphs (cursor tracks of long recordings) go through a script file so
# the command line stays well below the Windows limit of 32 767 characters.
FILTER_INLINE_MAX_CHARS = 12_000


def _even(value: int) -> int:
    return max(2, value - value % 2)


def filter_graph(cursor: tuple[str, str] | None) -> str:
    """The ``-filter_complex`` graph: input 0 is the frame slideshow, input 1 (only with
    a cursor) the pointer PNG. Output is constant ``EXPORT_FPS`` with even dimensions."""
    graph = f"[0:v]fps={EXPORT_FPS},scale=trunc(iw/2)*2:trunc(ih/2)*2"
    if cursor is not None:
        x_expr, y_expr = cursor
        graph += f"[v0];[v0][1:v]overlay=x='{x_expr}':y='{y_expr}':eval=frame:format=auto"
    return graph + ",format=yuv420p[vout]"


def build_command(
    ffmpeg: Path,
    config: Config,
    output: Path,
    duration_ms: int,
    start_ms: int,
    audio: Path | None,
    draw_cursor: bool,
    graph: str,
    graph_file: str | None = None,
) -> list[str]:
    """The ffmpeg argv; ``frames.txt``, ``cursor.png`` and the filter script are relative
    to the working directory the process is started in (the temporary directory).
    ``output`` is the file ffmpeg writes (the partial file); the container is forced
    with ``-f mp4`` so its name does not matter."""
    cmd = [
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-protocol_whitelist",
        "file,pipe",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        FRAMES_LIST_NAME,
    ]
    if draw_cursor:
        cmd += ["-i", CURSOR_PNG_NAME]
    if audio is not None:
        cmd += [
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-i",
            str(audio),
        ]
    if graph_file is not None:
        cmd += ["-filter_complex_script", graph_file]
    else:
        cmd += ["-filter_complex", graph]
    cmd += ["-map", "[vout]"]
    if audio is not None:
        audio_index = 2 if draw_cursor else 1
        cmd += [
            "-map",
            f"{audio_index}:a",
            "-c:a",
            "aac",
            "-b:a",
            f"{config.export_audio_bitrate_k}k",
            "-af",
            "apad",
        ]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        config.export_preset,
        "-crf",
        str(config.export_crf),
        "-r",
        str(EXPORT_FPS),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]
    return cmd


def _describe_command(cmd: list[str], limit: int = 600) -> str:
    """The command line for the log, with an overlong filter graph shortened."""
    shown = [arg if len(arg) <= limit else f"{arg[:limit]}...({len(arg)} chars)" for arg in cmd]
    return subprocess.list2cmdline(shown)


def _parse_progress_line(line: str) -> int | None:
    """Output time in milliseconds from one ``-progress`` line, or ``None``.

    ffmpeg reports ``out_time_us`` and ``out_time_ms``; both hold microseconds.
    """
    key, sep, value = line.strip().partition("=")
    if not sep or key not in ("out_time_us", "out_time_ms"):
        return None
    try:
        return max(0, int(value) // 1000)
    except ValueError:
        return None


class _FfmpegRun:
    """A running ffmpeg process with its progress and stderr tail read by threads."""

    def __init__(self, cmd: list[str], cwd: Path) -> None:
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            raise ExportError("FFMPEG_FAILED", f"cannot start ffmpeg {cmd[0]}: {exc}") from exc
        self.out_time_ms = 0
        self.stderr_tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self._threads = [
            threading.Thread(target=self._read_progress, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _read_progress(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            value = _parse_progress_line(raw.decode("utf-8", "replace"))
            if value is not None:
                self.out_time_ms = value

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail.append(line)

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=5)


def normalise_output(output: Path) -> Path:
    """Absolute output path ending in ``.mp4`` (appended when the suffix differs,
    case-insensitively); the SRT name is derived from this."""
    output = output.expanduser().resolve()
    if output.suffix.lower() != OUTPUT_SUFFIX:
        output = output.with_name(output.name + OUTPUT_SUFFIX)
    return output


def partial_output(output: Path) -> Path:
    """The sibling file ffmpeg renders into before it is moved onto ``output``."""
    return output.with_name(f"{output.stem}{PARTIAL_INFIX}{output.suffix}")


def _remove_file(path: Path) -> None:
    """Delete ``path`` if it exists; retries briefly (Windows may still hold it)."""
    for _ in range(10):
        try:
            if path.exists():
                path.unlink()
            return
        except OSError:
            time.sleep(0.1)
    log.warning("could not remove %s", path)


def _run_ffmpeg(
    cmd: list[str],
    cwd: Path,
    duration_ms: int,
    progress: ProgressCallback | None,
    cancel: CancelCheck | None,
) -> None:
    """Run ffmpeg to completion; the caller owns (and removes) the partial output."""
    run = _FfmpegRun(cmd, cwd)
    reported = -1
    try:
        while True:
            if cancel is not None and cancel():
                log.info("export cancelled, stopping ffmpeg")
                raise ExportError("CANCELLED", "export cancelled")
            code = run.wait(CANCEL_POLL_S)
            if progress is not None and run.out_time_ms != reported:
                reported = run.out_time_ms
                fraction = min(1.0, reported / duration_ms) if duration_ms > 0 else 0.0
                progress(fraction, f"encoding {reported / 1000:.1f} / {duration_ms / 1000:.1f} s")
            if code is not None:
                break
    except BaseException:
        run.kill()
        run.join()
        raise
    run.join()
    if code != 0:
        tail = "\n".join(run.stderr_tail)
        raise ExportError("FFMPEG_FAILED", f"ffmpeg exited with code {code}:\n{tail}")


def _output_error(action: str, path: Path, exc: OSError) -> ExportError:
    return ExportError("OUTPUT_ERROR", f"cannot {action} {path}: {exc}")


def _recording_duration_ms(recorded_duration_ms: int, capture_interval_ms: int, names: list) -> int:
    if recorded_duration_ms > 0:
        return recorded_duration_ms
    return names[-1].time_ms + max(1, capture_interval_ms)


def export_recording(
    request: ExportRequest,
    config: Config,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> ExportResult:
    """Render ``request`` to an MP4 (and an SRT next to it); see :mod:`video_story.export`.

    The output name is normalised with :func:`normalise_output` (``.mp4`` appended when
    missing). ffmpeg writes a ``<stem>.part.mp4`` sibling that replaces the output only
    on success; on cancel, failure or any exception only that partial file is removed.
    When no SRT is written, a stale ``.srt`` next to the output from an earlier export
    is removed. File-system failures raise ``ExportError`` with code ``OUTPUT_ERROR``.
    """
    started = time.monotonic()
    try:
        ffmpeg = ensure_ffmpeg(config)
    except FfmpegNotFound as exc:
        raise ExportError("FFMPEG_MISSING", str(exc)) from exc

    paths = request.paths
    base = paths.current_dir
    manifest_path = paths.manifest_path(base)
    if not manifest_path.exists():
        raise ExportError("NO_MANIFEST", f"no manifest.json in {base}")
    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError, KeyError) as exc:
        raise ExportError("NO_MANIFEST", f"cannot read {manifest_path}: {exc}") from exc
    screen = find_screen(manifest.screens, request.screen_id)
    if screen is None:
        available = ", ".join(s.id for s in manifest.screens)
        raise ExportError(
            "UNKNOWN_SCREEN", f"unknown screen {request.screen_id!r}; available: {available}"
        )

    frames_dir = paths.frames_dir(base)
    if not frames_dir.is_dir():
        raise ExportError("NO_FRAMES", f"no frames directory in {base}")
    index = FrameIndex.of(paths, base)
    names = [n for n in index.by_screen.get(screen.id, []) if not n.is_sidecar]
    if not names:
        raise ExportError("NO_FRAMES", f"no frames of {screen.id} in {frames_dir}")

    total_ms = _recording_duration_ms(
        manifest.recorded_duration_ms, manifest.capture_interval_ms, names
    )
    start_ms = int(request.start_ms)
    end_ms = total_ms if request.end_ms is None else min(int(request.end_ms), total_ms)
    if start_ms < 0:
        raise ExportError("INVALID_RANGE", "start must not be negative")
    planned: list[PlannedFrame] = plan_frames(names, start_ms, end_ms)
    duration_ms = end_ms - start_ms

    audio = paths.audio_path(base)
    audio_input = audio if audio.is_file() else None
    output = normalise_output(request.output)
    partial = partial_output(output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _output_error("create the output folder of", output, exc) from exc
    width, height = _even(screen.image_width), _even(screen.image_height)
    log.info(
        "exporting session %s %s [%d, %d] ms (%d frames, %s audio) -> %s",
        paths.session_id,
        screen.id,
        start_ms,
        end_ms,
        len(planned),
        "with" if audio_input else "without",
        output,
    )

    with tempfile.TemporaryDirectory(prefix="video-story-export-") as tmp:
        work = Path(tmp)
        (work / FRAMES_LIST_NAME).write_text(
            concat_list_text(frames_dir, planned), encoding="utf-8", newline="\n"
        )
        cursor: tuple[str, str] | None = None
        if request.draw_cursor:
            write_cursor_png(work / CURSOR_PNG_NAME)
            keyframes = cursor_keyframes(
                planned, sidecar_cursor_reader(index.info_dir), screen.id, CURSOR_HOTSPOT
            )
            cursor = cursor_expressions(keyframes)
        graph = filter_graph(cursor)
        graph_file: str | None = None
        if len(graph) > FILTER_INLINE_MAX_CHARS:
            graph_file = FILTER_SCRIPT_NAME
            (work / graph_file).write_text(graph, encoding="utf-8", newline="\n")
        cmd = build_command(
            ffmpeg,
            config,
            partial,
            duration_ms,
            start_ms,
            audio_input,
            request.draw_cursor,
            graph,
            graph_file,
        )
        log.info("ffmpeg command: %s", _describe_command(cmd))
        if graph_file is not None:
            log.info("filter graph (%d chars) in %s", len(graph), work / graph_file)
        if progress is not None:
            progress(0.0, "starting ffmpeg")
        try:
            _run_ffmpeg(cmd, work, duration_ms, progress, cancel)
            if not partial.is_file():
                raise ExportError("FFMPEG_FAILED", f"ffmpeg produced no output at {partial}")
            try:
                os.replace(partial, output)
            except OSError as exc:
                raise _output_error("write", output, exc) from exc
        finally:
            _remove_file(partial)

    srt: Path | None = None
    srt_path = output.with_suffix(".srt")
    transcript_path = paths.transcript_json_path(base)
    if request.write_srt and transcript_path.is_file():
        try:
            transcript = read_transcript(transcript_path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("cannot read %s, no subtitles written: %s", transcript_path, exc)
        else:
            try:
                srt = write_srt(srt_path, transcript, start_ms, end_ms)
            except OSError as exc:
                raise _output_error("write", srt_path, exc) from exc
    if srt is None and srt_path.is_file():
        log.info("removing stale subtitles %s (no SRT written for this export)", srt_path)
        try:
            srt_path.unlink()
        except OSError as exc:
            raise _output_error("remove stale subtitles", srt_path, exc) from exc
    if progress is not None:
        progress(1.0, "done")
    log.info(
        "export finished in %.1f s: %s (%d bytes)%s",
        time.monotonic() - started,
        output,
        os.path.getsize(output),
        f", subtitles {srt}" if srt else "",
    )
    return ExportResult(
        output=output,
        srt=srt,
        screen_id=screen.id,
        start_ms=start_ms,
        end_ms=end_ms,
        duration_ms=duration_ms,
        width=width,
        height=height,
        fps=EXPORT_FPS,
        frames_used=len(planned),
        ffmpeg=str(ffmpeg),
        command=cmd,
    )


__all__ = [
    "build_command",
    "export_recording",
    "filter_graph",
    "normalise_output",
    "partial_output",
]
