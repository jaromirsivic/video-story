"""The editor's archive queue.

Recordings the user archives (**Archive Video(s)** in the list's menu) and recordings
older than ``archive_after_days`` are archived one after another by a child process,
``video-story archive-stream <recording dir> --stop-on-eof``, which prints one JSON
event per line (see ``cmd_archive_stream`` in :mod:`video_story.host.cli`): a recording
whose transcript is not complete is transcribed first (phase ``transcribing``), then
archived screen by screen (phase ``archiving``).

The queue lives in the recordings themselves (``archive/archive.json``, status
``queued`` or ``archiving``): an editor closed half way leaves it there and the next
editor resumes it. Closing the editor closes the worker's stdin, which stops it
cleanly (FFmpeg cancelled, partial files removed); it is killed only when it does not
end in time.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, Signal

from video_story.core.config import Config, config_path
from video_story.core.session import SessionPaths
from video_story.platform.process import console_python

from .transcription import _error_message

log = logging.getLogger(__name__)

STOP_WAIT_MS = 15_000  # a stopped worker cancels FFmpeg and removes partial files
STDERR_TAIL_LINES = 12

PHASE_TRANSCRIBING = "transcribing"
PHASE_ARCHIVING = "archiving"


class ArchiveManager(QObject):
    """Runs the archive worker for the queued recordings, one at a time."""

    started = Signal(str)  # session id
    progress = Signal(str, str, float)  # session id, phase, fraction 0..1 of that phase
    finished = Signal(str, object)  # session id, the "done" event (dict)
    failed = Signal(str, str)  # session id, message

    def __init__(
        self, config: Config, config_file: Path | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.config_file = config_file
        self._queue: deque[SessionPaths] = deque()
        self._process: QProcess | None = None
        self._paths: SessionPaths | None = None
        self._buffer = ""
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._done: dict[str, Any] | None = None
        self._stopping = False
        self._phase: dict[str, tuple[str, float]] = {}  # session id -> (phase, fraction)
        self.failures: dict[str, str] = {}  # session id -> message (not retried this run)

    # State ------------------------------------------------------------------

    @property
    def running(self) -> str | None:
        return self._paths.session_id if self._process is not None and self._paths else None

    def is_queued(self, session_id: str) -> bool:
        """Waiting in the queue or being worked on now."""
        return self.running == session_id or any(p.session_id == session_id for p in self._queue)

    def phase(self, session_id: str) -> tuple[str, float] | None:
        """``(phase, fraction)`` of the recording being worked on (``None`` otherwise)."""
        return self._phase.get(session_id)

    @property
    def queued(self) -> list[str]:
        return [p.session_id for p in self._queue]

    # Queue ------------------------------------------------------------------

    def enqueue(self, paths: SessionPaths) -> bool:
        """Add a recording to the queue (once); the worker starts when it is idle."""
        if self._stopping or self.is_queued(paths.session_id):
            return False
        self.failures.pop(paths.session_id, None)
        self._queue.append(paths)
        self._start_next()
        return True

    def dequeue(self, session_id: str) -> bool:
        """Drop a recording that waits in the queue (not the one being worked on)."""
        before = len(self._queue)
        self._queue = deque(p for p in self._queue if p.session_id != session_id)
        return len(self._queue) != before

    def command(self, paths: SessionPaths) -> list[str]:
        """The worker command line (tests replace it with a script of their own)."""
        return [
            console_python(),
            "-X",
            "utf8",
            "-m",
            "video_story",
            "--config",
            str(self.config_file or config_path()),
            "archive-stream",
            str(paths.final_dir),
            "--stop-on-eof",
        ]

    def _start_next(self) -> None:
        if self._process is not None or self._stopping or not self._queue:
            return
        paths = self._queue.popleft()
        args = self.command(paths)
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._on_stdout)
        process.readyReadStandardError.connect(self._on_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        self._paths = paths
        self._buffer = ""
        self._stderr.clear()
        self._done = None
        self._phase[paths.session_id] = (PHASE_ARCHIVING, 0.0)
        log.info("archiving %s: %s", paths.session_id, " ".join(args))
        process.start(args[0], args[1:])
        self.started.emit(paths.session_id)

    def shutdown(self, timeout_ms: int = STOP_WAIT_MS) -> None:
        """Stop the worker cleanly (stdin closed), killing it only when it hangs. What
        is queued stays queued in the recordings and resumes in the next editor."""
        self._stopping = True
        self._queue.clear()
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        process.closeWriteChannel()
        if not process.waitForFinished(timeout_ms):
            log.warning("archive worker did not stop in time; killing it")
            process.kill()
            process.waitForFinished(3000)

    # Worker output ----------------------------------------------------------

    def _on_stdout(self) -> None:
        if self._process is not None:
            self._consume_stdout(self._process)

    def _consume_stdout(self, process: QProcess, final: bool = False) -> None:
        data = bytes(process.readAllStandardOutput().data()).decode("utf-8", errors="replace")
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._handle_line(line.strip())
        if final and self._buffer.strip():
            self._handle_line(self._buffer.strip())
            self._buffer = ""

    def _on_stderr(self) -> None:
        if self._process is not None:
            self._consume_stderr(self._process)

    def _consume_stderr(self, process: QProcess) -> None:
        data = bytes(process.readAllStandardError().data()).decode("utf-8", errors="replace")
        for line in data.splitlines():
            if line.strip():
                self._stderr.append(line.rstrip())
                log.debug("archive-stream: %s", line.rstrip())

    def _handle_line(self, line: str) -> None:
        paths = self._paths
        if not line or paths is None:
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        session_id = paths.session_id
        kind = event.get("event")
        if kind == "phase":
            phase = str(event.get("phase") or PHASE_ARCHIVING)
            self._phase[session_id] = (phase, 0.0)
            self.progress.emit(session_id, phase, 0.0)
        elif kind == "progress":
            try:
                fraction = max(0.0, min(1.0, float(event.get("fraction", 0.0))))
            except (TypeError, ValueError):
                return
            phase = str(event.get("phase") or PHASE_ARCHIVING)
            self._phase[session_id] = (phase, fraction)
            self.progress.emit(session_id, phase, fraction)
        elif kind == "done" or event.get("ok") is True:
            self._done = event

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        process, self._process = self._process, None
        paths, self._paths = self._paths, None
        if process is not None:
            process.deleteLater()
        if paths is not None:
            self._phase.pop(paths.session_id, None)
            message = "cannot start the archive worker"
            self.failures[paths.session_id] = message
            self.failed.emit(paths.session_id, message)
        self._start_next()

    def _on_finished(self, exit_code: int, status: QProcess.ExitStatus) -> None:
        process, self._process = self._process, None
        paths, self._paths = self._paths, None
        if process is None or paths is None:
            self._start_next()
            return
        self._consume_stdout(process, final=True)
        self._consume_stderr(process)
        process.deleteLater()
        session_id = paths.session_id
        self._phase.pop(session_id, None)
        done, self._done = self._done, None
        if self._stopping:
            log.info("archiving of %s stopped; it resumes in the next editor", session_id)
            return
        if done is not None and exit_code == 0 and status == QProcess.ExitStatus.NormalExit:
            log.info("archiving of %s finished: %s", session_id, done)
            self.finished.emit(session_id, done)
        else:
            message = _error_message(" | ".join(self._stderr) or f"exit code {exit_code}")
            log.warning("archiving of %s failed: %s", session_id, message)
            self.failures[session_id] = message
            self.failed.emit(session_id, message)
        self._start_next()


__all__ = ["PHASE_ARCHIVING", "PHASE_TRANSCRIBING", "ArchiveManager"]
