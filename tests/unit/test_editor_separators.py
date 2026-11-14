"""Separators of the editor's right side: the fixed line under the preview picture and the
draggable double-line splitter handle above the timeline."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPalette  # noqa: E402
from PySide6.QtWidgets import QLabel, QWidget  # noqa: E402

from video_story.editor.preview import PreviewPanel  # noqa: E402
from video_story.editor.separators import (  # noqa: E402
    HANDLE_LINE_GAP,
    HANDLE_WIDTH,
    SEPARATOR_LINE_HEIGHT,
    SeparatorLine,
    SeparatorSplitter,
    line_offsets,
    splitter_line_color,
)


def _palette(window: str) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(window))
    palette.setColor(QPalette.ColorRole.Dark, QColor("#A0A0A0"))
    return palette


def test_line_color_follows_the_theme():
    assert splitter_line_color(_palette("#F0F0F0")) == QColor("#A0A0A0")  # light: Dark role
    dark = QColor("#202020")
    assert splitter_line_color(_palette("#202020")) == dark.lighter(220)


def test_preview_has_a_fixed_line_between_the_picture_and_the_controls(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    layout = panel.layout()
    assert layout.itemAt(0).widget() is panel.view
    assert layout.itemAt(1).widget() is panel.separator
    assert isinstance(panel.separator, SeparatorLine)
    assert panel.separator.minimumHeight() == panel.separator.maximumHeight()  # not draggable
    assert panel.separator.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_separator_line_paints_one_line(qtbot):
    line = SeparatorLine()
    qtbot.addWidget(line)
    line.setPalette(_palette("#F0F0F0"))
    line.resize(50, line.height())
    image = line.grab().toImage()
    middle = line.height() // 2
    rows = [y for y in range(image.height()) if image.pixelColor(25, y) == QColor("#A0A0A0")]
    assert rows == [middle]


def test_splitter_handle_paints_two_lines_4_px_apart(qtbot):
    splitter = SeparatorSplitter(Qt.Orientation.Vertical)
    qtbot.addWidget(splitter)
    splitter.setPalette(_palette("#F0F0F0"))
    splitter.addWidget(QLabel("top"))
    splitter.addWidget(QWidget())
    splitter.resize(200, 200)
    splitter.show()
    assert splitter.handleWidth() == HANDLE_WIDTH
    handle = splitter.handle(1)
    image = handle.grab().toImage()
    rows = [y for y in range(image.height()) if image.pixelColor(100, y) == QColor("#A0A0A0")]
    assert len(rows) == 2 and rows[1] - rows[0] == HANDLE_LINE_GAP == 4


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 1.75, 2.0])
@pytest.mark.parametrize("origin", range(0, 9))
def test_handle_lines_land_on_whole_physical_pixels(dpr: float, origin: int):
    """With a fractional display scale both lines stay one physical pixel each, apart
    (the second one vanished at some splitter positions at 125 %)."""
    offsets = line_offsets(origin, HANDLE_WIDTH, dpr, 2)
    rows = [(origin + offset) * dpr for offset in offsets]
    assert all(abs(row - round(row)) < 1e-6 for row in rows)
    first, second = (round(row) for row in rows)
    assert second - first == max(2, round(HANDLE_LINE_GAP * dpr))
    assert origin * dpr <= first + 1e-6 and second + 1 <= (origin + HANDLE_WIDTH) * dpr + 1e-6
    # Painted where Qt puts a widget at ``origin``: exactly two separate physical rows.
    image = QImage(40, int(40 * dpr), QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(dpr)
    image.fill(QColor("#F0F0F0"))
    painter = QPainter(image)
    painter.translate(0, origin)
    for offset in offsets:
        painter.fillRect(QRectF(0, offset, 40, 1.0 / dpr), QColor("#A0A0A0"))
    painter.end()
    painted = [y for y in range(image.height()) if image.pixelColor(20, y) == QColor("#A0A0A0")]
    assert painted == [first, second]


def test_a_single_line_stays_in_the_middle():
    assert line_offsets(0, SEPARATOR_LINE_HEIGHT, 1.0, 1) == [1.0]
    rows = [(5 + o) * 1.25 for o in line_offsets(5, SEPARATOR_LINE_HEIGHT, 1.25, 1)]
    assert rows == [round(rows[0])]


def test_moving_the_splitter_repaints_the_handle(qtbot, monkeypatch):
    splitter = SeparatorSplitter(Qt.Orientation.Vertical)
    qtbot.addWidget(splitter)
    splitter.addWidget(QLabel("top"))
    splitter.addWidget(QWidget())
    splitter.resize(200, 200)
    splitter.show()
    calls: list[int] = []
    handle = splitter.handle(1)
    monkeypatch.setattr(handle, "update", lambda *a: calls.append(1))
    splitter.moveSplitter(120, 1)
    assert calls
