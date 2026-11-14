"""The worker process of the live transcription (``video-story transcribe-live``).

The recording window starts it at Record (see
:mod:`video_story.host.live_transcription`). It lowers its own priority, loads the
whisper model once, then transcribes the growing audio of the work directory window by
window (:class:`~video_story.transcribe.live.LiveCore`) and reports one JSON event per
line on stdout. It writes nothing: the window stores the segments, so killing the
worker at Stop never leaves a half-written file behind.

Events: ``start`` (``mode``, ``threads``, ``priority``, ``from_ms``), ``ready``
(``load_seconds``), ``committed`` (``segments``, ``transcribed_ms``, ``language``,
``start_ms``, ``end_ms``, ``cut``, ``speech``, ``seconds``, ``threads``).

Commands on stdin, one per line: ``threads N`` (the next windows use N threads) and
``stop``. When stdin closes - the window ended, also by a crash - the process exits at
once, even in the middle of a window.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.core.transcript import Transcript, read_transcript
from video_story.platform.process import lower_own_priority

from .ffmpeg import ensure_ffmpeg, resample_to_16k
from .growing_wav import GrowingMix
from .live import LiveCore, LiveParams, Resampler, SampleTranscriber, live_threads
from .whisper_backend import PyWhisperCppBackend

log = logging.getLogger(__name__)

EventCallback = Callable[[dict[str, Any]], None]


def read_resume_state(path: Path) -> Transcript | None:
    """The transcript stored so far (a restarted worker continues after it)."""
    if not path.exists():
        return None
    try:
        return read_transcript(path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return None


class _Commands:
    """Reads the command lines on a thread; EOF calls ``on_eof``."""

    def __init__(self, stream: IO[str] | None, on_eof: Callable[[], None]) -> None:
        self.queue: queue.Queue[str] = queue.Queue()
        self.stopped = threading.Event()
        self._on_eof = on_eof
        if stream is not None:
            threading.Thread(
                target=self._read, args=(stream,), name="live-commands", daemon=True
            ).start()

    def _read(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                command = line.strip()
                if command == "stop":
                    break
                if command:
                    self.queue.put(command)
        except (OSError, ValueError):
            pass
        self.stopped.set()
        self._on_eof()


def run_live_worker(
    paths: SessionPaths,
    config: Config,
    mode: str,
    *,
    emit: EventCallback,
    stdin: IO[str] | None = None,
    on_eof: Callable[[], None] | None = None,
    backend: SampleTranscriber | None = None,
    resample: Resampler | None = None,
    params: LiveParams | None = None,
    threads: int | None = None,
    lower_priority: bool = True,
) -> dict[str, Any]:
    """Transcribe the growing audio of ``paths.work_dir`` until stopped.

    ``on_eof`` defaults to ending the process at once (``os._exit``); tests pass their
    own and the loop then returns after the current window.
    """
    params = params or LiveParams()
    commands = _Commands(stdin, on_eof or (lambda: os._exit(0)))
    priority = lower_own_priority(mode) if lower_priority else "unchanged"
    count = threads or live_threads(mode, os.cpu_count())
    base = paths.work_dir
    stored = read_resume_state(paths.transcript_json_path(base))
    done_ms = 0
    language = None
    texts: list[str] = []
    if stored is not None and not stored.complete:
        done_ms = stored.transcribed_ms or max((s.end_ms for s in stored.segments), default=0)
        language = stored.language
        texts = [s.text for s in stored.segments]
    emit(
        {"event": "start", "mode": mode, "threads": count, "priority": priority, "from_ms": done_ms}
    )
    if backend is None:
        whisper = PyWhisperCppBackend(config.resolved_model_path())
        started = time.monotonic()
        whisper.load(count)
        backend = whisper
        emit({"event": "ready", "load_seconds": round(time.monotonic() - started, 1)})
    else:
        emit({"event": "ready", "load_seconds": 0.0})
    if resample is None:
        ffmpeg = ensure_ffmpeg(config, download=False)

        def resample(samples, rate):  # noqa: ANN001, ANN202 - Resampler
            return resample_to_16k(ffmpeg, samples, rate)

    mix = GrowingMix(paths.audio_path(base), paths.computer_audio_path(base))
    core = LiveCore(
        mix, backend, resample, count, params, done_ms=done_ms, language=language, texts=texts
    )
    windows = 0
    while not commands.stopped.is_set():
        _apply_commands(commands, core)
        window = core.next_window(mix.available_ms(params.safety_ms))
        if window is None:
            commands.stopped.wait(params.poll_ms / 1000)
            continue
        result = core.transcribe(window)
        windows += 1
        emit(
            {
                "event": "committed",
                "segments": [s.to_dict() for s in result.segments],
                "transcribed_ms": result.done_ms,
                "language": result.language,
                "start_ms": window.start_ms,
                "end_ms": window.end_ms,
                "cut": str(window.cut),
                "speech": result.speech,
                "seconds": round(result.seconds, 2),
                "threads": core.threads,
            }
        )
    return {"event": "stopped", "windows": windows, "transcribed_ms": core.done_ms}


def _apply_commands(commands: _Commands, core: LiveCore) -> None:
    while True:
        try:
            command = commands.queue.get_nowait()
        except queue.Empty:
            return
        name, _, value = command.partition(" ")
        if name == "threads":
            try:
                core.threads = max(1, int(value))
            except ValueError:
                continue
            log.info("live transcription now uses %d threads", core.threads)


__all__ = ["read_resume_state", "run_live_worker"]
