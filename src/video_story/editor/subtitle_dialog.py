"""Modal dialog that edits the text of the subtitles under the playhead.

The narration has two tracks. Where a subtitle of each lies under the playhead, the
dialog shows two multi-line text boxes, "Upper track" and "Lower track", each with its
own From and To; otherwise one box titled with the track of the one subtitle there.

From and To are shown read-only: the times come from whisper (or from dragging the
subtitle on the unlocked timeline) and the user corrects only the words here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from video_story.core.timeline import format_timestamp, parse_timestamp
from video_story.core.transcript import LOWER_TRACK, Segment

TIME_TEXT = re.compile(r"^\d{2,}:\d{2}:\d{2}(\.\d{1,3})?$")  # HH:MM:SS[.mmm]
TRACK_TITLES = {0: "Upper track", LOWER_TRACK: "Lower track"}


@dataclass
class _Part:
    """The fields of one subtitle in the dialog."""

    segment: Segment
    box: QGroupBox
    start_edit: QLineEdit
    end_edit: QLineEdit
    text_edit: QPlainTextEdit


def track_title(segment: Segment) -> str:
    return TRACK_TITLES.get(segment.track, TRACK_TITLES[0])


class SubtitleDialog(QDialog):
    """Edit the texts of one or two segments (their times are read-only); Save
    validates, Cancel discards.

    ``segments`` is one segment or a list of them (the upper one first). ``duration_ms``
    (when positive) is the upper limit of the times. After ``exec()`` returns
    ``Accepted``, :meth:`segments` holds the edited segments in the same order
    (:meth:`segment` the first one).
    """

    def __init__(
        self,
        segments: Segment | list[Segment],
        duration_ms: int = 0,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        originals = [segments] if isinstance(segments, Segment) else list(segments)
        if not originals:
            raise ValueError("no subtitle to edit")
        self.setWindowTitle("Edit Subtitle" if len(originals) == 1 else "Edit Subtitles")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._duration_ms = max(0, duration_ms)
        self._result: list[Segment] | None = None

        layout = QVBoxLayout(self)
        self.parts: list[_Part] = []
        for segment in originals:
            part = self._make_part(segment, len(originals) > 1)
            self.parts.append(part)
            layout.addWidget(part.box)
        first = self.parts[0]
        # The fields of the first subtitle, as before there were two tracks.
        self.start_edit = first.start_edit
        self.end_edit = first.end_edit
        self.text_edit = first.text_edit

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #C62828;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout.addWidget(self.error_label)
        layout.addWidget(self.buttons)
        self.text_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.text_edit.selectAll()

    def _make_part(self, segment: Segment, several: bool) -> _Part:
        box = QGroupBox(track_title(segment))
        start_edit = QLineEdit(format_timestamp(segment.start_ms))
        end_edit = QLineEdit(format_timestamp(segment.end_ms))
        for edit in (start_edit, end_edit):
            edit.setPlaceholderText("HH:MM:SS.mmm")
            edit.setMaximumWidth(140)
            font = edit.font()
            font.setFamily("Consolas")
            font.setStyleHint(font.StyleHint.Monospace)
            edit.setFont(font)
            # The times stay: only the words can be corrected here.
            edit.setReadOnly(True)
            edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            edit.setToolTip("The time of a subtitle cannot be changed here; edit the text only")
        text_edit = QPlainTextEdit(segment.text.strip())
        text_edit.setTabChangesFocus(True)
        text_edit.setMinimumHeight(text_edit.fontMetrics().height() * (4 if several else 5))
        form = QFormLayout(box)
        form.addRow("From", start_edit)
        form.addRow("To", end_edit)
        form.addRow("Text", text_edit)
        return _Part(segment, box, start_edit, end_edit, text_edit)

    def segment(self) -> Segment | None:
        """The first edited segment once the dialog was accepted, else ``None``."""
        return self._result[0] if self._result else None

    def segments(self) -> list[Segment] | None:
        """The edited segments (same order as given) once accepted, else ``None``."""
        return list(self._result) if self._result is not None else None

    def _validate_part(self, part: _Part) -> Segment | str:
        prefix = f"{part.box.title()}: " if len(self.parts) > 1 else ""
        times: list[int] = []
        for name, edit in (("From", part.start_edit), ("To", part.end_edit)):
            text = edit.text().strip()
            if not TIME_TEXT.match(text):
                return f"{prefix}{name}: enter a time as HH:MM:SS.mmm."
            times.append(parse_timestamp(text if "." in text else text + ".000"))
        start, end = times
        if end <= start:
            return f"{prefix}To must be later than From."
        if self._duration_ms and end > self._duration_ms:
            limit = format_timestamp(self._duration_ms)
            return f"{prefix}To must not be after the end of the recording ({limit})."
        text = " ".join(part.text_edit.toPlainText().split())
        if not text:
            return f"{prefix}The text must not be empty."
        return replace(part.segment, start_ms=start, end_ms=end, text=text)

    def accept(self) -> None:
        results: list[Segment] = []
        for part in self.parts:
            result = self._validate_part(part)
            if isinstance(result, str):
                self.error_label.setText(result)
                self.error_label.show()
                return
            results.append(result)
        self._result = results
        super().accept()


__all__ = ["TRACK_TITLES", "SubtitleDialog", "track_title"]
