"""Background transcription inside the editor.

After **Stop -> Video Editor** the recording reaches the editor untranscribed. The
editor then runs ``video-story transcribe-stream <recording dir> --from-ms N`` in a
child process and reads one JSON event per line from its stdout (see
:func:`video_story.transcribe.pipeline.stream_transcription`). Every ``segment`` is
stored in the :class:`LoadedRecording` right away (``transcript.json`` with
``complete`` false and ``transcribed_ms``) and reported to the window, which shows it
as a subtitle; ``done`` makes the transcript complete. Closing the editor kills the
child: whatever was stored stays, and the next time the recording is shown the
transcription resumes at ``transcribed_ms`` instead of starting over.

One recording is transcribed at a time: the one shown in the editor. Showing another
incomplete recording stops the running worker (its stored progress is kept) and starts
one for the new recording once the old process has ended.
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
from video_story.core.transcript import Segment
from video_story.platform.process import console_python

from .recording import LoadedRecording

log = logging.getLogger(__name__)

KILL_WAIT_MS = 3000
STDERR_TAIL_LINES = 12


class TranscriptionManager(QObject):
    """Runs the ``transcribe-stream`` worker for the recording it was given.

    The manager writes the segments into the recording itself (on the GUI thread, from
    the process signals); the signals tell the window what changed. They carry the
    session id so that the window can ignore events of a recording it no longer shows.
    """

    started = Signal(str)  # session id
    progress = Signal(str, float, int)  # session id, fraction 0..1, transcribed_ms
    segment = Signal(str, object)  # session id, Segment (already stored)
    finished = Signal(str, object)  # session id, the "done" event (dict); stored
    failed = Signal(str, str)  # session id, message

    def __init__(
        self, config: Config, config_file: Path | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.config_file = config_file
        self._process: QProcess | None = None
        self._recording: LoadedRecording | None = None
        self._pending: LoadedRecording | None = None  # starts once the killed worker ended
        self._buffer = ""
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._done: dict[str, Any] | None = None
        self._killed = False

    # State ------------------------------------------------------------------

    @property
    def running(self) -> str | None:
        """Session id of the recording being transcribed, ``None`` when idle."""
        if self._pending is not None:
            return self._pending.session_id
        return self._recording.session_id if self._process is not None else None

    @property
    def recording(self) -> LoadedRecording | None:
        """The recording the worker writes into (``None`` when idle)."""
        return self._pending if self._pending is not None else self._recording

    def is_running(self, session_id: str) -> bool:
        return self.running == session_id

    # Starting and stopping --------------------------------------------------

    def command(self, paths: SessionPaths, start_ms: int) -> list[str]:
        """The worker command line (tests replace it with a script of their own)."""
        return [
            console_python(),
            "-X",
            "utf8",
            "-m",
            "video_story",
            "--config",
            str(self.config_file or config_path()),
            "transcribe-stream",
            str(paths.final_dir),
            "--from-ms",
            str(max(0, int(start_ms))),
        ]

    def start(self, recording: LoadedRecording) -> bool:
        """Transcribe ``recording`` from where it stopped.

        Returns False when the transcript is complete or this recording is already
        being transcribed. A worker of another recording is killed first; the new one
        starts as soon as that process has ended.
        """
        if recording.transcript_complete:
            return False
        if self.is_running(recording.session_id):
            self.adopt(recording)
            return False
        if self._process is not None:
            self.stop()
            self._pending = recording
            return True
        self._launch(recording)
        return True

    def restart(self, recording: LoadedRecording) -> None:
        """Transcribe ``recording`` again from where its (just reset) transcript stands,
        even while a worker - of this recording or another - is still running: that one
        is killed and the new one starts as soon as it has ended."""
        if self._process is not None:
            self.stop()
            self._pending = recording
            return
        self._pending = None
        self._launch(recording)

    def adopt(self, recording: LoadedRecording) -> None:
        """The window shows a fresh ``LoadedRecording`` of the recording being
        transcribed: segments stored since it was loaded are merged into it and the
        worker writes into this object from now on."""
        current = self.recording
        if current is None or current is recording or current.session_id != recording.session_id:
            return
        if current.transcript is not None and not recording.transcript_complete:
            try:
                recording.add_segments(current.transcript.segments, current.transcribed_ms)
            except OSError as exc:
                log.warning("cannot merge the transcript of %s: %s", recording.session_id, exc)
        if self._pending is not None:
            self._pending = recording
        else:
            self._recording = recording

    def _launch(self, recording: LoadedRecording) -> None:
        start_ms = recording.transcribed_ms
        args = self.command(recording.paths, start_ms)
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._on_stdout)
        process.readyReadStandardError.connect(self._on_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        self._process = process
        self._recording = recording
        self._buffer = ""
        self._stderr.clear()
        self._done = None
        self._killed = False
        log.info("transcribing %s from %d ms: %s", recording.session_id, start_ms, " ".join(args))
        process.start(args[0], args[1:])
        self.started.emit(recording.session_id)

    def stop(self) -> None:
        """Kill the worker (the transcript stored so far is kept; it resumes later)."""
        self._pending = None
        process = self._process
        if process is None:
            return
        self._killed = True
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()

    def shutdown(self, timeout_ms: int = KILL_WAIT_MS) -> None:
        process = self._process
        self.stop()
        if process is not None and process.state() != QProcess.ProcessState.NotRunning:
            process.waitForFinished(timeout_ms)

    # Worker output ----------------------------------------------------------

    def _on_stdout(self) -> None:
        process = self._process
        if process is None:
            return
        self._consume_stdout(process)

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
        process = self._process
        if process is None:
            return
        self._consume_stderr(process)

    def _consume_stderr(self, process: QProcess) -> None:
        data = bytes(process.readAllStandardError().data()).decode("utf-8", errors="replace")
        for line in data.splitlines():
            if line.strip():
                self._stderr.append(line.rstrip())
                log.debug("transcribe-stream: %s", line.rstrip())

    def _handle_line(self, line: str) -> None:
        recording = self._recording
        if not line or recording is None or self._killed:
            return
        try:
            event = json.loads(line)
        except ValueError:
            log.debug("ignoring worker output: %s", line)
            return
        if not isinstance(event, dict):
            return
        kind = event.get("event")
        session_id = recording.session_id
        if kind == "segment":
            try:
                segment = Segment.from_dict(event)
            except (KeyError, ValueError, TypeError):
                return
            try:
                recording.add_segments([segment], segment.end_ms)
            except OSError as exc:
                log.warning("cannot store a segment of %s: %s", session_id, exc)
            self.segment.emit(session_id, segment)
        elif kind == "progress":
            try:
                fraction = float(event.get("fraction", 0.0))
                reached = int(event.get("transcribed_ms", 0))
            except (TypeError, ValueError):
                return
            self.progress.emit(session_id, max(0.0, min(1.0, fraction)), reached)
        elif kind == "done":
            self._done = event

    def _on_error(self, error: QProcess.ProcessError) -> None:
        if error != QProcess.ProcessError.FailedToStart:
            return
        process, self._process = self._process, None
        recording, self._recording = self._recording, None
        program = process.program() if process is not None else "?"
        if process is not None:
            process.deleteLater()
        if recording is not None and not self._killed:
            message = f"cannot start the transcription worker ({program})"
            log.warning("%s: %s", recording.session_id, message)
            self.failed.emit(recording.session_id, message)
        self._start_pending()

    def _on_finished(self, exit_code: int, status: QProcess.ExitStatus) -> None:
        process, self._process = self._process, None
        recording, self._recording = self._recording, None
        if process is None or recording is None:
            self._start_pending()
            return
        self._consume_stdout(process, final=True)
        self._consume_stderr(process)
        process.deleteLater()
        done, self._done = self._done, None
        session_id = recording.session_id
        if self._killed:
            log.info("transcription of %s stopped (worker killed)", session_id)
            self._start_pending()
            return
        if done is not None and exit_code == 0 and status == QProcess.ExitStatus.NormalExit:
            try:
                recording.finish_transcript(
                    done.get("language"), done.get("model"), done.get("backend")
                )
            except OSError as exc:
                log.warning("cannot write the transcript of %s: %s", session_id, exc)
                self.failed.emit(session_id, f"cannot write the transcript: {exc}")
            else:
                log.info("transcription of %s complete: %s", session_id, done)
                self.finished.emit(session_id, done)
            self._start_pending()
            return
        tail = " | ".join(self._stderr) or f"exit code {exit_code}"
        message = _error_message(tail)
        log.warning("transcription of %s failed: %s", session_id, message)
        self.failed.emit(session_id, message)
        self._start_pending()

    def _start_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and self._process is None and not pending.transcript_complete:
            self._launch(pending)


def _error_message(stderr_tail: str) -> str:
    """The CLI's JSON error object (last stderr line) as a sentence, else the tail."""
    for part in reversed(stderr_tail.split(" | ")):
        part = part.strip()
        if part.startswith("{"):
            try:
                data = json.loads(part)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
    return stderr_tail


__all__ = ["TranscriptionManager"]
