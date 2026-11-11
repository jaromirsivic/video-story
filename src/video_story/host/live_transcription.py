"""Transcription while recording, seen from the recording window.

:class:`LiveTranscriber` runs ``video-story transcribe-live <work dir>`` (see
:mod:`video_story.transcribe.live_worker`) while the recording runs, reads its events
on a thread and stores every finished window in the work directory's
``transcript.json``/``.txt`` (``complete`` false, ``transcribed_ms``). When the
recording is saved, those files move with it and the editor resumes at
``transcribed_ms``; when it is discarded, they go with it.

The worker is the only process that computes; it runs at a lowered priority and with
a part of the CPUs (Settings > Live Transcription). Nothing here may disturb the
recording: a worker that cannot start or keeps failing is logged and given up on, a
crashed one is restarted (at most :data:`MAX_RESTARTS` times) from where the stored
transcript ends. :meth:`stop` kills the worker and waits until nothing writes any more,
so the recording can be moved or deleted right afterwards. On Windows the worker sits
in a job object that kills it with the window; everywhere it also quits when its
stdin closes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from video_story.core.config import Config, config_path
from video_story.core.fileio import atomic_write_text
from video_story.core.session import SessionPaths
from video_story.core.transcript import (
    Segment,
    Transcript,
    merge_segments,
    transcript_to_text,
)
from video_story.platform.process import console_python
from video_story.transcribe.live_worker import read_resume_state

log = logging.getLogger(__name__)

MAX_RESTARTS = 2
RESTART_DELAY_S = 5.0
STOP_WAIT_S = 3.0
STDERR_TAIL_LINES = 12
# Throttle: more than this many dropped ticks within THROTTLE_WINDOW_S halves the
# threads; THROTTLE_RESTORE_S without any brings them back.
THROTTLE_DROPS = 2
THROTTLE_WINDOW_S = 10.0
THROTTLE_RESTORE_S = 120.0

CommandFactory = Callable[[SessionPaths, str], list[str]]


def worker_command(config_file: Path | None) -> CommandFactory:
    def command(paths: SessionPaths, mode: str) -> list[str]:
        return [
            console_python(),
            "-X",
            "utf8",
            "-m",
            "video_story",
            "--config",
            str(config_file or config_path()),
            "transcribe-live",
            str(paths.work_dir),
            "--mode",
            mode,
        ]

    return command


def live_transcription_available(config: Config) -> str | None:
    """Why transcription while recording cannot run (``None``: it can)."""
    if config.live_transcription == "off":
        return "switched off"
    if config.whisper_backend != "pywhispercpp":
        return "only the pywhispercpp backend transcribes while recording"
    from video_story.transcribe.pipeline import missing_requirements

    missing = missing_requirements(config)
    if missing:
        return "missing " + ", ".join(missing)
    return None


class LiveTranscriber:
    """Runs the live transcription worker of one recording (thread safe)."""

    def __init__(
        self,
        paths: SessionPaths,
        config: Config,
        *,
        config_file: Path | None = None,
        command: CommandFactory | None = None,
        restart_delay_s: float = RESTART_DELAY_S,
    ) -> None:
        self.paths = paths
        self.config = config
        self.mode = config.live_transcription
        self._command = command or worker_command(config_file)
        self._restart_delay_s = restart_delay_s
        self._lock = threading.RLock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._stopping = False
        self._job: Any = None
        self._transcript: Transcript | None = None
        self.restarts = 0
        self.windows = 0
        self.silent_windows = 0
        self.whisper_seconds = 0.0
        self.audio_ms = 0  # audio given to whisper (windows with speech)
        self.threads: int | None = None
        self.base_threads: int | None = None
        self.priority: str | None = None
        self.throttled = 0
        self.error: str | None = None
        self.started_at: float | None = None
        self._drops: deque[tuple[float, int]] = deque()
        self._last_drop_at: float | None = None

    # State ------------------------------------------------------------------

    @property
    def transcribed_ms(self) -> int:
        with self._lock:
            transcript = self._transcript
            return (transcript.transcribed_ms or 0) if transcript is not None else 0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def lag_ms(self, recorded_ms: int) -> int:
        """How far the stored transcript is behind the recording."""
        return max(0, int(recorded_ms) - self.transcribed_ms)

    def info(self, recorded_ms: int | None = None) -> dict[str, Any]:
        """Statistics for ``manifest.json`` (``live_transcription``)."""
        with self._lock:
            rtf = self.whisper_seconds / (self.audio_ms / 1000) if self.audio_ms else None
            return {
                "mode": self.mode,
                "threads": self.base_threads,
                "priority": self.priority,
                "throttled": self.throttled,
                "windows": self.windows,
                "silent_windows": self.silent_windows,
                "restarts": self.restarts,
                "transcribed_ms_at_stop": self.transcribed_ms,
                "recorded_ms": recorded_ms,
                "rtf": round(rtf, 3) if rtf is not None else None,
                "error": self.error,
            }

    # Lifecycle --------------------------------------------------------------

    def start(self) -> bool:
        """Start the worker; False when it could not (logged, the recording goes on)."""
        with self._lock:
            self._stopping = False
            self.started_at = time.monotonic()
            self._transcript = read_resume_state(
                self.paths.transcript_json_path(self.paths.work_dir)
            )
            if self._transcript is not None and self._transcript.complete:
                self._transcript = None
            return self._spawn()

    def _spawn(self) -> bool:
        args = self._command(self.paths, self.mode)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
                bufsize=0,
            )
        except OSError as exc:
            self.error = f"cannot start the live transcription: {exc}"
            log.warning("%s", self.error)
            return False
        self._assign_job(proc)
        self._proc = proc
        self._stderr.clear()
        self._reader = threading.Thread(
            target=self._read, args=(proc,), name="live-transcription", daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, args=(proc,), name="live-transcription-err", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()
        log.info("live transcription (%s) started: %s", self.mode, " ".join(args))
        return True

    def _assign_job(self, proc: subprocess.Popen[bytes]) -> None:
        if sys.platform != "win32":
            return
        try:
            from video_story.platform.windows_input import _Job

            if self._job is None:
                self._job = _Job()
            self._job.assign(proc)
        except Exception:  # noqa: BLE001 - the worker also ends when its stdin closes
            log.debug("live transcription worker not put into a job object", exc_info=True)

    def stop(self, timeout_s: float = STOP_WAIT_S) -> None:
        """End the worker and wait until no thread writes the transcript any more."""
        with self._lock:
            self._stopping = True
            proc, self._proc = self._proc, None
            reader = self._reader
        if proc is not None:
            _end(proc, timeout_s)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout_s + 2)
        if self._job is not None:
            self._job.close()
            self._job = None
        log.info("live transcription stopped at %d ms", self.transcribed_ms)

    def set_threads(self, threads: int) -> None:
        with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(f"threads {int(threads)}\n".encode())
                proc.stdin.flush()
            except OSError:
                return
            self.threads = int(threads)

    def observe_drops(self, dropped_ticks: int, now: float | None = None) -> None:
        """Called every second with the recorder's dropped ticks: halve the threads when
        frames start to drop, give them back after a quiet while."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.base_threads is None or self.threads is None:
                return
            self._drops.append((now, int(dropped_ticks)))
            while self._drops and now - self._drops[0][0] > THROTTLE_WINDOW_S:
                self._drops.popleft()
            first = self._drops[0][1]
            if int(dropped_ticks) - first > THROTTLE_DROPS:
                self._last_drop_at = now
                self._drops.clear()
                self._drops.append((now, int(dropped_ticks)))
                lower = max(2, self.threads // 2)
                if lower < self.threads:
                    self.throttled += 1
                    log.warning(
                        "frames are being dropped: live transcription %d -> %d threads",
                        self.threads,
                        lower,
                    )
                    self.set_threads(lower)
                return
            if (
                self.threads < self.base_threads
                and self._last_drop_at is not None
                and now - self._last_drop_at >= THROTTLE_RESTORE_S
            ):
                log.info("live transcription back to %d threads", self.base_threads)
                self._last_drop_at = None
                self.set_threads(self.base_threads)

    # Worker output ----------------------------------------------------------

    def _read(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                log.debug("live transcription: %s", line)
                continue
            if isinstance(event, dict):
                self._handle(event)
        code = proc.wait()
        self._ended(proc, code)

    def _read_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr.append(line)

    def _handle(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        with self._lock:
            if self._stopping:
                return
            if kind == "start":
                self.priority = str(event.get("priority") or "")
                threads = int(event.get("threads") or 0) or None
                self.base_threads = self.base_threads or threads
                if self.threads is not None and threads is not None and self.threads != threads:
                    self.set_threads(self.threads)  # a restarted worker keeps the throttle
                else:
                    self.threads = threads
            elif kind == "ready":
                log.info("live transcription model loaded in %s s", event.get("load_seconds"))
            elif kind == "committed":
                self._commit(event)

    def _commit(self, event: dict[str, Any]) -> None:
        try:
            segments = [Segment.from_dict(s) for s in event.get("segments") or []]
            reached = int(event["transcribed_ms"])
        except (KeyError, TypeError, ValueError):
            return
        self.windows += 1
        if event.get("speech"):
            self.whisper_seconds += float(event.get("seconds") or 0.0)
            self.audio_ms += max(0, int(event.get("end_ms", 0)) - int(event.get("start_ms", 0)))
        else:
            self.silent_windows += 1
        updated, _new = merge_segments(
            self._transcript, segments, reached, language=event.get("language")
        )
        updated.model = updated.model or self.config.resolved_model_path().stem
        updated.backend = updated.backend or self.config.whisper_backend
        base = self.paths.work_dir
        try:
            atomic_write_text(self.paths.transcript_txt_path(base), transcript_to_text(updated))
            atomic_write_text(
                self.paths.transcript_json_path(base),
                json.dumps(updated.to_dict(), indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            log.warning("live transcript not written: %s", exc)
            return
        self._transcript = updated

    def _ended(self, proc: subprocess.Popen[bytes], code: int) -> None:
        with self._lock:
            if self._stopping or self._proc is not proc:
                return
            self._proc = None
            tail = " | ".join(self._stderr) or f"exit code {code}"
            log.warning("live transcription worker ended (%s): %s", code, tail)
            if self.restarts >= MAX_RESTARTS:
                self.error = f"the worker ended {self.restarts + 1} times; last: {tail[-300:]}"
                return
            self.restarts += 1
        timer = threading.Timer(self._restart_delay_s, self._restart)
        timer.daemon = True
        timer.start()

    def _restart(self) -> None:
        with self._lock:
            if self._stopping or self._proc is not None:
                return
            log.info("restarting the live transcription (%d)", self.restarts)
            if not self._spawn():
                self.error = self.error or "the worker could not be restarted"


def _end(proc: subprocess.Popen[bytes], timeout_s: float) -> None:
    try:
        if proc.stdin is not None:
            proc.stdin.close()
    except OSError:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout_s)
    except subprocess.TimeoutExpired:
        log.warning("live transcription worker %d did not end in time", proc.pid)


__all__ = [
    "LiveTranscriber",
    "live_transcription_available",
    "worker_command",
]
