"""The small always-on-top control window: Record/Stop, Settings, Draw on Screen.

While recording, **Settings** gives its place to **Insert keypoint**: a click marks the
current moment in the recording (``manifest.json`` ``keypoints``; the editor shows them
as green lines that the crop handles snap to) and the button counts down 3, 2, 1 before
the next keypoint can be inserted. The text clipboard is tracked meanwhile
(:mod:`video_story.ui.clipboard`): every new text copied during the recording lands in
the frame sidecars of the next tick.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from video_story.capture.recorder import low_disk_message
from video_story.core.timeline import format_timestamp
from video_story.host.controller import SessionController
from video_story.host.editor_launcher import launch_editor

from .clipboard import ClipboardTracker
from .icons import draw_icon, keypoint_icon, record_icon, settings_icon, stop_icon
from .settings_dialog import SettingsDialog
from .stop_dialog import StopChoice, StopDialog, confirm_discard

log = logging.getLogger(__name__)

UI_TICK_MS = 200
KEYPOINT_LABEL = "Insert keypoint"
KEYPOINT_COOLDOWN_S = 3  # the button counts 3, 2, 1 before the next keypoint
SAVED_NOTE = "What was recorded so far is saved and opens in the Video Story Editor."


class ProcessingWorker(QObject):
    """Saves the recording (stop, manifest, final folder) in a QThread."""

    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, controller: SessionController) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        try:
            final_dir = self._controller.process()
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(str(final_dir))


class ControlWindow(QWidget):
    """Main window. ``on_finished`` is called with the final status when the session ends.

    However a recording ends - Stop and **Video Editor**, the auto stop, low disk space,
    a capture failure - it is saved and opened in the Video Story Editor; nothing is
    handed to the agent from here (only the editor's **Process** button does that).

    ``preflight`` returns what the transcription would miss (the Whisper model, FFmpeg);
    it is asked when Record is pressed so that the user hears about it at once.
    """

    def __init__(
        self,
        controller: SessionController,
        on_finished: Callable[[str], None],
        overlay_factory: Callable[[], QObject] | None = None,
        config_file: Path | None = None,
        preflight: Callable[[], list[str]] | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self._on_finished = on_finished
        self._overlay_factory = overlay_factory
        self._config_file = config_file
        self._preflight = preflight
        self._overlay: QObject | None = None
        self._processing = False
        self._halting = False  # stopping by itself: a notice is on screen, no closing
        self._done = False
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None

        self.setWindowTitle("Video Story")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowCloseButtonHint
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        row = QHBoxLayout()
        row.setSpacing(8)

        self.record_button = QPushButton(record_icon(), "Record")
        self.record_button.setToolTip("Start recording audio, screens and cursor")
        self.record_button.clicked.connect(self._on_record_or_stop)
        row.addWidget(self.record_button)

        self.settings_button = QPushButton(settings_icon(), "Settings")
        self.settings_button.setToolTip(
            "Recording folder, capture interval, image format, audio input"
        )
        self.settings_button.clicked.connect(self._open_settings)
        row.addWidget(self.settings_button)

        # Takes the place of Settings while recording (see the module docstring).
        self.keypoint_button = QPushButton(keypoint_icon(), KEYPOINT_LABEL)
        self.keypoint_button.setToolTip(
            "Mark this moment of the recording as a keypoint (shown as a green line in the "
            f"editor); the next one can follow after {KEYPOINT_COOLDOWN_S} s"
        )
        self.keypoint_button.clicked.connect(self.insert_keypoint)
        self.keypoint_button.hide()
        row.addWidget(self.keypoint_button)
        self._keypoint_countdown: int | None = None
        self._keypoint_timer = QTimer(self)
        self._keypoint_timer.setInterval(1000)
        self._keypoint_timer.timeout.connect(self._on_keypoint_tick)
        self._clipboard = ClipboardTracker(parent=self)

        self.draw_button = QPushButton(draw_icon(), "Draw on Screen")
        self.draw_button.setToolTip("Draw annotations on the screen (available while recording)")
        self.draw_button.setCheckable(True)
        self.draw_button.setEnabled(False)
        self.draw_button.toggled.connect(self._toggle_overlay)
        row.addWidget(self.draw_button)
        layout.addLayout(row)

        self.status_label = QLabel("Ready. Press Record to start.")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.status_label)

        self._timer = QTimer(self)
        self._timer.setInterval(UI_TICK_MS)
        self._timer.timeout.connect(self._tick)

        self.setFixedHeight(self.sizeHint().height())
        self._place_window()

    # Layout -----------------------------------------------------------------

    def _place_window(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.adjustSize()
        x = area.right() - self.width() - 24
        y = area.top() + 24
        self.move(x, y)

    # Record / Stop ----------------------------------------------------------

    def _on_record_or_stop(self) -> None:
        if self._processing or self._done:
            return
        if self.controller.recording:
            self._on_stop()
        else:
            self._on_record()

    def _confirm_missing_requirements(self) -> bool:
        """True when recording may start (nothing is missing, or the user accepts that
        the recording cannot be transcribed until the missing part is installed)."""
        if self._preflight is None:
            return True
        try:
            missing = self._preflight()
        except Exception:  # noqa: BLE001 - a broken check must not block recording
            log.exception("preflight check failed")
            return True
        if not missing:
            return True
        answer = QMessageBox.question(
            self,
            "Transcription is not ready",
            "Missing: " + ", ".join(missing) + ".\n\n"
            "You can record, but the recording cannot be transcribed until you run "
            "`video-story setup` (it downloads the missing parts).\n\nRecord anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_record(self) -> None:
        if not self._confirm_missing_requirements():
            return
        try:
            self.controller.start_recording()
        except Exception as exc:
            log.exception("cannot start recording")
            QMessageBox.critical(self, "Cannot start recording", f"{exc}")
            return
        self.record_button.setIcon(stop_icon())
        self.record_button.setText("Stop")
        self.record_button.setToolTip("Stop and decide what to do with the recording")
        self._show_recording_controls(True)
        self.draw_button.setEnabled(True)
        self._start_clipboard_tracking()
        self._timer.start()
        self._tick()

    def _show_recording_controls(self, recording: bool) -> None:
        """Settings while idle, Insert keypoint while recording (same slot of the row)."""
        self.settings_button.setEnabled(not recording)
        self.settings_button.setVisible(not recording)
        self._reset_keypoint_button()
        self.keypoint_button.setVisible(recording)
        self.adjustSize()

    # Keypoints --------------------------------------------------------------

    @property
    def keypoint_countdown(self) -> int | None:
        """Seconds until the next keypoint may be inserted, ``None`` when ready."""
        return self._keypoint_countdown

    def insert_keypoint(self) -> int | None:
        """Mark the current moment; returns its time (ms) or ``None`` when refused."""
        recorder = self.controller.recorder
        add = getattr(recorder, "add_keypoint", None)
        if not self.controller.recording or add is None or self._keypoint_countdown is not None:
            return None
        time_ms = add()
        if time_ms is None:
            return None
        self._keypoint_countdown = KEYPOINT_COOLDOWN_S
        self.keypoint_button.setEnabled(False)
        self._show_keypoint_countdown()
        self._keypoint_timer.start()
        self._tick()
        return int(time_ms)

    def _show_keypoint_countdown(self) -> None:
        if self._keypoint_countdown is not None:
            self.keypoint_button.setText(f"{KEYPOINT_LABEL} ({self._keypoint_countdown})")

    def _on_keypoint_tick(self) -> None:
        if self._keypoint_countdown is None:
            self._keypoint_timer.stop()
            return
        self._keypoint_countdown -= 1
        if self._keypoint_countdown > 0:
            self._show_keypoint_countdown()
            return
        self._reset_keypoint_button()

    def _reset_keypoint_button(self) -> None:
        self._keypoint_timer.stop()
        self._keypoint_countdown = None
        self.keypoint_button.setText(KEYPOINT_LABEL)
        self.keypoint_button.setEnabled(True)

    # Clipboard --------------------------------------------------------------

    def _start_clipboard_tracking(self) -> None:
        recorder = self.controller.recorder
        if recorder is None:
            return
        try:
            self._clipboard.start()
        except Exception:  # noqa: BLE001 - the clipboard must never stop a recording
            log.exception("clipboard tracking unavailable")
            return
        recorder.clipboard_provider = self._clipboard.take

    def _stop_clipboard_tracking(self) -> None:
        self._clipboard.stop()
        recorder = self.controller.recorder
        if recorder is not None:
            recorder.clipboard_provider = None

    def _on_stop(self) -> None:
        while True:
            dialog = StopDialog(self)
            dialog.exec()
            if self._processing or self._halting or not self.controller.recording:
                # The recording ended by itself while the dialog was open (auto stop,
                # low disk space): saving it has taken over.
                return
            choice = dialog.choice
            if choice is StopChoice.CONTINUE:
                return
            if choice is StopChoice.EDITOR:
                self._begin_processing()
                return
            if confirm_discard(self):
                self._discard()
                return
            # Not confirmed: back to the Stop dialog, recording still running.

    def _discard(self) -> None:
        self._close_overlay()
        self._stop_clipboard_tracking()
        try:
            self.controller.discard()
        except Exception as exc:
            log.exception("discard failed")
            QMessageBox.critical(self, "Discard failed", f"{exc}")
        self._timer.stop()
        self.record_button.setIcon(record_icon())
        self.record_button.setText("Record")
        self.record_button.setToolTip("Start recording audio, screens and cursor")
        self._show_recording_controls(False)
        self.draw_button.setEnabled(False)
        self.status_label.setText("Recording discarded. Press Record to start again.")

    # Processing -------------------------------------------------------------

    def _begin_processing(self) -> None:
        """Save the recording and open it in the editor (nothing is transcribed here:
        the editor does that in the background, so there is no dialog to wait in)."""
        if self._processing:
            return
        self._processing = True
        self._timer.stop()
        self._close_overlay()
        self._stop_clipboard_tracking()
        self.record_button.setEnabled(False)
        self.draw_button.setEnabled(False)
        self._reset_keypoint_button()
        self.keypoint_button.setEnabled(False)
        self._thread = QThread(self)
        self._worker = ProcessingWorker(self.controller)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self.status_label.setText("Opening the Video Story Editor...")
        self._worker.finished.connect(self._on_processed)
        self._worker.failed.connect(self._on_processing_failed)
        self._thread.start()

    def _halt_recording(self, title: str, text: str) -> None:
        """The recording cannot go on (low disk space, a write or capture failure):
        stop it first - the microphone must not run on behind a message box -, tell the
        user why, then save what there is and open the editor."""
        if self._processing or self._halting:
            return
        self._halting = True
        self._timer.stop()
        self._close_overlay()
        self._stop_clipboard_tracking()
        try:
            self.controller.stop_recording()
        except Exception:  # noqa: BLE001 - saving below reports what is wrong
            log.exception("stopping the recording failed")
        self.status_label.setText(title)
        QMessageBox.warning(self, title, f"{text}\n\n{SAVED_NOTE}")
        self._halting = False
        self._begin_processing()

    def _stop_thread(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None

    def _on_processed(self, final_dir: str) -> None:
        self._stop_thread()
        self._done = True
        log.info("recording saved: %s", final_dir)
        session_id = self.controller.paths.session_id
        try:
            info = launch_editor(
                self.controller.config,
                session_id,
                self._config_file,
                recording_session=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - the recording must not be lost
            log.exception("cannot open the editor")
            self.controller.editor_failed(str(exc))
            QMessageBox.warning(
                self,
                "Video Editor",
                f"The editor could not be opened: {exc}\n\n"
                "The recording was saved and nothing was handed to the agent. Open it "
                "later with Edit Video Story.",
            )
            self._finish("cancelled")
            return
        self.controller.editor_opened(int(info.get("pid") or 0))
        self._finish("editing")

    def _on_processing_failed(self, message: str) -> None:
        self._stop_thread()
        self._done = True
        QMessageBox.critical(
            self,
            "Saving the recording failed",
            f"{message}\n\nThe recorded files were kept; the next start of Video Story or "
            "of the editor finishes the recording and lists it in the editor.",
        )
        self._finish("failed")

    def _finish(self, status: str) -> None:
        self._timer.stop()
        self._close_overlay()
        self.hide()
        self._on_finished(status)

    # Settings ---------------------------------------------------------------

    def _open_settings(self) -> None:
        if self.controller.recording:
            return
        # A new Recording Folder applies to this session at once: the session moves there
        # while the dialog saves (see SessionController.relocating), so the recording, the
        # editor and the waiting agent all use the folder the user chose.
        dialog = SettingsDialog(
            self.controller.config,
            self,
            current_session_id=self.controller.paths.session_id,
            config_file=self._config_file,
            save_guard=lambda config: self.controller.relocating(config.recording_folder),
        )
        if dialog.exec() and dialog.result_config is not None:
            self.controller.config = dialog.result_config
            self.status_label.setText("Settings saved. Press Record to start.")

    # Drawing overlay --------------------------------------------------------

    def _toggle_overlay(self, checked: bool) -> None:
        if checked:
            if not self.controller.recording:
                self.draw_button.setChecked(False)
                return
            if self._overlay is None and self._overlay_factory is not None:
                try:
                    self._overlay = self._overlay_factory()
                    closed = getattr(self._overlay, "closed", None)
                    if closed is not None:
                        closed.connect(self._overlay_closed)
                    keep_above = getattr(self._overlay, "keep_above", None)
                    if keep_above is not None:
                        # Stay movable and clickable above the drawing surface.
                        keep_above(self)
                    self._connect_overlay_to_recorder()
                except Exception as exc:
                    log.exception("cannot create overlay")
                    QMessageBox.warning(self, "Draw on Screen", f"Drawing is not available: {exc}")
                    self.draw_button.setChecked(False)
                    return
            if self._overlay is not None:
                # The overlay takes the keyboard focus (B / R / A pick a tool); the overlay
                # keeps this window above it (keep_above).
                self._overlay.show()  # type: ignore[attr-defined]
                self.raise_()
        elif self._overlay is not None:
            self._overlay.hide()  # type: ignore[attr-defined]

    def _connect_overlay_to_recorder(self) -> None:
        """Let the recorder store the drawings in every frame sidecar."""
        recorder = self.controller.recorder
        if recorder is None or self._overlay is None:
            return
        set_screens = getattr(self._overlay, "set_screens", None)
        set_clock = getattr(self._overlay, "set_clock", None)
        drawings = getattr(self._overlay, "drawings", None)
        drawing_now = getattr(self._overlay, "drawing_now", None)
        zoom = getattr(self._overlay, "zoom", None)
        if set_screens is not None:
            set_screens(list(getattr(recorder, "screens", [])))
        clock = getattr(recorder, "clock", None)
        if set_clock is not None and clock is not None:
            # started_ms / finished_ms of the shapes share the timeline of the frames.
            set_clock(clock.elapsed_ms)
        if drawings is not None:
            recorder.drawings_provider = drawings
        if drawing_now is not None:
            recorder.drawing_now_provider = drawing_now
        if zoom is not None:
            recorder.zoom_provider = zoom

    def _overlay_closed(self) -> None:
        self.draw_button.blockSignals(True)
        self.draw_button.setChecked(False)
        self.draw_button.blockSignals(False)

    def _close_overlay(self) -> None:
        recorder = self.controller.recorder
        if recorder is not None:
            recorder.drawings_provider = None
            recorder.drawing_now_provider = None
            recorder.zoom_provider = None
        if self._overlay is not None:
            try:
                self._overlay.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                log.exception("overlay close failed")
            self._overlay = None
        self.draw_button.blockSignals(True)
        self.draw_button.setChecked(False)
        self.draw_button.blockSignals(False)

    # Periodic UI update -----------------------------------------------------

    def _tick(self) -> None:
        recorder = self.controller.recorder
        if recorder is None or self._processing or self._halting:
            return
        if recorder.auto_stopped:
            self.status_label.setText("Auto stop reached. Opening the editor...")
            self._begin_processing()
            return
        if getattr(recorder, "low_disk", False):
            self._halt_recording(
                "Recording stopped: disk almost full",
                low_disk_message(self.controller.paths.recording_folder),
            )
            return
        if not recorder.running and self.controller.summary is None:
            # Capture thread died: surface the error and stop cleanly.
            self._halt_recording(
                "Recording stopped",
                "Screen capture stopped unexpectedly. See recording.log.",
            )
            return
        error = getattr(recorder, "error", None)
        if error:
            # Frames or audio can no longer be written (the folder went away, ...).
            self._halt_recording(
                "Recording stopped: files cannot be written",
                f"The recording could not be written: {error}",
            )
            return
        self._clipboard.poll()
        elapsed = format_timestamp(recorder.elapsed_ms)
        remaining = max(0, self.controller.config.auto_stop_ms - recorder.elapsed_ms)
        keypoints = len(getattr(recorder, "keypoints", None) or ())
        suffix = f"  |  {keypoints} keypoint{'s' if keypoints != 1 else ''}" if keypoints else ""
        self.status_label.setText(
            f"Recording {elapsed[:-4]}  |  auto stop in {format_timestamp(remaining)[:-4]}  |  "
            f"{len(recorder.screens)} screen(s), {recorder.frame_count} frames" + suffix
        )

    # Close ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._done:
            event.accept()
            return
        if self._processing or self._halting:
            event.ignore()
            return
        if self.controller.recording:
            event.ignore()
            self._on_stop()
            return
        try:
            self.controller.cancel()
        except Exception:  # pragma: no cover
            log.exception("cancel failed")
        self._done = True
        event.accept()
        self._on_finished("cancelled")


__all__ = ["KEYPOINT_COOLDOWN_S", "KEYPOINT_LABEL", "ControlWindow", "ProcessingWorker"]
