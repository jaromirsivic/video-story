"""Encode one audio track of a recording to AAC (``.m4a``) and check the written file.

Archiving (:mod:`video_story.export.archive`) keeps every audio track this way before
it deletes ``audio/``: the MP4 of a screen holds only the tracks that were switched on
in the editor, the ``.m4a`` files hold each track on its own, whatever was switched on.

The encoder is FFmpeg's built-in ``aac`` (the one the MP4 export uses) at
``ARCHIVE_AUDIO_BITRATE_K`` kb/s in an MP4 container; the channel count and the sample
rate of the WAV are kept. As with the video (:func:`.ffmpeg_export.verify_video`),
ffmpeg's exit code is not taken for proof: :func:`verify_audio` reads the audio stream
of the written file back and compares its length with the length of the WAV.
"""

from __future__ import annotations

import logging
from pathlib import Path

from video_story.core.audio_mix import wav_info
from video_story.core.config import Config
from video_story.transcribe.ffmpeg import FfmpegNotFound, ensure_ffmpeg

from . import CancelCheck, ExportError, ProgressCallback
from .ffmpeg_export import _describe_command, _run_ffmpeg

log = logging.getLogger(__name__)

# Every track is mono (speech, or what the computer played): 96 kb/s is the most AAC-LC
# takes for one channel at 16 kHz and plenty at 48 kHz. A constant, not a setting.
ARCHIVE_AUDIO_BITRATE_K = 96
# How much shorter than the WAV the audio stream of the written file may be (see
# :func:`verify_audio`). The lengths normally agree to the millisecond (the container
# records the encoder's priming and padding, at most a few AAC frames of 1024 samples:
# 64 ms each at 16 kHz); 150 ms leaves room for an ffmpeg that reports them differently
# and is still far below anything a listener would miss.
AUDIO_VERIFY_TOLERANCE_MS = 150


def encode_command(ffmpeg: Path, source: Path, output: Path) -> list[str]:
    """The ffmpeg argv that encodes the first audio stream of ``source`` to AAC.

    ``output`` is the file ffmpeg writes (a partial file); the container is forced with
    ``-f mp4`` so its name does not matter. No ``-ar``/``-ac``: the encoder keeps the
    channels and the sample rate of the input (ffmpeg resamples only a rate AAC does
    not have, e.g. 22 000 Hz to 22 050 Hz; the usual 16, 44.1 and 48 kHz stay)."""
    return [
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        f"{ARCHIVE_AUDIO_BITRATE_K}k",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]


def audio_probe_command(ffmpeg: Path, audio: Path) -> list[str]:
    """The ffmpeg argv that reads the first audio stream of ``audio`` to the end without
    decoding it (stream copy into the null muxer); ``-progress`` then reports the time
    the stream reaches. Like :func:`.ffmpeg_export.probe_command`, it needs no
    ``ffprobe``."""
    return [
        str(ffmpeg),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(audio),
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-f",
        "null",
        "-",
    ]


def wav_duration_ms(path: Path) -> int:
    """Length of the WAV ``path`` in milliseconds, from its header (frames / rate).

    Raises ``ExportError`` ``AUDIO_UNREADABLE`` when the header cannot be read."""
    info = wav_info(path)
    if info is None or info.sample_rate <= 0:
        raise ExportError("AUDIO_UNREADABLE", f"cannot read the audio track {path}")
    return info.frames * 1000 // info.sample_rate


def _ffmpeg(config: Config) -> Path:
    try:
        return ensure_ffmpeg(config)
    except FfmpegNotFound as exc:
        raise ExportError("FFMPEG_MISSING", str(exc)) from exc


def encode_audio(
    source: Path,
    output: Path,
    config: Config,
    progress: ProgressCallback | None = None,
    cancel: CancelCheck | None = None,
) -> None:
    """Encode the WAV ``source`` to AAC in the MP4 file ``output`` (overwritten).

    The caller owns ``output``: it should be a partial name that gets the final one
    only after :func:`verify_audio` accepted it.

    Raises ``ExportError``: ``AUDIO_UNREADABLE`` (no WAV header), ``FFMPEG_FAILED``,
    ``OUTPUT_TRUNCATED`` (ffmpeg could not read the WAV to its end), ``FFMPEG_MISSING``,
    ``CANCELLED``.
    """
    ffmpeg = _ffmpeg(config)
    duration_ms = wav_duration_ms(source)
    cmd = encode_command(ffmpeg, source, output)
    log.info("ffmpeg command: %s", _describe_command(cmd))
    run = _run_ffmpeg(cmd, output.parent, duration_ms, progress, cancel)
    if run.input_error is not None:
        raise ExportError(
            "OUTPUT_TRUNCATED",
            f"ffmpeg could not read {source.name} to its end:\n{run.input_error}",
        )
    if not output.is_file():
        raise ExportError("FFMPEG_FAILED", f"ffmpeg produced no output at {output}")


def verify_audio(
    audio: Path, duration_ms: int, config: Config, cancel: CancelCheck | None = None
) -> int:
    """Check that the audio stream of ``audio`` is as long as ``duration_ms``.

    The stream is read to its end by ffmpeg itself (:func:`audio_probe_command`); the
    time it reaches may be at most ``AUDIO_VERIFY_TOLERANCE_MS`` short of
    ``duration_ms``. Returns that time in milliseconds.

    Raises ``ExportError``: ``OUTPUT_TRUNCATED`` when the stream is shorter,
    ``OUTPUT_UNVERIFIED`` when the file cannot be read back (no audio stream, damaged),
    ``FFMPEG_MISSING`` without ffmpeg, ``CANCELLED`` when ``cancel`` said so.
    """
    ffmpeg = _ffmpeg(config)
    try:
        run = _run_ffmpeg(audio_probe_command(ffmpeg, audio), audio.parent, 0, None, cancel)
    except ExportError as exc:
        if exc.code == "CANCELLED":
            raise
        raise ExportError("OUTPUT_UNVERIFIED", f"cannot read {audio} back: {exc}") from exc
    if run.input_error is not None:
        detail = "\n".join(run.stderr_tail) or run.input_error
        raise ExportError("OUTPUT_UNVERIFIED", f"cannot read {audio} to its end: {detail}")
    if run.out_time_ms < duration_ms - AUDIO_VERIFY_TOLERANCE_MS:
        raise ExportError(
            "OUTPUT_TRUNCATED",
            f"the audio is cut short: it holds {run.out_time_ms / 1000:.3f} s "
            f"instead of {duration_ms / 1000:.3f} s",
        )
    return run.out_time_ms


__all__ = [
    "ARCHIVE_AUDIO_BITRATE_K",
    "AUDIO_VERIFY_TOLERANCE_MS",
    "audio_probe_command",
    "encode_audio",
    "encode_command",
    "verify_audio",
    "wav_duration_ms",
]
