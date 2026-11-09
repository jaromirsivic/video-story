"""Offscreen tests for the drawing overlay and the palette."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPixmap, QWheelEvent
from PySide6.QtWidgets import QApplication

from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.ui.overlay import (
    MAX_ZOOM,
    DrawingOverlay,
    OverlayWindow,
    Shape,
    Tool,
    shape_to_json,
)
from video_story.ui.palette import DEFAULT_COLOR, PALETTE, PaletteColor, find_color


def _mouse(
    window: OverlayWindow,
    kind: QEvent.Type,
    pos: QPoint,
    pressed: bool,
    button: Qt.MouseButton = Qt.MouseButton.LeftButton,
) -> None:
    local = QPointF(pos)
    event = QMouseEvent(
        kind,
        local,
        window.mapToGlobal(local),
        button,
        button if pressed else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(window, event)


def _wheel(window: OverlayWindow, pos: QPoint, notches: float) -> None:
    local = QPointF(pos)
    event = QWheelEvent(
        local,
        window.mapToGlobal(local),
        QPoint(0, 0),
        QPoint(0, round(120 * notches)),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(window, event)


def _key(key: Qt.Key) -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def _snapshot(window: OverlayWindow) -> QPixmap:
    pixmap = QPixmap(window.size())
    pixmap.fill(QColor("#336699"))
    return pixmap


def _park_toolbar(overlay: DrawingOverlay) -> None:
    """Move the toolbar to the bottom-right corner, away from where the tests draw."""
    toolbar = overlay._toolbar
    assert toolbar is not None
    area = QGuiApplication.primaryScreen().geometry()
    toolbar.move(area.bottomRight() - QPoint(toolbar.width(), toolbar.height()))
    toolbar.user_moved = True


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
    _park_toolbar(overlay)
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
    for number, entry in enumerate(PALETTE, start=1):
        qtbot.keyClick(window, Qt.Key(Qt.Key.Key_0 + number))
        assert overlay.color.name().upper() == entry.hex.upper()
    qtbot.keyClick(window, Qt.Key.Key_1)
    drag(window, [(10, 10), (20, 20)])
    drag(window, [(30, 30), (40, 40)])
    qtbot.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(overlay.shapes()) == 1
    qtbot.keyClick(window, Qt.Key.Key_C)  # plain C no longer clears (too easy to hit)
    assert len(overlay.shapes()) == 1
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    qtbot.keyClick(window, Qt.Key.Key_C, ctrl_shift)
    assert overlay.shapes() == []


def test_shortcuts_work_from_other_windows_while_drawing(overlay, qtbot):
    """Esc / Ctrl+Z / Ctrl+Shift+C are application-wide while the overlay is shown."""
    from PySide6.QtWidgets import QWidget

    other = QWidget()
    qtbot.addWidget(other)
    other.show()
    other.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is other, timeout=1000)
    window = overlay.windows[0]
    drag(window, [(10, 10), (40, 40)])
    drag(window, [(50, 50), (80, 80)])
    shortcuts = {s.key().toString(): s for s in overlay._shortcuts}
    assert set(shortcuts) == {"Esc", "Ctrl+Z", "Ctrl+Shift+C", "R", "A", "B"} | {
        str(n) for n in range(1, 9)
    }
    assert all(
        s.context() == Qt.ShortcutContext.ApplicationShortcut and s.isEnabled()
        for s in shortcuts.values()
    )
    qtbot.keyClick(other, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(overlay.shapes()) == 1
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    qtbot.keyClick(other, Qt.Key.Key_C, ctrl_shift)
    assert overlay.shapes() == []
    with qtbot.waitSignal(overlay.closed, timeout=1000):
        qtbot.keyClick(other, Qt.Key.Key_Escape)
    assert not overlay.is_visible()
    assert not any(s.isEnabled() for s in shortcuts.values())  # off while hidden


def test_right_button_always_draws_an_arrow(overlay):
    window = overlay.windows[0]
    right = Qt.MouseButton.RightButton
    for tool in (Tool.RECTANGLE, Tool.BRUSH, Tool.ARROW):
        overlay.set_tool(tool)
        _mouse(window, QEvent.Type.MouseButtonPress, QPoint(100, 100), True, right)
        _mouse(window, QEvent.Type.MouseMove, QPoint(150, 120), True, right)
        # A left press during the right-button drag does not start another shape.
        _mouse(window, QEvent.Type.MouseButtonPress, QPoint(150, 120), True)
        _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(150, 120), False)
        _mouse(window, QEvent.Type.MouseMove, QPoint(200, 160), True, right)
        _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(200, 160), False, right)
        assert overlay.tool is tool  # the selected tool is left alone
    shapes = overlay.shapes()
    assert [s.tool for s in shapes] == [Tool.ARROW] * 3
    assert all(s.points == [QPointF(100, 100), QPointF(200, 160)] for s in shapes)
    # The left button still draws with the selected tool.
    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(300, 300), (350, 340)])
    assert overlay.shapes()[-1].tool is Tool.RECTANGLE


def test_kept_window_can_be_moved_and_clicked_through_the_overlay(overlay, qtbot):
    """A window kept above the overlay (the control window) may still end up below it:
    a press on its spot moves it or clicks its button, and draws nothing."""
    from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    other = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
    qtbot.addWidget(other)
    button = QPushButton("Stop", other)
    QVBoxLayout(other).addWidget(button)
    clicks: list[bool] = []
    button.clicked.connect(lambda: clicks.append(True))
    other.resize(160, 60)
    other.move(QGuiApplication.primaryScreen().geometry().topLeft() + QPoint(400, 300))
    other.show()
    overlay.keep_above(other)
    window = overlay.windows[0]

    start = other.pos()
    inside = window.mapFromGlobal(other.mapToGlobal(QPoint(4, 4)))
    drag(window, [(inside.x(), inside.y()), (inside.x() + 120, inside.y() + 70)])
    assert overlay.shapes() == []
    assert other.pos() == start + QPoint(120, 70)
    assert clicks == []

    # A click without moving lands on the button under the cursor.
    on_button = window.mapFromGlobal(button.mapToGlobal(button.rect().center()))
    drag(window, [(on_button.x(), on_button.y()), (on_button.x(), on_button.y())])
    qtbot.waitUntil(lambda: clicks == [True], timeout=1000)
    assert other.pos() == start + QPoint(120, 70) and overlay.shapes() == []

    # The right button does not draw under it either.
    _mouse(window, QEvent.Type.MouseButtonPress, on_button, True, Qt.MouseButton.RightButton)
    _mouse(
        window, QEvent.Type.MouseMove, on_button + QPoint(80, 80), True, Qt.MouseButton.RightButton
    )
    _mouse(
        window,
        QEvent.Type.MouseButtonRelease,
        on_button + QPoint(80, 80),
        False,
        Qt.MouseButton.RightButton,
    )
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


def test_toolbar_order_cross_and_drag(overlay, qtbot):
    from PySide6.QtWidgets import QPushButton, QToolButton

    toolbar = overlay._toolbar
    assert toolbar is not None
    toolbar.move(QGuiApplication.primaryScreen().geometry().topLeft() + QPoint(16, 16))
    tools = [b.text() for b in toolbar.findChildren(QToolButton) if b.isCheckable()]
    assert tools == ["Rect (R)", "Arrow (A)", "Brush (B)"]
    actions = [b.text() for b in toolbar.findChildren(QPushButton)]
    assert actions == ["Undo (Ctrl+Z)", "Clear (Ctrl+Shift+C)", "Close (Esc)"]

    # Drag the title bar: the toolbar follows and keeps its spot after hide/show.
    start = toolbar.pos()
    grab = QPointF(20, 8)
    for kind, delta, buttons in (
        (QEvent.Type.MouseButtonPress, QPoint(0, 0), Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, QPoint(150, 90), Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, QPoint(150, 90), Qt.MouseButton.NoButton),
    ):
        global_pos = QPointF(toolbar.mapToGlobal(grab.toPoint()) + delta - (toolbar.pos() - start))
        event = QMouseEvent(
            kind,
            grab,
            global_pos,
            Qt.MouseButton.LeftButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(toolbar, event)
    assert toolbar.pos() == start + QPoint(150, 90) and toolbar.user_moved
    overlay.hide()
    overlay.show()
    assert toolbar.pos() == start + QPoint(150, 90)

    # The cross leaves drawing mode like Close.
    with qtbot.waitSignal(overlay.closed, timeout=1000):
        toolbar.close_cross.click()
    assert not overlay.is_visible()


def test_press_on_toolbar_spot_moves_toolbar_instead_of_drawing(overlay):
    """The overlay may lie above the toolbar (window activation): a press on the
    toolbar's spot drags the toolbar whatever tool is selected, and draws nothing."""
    toolbar = overlay._toolbar
    window = overlay.windows[0]
    overlay.set_tool(Tool.RECTANGLE)
    start = toolbar.pos()
    inside = window.mapFromGlobal(toolbar.mapToGlobal(QPoint(30, 8)))
    drag(window, [(inside.x(), inside.y()), (inside.x() - 100, inside.y() - 80)])
    assert overlay.shapes() == []
    assert toolbar.pos() == start - QPoint(100, 80) and toolbar.user_moved
    # Outside the toolbar the overlay draws as usual.
    drag(window, [(10, 10), (60, 60)])
    assert len(overlay.shapes()) == 1


