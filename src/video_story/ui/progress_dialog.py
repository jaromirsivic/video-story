"""Progress dialog shown while the audio is transcribed."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout, QWidget


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
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        layout.addWidget(self.bar)
        self.setMinimumWidth(360)

    def set_progress(self, fraction: float, message: str) -> None:
        self.bar.setValue(int(max(0.0, min(1.0, fraction)) * 100))
        if message:
            self.message_label.setText(message)

    def reject(self) -> None:  # cannot be cancelled
        return None


__all__ = ["ProgressDialog"]
