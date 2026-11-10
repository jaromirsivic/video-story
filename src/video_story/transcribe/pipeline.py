"""Offline transcription of a recorded session: ffmpeg -> whisper -> transcript files.

:func:`transcribe_audio` runs whisper over the audio of a recording - the mix of the
tracks switched on in the editor (the microphone and the computer's sound, see
:mod:`video_story.core.audio_mix`) - optionally from ``start_ms`` on, which is how the
editor resumes an interrupted transcription, and returns the
segments; :func:`transcribe_recording` does that for the whole audio and writes
``transcript.txt``/``transcript.json``; :func:`stream_transcription` reports every
segment as a JSON event the moment whisper produces it (the ``transcribe-stream``
command the editor runs in a child process). All of them block for the whole run
(roughly a third of the audio duration with ``large-v3-turbo`` on a modern CPU);
callers with a UI run them off the GUI thread.
"""

from __future__ import annotations

import logging
import shutil
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_story.core.audio_mix import existing_tracks, selected_sources
from video_story.core.config import Config
from video_story.core.manifest import TranscriptInfo
from video_story.core.session import SessionPaths
from video_story.core.timeline import format_timestamp
from video_story.core.transcript import (
    Segment,
    Transcript,
    drop_credit_hallucinations,
    is_credit_hallucination,
    normalize_segments,
    write_transcript,
)

from .ffmpeg import convert_to_16k_mono, ensure_ffmpeg, find_ffmpeg
from .whisper_backend import (
    WHISPER_SAMPLE_RATE,
    TranscriptionBackend,
    create_backend,
    wav_duration_ms,
)

log = logging.getLogger(__name__)

PROGRESS_CONVERT_END = 0.05
PROGRESS_TRANSCRIBE_END = 0.98

TRANSCRIBING_MESSAGE = "Transcribing audio, this may take a while"
SEGMENT_PREVIEW_CHARS = 120  # the UI elides further to its width

ProgressCallback = Callable[[float, str], None]
SegmentCallback = Callable[[Segment], None]
EventCallback = Callable[[dict[str, Any]], None]


def missing_requirements(config: Config) -> list[str]:
    """What transcribing a recording would lack right now (empty: everything is there).

    Asked before Record, so that the user learns about a missing model or FFmpeg at
    once and not after the recording. Nothing is downloaded or started here.
    """
    missing: list[str] = []
    model = config.resolved_model_path()
    if not model.is_file():
        missing.append(f"the Whisper model ({model})")
    if find_ffmpeg(config) is None:
        missing.append("FFmpeg")
    if config.whisper_backend == "whisper-cli" and (
        config.whisper_cli_path is None or not Path(config.whisper_cli_path).is_file()
    ):
        missing.append("the whisper-cli program (whisper_cli_path)")
    return missing


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


def trim_wav_start(path: Path, start_ms: int, chunk_frames: int = 1 << 16) -> None:
    """Rewrite the WAV at ``path`` so that it starts at ``start_ms`` (in place)."""
    if start_ms <= 0:
        return
    tmp = path.with_name(path.name + ".trim")
    with wave.open(str(path), "rb") as src:
        rate = src.getframerate() or WHISPER_SAMPLE_RATE
        total = src.getnframes()
        first = min(total, int(start_ms * rate / 1000))
        with wave.open(str(tmp), "wb") as dst:
            dst.setnchannels(src.getnchannels())
            dst.setsampwidth(src.getsampwidth())
            dst.setframerate(rate)
            src.setpos(first)
            remaining = total - first
            while remaining > 0:
                data = src.readframes(min(chunk_frames, remaining))
                if not data:
                    break
                dst.writeframes(data)
                remaining -= len(data) // (src.getnchannels() * src.getsampwidth())
    shutil.move(str(tmp), str(path))


