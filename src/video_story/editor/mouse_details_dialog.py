"""The Mouse window: everything recorded about the mouse at the playhead.

Opened by a click on the mouse left of the transport. It does not block the editor: it
stays open beside it and follows the playhead while playing, stepping or dragging the
red line. Panels from top to bottom, each across the whole width, short fields side by
side inside a panel and multi-line fields across it (see :mod:`.mouse_details`):

``Buttons and wheel``  five boxes (Left, Middle, Right, X1 back, X2 forward; red while
                       held) and four wheel arrows (yellow while turning that way)
``Action``             the action in progress, its time, modifiers, injected, start,
                       now, end, scroll / gesture
``Element under mouse`` when it was read, role, name, bounds, parents, drop target and
                       the content (value, text at the click or the selection)
``Window``             title, process, since when
``Clipboard``          when it last changed, length, which copy, the text
``Recent mouse events`` timeline lines around the moment, the current one marked

Every field is read-only; its text can be selected and copied.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from video_story.core.timeline import format_timestamp

from .mouse_activity import PRESSED_FILL, WHEEL_FILL
from .mouse_details import (
    BUTTONS,
    RECENT_AFTER,
    RECENT_BEFORE,
    WHEEL_DIRECTIONS,
    MouseMoment,
)

TITLE = "Mouse"
PREFERRED_WIDTH = 1100
BUTTON_LABELS = {
    "left": "Left",
    "middle": "Middle",
    "right": "Right",
    "x1": "X1 back",
    "x2": "X2 forward",
}
WHEEL_ARROWS = {"up": "▲", "down": "▼", "left": "◀", "right": "▶"}
WHEEL_CELLS = {"up": (0, 1), "left": (1, 0), "right": (1, 2), "down": (2, 1)}
NOT_RECORDED = "Mouse actions were not recorded"
LOADING = "Loading..."
RECENT_ROWS = RECENT_BEFORE + 1 + RECENT_AFTER  # no scrolling for the usual lines

# (attribute of MouseMoment, label, columns it spans) per panel; four columns.
ACTION_FIELDS = (
    ("action", "Action", 1),
    ("action_time", "Time", 1),
    ("modifiers", "Modifiers", 1),
    ("injected", "Injected", 1),
    ("start", "Start", 1),
    ("now", "Now", 1),
    ("end", "End", 1),
    ("scroll", "Scroll / gesture", 1),
)
ELEMENT_FIELDS = (
    ("element_captured", "Captured", 1),
    ("element_role", "Role", 1),
    ("element_name", "Name", 1),
    ("element_bounds", "Bounds", 1),
    ("element_parents", "Parents", 2),
    ("drop_target", "Drop target", 2),
)
WINDOW_FIELDS = (
    ("window_title", "Title", 2),
    ("window_process", "Process", 1),
    ("window_since", "Since", 1),
)
CLIPBOARD_FIELDS = (
    ("clipboard_changed", "Changed", 2),
    ("clipboard_length", "Length", 1),
    ("clipboard_copy", "Copy", 1),
)


def _mono(widget: QWidget) -> None:
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSizeF(max(8.0, widget.font().pointSizeF() - 0.5))
    widget.setFont(font)


class MouseDetailsDialog(QDialog):
    """Non-modal window beside the editor; :meth:`show_moment` fills it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mouseDetails")
        self.setWindowTitle(TITLE)
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.fields: dict[str, QLineEdit] = {}
        self.texts: dict[str, QPlainTextEdit] = {}
        self.labels: dict[str, QLabel] = {}
        self.button_boxes: dict[str, QLabel] = {}
        self.wheel_arrows: dict[str, QLabel] = {}
        self._lit: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self.title_label = QLabel("Mouse")
        font = self.title_label.font()
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setBold(True)
        self.title_label.setFont(font)
        self.where_label = QLabel("")
        self.where_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        header.addWidget(self.title_label)
        header.addSpacing(12)
        header.addWidget(self.where_label, 1)
        layout.addLayout(header)

        layout.addWidget(self._buttons_panel())
        layout.addWidget(self._panel("Action", ACTION_FIELDS))
        layout.addWidget(self._panel("Element under mouse", ELEMENT_FIELDS, "content", "Content"))
        layout.addWidget(self._panel("Window", WINDOW_FIELDS))
        layout.addWidget(self._panel("Clipboard", CLIPBOARD_FIELDS, "clipboard_text", "Text"))
        recent = QGroupBox("Recent mouse events")
        recent_layout = QVBoxLayout(recent)
        self.texts["recent"] = self._text(recent, rows=RECENT_ROWS, wrap=False)
        recent_layout.addWidget(self.texts["recent"])
        layout.addWidget(recent)
        layout.addStretch(1)
        self._restyle()

    # Building ---------------------------------------------------------------

    def _buttons_panel(self) -> QGroupBox:
        box = QGroupBox("Buttons and wheel")
        row = QHBoxLayout(box)
        row.setSpacing(16)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for button in BUTTONS:
            label = QLabel(BUTTON_LABELS[button])
            label.setObjectName(f"mouseButton_{button}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setMinimumHeight(44)
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.button_boxes[button] = label
            buttons.addWidget(label, 1)
        row.addLayout(buttons, 1)
        wheel = QGridLayout()
        wheel.setSpacing(2)
        for direction in WHEEL_DIRECTIONS:
            arrow = QLabel(WHEEL_ARROWS[direction])
            arrow.setObjectName(f"wheel_{direction}")
            arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
            arrow.setFixedSize(32, 26)
            arrow.setToolTip(f"Wheel {direction}")
            self.wheel_arrows[direction] = arrow
            wheel.addWidget(arrow, *WHEEL_CELLS[direction])
        caption = QLabel("wheel")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        wheel.addWidget(caption, 1, 1)
        row.addLayout(wheel)
        return box

    def _panel(
        self,
        title: str,
        specs: tuple[tuple[str, str, int], ...],
        text_key: str | None = None,
        text_label: str = "",
    ) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        for column in range(4):
            grid.setColumnStretch(column, 1)
        row = column = 0
        for key, label_text, span in specs:
            if column + span > 4:
                row, column = row + 2, 0
            label = QLabel(label_text)
            field = QLineEdit(self)
            field.setReadOnly(True)
            field.setObjectName(key)
            _mono(field)
            label.setBuddy(field)
            grid.addWidget(label, row, column, 1, span)
            grid.addWidget(field, row + 1, column, 1, span)
            self.fields[key] = field
            self.labels[key] = label
            column += span
        layout.addLayout(grid)
        if text_key is not None:
            label = QLabel(text_label)
            self.labels[text_key] = label
            layout.addWidget(label)
            self.texts[text_key] = self._text(box, rows=4, wrap=True)
            self.texts[text_key].setObjectName(text_key)
            layout.addWidget(self.texts[text_key])
        return box

    def _text(self, parent: QWidget, rows: int, wrap: bool) -> QPlainTextEdit:
        text = QPlainTextEdit(parent)
        text.setReadOnly(True)
        _mono(text)
        text.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )
        height = text.fontMetrics().lineSpacing() * rows + 14
        if not wrap:  # long lines scroll sideways: the scroll bar must not hide a line
            height += text.horizontalScrollBar().sizeHint().height()
        text.setFixedHeight(height)
        return text

    # Colours ----------------------------------------------------------------

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange:
            self._restyle()  # the editor switched between the light and the dark theme

    def _restyle(self) -> None:
        palette = self.palette()
        border = palette.color(QPalette.ColorRole.WindowText)
        idle = (
            f"border: 1px solid {border.name()}; border-radius: 4px;"
            f" color: {palette.color(QPalette.ColorRole.WindowText).name()};"
            f" background: {palette.color(QPalette.ColorRole.Base).name()};"
        )
        held = f"border: 1px solid {border.name()}; border-radius: 4px; color: #ffffff;"
        held += f" background: {PRESSED_FILL};"
        turning = f"border: 1px solid {border.name()}; border-radius: 4px; color: #000000;"
        turning += f" background: {WHEEL_FILL};"
        self._styles = {"idle": idle, "held": held, "turning": turning}
        buttons, wheels = self._lit
        for button, box in self.button_boxes.items():
            box.setStyleSheet(held if button in buttons else idle)
        for direction, arrow in self.wheel_arrows.items():
            arrow.setStyleSheet(turning if direction in wheels else idle)

    # Filling ----------------------------------------------------------------

    @staticmethod
    def _set(widget: QLineEdit | QPlainTextEdit, text: str) -> None:
        """Only real changes: a selection the user made survives a still playhead."""
        if isinstance(widget, QLineEdit):
            if widget.text() != text:
                widget.setText(text)
                widget.setCursorPosition(0)
        elif widget.toPlainText() != text:
            widget.setPlainText(text)

    def _set_lit(self, held: dict[str, int], wheel: frozenset[str]) -> None:
        for button, box in self.button_boxes.items():
            text = BUTTON_LABELS[button]
            if button in held:
                text += f"\n{held[button] / 1000:.1f} s"
            if box.text() != text:
                box.setText(text)
        lit = (frozenset(held), frozenset(wheel))
        if lit != self._lit:
            self._lit = lit
            for button, box in self.button_boxes.items():
                box.setStyleSheet(self._styles["held" if button in lit[0] else "idle"])
            for direction, arrow in self.wheel_arrows.items():
                arrow.setStyleSheet(self._styles["turning" if direction in lit[1] else "idle"])

    def show_moment(self, moment: MouseMoment, where: str = "") -> None:
        """Fill every field for ``moment``; ``where`` (screen, cursor) goes in the header."""
        self.title_label.setText(f"Mouse at {format_timestamp(moment.time_ms)}")
        self.where_label.setText(where if moment.recorded else f"{where}   {NOT_RECORDED}".strip())
        self._set_lit(moment.held, moment.wheel)
        for key, field in self.fields.items():
            self._set(field, str(getattr(moment, key)))
        self.labels["content"].setText(moment.content_label)
        for key, text in self.texts.items():
            self._set(text, str(getattr(moment, key)))

    def show_message(self, message: str) -> None:
        """No moment to show: still loading, or no recording."""
        self.title_label.setText(TITLE)
        self.where_label.setText(message)
        self._set_lit({}, frozenset())
        for field in self.fields.values():
            self._set(field, "")
        for text in self.texts.values():
            self._set(text, "")
        self.labels["content"].setText("Content")

    def fit_to(self, editor: QWidget) -> None:
        """About PREFERRED_WIDTH wide (at most 80 % of the editor), tall enough for all
        panels when the screen allows it."""
        screen = editor.screen().availableGeometry() if editor.screen() else None
        width = min(PREFERRED_WIDTH, max(640, int(editor.width() * 0.8)))
        height = self.widget_height()
        if screen is not None:
            width = min(width, screen.width() - 40)
            height = min(height, screen.height() - 80)
        self.resize(width, height)
        center = editor.frameGeometry().center()
        self.move(center.x() - width // 2, center.y() - height // 2)

    def widget_height(self) -> int:
        scroll = self.findChild(QScrollArea)
        body = scroll.widget() if scroll is not None else None
        return (body.sizeHint().height() if body is not None else 700) + 4


__all__ = ["TITLE", "MouseDetailsDialog"]
