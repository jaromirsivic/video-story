"""Offscreen tests for the drawing overlay and the palette."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication

from video_story.ui.overlay import DrawingOverlay, OverlayWindow, Shape, Tool
from video_story.ui.palette import DEFAULT_COLOR, PALETTE, PaletteColor, find_color


def _mouse(window: OverlayWindow, kind: QEvent.Type, pos: QPoint, pressed: bool) -> None:
    local = QPointF(pos)
    event = QMouseEvent(
        kind,
        local,
        window.mapToGlobal(local),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton if pressed else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, event)


def drag(window: OverlayWindow, points: list[tuple[int, int]]) -> None:
    """Simulate a left-button press at the first point, moves, and release at the last."""
    first, *middle, last = [QPoint(*p) for p in points]
    _mouse(window, QEvent.Type.MouseButtonPress, first, True)
    for point in middle:
        _mouse(window, QEvent.Type.MouseMove, point, True)
    _mouse(window, QEvent.Type.MouseMove, last, True)
    _mouse(window, QEvent.Type.MouseButtonRelease, last, False)


@pytest.fixture
def overlay(qtbot):
    overlay = DrawingOverlay()
    overlay.show()
    for window in overlay.windows:
        qtbot.addWidget(window)
    yield overlay
    overlay.close()


def test_palette_has_16_unique_colors_and_red_first():
    assert len(PALETTE) == 16
    assert len({c.hex.upper() for c in PALETTE}) == 16
    assert len({c.name for c in PALETTE}) == 16
    assert PALETTE[0] == DEFAULT_COLOR == PaletteColor("Red", "#FF0000")
    assert DEFAULT_COLOR.rgb == (255, 0, 0)
    for entry in PALETTE:
        assert QColor(entry.hex).isValid(), entry
    assert find_color("#ff0000") is PALETTE[0]
    assert find_color("#123456") is None


def test_one_window_per_screen(overlay):
    screens = QGuiApplication.screens()
    assert len(overlay.windows) == len(screens) >= 1
    assert overlay.is_visible()
    for index, (window, screen) in enumerate(zip(overlay.windows, screens, strict=True)):
        assert window.isVisible()
        assert window.screen_index == index
        assert window.geometry() == screen.geometry()
        assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert not window.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        flags = window.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert flags & Qt.WindowType.Tool
        assert window.cursor().shape() == Qt.CursorShape.CrossCursor


def test_set_tool_and_color(overlay):
    assert overlay.tool is Tool.BRUSH
    assert overlay.color == QColor("#FF0000")
    overlay.set_tool(Tool.ARROW)
    assert overlay.tool is Tool.ARROW
    overlay.set_color(QColor("#00FF00"))
    assert overlay.color == QColor("#00FF00")
    overlay.set_color(PALETTE[3])
    assert overlay.color == QColor(PALETTE[3].hex)


def test_brush_drag_adds_polyline(overlay):
    window = overlay.windows[0]
    drag(window, [(10, 10), (20, 15), (30, 30), (45, 60)])
    shapes = overlay.shapes()
    assert len(shapes) == 1
    shape = shapes[0]
    assert shape.tool is Tool.BRUSH
    assert shape.screen_index == 0
    assert shape.color == QColor("#FF0000")
    assert len(shape.points) >= 4
    assert shape.points[0] == QPointF(10, 10)
    assert shape.points[-1] == QPointF(45, 60)


def test_rectangle_and_arrow(overlay):
    window = overlay.windows[0]
    overlay.set_tool(Tool.RECTANGLE)
    overlay.set_color(QColor("#0040FF"))
    drag(window, [(100, 100), (150, 120), (200, 180)])
    overlay.set_tool(Tool.ARROW)
    drag(window, [(300, 300), (320, 310), (400, 350)])
    shapes = overlay.shapes()
    assert [s.tool for s in shapes] == [Tool.RECTANGLE, Tool.ARROW]
    rect, arrow = shapes
    assert rect.points == [QPointF(100, 100), QPointF(200, 180)]
    assert rect.color == QColor("#0040FF")
    assert arrow.points == [QPointF(300, 300), QPointF(400, 350)]


def test_zero_size_rectangle_is_discarded(overlay):
    window = overlay.windows[0]
    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(50, 50), (50, 50)])
    assert overlay.shapes() == []


def test_undo_and_clear(overlay):
    window = overlay.windows[0]
    drag(window, [(10, 10), (20, 20)])
    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(30, 30), (60, 60)])
    assert [s.tool for s in overlay.shapes()] == [Tool.BRUSH, Tool.RECTANGLE]
    overlay.undo()
    assert [s.tool for s in overlay.shapes()] == [Tool.BRUSH]
    drag(window, [(70, 70), (90, 95)])
    overlay.clear()
    assert overlay.shapes() == []
    overlay.undo()  # nothing to undo must not fail
    assert overlay.shapes() == []


def test_keyboard_shortcuts(overlay, qtbot):
    window = overlay.windows[0]
    qtbot.keyClick(window, Qt.Key.Key_R)
    assert overlay.tool is Tool.RECTANGLE
    qtbot.keyClick(window, Qt.Key.Key_A)
    assert overlay.tool is Tool.ARROW
    qtbot.keyClick(window, Qt.Key.Key_B)
    assert overlay.tool is Tool.BRUSH
    drag(window, [(10, 10), (20, 20)])
    drag(window, [(30, 30), (40, 40)])
    qtbot.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(overlay.shapes()) == 1
    qtbot.keyClick(window, Qt.Key.Key_C)
    assert overlay.shapes() == []


def test_escape_emits_closed_and_hides(overlay, qtbot):
    window = overlay.windows[0]
    with qtbot.waitSignal(overlay.closed, timeout=1000):
        qtbot.keyClick(window, Qt.Key.Key_Escape)
    assert not overlay.is_visible()
    assert all(not w.isVisible() for w in overlay.windows)


def test_hide_and_close_do_not_emit_closed(overlay, qtbot):
    with qtbot.assertNotEmitted(overlay.closed):
        overlay.hide()
        overlay.close()
    assert not overlay.is_visible()
    assert overlay.windows == []


def test_hide_and_show_keep_shapes(overlay):
    drag(overlay.windows[0], [(10, 10), (30, 30)])
    overlay.hide()
    assert not overlay.is_visible()
    assert len(overlay.shapes()) == 1
    overlay.show()
    assert overlay.is_visible()
    assert len(overlay.shapes()) == 1
    assert overlay.windows[0].isVisible()


def test_add_shape_programmatically(overlay):
    overlay.add_shape(
        Shape(Tool.RECTANGLE, QColor("#FF0000"), [QPointF(10, 10), QPointF(50, 50)], 0)
    )
    assert len(overlay.shapes()) == 1
    with pytest.raises(IndexError):
        overlay.add_shape(Shape(Tool.BRUSH, QColor("#FF0000"), [QPointF(1, 1)], 99))
