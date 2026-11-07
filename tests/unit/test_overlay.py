"""Offscreen tests for the drawing overlay and the palette."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QApplication

from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.ui.overlay import DrawingOverlay, OverlayWindow, Shape, Tool, shape_to_json
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
    assert len(PALETTE) == 8
    assert len({c.hex.upper() for c in PALETTE}) == 8
    assert len({c.name for c in PALETTE}) == 8
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
    assert overlay.tool is Tool.RECTANGLE  # rectangle is the default tool
    assert overlay.color == QColor("#FF0000")
    overlay.set_tool(Tool.ARROW)
    assert overlay.tool is Tool.ARROW
    overlay.set_color(QColor("#00FF00"))
    assert overlay.color == QColor("#00FF00")
    overlay.set_color(PALETTE[3])
    assert overlay.color == QColor(PALETTE[3].hex)


def test_brush_drag_adds_polyline(overlay):
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
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
    overlay.set_tool(Tool.BRUSH)
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


def _recorded_screens():
    """Screens matching the offscreen QScreens (origin 0,0), as the recorder would list them."""
    raw = [
        RawScreen(
            x=s.geometry().x(),
            y=s.geometry().y(),
            width=round(s.geometry().width() * s.devicePixelRatio()),
            height=round(s.geometry().height() * s.devicePixelRatio()),
            primary=s is QGuiApplication.primaryScreen(),
            scale=s.devicePixelRatio(),
        )
        for s in QGuiApplication.screens()
    ]
    return assign_screen_ids(raw)


def test_drawings_snapshot_in_screen_pixels(overlay):
    assert overlay.drawings() == []  # no recorded screens yet
    screens = _recorded_screens()
    overlay.set_screens(screens)
    window = overlay.windows[0]
    dpr = QGuiApplication.screens()[0].devicePixelRatio()

    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(10, 20), (110, 220)])
    overlay.set_tool(Tool.ARROW)
    overlay.set_color("#00FF00")
    drag(window, [(50, 50), (150, 60)])
    overlay.set_tool(Tool.BRUSH)
    drag(window, [(5, 5), (6, 7), (9, 9)])

    drawings = overlay.drawings()
    assert [d["tool"] for d in drawings] == ["rectangle", "arrow", "brush"]
    rect, arrow, brush = drawings
    assert rect["color"] == "#FF0000" and rect["screen"] == "screen1"
    assert rect["from"] == {"screen": "screen1", "x": round(10 * dpr), "y": round(20 * dpr)}
    assert rect["to"] == {"screen": "screen1", "x": round(110 * dpr), "y": round(220 * dpr)}
    assert arrow["color"] == "#00FF00" and arrow["from"]["x"] == round(50 * dpr)
    assert arrow["to"] == {"screen": "screen1", "x": round(150 * dpr), "y": round(60 * dpr)}
    assert brush["color"] == "#00FF00" and [(p["x"], p["y"]) for p in brush["path"]] == [
        (round(5 * dpr), round(5 * dpr)),
        (round(6 * dpr), round(7 * dpr)),
        (round(9 * dpr), round(9 * dpr)),
    ]
    assert all(p["screen"] == "screen1" for p in brush["path"])

    # The shape being dragged is on screen, so it is part of the snapshot.
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(1, 1), True)
    _mouse(window, QEvent.Type.MouseMove, QPoint(3, 3), True)
    assert len(overlay.drawings()) == 4
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(3, 3), False)

    overlay.undo()
    assert len(overlay.drawings()) == 3
    overlay.hide()
    assert overlay.drawings() == []  # hidden: nothing is on screen
    overlay.show()
    assert len(overlay.drawings()) == 3
    overlay.clear()
    assert overlay.drawings() == []


def test_shape_to_json_clamps_to_image():
    screen = _recorded_screens()[0]
    shape = Shape(Tool.ARROW, QColor("#0000FF"), [QPointF(-5, -5), QPointF(10**6, 10**6)], 0)
    data = shape_to_json(shape, screen, 2.0)
    assert data["from"] == {"screen": "screen1", "x": 0, "y": 0}
    assert data["to"] == {
        "screen": "screen1",
        "x": screen.image_width - 1,
        "y": screen.image_height - 1,
    }
    assert data["stroke_width"] == 8 and data["color"] == "#0000FF"


def test_add_shape_programmatically(overlay):
    overlay.add_shape(
        Shape(Tool.RECTANGLE, QColor("#FF0000"), [QPointF(10, 10), QPointF(50, 50)], 0)
    )
    assert len(overlay.shapes()) == 1
    with pytest.raises(IndexError):
        overlay.add_shape(Shape(Tool.BRUSH, QColor("#FF0000"), [QPointF(1, 1)], 99))
