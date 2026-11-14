"""Separator lines of the editor's right side.

The preview and the timeline below it are divided by a splitter whose handle draws two
thin lines 4 px apart (it can be dragged); the picture and the transport below it are
divided by one thin line of the same color (:class:`SeparatorLine`, it cannot be
dragged). Both take the color from the palette, so they follow the light/dark theme.

Every line is exactly one physical pixel thick and lies on the physical pixel grid of
the window, also with a fractional display scale (125 %, 150 %): a 1 px pen at such a
scale falls between physical pixels depending on where the widget is, and one of the
two lines of the handle could vanish while it was dragged. The handle is also repainted
whole after every move.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPalette
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


def line_offsets(origin: float, length: float, dpr: float, count: int) -> list[float]:
    """Where to draw ``count`` (1 or 2) lines across a widget ``length`` logical pixels
    long whose first pixel lies at ``origin`` logical pixels in the window: offsets in
    logical pixels from the widget's start, each line one physical pixel thick
    (``1 / dpr`` logical) on a whole physical pixel. Two lines are centred, a whole
    number of physical pixels apart (``HANDLE_LINE_GAP`` scaled, at least 2)."""
    dpr = dpr if dpr > 0 else 1.0
    start = origin * dpr
    end = (origin + length) * dpr
    center = (start + end) / 2.0
    if count <= 1:
        rows = [math.floor(center - 0.5)] if length % 2 else [math.floor(center)]
    else:
        gap = max(2, round(HANDLE_LINE_GAP * dpr))
        first = math.floor(center - gap / 2.0)
        rows = [first, first + gap]
    low, high = math.ceil(start), math.floor(end) - 1  # physical rows inside the widget
    return [(min(max(row, low), high) - start) / dpr for row in rows]


def paint_lines(
    painter: QPainter,
    widget: QWidget,
    orientation: Qt.Orientation,
    color: QColor,
    count: int,
) -> None:
    """Draw ``count`` lines across ``widget`` (horizontal lines for a vertical layout),
    aligned to the physical pixels of its window (see :func:`line_offsets`)."""
    dpr = widget.devicePixelRatioF() or 1.0
    origin = widget.mapTo(widget.window(), QPoint(0, 0))
    thickness = 1.0 / dpr
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    if orientation == Qt.Orientation.Vertical:  # horizontal lines
        for offset in line_offsets(origin.y(), widget.height(), dpr, count):
            painter.fillRect(QRectF(0, offset, widget.width(), thickness), color)
    else:
        for offset in line_offsets(origin.x(), widget.width(), dpr, count):
            painter.fillRect(QRectF(offset, 0, thickness, widget.height()), color)


class SeparatorHandle(QSplitterHandle):
    """Splitter handle with two thin lines across, ``HANDLE_LINE_GAP`` px apart: the
    double line tells it can be dragged (the style's grip dots alone vanish on a white
    background)."""

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_lines(painter, self, self.orientation(), splitter_line_color(self.palette()), 2)
        painter.end()


class SeparatorSplitter(QSplitter):
    """QSplitter whose handles draw :class:`SeparatorHandle`'s double line."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(HANDLE_WIDTH)
        # A move shifts the neighbours; with a fractional display scale their pixels can
        # land on the handle's: paint the whole handle again afterwards.
        self.splitterMoved.connect(self._repaint_handles)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802
        return SeparatorHandle(self.orientation(), self)

    def _repaint_handles(self, _pos: int = 0, _index: int = 0) -> None:
        for index in range(1, self.count()):
            self.handle(index).update()


class SeparatorLine(QWidget):
    """One thin horizontal line in the color of the splitter handles; not draggable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(SEPARATOR_LINE_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        paint_lines(painter, self, Qt.Orientation.Vertical, splitter_line_color(self.palette()), 1)
        painter.end()


__all__ = [
    "HANDLE_LINE_GAP",
    "HANDLE_WIDTH",
    "SEPARATOR_LINE_HEIGHT",
    "SeparatorHandle",
    "SeparatorLine",
    "SeparatorSplitter",
    "line_offsets",
    "paint_lines",
    "splitter_line_color",
]
