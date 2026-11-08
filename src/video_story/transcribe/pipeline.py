"""Offline transcription of a recorded session: ffmpeg -> whisper -> transcript files.

:func:`transcribe_recording` blocks for the whole run (roughly a quarter of the audio
duration with ``ggml-small`` on a modern CPU); callers with a UI must run it in a
worker thread and feed ``progress`` into their status display.
"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from video_story.core.config import Config
from video_story.core.manifest import TranscriptInfo
from video_story.core.session import SessionPaths
from video_story.core.timeline import format_timestamp
from video_story.core.transcript import (
    Segment,
    Transcript,
    normalize_segments,
    write_transcript,
)

from .ffmpeg import convert_to_16k_mono, ensure_ffmpeg
from .whisper_backend import TranscriptionBackend, create_backend, wav_duration_ms

log = logging.getLogger(__name__)

PROGRESS_CONVERT_END = 0.05
PROGRESS_TRANSCRIBE_END = 0.98

TRANSCRIBING_MESSAGE = "Transcribing audio, this may take a while"
SEGMENT_PREVIEW_CHARS = 120  # the UI elides further to its width

ProgressCallback = Callable[[float, str], None]


def transcribing_message(last: Segment | None) -> str:
    """Progress text while whisper runs: a fixed first line, then (once whisper has
    produced a segment) ``HH:MM:SS  text`` of the latest segment on a second line, so
    the user can see roughly how far the transcription got."""
    if last is None:
        return TRANSCRIBING_MESSAGE
    text = " ".join(last.text.split())
    if len(text) > SEGMENT_PREVIEW_CHARS:
        text = text[:SEGMENT_PREVIEW_CHARS].rstrip()
    return f"{TRANSCRIBING_MESSAGE}\n{format_timestamp(last.start_ms)[:-4]}  {text}"


@dataclass
class TranscriptionResult:
    transcript: Transcript
    info: TranscriptInfo
    audio_duration_ms: int


def _audio_frames(path: Path) -> int | None:
    """Number of frames in a WAV, ``None`` when the file cannot be parsed as WAV."""
    try:
        with wave.open(str(path), "rb") as wf:
            return wf.getnframes()
    except (OSError, wave.Error, EOFError):
        return None


def transcribe_recording(
    paths: SessionPaths,
    config: Config,
    *,
    base_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    backend: TranscriptionBackend | None = None,
) -> TranscriptionResult:
    """Transcribe ``audio.wav`` in ``base_dir`` and write ``transcript.txt``/``.json``.

    ``base_dir`` defaults to the session work directory (the UI transcribes before
    finalising); the ``transcribe DIR`` command passes the final directory. The
    intermediate ``audio16k.wav`` is removed afterwards. A silent or empty recording
    yields a transcript with zero segments, not an error.
    """
    base = Path(base_dir) if base_dir is not None else paths.work_dir
    audio = paths.audio_path(base)
    audio16k = paths.audio_16k_path(base)
    txt_path = paths.transcript_txt_path(base)
    json_path = paths.transcript_json_path(base)

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), message)

    started = time.monotonic()
    model_path = config.resolved_model_path()
    backend = backend or create_backend(config)
    transcript = Transcript(language=None, segments=[], model=model_path.stem, backend=backend.name)
    audio_duration_ms = 0

    report(0.0, "Preparing audio")
    if not audio.is_file():
        raise FileNotFoundError(f"audio file not found: {audio}")
    frames = _audio_frames(audio)
    if frames == 0:
        log.warning("Recording %s contains no audio samples; writing an empty transcript", audio)
    else:
        ffmpeg = ensure_ffmpeg(config, download=False)
        convert_to_16k_mono(ffmpeg, audio, audio16k)
        try:
            audio_duration_ms = wav_duration_ms(audio16k)
            report(PROGRESS_CONVERT_END, transcribing_message(None))
            span = PROGRESS_TRANSCRIBE_END - PROGRESS_CONVERT_END
            last_segment: list[Segment] = []  # latest segment whisper produced

            def on_backend_segment(segment: Segment) -> None:
                if segment.text.strip():
                    last_segment[:] = [segment]

            def on_backend_progress(fraction: float) -> None:
                last = last_segment[0] if last_segment else None
                report(PROGRESS_CONVERT_END + span * fraction, transcribing_message(last))

            transcript = backend.transcribe(
                audio16k,
                language="auto",
                threads=config.whisper_threads,
                progress=on_backend_progress if progress is not None else None,
                on_segment=on_backend_segment if progress is not None else None,
            )
        finally:
            try:
                audio16k.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - best effort cleanup
                log.warning("cannot delete %s: %s", audio16k, exc)

    report(PROGRESS_TRANSCRIBE_END, "Writing transcript")
    transcript.segments = normalize_segments(transcript.segments)
    transcript.model = transcript.model or model_path.stem
    transcript.backend = transcript.backend or backend.name
    write_transcript(txt_path, json_path, transcript)
    elapsed = time.monotonic() - started
    info = TranscriptInfo(
        txt=txt_path.name,
        json_file=json_path.name,
        language=transcript.language,
        model=model_path.stem,
        backend=backend.name,
        segments=len(transcript.segments),
        duration_s=round(elapsed, 3),
    )
    log.info(
        "Transcribed %d ms of audio into %d segments (%s) in %.1f s with %s",
        audio_duration_ms,
        len(transcript.segments),
        transcript.language,
        elapsed,
        backend.name,
    )
    report(1.0, "Done")
    return TranscriptionResult(
        transcript=transcript, info=info, audio_duration_ms=audio_duration_ms
    )


__all__ = [
    "PROGRESS_CONVERT_END",
    "PROGRESS_TRANSCRIBE_END",
    "ProgressCallback",
    "TranscriptionResult",
    "transcribe_recording",
]
