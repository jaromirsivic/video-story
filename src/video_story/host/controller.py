"""Session controller: state transitions shared by the UI and the headless tools.

The controller owns the :class:`Recorder`, writes ``state.json`` on every transition
and finalises the recording directory (the editor transcribes). It has no Qt
dependency; the UI calls it from the GUI thread and runs :meth:`process` in a worker
thread.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
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
from video_story.host.live_transcription import LiveTranscriber, live_transcription_available
from video_story.platform.base import Platform

log = logging.getLogger(__name__)


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
        # Settings > Live Transcription: the worker of the running recording, and its
        # statistics once the recording stopped (manifest ``live_transcription``).
        self.config_file: Path | None = None
        self.live: LiveTranscriber | None = None
        self.live_info: dict[str, object] | None = None
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
        self._start_live()

    def _start_live(self) -> None:
        """Transcribe while recording when Settings ask for it (never fails the recording)."""
        self.live_info = None
        reason = live_transcription_available(self.config)
        if reason is not None:
            log.info("no transcription while recording: %s", reason)
            return
        live = self.create_live_transcriber()
        try:
            started = live.start()
        except Exception:  # noqa: BLE001 - the recording must go on
            log.exception("the live transcription did not start")
            return
        if started:
            self.live = live
        else:
            self.live_info = live.info()

    def create_live_transcriber(self) -> LiveTranscriber:
        """The worker manager (tests replace it)."""
        return LiveTranscriber(self.paths, self.config, config_file=self.config_file)

    def _stop_live(self, recorded_ms: int) -> None:
        live, self.live = self.live, None
        if live is None:
            return
        try:
            live.stop()
        except Exception:  # noqa: BLE001 - saving the recording comes first
            log.exception("the live transcription did not stop cleanly")
        self.live_info = live.info(recorded_ms)

    def live_tick(self) -> int | None:
        """Every second while recording: lets the live transcription react to dropped
        frames; returns how far it is behind (ms), ``None`` when it does not run."""
        live, recorder = self.live, self.recorder
        if live is None or recorder is None:
            return None
        live.observe_drops(recorder.dropped_ticks)
        return live.lag_ms(recorder.elapsed_ms)

    def stop_recording(self) -> RecordingSummary:
        if self.recorder is None:
            raise ControllerError("not recording")
        if self.summary is None:
            self.summary = self.recorder.stop()
            self._stop_live(self.summary.recorded_duration_ms)
        return self.summary

    def process(self) -> Path:
        """Stop and save the recording. Blocking: run in a worker thread.

        Returns the final recording directory. Nothing is transcribed here: the
        recording is finalised at once (manifest without a transcript) and the session
        ends in the non-final ``editing`` state. The window then opens the editor
        (:meth:`editor_opened`), which transcribes in the background while the user
        already works with the recording, and only its **Process** button hands the
        recording to the agent. This is the one way a recording ends, whoever stopped
        it (the user, the auto stop, low disk space, a capture failure). When the
        editor cannot start, :meth:`editor_failed` ends the session without handing
        anything over.
        """
        summary = self.stop_recording()
        self._set_status(
            Status.PROCESSING,
            "Saving the recording; the Video Story Editor transcribes it.",
            recorded_duration_ms=summary.recorded_duration_ms,
            progress=0.0,
        )
        try:
            manifest = summary.to_manifest(self.config, self.platform.name)
            manifest.finished_at = now_iso()
            manifest.live_transcription = self.live_info
            if summary.error:
                manifest.notes.append(summary.error)
            write_manifest(self.paths.manifest_path(self.paths.work_dir), manifest)
            self._detach_log()
            final_dir = self.paths.finalize()
        except Exception as exc:
            log.exception("saving the recording failed")
            self._detach_log()
            self._set_status(
                Status.FAILED,
                f"Saving the recording failed: {exc}",
                error_code="PROCESSING_FAILED",
                recording_dir=str(self.paths.work_dir),
            )
            raise
        self._set_status(
            Status.EDITING,
            "Recording saved; opening the Video Story Editor, which transcribes it "
            "(waiting for Process).",
            progress=0.0,
            recording_dir=str(final_dir),
            manifest=str(self.paths.manifest_path(final_dir)),
            transcript_txt=str(self.paths.transcript_txt_path(final_dir)),
            transcript_json=str(self.paths.transcript_json_path(final_dir)),
            frames_dir=str(self.paths.frames_dir(final_dir)),
            frames_info_dir=str(self.paths.frames_info_dir(final_dir)),
            recorded_duration_ms=summary.recorded_duration_ms,
        )
        self.recorder = None
        return final_dir

    def editor_opened(self, editor_pid: int) -> None:
        """The editor process now owns the ``editing`` session (liveness by its pid)."""
        self._set_status(
            Status.EDITING,
            "Video Story Editor open; transcribing in the background, waiting for the user "
            "to press Process.",
            pid=editor_pid,
        )

    def editor_failed(self, message: str) -> None:
        """The editor could not be opened: nobody pressed Process, so the agent must not
        act. The recording is kept and can be opened later with Edit Video Story."""
        self._set_status(
            Status.CANCELLED,
            f"Recording saved, but the editor could not be opened ({message}); nothing "
            "should be done. Open it later with Edit Video Story.",
        )

    @contextmanager
    def relocating(self, recording_folder: Path) -> Iterator[None]:
        """Continue this session, which has no recording yet, in another recording folder.

        Used while the Settings dialog saves a new Recording Folder: ``state.json``
        appears in the new folder *before* the configuration points there (an agent in
        its ``wait`` loop resolves the session through the configuration) and the old
        one goes away only after the body succeeded. A failing body puts everything
        back.
        """
        if recording_folder == self.paths.recording_folder:
            yield
            return
        if self.recorder is not None:
            raise ControllerError("the recording folder cannot change during a recording")
        previous = self.paths
        target = SessionPaths(recording_folder, previous.session_id)
        if target.work_dir.exists() or target.final_dir.exists():
            raise ControllerError(
                f"{recording_folder} already holds a recording named {previous.session_id}"
            )
        self._move_to(target)
        try:
            yield
        except BaseException:
            self._move_to(previous)
            self._forget(target)
            raise
        self._forget(previous)
        log.info("session %s moved to %s", previous.session_id, recording_folder)

    def _move_to(self, paths: SessionPaths) -> None:
        paths.work_dir.mkdir(parents=True, exist_ok=True)
        self.paths = paths
        self.state.recording_folder = str(paths.recording_folder)
        self._write_state()
        paths.lock_path.write_text(str(os.getpid()), encoding="utf-8")

    @staticmethod
    def _forget(paths: SessionPaths) -> None:
        """Remove the session files of a folder this session no longer lives in.
        ``ui.log`` may stay behind: the window process holds it open."""
        for path in (paths.state_path, paths.lock_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("cannot remove %s: %s", path, exc)
        paths.remove_work_dir_if_empty()

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


__all__ = ["ControllerError", "SessionController"]
