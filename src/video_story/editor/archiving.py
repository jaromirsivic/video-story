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
end in time. The worker lowers its own priority, so FFmpeg and whisper give way to
whatever else runs (the transcription the user waits for).

A worker that fails records it in ``archive.json`` itself: while nothing was deleted
the recording is given back (status ``failed``, see
:mod:`video_story.core.archive_state`). A worker that died without a word - crashed,
killed, never started - cannot; the manager then records the failure for it
(:func:`~video_story.core.archive_state.record_archive_failure`, which leaves a state
alone that is already ``failed``, so every failed attempt is counted exactly once).
A worker the manager stopped itself is no failure.

**Cancel Archiving** (:meth:`ArchiveManager.cancel`) takes one recording out while
nothing of it was deleted. A waiting one leaves the queue at once; the one being worked
on is stopped like at the editor's close (stdin closed, killed only after
``cancel_wait_ms``) without stopping the queue, and the cancel is finished when the
worker's ``finished`` signal arrives - the GUI thread does not wait for it. Then
:func:`~video_story.export.archive.cancel_archive` looks at ``archive.json`` again: the
worker may have done a screen (and deleted its frames) or even the whole archive before
the stop took effect. Nothing done: the status becomes ``cancelled`` with
``failed_attempts`` as it was at the click, whatever the stopped or killed worker said
(``CANCEL_DONE``). A screen done: it stays ``archiving`` and goes back to the head of
the queue to finish (``CANCEL_TOO_LATE``). Archived: it stays so (``CANCEL_FINISHED``).
None of this is a failure; :attr:`ArchiveManager.cancelled` tells the window which one
it was, and the queue goes on with the next recording.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from video_story.core.archive_state import read_archive_state, record_archive_failure
from video_story.core.config import Config, config_path
from video_story.core.session import SessionPaths
from video_story.platform.process import console_python

from .transcription import _error_message

log = logging.getLogger(__name__)

STOP_WAIT_MS = 15_000  # a stopped worker cancels FFmpeg and removes partial files
STDERR_TAIL_LINES = 12

PHASE_TRANSCRIBING = "transcribing"
PHASE_ARCHIVING = "archiving"

# How a Cancel Archiving ended (the ``cancelled`` signal).
CANCEL_DONE = "cancelled"  # status cancelled: an ordinary recording again
CANCEL_TOO_LATE = "too_late"  # a screen was done meanwhile: it is archived to the end
CANCEL_FINISHED = "finished"  # the worker had archived it all meanwhile
CANCEL_NOT_SAVED = "not_saved"  # archive.json could not be written


