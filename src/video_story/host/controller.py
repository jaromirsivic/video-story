"""Session controller: state transitions shared by the UI and the headless tools.

The controller owns the :class:`Recorder`, writes ``state.json`` on every transition,
runs transcription and finalises the recording directory. It has no Qt dependency;
the UI calls it from the GUI thread and runs :meth:`process` in a worker thread.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

from video_story.capture.recorder import Recorder, RecordingSummary
from video_story.core.config import Config
from video_story.core.manifest import write_manifest
from video_story.core.session import (
    SessionPaths,
    SessionState,
    Status,
    now_iso,
    read_state,
    write_state,
)
from video_story.platform.base import Platform

log = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]


class ControllerError(RuntimeError):
    pass


class SessionController:
    def __init__(self, config: Config, platform: Platform, paths: SessionPaths) -> None:
        self.config = config
        self.platform = platform
        self.paths = paths
        self.recorder: Recorder | None = None
        self.summary: RecordingSummary | None = None
        self.discarded_recordings = 0
        self._log_handler: logging.Handler | None = None
        self._lock = threading.Lock()
        self.paths.work_dir.mkdir(parents=True, exist_ok=True)
        existing = self._read_existing_state()
        self.state = existing or SessionState(
            session_id=paths.session_id,
            status=Status.READY,
            recording_folder=str(paths.recording_folder),
        )
        self.state.pid = os.getpid()
        self.state.status = Status.READY
        self.state.message = "Control window open; press Record to start."
        self._write_state()
        self.paths.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    # State ------------------------------------------------------------------

    def _read_existing_state(self) -> SessionState | None:
        if self.paths.state_path.exists():
            try:
                return read_state(self.paths.state_path)
            except RuntimeError:
                return None
        return None

    def _write_state(self) -> None:
        with self._lock:
            write_state(self.paths.state_path, self.state)

    def _set_status(self, status: Status, message: str | None = None, **fields: object) -> None:
        self.state.status = status
        self.state.message = message
        for key, value in fields.items():
            setattr(self.state, key, value)
        self._write_state()
        log.info("state -> %s%s", status, f" ({message})" if message else "")

    @property
    def recording(self) -> bool:
        return self.recorder is not None and self.summary is None

    @property
    def elapsed_ms(self) -> int:
        return self.recorder.elapsed_ms if self.recorder else 0

    # Logging ----------------------------------------------------------------

    def _attach_log(self) -> None:
        path = self.paths.log_path(self.paths.work_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _detach_log(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None

    # Transitions ------------------------------------------------------------

    def start_recording(self) -> None:
        if self.recording:
            raise ControllerError("already recording")
        self.summary = None
        self.paths.create_work_dirs()
        self._attach_log()
        recorder = Recorder(self.config, self.platform, self.paths)
        try:
            recorder.start()
        except Exception:
            self._detach_log()
            raise
        self.recorder = recorder
        self._set_status(
            Status.RECORDING,
            "Recording.",
            recording_started_at=recorder.started_at,
            recorded_duration_ms=None,
        )

    def stop_recording(self) -> RecordingSummary:
        if self.recorder is None:
            raise ControllerError("not recording")
        if self.summary is None:
            self.summary = self.recorder.stop()
        return self.summary

    def process(
        self, progress: ProgressCallback | None = None, hand_to_editor: bool = False
    ) -> Path:
        """Stop, transcribe and finalise. Blocking: run in a worker thread.

        Returns the final recording directory. With ``hand_to_editor`` the session ends
        in the non-final ``editing`` state instead of ``processed``: the window then
        opens the editor (:meth:`editor_opened`) or, when that fails,
        :meth:`editor_failed` finishes the session with the whole range.
        """
        from video_story.transcribe.pipeline import transcribe_recording

        summary = self.stop_recording()
        message = (
            "Auto stop reached; processing." if summary.auto_stopped else "Processing recording."
        )
        self._set_status(
            Status.PROCESSING,
            message,
            recorded_duration_ms=summary.recorded_duration_ms,
            progress=0.0,
        )

        def _progress(fraction: float, text: str) -> None:
            self.state.progress = max(0.0, min(1.0, fraction))
            self.state.message = text
            self._write_state()
            if progress:
                progress(fraction, text)

        try:
            result = transcribe_recording(
                self.paths, self.config, base_dir=self.paths.work_dir, progress=_progress
            )
            manifest = summary.to_manifest(self.config, self.platform.name)
            manifest.transcript = result.info
            manifest.finished_at = now_iso()
            if summary.error:
                manifest.notes.append(summary.error)
            write_manifest(self.paths.manifest_path(self.paths.work_dir), manifest)
            self._detach_log()
            final_dir = self.paths.finalize()
        except Exception as exc:
            log.exception("processing failed")
            self._detach_log()
            self._set_status(
                Status.FAILED,
                f"Processing failed: {exc}",
                error_code="PROCESSING_FAILED",
                recording_dir=str(self.paths.work_dir),
            )
            raise
        duration = summary.recorded_duration_ms
        paths_fields = dict(
            recording_dir=str(final_dir),
            manifest=str(self.paths.manifest_path(final_dir)),
            transcript_txt=str(self.paths.transcript_txt_path(final_dir)),
            transcript_json=str(self.paths.transcript_json_path(final_dir)),
            frames_dir=str(self.paths.frames_dir(final_dir)),
            recorded_duration_ms=duration,
        )
        if hand_to_editor:
            self._set_status(
                Status.EDITING,
                "Recording processed; opening the Video Story Editor (waiting for Process).",
                progress=1.0,
                **paths_fields,
            )
        else:
            self._set_status(
                Status.PROCESSED,
                "Recording processed; transcript and frames are ready.",
                progress=1.0,
                crop={"start_ms": 0, "end_ms": duration},
                processed_via="stop_dialog",
                **paths_fields,
            )
        self.recorder = None
        return final_dir

    def editor_opened(self, editor_pid: int) -> None:
        """The editor process now owns the ``editing`` session (liveness by its pid)."""
        self._set_status(
            Status.EDITING,
            "Video Story Editor open; waiting for the user to press Process.",
            pid=editor_pid,
        )

    def editor_failed(self, message: str) -> None:
        """The editor could not be opened: finish as processed with the whole range."""
        duration = self.state.recorded_duration_ms or 0
        self._set_status(
            Status.PROCESSED,
            f"Editor could not be opened ({message}); act on the whole recording.",
            crop={"start_ms": 0, "end_ms": duration},
            processed_via="stop_dialog",
        )

    def discard(self) -> None:
        """Stop and delete the current recording; the session returns to READY."""
        if self.recorder is not None:
            self.stop_recording()
        self._detach_log()
        self.paths.discard(keep_state=True)
        self.discarded_recordings += 1
        self.recorder = None
        self.summary = None
        self._set_status(
            Status.READY,
            f"Recording discarded ({self.discarded_recordings}); press Record to start again.",
            recording_started_at=None,
            recorded_duration_ms=None,
        )

    def cancel(self, message: str = "Control window closed without a recording.") -> None:
        """Window closed in the READY state: the session ends without a recording."""
        if self.recording:
            raise ControllerError("cannot cancel while recording")
        self._detach_log()
        self._set_status(Status.CANCELLED, message)
        try:
            self.paths.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    def fail(self, message: str, code: str = "ERROR") -> None:
        self._detach_log()
        self._set_status(Status.FAILED, message, error_code=code)

    def release_lock(self) -> None:
        try:
            self.paths.lock_path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["ControllerError", "ProgressCallback", "SessionController"]
