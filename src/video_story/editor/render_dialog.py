"""Render flow: Save As dialog, export in a QThread with progress and Cancel, result box."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from video_story.core.config import Config
from video_story.export import ExportError, ExportRequest, ExportResult

log = logging.getLogger(__name__)

# export_fn(request, config, progress=..., cancel=...) -> ExportResult
ExportFn = Callable[..., ExportResult]


def default_export_fn(
    request: ExportRequest,
    config: Config,
    progress: Callable[[float, str], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> ExportResult:
    """The real FFmpeg export (imported lazily: the module may be installed later)."""
    from video_story.export.ffmpeg_export import export_recording

    return export_recording(request, config, progress=progress, cancel=cancel)


def default_output_path(config: Config, session_id: str, screen_id: str) -> Path:
    return config.recording_folder / f"{session_id}_{screen_id}.mp4"


def ask_output_path(parent: QWidget | None, suggested: Path) -> Path | None:
    """Save As dialog; ``None`` when cancelled. Ensures the ``.mp4`` suffix."""
    chosen, _filter = QFileDialog.getSaveFileName(
        parent, "Render video", str(suggested), "MP4 video (*.mp4)"
    )
    if not chosen:
        return None
    path = Path(chosen)
    if path.suffix.lower() != ".mp4":
        path = path.with_suffix(".mp4")
    return path


class ExportWorker(QObject):
    """Runs ``export_fn`` in a worker thread, relaying progress and the cancel flag."""

    progress = Signal(float, str)
    finished = Signal(object)  # ExportResult
    failed = Signal(str)

    def __init__(self, request: ExportRequest, config: Config, export_fn: ExportFn) -> None:
        super().__init__()
        self._request = request
        self._config = config
        self._export_fn = export_fn
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            result = self._export_fn(
                self._request,
                self._config,
                progress=self.progress.emit,
                cancel=lambda: self._cancelled,
            )
        except ExportError as exc:
            self.failed.emit(f"{exc} ({exc.code})" if exc.code else str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - shown to the user
            log.exception("export failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class RenderProgressDialog(QDialog):
    """Modal progress dialog with a Cancel button; closing it also cancels."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rendering")
        self.setModal(True)
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.CustomizeWindowHint | Qt.WindowType.WindowTitleHint
        )
        layout = QVBoxLayout(self)
        self.message_label = QLabel("Preparing export")
        layout.addWidget(self.message_label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.setMinimumWidth(420)
        self.result: ExportResult | None = None
        self.error: str | None = None

    # Slots: the worker's signals land here (queued into the GUI thread).

    def finish_with(self, result: object) -> None:
        self.result = result  # type: ignore[assignment]
        self.accept()

    def fail_with(self, message: str) -> None:
        self.error = message
        self.done(QDialog.DialogCode.Rejected)

    def set_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 1000))
        if message:
            self.message_label.setText(message)

    def _on_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.message_label.setText("Cancelling")
        self.cancel_requested.emit()

    def reject(self) -> None:  # Esc / close box = Cancel, the dialog stays until the worker ends
        if self.cancel_button.isEnabled():
            self._on_cancel()


def run_render(
    parent: QWidget | None,
    config: Config,
    request: ExportRequest,
    export_fn: ExportFn | None = None,
    show_result: bool = True,
) -> ExportResult | None:
    """Export ``request`` with a progress dialog; returns the result or ``None``.

    Blocks the caller in a nested event loop while the worker thread runs. The result
    (or the error) is shown in a message box unless ``show_result`` is False.
    """
    worker = ExportWorker(request, config, export_fn or default_export_fn)
    thread = QThread()
    worker.moveToThread(thread)
    dialog = RenderProgressDialog(parent)
    # Connect to methods of the dialog (a GUI-thread QObject) so the calls are queued;
    # a plain Python callable would run on the worker thread.
    thread.started.connect(worker.run)
    worker.progress.connect(dialog.set_progress)
    worker.finished.connect(dialog.finish_with)
    worker.failed.connect(dialog.fail_with)
    dialog.cancel_requested.connect(worker.cancel, Qt.ConnectionType.DirectConnection)
    thread.start()
    try:
        dialog.exec()
    finally:
        thread.quit()
        thread.wait(30_000)
        worker.deleteLater()
        dialog.deleteLater()

    result = dialog.result
    error = dialog.error
    if show_result:
        if result is not None:
            lines = [f"Video saved to:\n{result.output}"]
            if result.srt:
                lines.append(f"Subtitles saved to:\n{result.srt}")
            lines.append(
                f"{result.width}x{result.height}, {result.duration_ms / 1000:.1f} s, "
                f"screen {result.screen_id}"
            )
            QMessageBox.information(parent, "Render finished", "\n\n".join(lines))
        elif worker.cancelled:
            QMessageBox.information(parent, "Render cancelled", "The export was cancelled.")
        else:
            QMessageBox.critical(parent, "Render failed", error or "Unknown error")
    return result


__all__ = [
    "ExportFn",
    "ExportWorker",
    "RenderProgressDialog",
    "ask_output_path",
    "default_export_fn",
    "default_output_path",
    "run_render",
]