class ArchiveManager(QObject):
    """Runs the archive worker for the queued recordings, one at a time."""

    started = Signal(str)  # session id
    progress = Signal(str, str, float)  # session id, phase, fraction 0..1 of that phase
    finished = Signal(str, object)  # session id, the "done" event (dict)
    failed = Signal(str, str)  # session id, message
    cancelled = Signal(str, str)  # session id, CANCEL_DONE / _TOO_LATE / _FINISHED / ...

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
        # The worker being stopped for Cancel Archiving: its session id and the
        # failed_attempts of the recording when the user cancelled.
        self._cancelling: tuple[str, int] | None = None
        self.cancel_wait_ms = STOP_WAIT_MS  # then the cancelled worker is killed
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._kill_cancelled)

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

    # Cancel Archiving -------------------------------------------------------

    def _is_cancelling(self, session_id: str) -> bool:
        return self._cancelling is not None and self._cancelling[0] == session_id

    def can_cancel(self, paths: SessionPaths) -> bool:
        """Cancel Archiving applies to the recording: it waits for archiving or is being
        archived (in this queue, or only in its ``archive.json``) and nothing of it was
        deleted yet; also while its cancel is under way."""
        if self._stopping:
            return False
        if self._is_cancelling(paths.session_id):
            return True
        state = read_archive_state(paths, paths.final_dir)
        return state is not None and state.cancellable

    def cancel(self, paths: SessionPaths) -> bool:
        """Cancel Archiving (module docstring); returns whether the cancel was started.

        A waiting recording leaves the queue and is ``cancelled`` at once. The worker of
        the one being worked on gets its stdin closed (and is killed after
        ``cancel_wait_ms``); the cancel is finished in :meth:`_on_finished`, the queue
        goes on after it. Either way :attr:`cancelled` tells how it ended.
        """
        session_id = paths.session_id
        if self._stopping:
            return False
        if self._is_cancelling(session_id):
            return True  # under way already
        state = read_archive_state(paths, paths.final_dir)
        if state is None or not state.cancellable:
            return False
        process = self._process
        if self.running == session_id and process is not None:
            self._cancelling = (session_id, state.failed_attempts)
            log.info("cancelling the archiving of %s: stopping its worker", session_id)
            process.closeWriteChannel()
            self._kill_timer.start(self.cancel_wait_ms)
            return True
        self.dequeue(session_id)
        self.cancelled.emit(session_id, self._finish_cancel(paths, state.failed_attempts))
        return True

    def _take_cancel(self, session_id: str) -> int | None:
        """The ``failed_attempts`` saved when ``session_id`` was cancelled, and the
        cancel is no longer under way; ``None`` when it is not being cancelled."""
        if self._cancelling is None or self._cancelling[0] != session_id:
            return None
        attempts = self._cancelling[1]
        self._cancelling = None
        self._kill_timer.stop()
        return attempts

    def _kill_cancelled(self) -> None:
        """The cancelled worker did not end in time: kill it (``_on_finished`` follows)."""
        process = self._process
        if process is None or self._cancelling is None or self.running != self._cancelling[0]:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            log.warning("cancelled archive worker of %s did not stop; killing it", self.running)
            process.kill()

    def _finish_cancel(self, paths: SessionPaths, failed_attempts: int) -> str:
        """No worker runs for ``paths`` any more: make the cancel final (see
        :func:`~video_story.export.archive.cancel_archive`) and say how it ended."""
        from video_story.export.archive import cancel_archive

        try:
            state = cancel_archive(paths, failed_attempts)
        except OSError as exc:
            log.warning("cannot cancel the archiving of %s: %s", paths.session_id, exc)
            return CANCEL_NOT_SAVED
        if state is not None and state.archived:
            return CANCEL_FINISHED
        if state is not None and state.pending:
            return CANCEL_TOO_LATE  # a screen is done: its frames are gone
        return CANCEL_DONE

    def _end_cancel(self, paths: SessionPaths, failed_attempts: int) -> None:
        """The cancelled worker has ended (stopped, killed, finished, never started)."""
        session_id = paths.session_id
        outcome = self._finish_cancel(paths, failed_attempts)
        log.info("archiving of %s: cancel ended with %s", session_id, outcome)
        if self._stopping:
            return  # the editor is closing; a half done archive resumes in the next one
        self.cancelled.emit(session_id, outcome)
        if outcome == CANCEL_TOO_LATE:
            self._queue.appendleft(paths)  # the frames of a screen are gone: finish it
        self._start_next()

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
        self._kill_timer.stop()
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
        attempts = self._take_cancel(paths.session_id) if paths is not None else None
        if paths is not None and attempts is not None:
            self._phase.pop(paths.session_id, None)
            self._end_cancel(paths, attempts)  # starts the next one
            return
        if paths is not None:
            self._phase.pop(paths.session_id, None)
            message = "cannot start the archive worker"
            self._record_failure(paths, message)
            self.failures[paths.session_id] = message
            self.failed.emit(paths.session_id, message)
        self._start_next()

    def _record_failure(self, paths: SessionPaths, message: str) -> None:
        """Make sure the failed attempt is in ``archive.json`` before the window hears
        of it (a no-op when the worker recorded it itself): with nothing deleted the
        recording is an ordinary recording again."""
        try:
            record_archive_failure(paths, message)
        except OSError as exc:
            log.warning("cannot record the failed archiving of %s: %s", paths.session_id, exc)

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
        attempts = self._take_cancel(session_id)
        if attempts is not None:
            # Cancel Archiving: no failure, whatever the worker said or how it ended.
            self._end_cancel(paths, attempts)
            return
        if self._stopping:
            log.info("archiving of %s stopped; it resumes in the next editor", session_id)
            return
        if done is not None and exit_code == 0 and status == QProcess.ExitStatus.NormalExit:
            log.info("archiving of %s finished: %s", session_id, done)
            self.finished.emit(session_id, done)
        else:
            message = _error_message(" | ".join(self._stderr) or f"exit code {exit_code}")
            log.warning("archiving of %s failed: %s", session_id, message)
            self._record_failure(paths, message)
            self.failures[session_id] = message
            self.failed.emit(session_id, message)
        self._start_next()


__all__ = [
    "CANCEL_DONE",
    "CANCEL_FINISHED",
    "CANCEL_NOT_SAVED",
    "CANCEL_TOO_LATE",
    "PHASE_ARCHIVING",
    "PHASE_TRANSCRIBING",
    "ArchiveManager",
]
