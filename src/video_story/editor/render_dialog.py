"""Render Video dialog (screen and MP4 file), the Save As dialog and the export worker."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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


SELECT_FILE_PLACEHOLDER = "Select File Name"
RENDER_DIALOG_TITLE = "Render Video"


class FileNameEdit(QLineEdit):
    """Read-only field that asks for the file name when clicked."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setPlaceholderText(SELECT_FILE_PLACEHOLDER)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click to choose where the MP4 video is saved")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class RenderVideoDialog(QDialog):
    """**Render Video...**: the screen to render and the MP4 file to write.

    ``screens`` are ``(screen id, label)`` pairs; ``suggest(screen_id)`` is the file
    name offered by the Save As dialog. The field starts empty (placeholder
    ``Select File Name``); a click on it opens the Save As dialog (MP4 only). Render
    is enabled once a file is chosen.
    """

    def __init__(
        self,
        screens: list[tuple[str, str]],
        default_screen: str,
        suggest: Callable[[str], Path],
        parent: QWidget | None = None,
        title: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(RENDER_DIALOG_TITLE)
        self.setModal(True)
        self._suggest = suggest
        self._output: Path | None = None
        layout = QVBoxLayout(self)
        if title:
            layout.addWidget(QLabel(title))
        form = QFormLayout()
        self.screen_combo = QComboBox()
        for screen_id, label in screens:
            self.screen_combo.addItem(label, screen_id)
        index = self.screen_combo.findData(default_screen)
        self.screen_combo.setCurrentIndex(max(0, index))
        form.addRow("Screen", self.screen_combo)
        self.file_edit = FileNameEdit()
        self.file_edit.clicked.connect(self.choose_file)
        form.addRow("File", self.file_edit)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        self.render_button = QPushButton("Render")
        self.render_button.setDefault(True)
        self.render_button.setEnabled(False)
        self.render_button.clicked.connect(self.accept)
        row.addWidget(self.render_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.setMinimumWidth(460)

    @property
    def screen_id(self) -> str:
        return str(self.screen_combo.currentData())

    @property
    def output(self) -> Path | None:
        """The chosen MP4 file, ``None`` until one is chosen."""
        return self._output

    def set_output(self, path: Path | None) -> None:
        self._output = path
        self.file_edit.setText(str(path) if path is not None else "")
        self.render_button.setEnabled(path is not None)

    def choose_file(self) -> Path | None:
        """The Save As dialog (MP4 only); a cancelled dialog keeps the previous choice."""
        suggested = self._output or self._suggest(self.screen_id)
        chosen = ask_output_path(self, suggested)
        if chosen is not None:
            self.set_output(chosen)
        return chosen


__all__ = [
    "SELECT_FILE_PLACEHOLDER",
    "ExportFn",
    "ExportWorker",
    "FileNameEdit",
    "RenderVideoDialog",
    "ask_output_path",
    "default_export_fn",
    "default_output_path",
]