def test_toolbar_layout_rows_and_outline(overlay):
    """Undo/Clear/Close above Rect/Arrow/Brush (with icons), one row of 8 colors below."""
    from PySide6.QtWidgets import QPushButton

    toolbar = overlay._toolbar
    actions = toolbar.findChildren(QPushButton)
    tools = [toolbar._tool_buttons[t] for t in (Tool.RECTANGLE, Tool.ARROW, Tool.BRUSH)]
    assert len({b.y() for b in actions}) == 1 and len({b.y() for b in tools}) == 1
    assert actions[0].y() < tools[0].y()
    assert [b.x() for b in actions] == [b.x() for b in tools]  # same columns
    swatches = list(toolbar._swatches.values())
    assert [s.text() for s in swatches] == [str(n) for n in range(1, 9)]
    rows = sorted({s.y() for s in swatches})
    assert len(rows) == 1 and rows[0] > tools[0].y()
    assert [s.x() for s in swatches] == sorted(s.x() for s in swatches)
    assert all(not b.icon().isNull() for b in tools)
    assert len({b.icon().cacheKey() for b in tools}) == 3
    assert "border: 2px solid #000000" in toolbar.styleSheet()


def test_toolbar_keeps_its_size_when_resized(overlay, qtbot):
    """Moving across screens with different DPI must not leave the toolbar enlarged."""
    toolbar = overlay._toolbar
    size = toolbar.size()
    toolbar.resize(size.width() * 2, size.height() * 3)
    qtbot.wait(10)
    assert toolbar.size() == size


