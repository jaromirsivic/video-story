"""Dialogs shown after Stop: choose what to do, and confirm discarding."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget


class StopChoice(Enum):
    PROCESS = "process"
    EDITOR = "editor"  # process, then open the recording in the Video Story Editor
    CONTINUE = "continue"
    DISCARD = "discard"


class StopDialog(QDialog):
    """Four choices. Recording keeps running while this dialog is open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stop Recording")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)
        self.choice: StopChoice = StopChoice.CONTINUE

        layout = QVBoxLayout(self)
        info = QLabel(
            "Recording continues until you choose Process Recording, Video Editor or discard it."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.process_button = QPushButton("Process Recording")
        self.process_button.setToolTip("Stop recording and transcribe the audio")
        self.process_button.setDefault(True)
        self.editor_button = QPushButton("Video Editor")
        self.editor_button.setToolTip(
            "Stop recording, transcribe the audio and open the recording in the Video Story "
            "Editor to crop it before the agent processes it"
        )
        self.continue_button = QPushButton("Continue Recording")
        self.continue_button.setToolTip("Close this dialog and keep recording")
        self.discard_button = QPushButton("Discard Recording")
        self.discard_button.setToolTip("Delete all files of this recording")
        for button, choice in (
            (self.process_button, StopChoice.PROCESS),
            (self.editor_button, StopChoice.EDITOR),
            (self.continue_button, StopChoice.CONTINUE),
            (self.discard_button, StopChoice.DISCARD),
        ):
            button.clicked.connect(lambda _checked=False, c=choice: self._pick(c))
            layout.addWidget(button)
        self.setMinimumWidth(340)

    def _pick(self, choice: StopChoice) -> None:
        self.choice = choice
        self.accept()

    def reject(self) -> None:  # Esc / close = continue recording
        self.choice = StopChoice.CONTINUE
        super().reject()


def confirm_discard(parent: QWidget | None = None) -> bool:
    """Second dialog: returns True only when the user confirms deletion."""
    box = QMessageBox(parent)
    box.setWindowTitle("Discard Recording")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setText("Delete this recording?")
    box.setInformativeText(
        "All files of this recording (audio, screenshots and metadata) will be deleted. "
        "This cannot be undone."
    )
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    delete = box.addButton("Delete Recording", QMessageBox.ButtonRole.DestructiveRole)
    keep = box.addButton("Keep Recording", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is delete


__all__ = ["StopChoice", "StopDialog", "confirm_discard"]