def transcribe_audio(
    paths: SessionPaths,
    config: Config,
    *,
    base_dir: Path | None = None,
    start_ms: int = 0,
    progress: ProgressCallback | None = None,
    on_segment: SegmentCallback | None = None,
    backend: TranscriptionBackend | None = None,
) -> TranscriptionResult:
    """Transcribe the audio in ``base_dir`` from ``start_ms`` on; nothing is written.

    The audio is the mix of the tracks switched on (``audio/tracks.json``); with every
    track switched off the transcript is empty.

    Segment times are absolute (milliseconds since the recording start) even when
    ``start_ms`` is positive. ``on_segment`` receives every segment with text as soon
    as whisper produces it; hallucinated subtitle credits are dropped (see
    :func:`~video_story.core.transcript.drop_credit_hallucinations`). ``progress``
    reports ``0.0`` to :data:`PROGRESS_TRANSCRIBE_END`. The intermediate
    ``audio16k.wav`` is removed afterwards. A silent or empty recording yields a
    transcript with zero segments, not an error.
    """
    base = Path(base_dir) if base_dir is not None else paths.work_dir
    audio16k = paths.audio_16k_path(base)
    start_ms = max(0, int(start_ms))

    def report(fraction: float, message: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), message)

    started = time.monotonic()
    model_path = config.resolved_model_path()
    backend = backend or create_backend(config)
    transcript = Transcript(language=None, segments=[], model=model_path.stem, backend=backend.name)
    audio_duration_ms = 0
    streamed: list[Segment] = []

    def shift(segment: Segment) -> Segment:
        """Absolute times (``start_ms`` added), clamped like :func:`normalize_segments`
        and never past the end of the audio (whisper pads its last window)."""
        start = max(0, segment.start_ms + start_ms)
        end = max(start, segment.end_ms + start_ms)
        if audio_duration_ms > 0:
            end = max(start, min(end, audio_duration_ms))
        return Segment(start, end, segment.text.strip())

    def deliver(segment: Segment) -> None:
        """A segment whisper just produced: forwarded to ``on_segment`` unless empty or
        a hallucinated credits line."""
        absolute = shift(segment)
        if not absolute.text or is_credit_hallucination(absolute.text):
            return
        streamed.append(absolute)
        if on_segment is not None:
            on_segment(absolute)

    report(0.0, "Preparing audio")
    if not existing_tracks(paths, base):
        raise FileNotFoundError(f"audio file not found: {paths.audio_path(base)}")
    sources = [s for s in selected_sources(paths, base) if _audio_frames(s) != 0]
    if not sources:
        log.warning("Recording %s has no audio switched on (or no samples); empty transcript", base)
    else:
        ffmpeg = ensure_ffmpeg(config, download=False)
        convert_to_16k_mono(ffmpeg, sources, audio16k)
        try:
            audio_duration_ms = wav_duration_ms(audio16k)
            if start_ms > 0:
                trim_wav_start(audio16k, start_ms)
            report(PROGRESS_CONVERT_END, transcribing_message(None))
            span = PROGRESS_TRANSCRIBE_END - PROGRESS_CONVERT_END
            last_segment: list[Segment] = []  # latest segment whisper produced
            wants_feedback = progress is not None or on_segment is not None

            def on_backend_segment(segment: Segment) -> None:
                deliver(segment)
                if streamed:
                    last_segment[:] = [streamed[-1]]

            def on_backend_progress(fraction: float) -> None:
                last = last_segment[0] if last_segment else None
                report(PROGRESS_CONVERT_END + span * fraction, transcribing_message(last))

            if start_ms < audio_duration_ms or audio_duration_ms == 0:
                transcript = backend.transcribe(
                    audio16k,
                    language="auto",
                    threads=config.whisper_threads,
                    progress=on_backend_progress if progress is not None else None,
                    on_segment=on_backend_segment if wants_feedback else None,
                )
            else:
                log.info("nothing left to transcribe after %d ms", start_ms)
        finally:
            try:
                audio16k.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - best effort cleanup
                log.warning("cannot delete %s: %s", audio16k, exc)

    # The list whisper returned is authoritative; segments it did not stream (whisper-cli
    # reports only at the end) are delivered now so that listeners see every segment.
    returned = drop_credit_hallucinations(
        normalize_segments([shift(s) for s in transcript.segments])
    )
    if on_segment is not None:
        seen = set(streamed)
        for segment in returned:
            if segment not in seen:
                on_segment(segment)
    transcript.segments = returned
    transcript.model = transcript.model or model_path.stem
    transcript.backend = transcript.backend or backend.name
    elapsed = time.monotonic() - started
    info = TranscriptInfo(
        txt=paths.transcript_txt_path(base).name,
        json_file=paths.transcript_json_path(base).name,
        language=transcript.language,
        model=model_path.stem,
        backend=backend.name,
        segments=len(transcript.segments),
        duration_s=round(elapsed, 3),
    )
    log.info(
        "Transcribed %d ms of audio (from %d ms) into %d segments (%s) in %.1f s with %s",
        audio_duration_ms,
        start_ms,
        len(transcript.segments),
        transcript.language,
        elapsed,
        backend.name,
    )
    return TranscriptionResult(
        transcript=transcript, info=info, audio_duration_ms=audio_duration_ms
    )


