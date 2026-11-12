"""Separators of the editor's right side: the fixed line under the preview picture and the
draggable double-line splitter handle above the timeline."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QLabel, QWidget  # noqa: E402

from video_story.editor.preview import PreviewPanel  # noqa: E402
from video_story.editor.separators import (  # noqa: E402
    HANDLE_LINE_GAP,
    HANDLE_WIDTH,
    SeparatorLine,
    SeparatorSplitter,
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
