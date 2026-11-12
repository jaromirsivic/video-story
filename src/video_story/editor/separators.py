"""Separator lines of the editor's right side.

The preview and the timeline below it are divided by a splitter whose handle draws two
thin lines 4 px apart (it can be dragged); the picture and the transport below it are
divided by one thin line of the same color (:class:`SeparatorLine`, it cannot be
dragged). Both take the color from the palette, so they follow the light/dark theme.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QSplitter, QSplitterHandle, QWidget

HANDLE_LINE_GAP = 4  # pixels between the two lines of a draggable splitter handle
HANDLE_WIDTH = 9  # the splitter handle: both lines and a margin around them
SEPARATOR_LINE_HEIGHT = 3  # the fixed (not draggable) single line widget


def splitter_line_color(palette: QPalette) -> QColor:
    """The color of the separator lines, visible on light and dark backgrounds."""
    window = palette.color(QPalette.ColorRole.Window)
    if window.lightness() >= 128:
        return palette.color(QPalette.ColorRole.Dark)
    return window.lighter(220)


class SeparatorHandle(QSplitterHandle):
    """Splitter handle with two thin lines across, ``HANDLE_LINE_GAP`` px apart: the
    double line tells it can be dragged (the style's grip dots alone vanish on a white
    background)."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(QPen(splitter_line_color(self.palette()), 1))
        first = -(HANDLE_LINE_GAP // 2)
        second = first + HANDLE_LINE_GAP
        if self.orientation() == Qt.Orientation.Vertical:  # horizontal lines
            middle = self.height() // 2
            for y in (middle + first, middle + second):
                painter.drawLine(0, y, self.width(), y)
        else:
            middle = self.width() // 2
            for x in (middle + first, middle + second):
                painter.drawLine(x, 0, x, self.height())
        painter.end()


class SeparatorSplitter(QSplitter):
    """QSplitter whose handles draw :class:`SeparatorHandle`'s double line."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(HANDLE_WIDTH)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802
        return SeparatorHandle(self.orientation(), self)


class SeparatorLine(QWidget):
    """One thin horizontal line in the color of the splitter handles; not draggable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(SEPARATOR_LINE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(QPen(splitter_line_color(self.palette()), 1))
        y = self.height() // 2
        painter.drawLine(0, y, self.width(), y)
        painter.end()


__all__ = [
    "HANDLE_LINE_GAP",
    "HANDLE_WIDTH",
    "SeparatorHandle",
    "SeparatorLine",
    "SeparatorSplitter",
    "splitter_line_color",
]
