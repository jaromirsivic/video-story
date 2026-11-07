"""The small always-on-top control window: Record/Stop, Settings, Draw on Screen."""

from __future__ import annotations

import logging
from collections.abc import Callable

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

from video_story.core.timeline import format_timestamp
from video_story.host.controller import SessionController

from .icons import draw_icon, record_icon, settings_icon, stop_icon
from .progress_dialog import ProgressDialog
from .settings_dialog import SettingsDialog
from .stop_dialog import StopChoice, StopDialog, confirm_discard

log = logging.getLogger(__name__)

UI_TICK_MS = 200


class ProcessingWorker(QObject):
    """Runs the blocking processing step in a QThread."""

    progress = Signal(float, str)
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, controller: SessionController) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        try:
            final_dir = self._controller.process(progress=self.progress.emit)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(str(final_dir))


class ControlWindow(QWidget):
    """Main window. ``on_finished`` is called with the final status when the session ends."""

    def __init__(
        self,
        controller: SessionController,
        on_finished: Callable[[str], None],
        overlay_factory: Callable[[], QObject] | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self._on_finished = on_finished
        self._overlay_factory = overlay_factory
        self._overlay: QObject | None = None
        self._processing = False
        self._done = False
        self._thread: QThread | None = None
        self._worker: ProcessingWorker | None = None
        self._progress_dialog: ProgressDialog | None = None

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

    def _on_record(self) -> None:
        try:
            self.controller.start_recording()
        except Exception as exc:
            log.exception("cannot start recording")
            QMessageBox.critical(self, "Cannot start recording", f"{exc}")
            return
        self.record_button.setIcon(stop_icon())
        self.record_button.setText("Stop")
        self.record_button.setToolTip("Stop and decide what to do with the recording")
        self.settings_button.setEnabled(False)
        self.draw_button.setEnabled(True)
        self._timer.start()
        self._tick()

    def _on_stop(self) -> None:
        while True:
            dialog = StopDialog(self)
            dialog.exec()
            if self.controller.recorder is not None and self.controller.recorder.auto_stopped:
                # Auto stop fired while the dialog was open: processing takes over.
                self._begin_processing()
                return
            choice = dialog.choice
            if choice is StopChoice.CONTINUE:
                return
            if choice is StopChoice.PROCESS:
                self._begin_processing()
                return
            if confirm_discard(self):
                self._discard()
                return
            # Not confirmed: back to the Stop dialog, recording still running.

    def _discard(self) -> None:
        self._close_overlay()
        try:
            self.controller.discard()
        except Exception as exc:
            log.exception("discard failed")
            QMessageBox.critical(self, "Discard failed", f"{exc}")
        self._timer.stop()
        self.record_button.setIcon(record_icon())
        self.record_button.setText("Record")
        self.record_button.setToolTip("Start recording audio, screens and cursor")
        self.settings_button.setEnabled(True)
        self.draw_button.setEnabled(False)
        self.status_label.setText("Recording discarded. Press Record to start again.")

    # Processing -------------------------------------------------------------

    def _begin_processing(self) -> None:
        if self._processing:
            return
        self._processing = True
        self._timer.stop()
        self._close_overlay()
        self.record_button.setEnabled(False)
        self.draw_button.setEnabled(False)
        self.status_label.setText("Processing recording...")

        self._progress_dialog = ProgressDialog(self)
        self._thread = QThread(self)
        self._worker = ProcessingWorker(self.controller)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._progress_dialog.set_progress)
        self._worker.finished.connect(self._on_processed)
        self._worker.failed.connect(self._on_processing_failed)
        self._thread.start()
        self._progress_dialog.show()

    def _stop_thread(self) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.hide()
            self._progress_dialog.deleteLater()
            self._progress_dialog = None
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
            self._thread = None
        self._worker = None

    def _on_processed(self, final_dir: str) -> None:
        self._stop_thread()
        self._done = True
        log.info("processed: %s", final_dir)
        self._finish("processed")

    def _on_processing_failed(self, message: str) -> None:
        self._stop_thread()
        self._done = True
        QMessageBox.critical(
            self,
            "Processing failed",
            f"{message}\n\nThe recorded files were kept in the work folder.",
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
        dialog = SettingsDialog(self.controller.config, self)
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
                except Exception as exc:
                    log.exception("cannot create overlay")
                    QMessageBox.warning(self, "Draw on Screen", f"Drawing is not available: {exc}")
                    self.draw_button.setChecked(False)
                    return
            if self._overlay is not None:
                self._overlay.show()  # type: ignore[attr-defined]
                self.raise_()
                self.activateWindow()
        elif self._overlay is not None:
            self._overlay.hide()  # type: ignore[attr-defined]

    def _overlay_closed(self) -> None:
        self.draw_button.blockSignals(True)
        self.draw_button.setChecked(False)
        self.draw_button.blockSignals(False)

    def _close_overlay(self) -> None:
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
        if recorder is None or self._processing:
            return
        if recorder.auto_stopped:
            self.status_label.setText("Auto stop reached. Processing...")
            self._begin_processing()
            return
        if not recorder.running and self.controller.summary is None:
            # Capture thread died: surface the error and stop cleanly.
            self._timer.stop()
            QMessageBox.critical(
                self, "Recording stopped", "Screen capture stopped unexpectedly. See recording.log."
            )
            self._begin_processing()
            return
        elapsed = format_timestamp(recorder.elapsed_ms)
        remaining = max(0, self.controller.config.auto_stop_ms - recorder.elapsed_ms)
        self.status_label.setText(
            f"Recording {elapsed[:-4]}  |  {len(recorder.screens)} screen(s), "
            f"{recorder.frame_count} frames  |  auto stop in {format_timestamp(remaining)[:-4]}"
        )

    # Close ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._done:
            event.accept()
            return
        if self._processing:
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


__all__ = ["ControlWindow", "ProcessingWorker"]
