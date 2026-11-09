"""The **Instructions** dialog: master instructions for the AI about the whole recording."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from video_story.core.instructions import MAX_INSTRUCTIONS_CHARS, normalize_instructions

TITLE = "Instructions for the AI"
HEADING = "Master instructions for the AI about the whole recording"
EXPLANATION = (
    "These instructions frame everything you said in the recording: the AI reads them first "
    "and applies them to the cropped transcript and the screenshots. Say what you want to get "
    "out of this recording, for example “Create an implementation plan from what I "
    "describe” or “Answer in Czech and keep the existing code style”. "
    "Leave the box empty when the recording speaks for itself."
)
PLACEHOLDER = "e.g. Create an implementation plan from what I describe in this recording."


class InstructionsDialog(QDialog):
    """Edit the instructions; ``instructions()`` is the normalised text after OK."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(TITLE)
        self.setModal(True)
        layout = QVBoxLayout(self)
        heading = QLabel(HEADING)
        heading_font = QFont(heading.font())
        heading_font.setBold(True)
        heading_font.setPointSizeF(heading_font.pointSizeF() + 1)
        heading.setFont(heading_font)
        layout.addWidget(heading)
        explanation = QLabel(EXPLANATION)
        explanation.setWordWrap(True)
        explanation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(explanation)
        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setPlaceholderText(PLACEHOLDER)
        self.text_edit.setPlainText(text)
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.setMinimumSize(520, 220)
        layout.addWidget(self.text_edit, 1)
        self.counter = QLabel("")
        self.counter.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.counter)
        self.text_edit.textChanged.connect(self._update_counter)
        self._update_counter()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.text_edit.setFocus()

    def _update_counter(self) -> None:
        count = len(self.text_edit.toPlainText())
        self.counter.setText(f"{count} / {MAX_INSTRUCTIONS_CHARS} characters")

    def instructions(self) -> str:
        return normalize_instructions(self.text_edit.toPlainText())


__all__ = ["EXPLANATION", "HEADING", "InstructionsDialog", "TITLE"]
