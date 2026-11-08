"""Modal dialog that edits one subtitle (transcript segment): From, To and the text."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from video_story.core.timeline import format_timestamp, parse_timestamp
from video_story.core.transcript import Segment

TIME_TEXT = re.compile(r"^\d{2,}:\d{2}:\d{2}(\.\d{1,3})?$")  # HH:MM:SS[.mmm]


class SubtitleDialog(QDialog):
    """Edit the time range and the text of one segment; Save validates, Cancel discards.

    ``duration_ms`` (when positive) is the upper limit of both times. After ``exec()``
    returns ``Accepted``, :meth:`segment` holds the edited segment.
    """

    def __init__(self, segment: Segment, duration_ms: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Edit Subtitle")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._duration_ms = max(0, duration_ms)
        self._result: Segment | None = None

        self.start_edit = QLineEdit(format_timestamp(segment.start_ms))
        self.end_edit = QLineEdit(format_timestamp(segment.end_ms))
        for edit in (self.start_edit, self.end_edit):
            edit.setPlaceholderText("HH:MM:SS.mmm")
            edit.setMaximumWidth(140)
            font = edit.font()
            font.setFamily("Consolas")
            font.setStyleHint(font.StyleHint.Monospace)
            edit.setFont(font)
        self.text_edit = QPlainTextEdit(segment.text.strip())
        self.text_edit.setTabChangesFocus(True)
        self.text_edit.setMinimumHeight(self.text_edit.fontMetrics().height() * 5)

        form = QFormLayout()
        form.addRow("From", self.start_edit)
        form.addRow("To", self.end_edit)
        form.addRow("Text", self.text_edit)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.text_edit.selectAll()

    def segment(self) -> Segment | None:
        """The edited segment once the dialog was accepted, else ``None``."""
        return self._result

    def _validate(self) -> Segment | str:
        times: list[int] = []
        for name, edit in (("From", self.start_edit), ("To", self.end_edit)):
            text = edit.text().strip()
            if not TIME_TEXT.match(text):
                return f"{name}: enter a time as HH:MM:SS.mmm."
            times.append(parse_timestamp(text if "." in text else text + ".000"))
        start, end = times
        if end <= start:
            return "To must be later than From."
        if self._duration_ms and end > self._duration_ms:
            limit = format_timestamp(self._duration_ms)
            return f"To must not be after the end of the recording ({limit})."
        text = " ".join(self.text_edit.toPlainText().split())
        if not text:
            return "The text must not be empty."
        return Segment(start, end, text)

    def accept(self) -> None:
        result = self._validate()
        if isinstance(result, str):
            self.error_label.setText(result)
            self.error_label.show()
            return
        self._result = result
        super().accept()


__all__ = ["SubtitleDialog"]
