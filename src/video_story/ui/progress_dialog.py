"""Progress dialog shown while the audio is transcribed.

The progress text is one or two lines: the first is the step ("Transcribing audio,
this may take a while"), the optional second shows where whisper is in the recording
(``HH:MM:SS  latest transcribed text``). The second line is shown on a row of its own,
cut to the dialog width and always ending in an ellipsis.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout, QWidget

ELLIPSIS = "…"


class ProgressDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, title: str = "Transcribing Audio") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setModal(True)
        layout = QVBoxLayout(self)
        self.title_label = QLabel(title)
        font = self.title_label.font()
        font.setBold(True)
        self.title_label.setFont(font)
        layout.addWidget(self.title_label)
        self.message_label = QLabel("Preparing audio")
        layout.addWidget(self.message_label)
        self.detail_label = QLabel("")
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.hide()
        layout.addWidget(self.detail_label)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        self.setMinimumWidth(420)
        self._detail = ""

    def set_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        if not message:
            return
        first, _, detail = message.partition("\n")
        self.message_label.setText(first)
        self._detail = detail.strip()
        self._show_detail()

    def _show_detail(self) -> None:
        if not self._detail:
            self.detail_label.hide()
            return
        width = max(100, self.contentsRect().width() - 24)
        metrics = self.detail_label.fontMetrics()
        text = metrics.elidedText(self._detail, Qt.TextElideMode.ElideRight, width)
        if not text.endswith(ELLIPSIS):
            text += ELLIPSIS
        self.detail_label.setText(text)
        self.detail_label.setToolTip(self._detail)
        self.detail_label.show()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._show_detail()

    def reject(self) -> None:  # cannot be cancelled
        return None


__all__ = ["ProgressDialog"]