def transcribe_recording(
    paths: SessionPaths,
    config: Config,
    *,
    base_dir: Path | None = None,
    progress: ProgressCallback | None = None,
    backend: TranscriptionBackend | None = None,
) -> TranscriptionResult:
    """Transcribe the whole audio in ``base_dir`` and write ``transcript.txt``/``.json``.

    ``base_dir`` defaults to the session work directory; the ``transcribe DIR``
    command passes the final directory. The written transcript is complete.
    """
    base = Path(base_dir) if base_dir is not None else paths.work_dir
    result = transcribe_audio(paths, config, base_dir=base, progress=progress, backend=backend)
    if progress is not None:
        progress(PROGRESS_TRANSCRIBE_END, "Writing transcript")
    result.transcript.complete = True
    result.transcript.transcribed_ms = None
    write_transcript(
        paths.transcript_txt_path(base), paths.transcript_json_path(base), result.transcript
    )
    if progress is not None:
        progress(1.0, "Done")
    return result


def stream_transcription(
    paths: SessionPaths,
    config: Config,
    *,
    base_dir: Path | None = None,
    start_ms: int = 0,
    emit: EventCallback,
    backend: TranscriptionBackend | None = None,
) -> dict[str, Any]:
    """Transcribe from ``start_ms`` on, reporting events; returns the ``done`` event.

    Events (dicts with an ``event`` key): ``start`` (``start_ms``), ``audio``
    (``duration_ms``), ``segment`` (``start_ms``, ``end_ms``, ``text``), ``progress``
    (``fraction`` 0..1, ``transcribed_ms``: everything up to there is transcribed) and
    finally the returned ``done`` (``language``, ``model``, ``backend``,
    ``audio_duration_ms``, ``segments``, ``seconds``). Nothing is written: the caller
    owns the transcript files.
    """
    started = time.monotonic()
    reached = [max(0, int(start_ms))]  # end of the latest segment delivered
    emit({"event": "start", "start_ms": reached[0]})

    def on_segment(segment: Segment) -> None:
        reached[0] = max(reached[0], segment.end_ms)
        emit({"event": "segment", **segment.to_dict()})

    def on_progress(fraction: float, _message: str) -> None:
        if fraction < PROGRESS_CONVERT_END:
            return
        span = PROGRESS_TRANSCRIBE_END - PROGRESS_CONVERT_END
        whisper = max(0.0, min(1.0, (fraction - PROGRESS_CONVERT_END) / span))
        emit({"event": "progress", "fraction": whisper, "transcribed_ms": reached[0]})

    result = transcribe_audio(
        paths,
        config,
        base_dir=base_dir,
        start_ms=start_ms,
        progress=on_progress,
        on_segment=on_segment,
        backend=backend,
    )
    done = {
        "event": "done",
        "language": result.transcript.language,
        "model": result.transcript.model,
        "backend": result.transcript.backend,
        "audio_duration_ms": result.audio_duration_ms,
        "segments": len(result.transcript.segments),
        "seconds": round(time.monotonic() - started, 1),
    }
    return done


__all__ = [
    "PROGRESS_CONVERT_END",
    "PROGRESS_TRANSCRIBE_END",
    "TRANSCRIBING_MESSAGE",
    "EventCallback",
    "ProgressCallback",
    "SegmentCallback",
    "TranscriptionResult",
    "missing_requirements",
    "stream_transcription",
    "transcribe_audio",
    "transcribe_recording",
    "transcribing_message",
    "trim_wav_start",
]
