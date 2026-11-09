"""Dialogs shown after Stop: choose what to do, and confirm discarding."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from .icons import discard_icon, pencil_icon, record_icon

BUTTON_ICON_SIZE = QSize(20, 20)


class StopChoice(Enum):
    # A stopped recording always goes through the editor: only its Process button hands
    # the recording to the agent.
    EDITOR = "editor"  # save the recording and open it in the Video Story Editor
    CONTINUE = "continue"
    DISCARD = "discard"


class StopDialog(QDialog):
    """Three choices. Recording keeps running while this dialog is open."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stop Recording")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setModal(True)
        self.choice: StopChoice = StopChoice.CONTINUE

        layout = QVBoxLayout(self)
        info = QLabel("Recording continues until you choose Video Editor or discard it.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.editor_button = QPushButton(pencil_icon(), "Video Editor")
        self.editor_button.setToolTip(
            "Stop recording and open it at once in the Video Story Editor, which transcribes "
            "the audio in the background while you crop it; its green Process button hands "
            "the result to the agent"
        )
        # Video Editor is the first and default choice (Enter picks it).
        self.editor_button.setDefault(True)
        self.editor_button.setAutoDefault(True)
        self.continue_button = QPushButton(record_icon(), "Continue Recording")
        self.continue_button.setToolTip("Close this dialog and keep recording")
        self.discard_button = QPushButton(discard_icon(), "Discard Recording")
        self.discard_button.setToolTip("Delete all files of this recording")
        for button, choice in (
            (self.editor_button, StopChoice.EDITOR),
            (self.continue_button, StopChoice.CONTINUE),
            (self.discard_button, StopChoice.DISCARD),
        ):
            button.setIconSize(BUTTON_ICON_SIZE)
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
    delete.setIcon(discard_icon())
    keep = box.addButton("Keep Recording", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is delete


__all__ = ["StopChoice", "StopDialog", "confirm_discard"]