def test_toolbar_header_strip(overlay):
    toolbar = overlay._toolbar
    assert toolbar.header.cursor().shape() == Qt.CursorShape.SizeAllCursor
    assert toolbar.close_cross.parent() is toolbar.header


def test_wheel_zoom_needs_a_snapshot(overlay):
    window = overlay.windows[0]
    window.set_snapshot(None)
    _wheel(window, QPoint(100, 100), 3)
    assert window.zoom == 1.0


def test_wheel_zoom_around_cursor_and_limits(overlay):
    window = overlay.windows[0]
    window.set_snapshot(_snapshot(window))
    anchor = QPoint(200, 150)
    before = window.to_canvas(QPointF(anchor))
    _wheel(window, anchor, 1)
    assert window.zoom == pytest.approx(1.25)
    after = window.to_canvas(QPointF(anchor))
    assert after.x() == pytest.approx(before.x()) and after.y() == pytest.approx(before.y())
    _wheel(window, anchor, 50)
    assert window.zoom == MAX_ZOOM
    _wheel(window, anchor, -50)
    assert window.zoom == 1.0 and window.offset == QPointF(0, 0)
    _wheel(window, anchor, 2)
    QApplication.sendEvent(window, _key(Qt.Key.Key_0))
    assert window.zoom == 1.0


def test_middle_button_pans_only_while_zoomed(overlay):
    window = overlay.windows[0]
    window.set_snapshot(_snapshot(window))
    middle = Qt.MouseButton.MiddleButton
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(100, 100), True, middle)
    _mouse(window, QEvent.Type.MouseMove, QPoint(50, 50), True, middle)
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(50, 50), False, middle)
    assert window.offset == QPointF(0, 0)

    window.set_zoom(4.0, QPointF(0, 0))
    assert window.offset == QPointF(0, 0)
    window._set_offset(QPointF(20, 20))
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(100, 100), True, middle)
    _mouse(window, QEvent.Type.MouseMove, QPoint(60, 80), True, middle)
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(60, 80), False, middle)
    assert window.offset == QPointF(30, 25)  # dragged 40, 20 view px = 10, 5 canvas px
    assert window.cursor().shape() == Qt.CursorShape.CrossCursor
    # Panning stops at the edges of the picture.
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(0, 0), True, middle)
    _mouse(window, QEvent.Type.MouseMove, QPoint(5000, 5000), True, middle)
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(5000, 5000), False, middle)
    assert window.offset == QPointF(0, 0)


def test_drawing_while_zoomed_uses_canvas_coordinates(overlay):
    overlay.set_screens(_recorded_screens())
    window = overlay.windows[0]
    dpr = QGuiApplication.screens()[0].devicePixelRatio()
    window.set_snapshot(_snapshot(window))
    drag(window, [(40, 40), (80, 60)])  # at 100 %
    window.set_zoom(2.0, QPointF(0, 0))
    window._set_offset(QPointF(20, 30))
    drag(window, [(0, 0), (40, 20)])  # canvas (20, 30) -> (40, 40)
    _first, second = overlay.shapes()
    assert second.points[0] == QPointF(20, 30) and second.points[-1] == QPointF(40, 40)

    # The sidecar data describes the zoomed image: shapes in view pixels, plus the zoom.
    drawings = overlay.drawings()
    assert drawings[0]["from"] == {"screen": "screen1", "x": round(40 * dpr), "y": round(20 * dpr)}
    assert drawings[1]["from"] == {"screen": "screen1", "x": 0, "y": 0}
    assert overlay.zoom() == [
        {
            "screen": "screen1",
            "factor": 2.0,
            "x": round(20 * dpr),
            "y": round(30 * dpr),
            "width": round(window.width() / 2 * dpr),
            "height": round(window.height() / 2 * dpr),
        }
    ]
    # Shapes scrolled out of view are left out of the sidecar.
    window.set_zoom(MAX_ZOOM, QPointF(window.width(), window.height()))
    assert overlay.drawings() == []
    window.reset_zoom()
    assert len(overlay.drawings()) == 2 and overlay.zoom() == []


def test_hide_resets_zoom(overlay):
    window = overlay.windows[0]
    window.set_snapshot(_snapshot(window))
    window.set_zoom(3.0)
    overlay.hide()
    assert window.zoom == 1.0 and not window.has_snapshot()
