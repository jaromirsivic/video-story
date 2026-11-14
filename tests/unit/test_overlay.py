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
    FLASH_RED,
    MAX_ZOOM,
    DrawingOverlay,
    OverlayWindow,
    Shape,
    Tool,
    flash_border_px,
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


def test_palette_has_9_unique_colors_red_first_black_last():
    assert len(PALETTE) == 9
    assert len({c.hex.upper() for c in PALETTE}) == 9
    assert len({c.name for c in PALETTE}) == 9
    assert PALETTE[-1] == PaletteColor("Black", "#000000")
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
    qtbot.keyClick(window, Qt.Key.Key_C)  # a plain C no longer clears
    assert len(overlay.shapes()) == 1
    ctrl_alt = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    qtbot.keyClick(window, Qt.Key.Key_C, ctrl_alt)
    assert overlay.shapes() == []
    qtbot.keyClick(window, Qt.Key.Key_E)
    assert overlay.tool is Tool.ELLIPSE
    qtbot.keyClick(window, Qt.Key.Key_L)
    assert overlay.tool is Tool.LINE
    qtbot.keyClick(window, Qt.Key.Key_U)
    assert overlay.tool is Tool.ERASER
    qtbot.keyClick(window, Qt.Key.Key_S)
    assert overlay.sketch
    qtbot.keyClick(window, Qt.Key.Key_S)
    assert not overlay.sketch


def test_shortcuts_work_from_other_windows_while_drawing(overlay, qtbot):
    """Esc / Ctrl+Z / Ctrl+Alt+C and the other keys are application-wide while drawing."""
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
    keys = {"Esc", "Ctrl+Z", "Ctrl+Y", "Ctrl+G", "Ctrl+Alt+C", "S", "R", "E", "L", "B", "A", "U"}
    assert set(shortcuts) == keys | {str(n) for n in range(1, 10)}
    assert all(
        s.context() == Qt.ShortcutContext.ApplicationShortcut and s.isEnabled()
        for s in shortcuts.values()
    )
    qtbot.keyClick(other, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(overlay.shapes()) == 1
    ctrl_alt = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    qtbot.keyClick(other, Qt.Key.Key_C, ctrl_alt)
    assert overlay.shapes() == []
    with qtbot.waitSignal(overlay.closed, timeout=1000):
        qtbot.keyClick(other, Qt.Key.Key_Escape)
    assert not overlay.is_visible()
    assert not any(s.isEnabled() for s in shortcuts.values())  # off while hidden


def test_right_button_always_draws_an_arrow(overlay):
    window = overlay.windows[0]
    right = Qt.MouseButton.RightButton
    for tool in (Tool.RECTANGLE, Tool.BRUSH, Tool.ARROW, Tool.ERASER):
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
    assert [s.tool for s in shapes] == [Tool.ARROW] * 4
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
    assert overlay.leaving  # the green flash first, then the windows go
    qtbot.waitUntil(lambda: all(not w.isVisible() for w in overlay.windows), timeout=2000)


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


def test_hide_paints_the_windows_before_hiding_them(overlay, monkeypatch):
    """A hidden translucent window shows its last picture for a moment when it appears
    again: hide() paints the cleared state (sketch off, no zoom) while still shown."""
    window = overlay.windows[0]
    drag(window, [(10, 10), (30, 30)])
    overlay.set_sketch(True)
    painted: list[tuple[bool, bool, int]] = []
    real_repaint = window.repaint

    def repaint() -> None:
        painted.append((window.isVisible(), overlay.sketch, len(overlay.shapes())))
        real_repaint()

    monkeypatch.setattr(window, "repaint", repaint)
    overlay.clear()
    overlay.hide()
    assert painted == [(True, False, 0)]
    assert not window.isVisible()


def test_user_close_emits_closed_before_hiding(overlay, qtbot):
    """The owner clears the shapes on ``closed`` while the windows are still shown."""
    drag(overlay.windows[0], [(10, 10), (30, 30)])
    seen: list[bool] = []
    overlay.closed.connect(lambda: (seen.append(overlay.is_visible()), overlay.clear()))
    overlay._user_close()
    assert seen == [True] and not overlay.is_visible() and overlay.shapes() == []


def test_flash_border_thickness_is_20_px_or_1_percent():
    assert flash_border_px(2560, 1600) == 26
    assert flash_border_px(1600, 2560) == 26  # the longer side, whatever the orientation
    assert flash_border_px(1920, 1080) == 20
    assert flash_border_px(1280, 720) == 20


def _flash_colors(overlay: DrawingOverlay, qtbot, until) -> list[str | None]:
    """The flash colors seen (each once, in order) until ``until()`` holds."""
    seen: list[str | None] = []

    def note() -> bool:
        color = overlay.flash_color
        name = color.name() if color is not None else None
        if not seen or seen[-1] != name:
            seen.append(name)
        return until()

    qtbot.waitUntil(note, timeout=3000)
    return seen


def test_show_flashes_red_and_drawing_works_at_once(qtbot):
    overlay = DrawingOverlay()
    try:
        overlay.show()
        for window in overlay.windows:
            qtbot.addWidget(window)
        _park_toolbar(overlay)
        assert overlay.flash_color == FLASH_RED
        drag(overlay.windows[0], [(100, 100), (160, 160)])  # during the flash
        assert len(overlay.shapes()) == 1
        colors = _flash_colors(overlay, qtbot, lambda: overlay.flash_color is None)
        assert colors == ["#ff0000", "#8b0000", "#ff0000", "#8b0000", None]
        assert overlay.is_visible() and len(overlay.shapes()) == 1
    finally:
        overlay.close()


def test_flash_is_not_a_shape(overlay):
    """The border is painted on the windows only: never in drawings, ids or Undo."""
    overlay._start_flash((FLASH_RED,))
    assert overlay.flash_color == FLASH_RED
    assert overlay.drawings() == [] and overlay.drawing_now() is None
    assert overlay.shapes() == []
    window = overlay.windows[0]
    image = window.grab().toImage()
    assert image.pixelColor(2, 2) == FLASH_RED  # painted in the corner of the screen


def test_leave_clears_at_once_then_flashes_green_and_hides(overlay, qtbot):
    window = overlay.windows[0]
    drag(window, [(100, 100), (160, 160)])
    overlay.set_sketch(True)
    overlay.leave()
    # At once: shapes gone, back on the screens, sidecars say "not drawing".
    assert overlay.shapes() == [] and not overlay.sketch and overlay.background() is None
    assert overlay.leaving and window.isVisible()
    # The windows swallow the mouse meanwhile: nothing is drawn.
    drag(window, [(200, 200), (260, 260)])
    assert overlay.shapes() == []
    colors = _flash_colors(overlay, qtbot, lambda: not window.isVisible())
    assert colors == ["#00e000", "#006400", None]
    assert not overlay.leaving and not overlay.is_visible()


def test_show_during_leave_starts_again_with_red(overlay, qtbot):
    overlay.leave()
    assert overlay.leaving
    overlay.show()
    assert not overlay.leaving and overlay.is_visible()
    assert overlay.flash_color == FLASH_RED
    qtbot.wait(700)  # the old green flash must not hide the overlay
    assert overlay.is_visible() and overlay.windows[0].isVisible()


def test_hide_and_close_skip_the_flash(overlay):
    overlay.leave()
    overlay.hide()
    assert not overlay.leaving and overlay.flash_color is None
    assert all(not w.isVisible() for w in overlay.windows)


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

    assert [d["id"] for d in drawings] == [1, 2, 3]
    assert overlay.drawing_now() is None

    # The shape being dragged is on screen but not finished: only drawing_now reports it.
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(1, 1), True)
    _mouse(window, QEvent.Type.MouseMove, QPoint(3, 3), True)
    assert len(overlay.drawings()) == 3
    assert overlay.drawing_now() == {
        "tool": "brush",
        "color": "#00FF00",
        "screen": "screen1",
        "started_ms": None,  # no clock set
    }
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(3, 3), False)
    assert len(overlay.drawings()) == 4 and overlay.drawing_now() is None
    assert overlay.drawings()[-1]["id"] == 4

    overlay.undo()
    assert len(overlay.drawings()) == 3
    overlay.hide()
    assert overlay.drawings() == []  # hidden: nothing is on screen
    overlay.show()
    assert len(overlay.drawings()) == 3
    overlay.clear()
    assert overlay.drawings() == []


def test_shapes_carry_id_and_draw_times_from_the_clock(overlay):
    overlay.set_screens(_recorded_screens())
    window = overlay.windows[0]
    now = [1000]
    overlay.set_clock(lambda: now[0])
    overlay.set_tool(Tool.RECTANGLE)

    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(10, 10), True)
    assert overlay.drawing_now() == {
        "tool": "rectangle",
        "color": "#FF0000",
        "screen": "screen1",
        "started_ms": 1000,
    }
    now[0] = 1400
    _mouse(window, QEvent.Type.MouseMove, QPoint(50, 40), True)
    assert overlay.drawings() == []  # still dragging: no geometry in the sidecars yet
    now[0] = 1800
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(60, 50), False)
    assert overlay.drawing_now() is None
    (rect,) = overlay.drawings()
    assert (rect["id"], rect["started_ms"], rect["finished_ms"]) == (1, 1000, 1800)

    # A click without movement draws nothing and clears drawing_now.
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(5, 5), True)
    assert overlay.drawing_now() is not None
    _mouse(window, QEvent.Type.MouseButtonRelease, QPoint(5, 5), False)
    assert overlay.drawing_now() is None and len(overlay.drawings()) == 1

    # Hiding mid-drag drops the gesture.
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(5, 5), True)
    overlay.hide()
    assert overlay.drawing_now() is None
    overlay.show()
    assert overlay.drawing_now() is None and len(overlay.drawings()) == 1

    # Programmatic shapes get the next id and the current time; ids are never reused.
    now[0] = 2500
    overlay.add_shape(Shape(Tool.ARROW, QColor("#0000FF"), [QPointF(1, 1), QPointF(9, 9)], 0))
    overlay.undo()
    overlay.add_shape(Shape(Tool.ARROW, QColor("#0000FF"), [QPointF(1, 1), QPointF(9, 9)], 0))
    first, second = overlay.drawings()
    assert first["id"] == 1
    assert (second["id"], second["started_ms"], second["finished_ms"]) == (3, 2500, 2500)


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
    assert tools == ["(R)", "(E)", "(L)", "(B)", "(A)", "(U)"]
    actions = [b.text() for b in toolbar.findChildren(QPushButton)]
    assert actions == ["Sketch (S)", "Snap to grid (Ctrl+G)", "Clear (Ctrl+Alt+C)"]

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

    # The cross leaves drawing mode (there is no Close button any more).
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
    """Sketch/Undo/Clear above the six tools (icons), one row of 9 colors below."""
    from PySide6.QtWidgets import QPushButton

    toolbar = overlay._toolbar
    actions = toolbar.findChildren(QPushButton)
    order = (Tool.RECTANGLE, Tool.ELLIPSE, Tool.LINE, Tool.BRUSH, Tool.ARROW, Tool.ERASER)
    tools = [toolbar._tool_buttons[t] for t in order]
    assert len({b.y() for b in actions}) == 1 and len({b.y() for b in tools}) == 1
    assert actions[0].y() < tools[0].y()
    # Every action button spans two tool columns.
    assert [b.x() for b in actions] == [tools[i].x() for i in (0, 2, 4)]
    assert [b.x() for b in tools] == sorted(b.x() for b in tools)
    swatches = list(toolbar._swatches.values())
    assert [s.text() for s in swatches] == [str(n) for n in range(1, 10)]
    rows = sorted({s.y() for s in swatches})
    assert len(rows) == 1 and rows[0] > tools[0].y()
    assert [s.x() for s in swatches] == sorted(s.x() for s in swatches)
    assert all(not b.icon().isNull() for b in tools)
    assert len({b.icon().cacheKey() for b in tools}) == 6
    assert all(b.toolTip() for b in tools)
    assert tools[0].toolTip().startswith("Rectangle (R): draw a rectangle")
    assert all(b.toolTip() for b in actions)
    # The toolbar never becomes the active window: tooltips must show anyway.
    assert toolbar.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips)
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


def test_ellipse_and_line_shapes_and_json(overlay):
    overlay.set_screens(_recorded_screens())
    window = overlay.windows[0]
    dpr = QGuiApplication.screens()[0].devicePixelRatio()
    overlay.set_tool(Tool.ELLIPSE)
    drag(window, [(100, 100), (150, 130), (200, 160)])
    overlay.set_tool(Tool.LINE)
    drag(window, [(300, 300), (310, 320), (400, 350)])
    overlay.set_tool(Tool.ELLIPSE)
    drag(window, [(500, 500), (500, 500)])  # no size: nothing drawn
    ellipse, line = overlay.shapes()
    assert ellipse.tool is Tool.ELLIPSE and ellipse.points == [QPointF(100, 100), QPointF(200, 160)]
    assert line.tool is Tool.LINE and line.points == [QPointF(300, 300), QPointF(400, 350)]
    data = overlay.drawings()
    assert [d["tool"] for d in data] == ["ellipse", "line"]
    assert data[0]["to"] == {"screen": "screen1", "x": round(200 * dpr), "y": round(160 * dpr)}
    assert data[1]["from"] == {"screen": "screen1", "x": round(300 * dpr), "y": round(300 * dpr)}
    assert all(d["background"] == "screen" for d in data)


def _draw_samples(overlay, window):
    """A rectangle, an ellipse and a brush stroke, side by side."""
    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(100, 100), (300, 300)])
    overlay.set_tool(Tool.ELLIPSE)
    drag(window, [(400, 100), (600, 300)])
    overlay.set_tool(Tool.BRUSH)
    drag(window, [(100, 400), (200, 450), (300, 400)])


def test_eraser_removes_only_shapes_whose_line_it_touches(overlay):
    window = overlay.windows[0]
    _draw_samples(overlay, window)
    overlay.set_tool(Tool.ERASER)
    assert window.cursor().shape() == Qt.CursorShape.BitmapCursor
    drag(window, [(150, 150), (250, 250)])  # inside the rectangle: nothing
    drag(window, [(480, 180), (520, 220)])  # inside the ellipse: nothing
    drag(window, [(700, 700), (800, 800)])  # nowhere near: nothing
    assert [s.tool for s in overlay.shapes()] == [Tool.RECTANGLE, Tool.ELLIPSE, Tool.BRUSH]
    drag(window, [(150, 150), (150, 350)])  # across the bottom edge of the rectangle
    assert [s.tool for s in overlay.shapes()] == [Tool.ELLIPSE, Tool.BRUSH]
    drag(window, [(200, 500), (200, 420)])  # across the brush stroke
    drag(window, [(500, 50), (500, 94)])  # 6 px above the ellipse: within reach
    assert overlay.shapes() == []
    overlay.set_tool(Tool.RECTANGLE)
    assert window.cursor().shape() == Qt.CursorShape.CrossCursor


def test_one_eraser_drag_removes_several_and_undo_brings_them_back(overlay):
    overlay.set_screens(_recorded_screens())
    window = overlay.windows[0]
    _draw_samples(overlay, window)
    ids = [d["id"] for d in overlay.drawings()]
    overlay.set_tool(Tool.ERASER)
    drag(window, [(50, 200), (350, 200), (700, 200)])  # one drag through rect and ellipse
    assert [s.tool for s in overlay.shapes()] == [Tool.BRUSH]
    overlay.undo()  # the whole drag comes back, same ids, same order
    assert [d["id"] for d in overlay.drawings()] == ids
    overlay.undo()  # then Undo removes the last shape drawn again
    assert [s.tool for s in overlay.shapes()] == [Tool.RECTANGLE, Tool.ELLIPSE]
    # Erasing a shape and undoing twice: the erase, then the shape itself.
    drag(window, [(400, 200), (420, 200)])  # touches the ellipse's left edge
    assert [s.tool for s in overlay.shapes()] == [Tool.RECTANGLE]
    overlay.undo()
    overlay.undo()
    assert [s.tool for s in overlay.shapes()] == [Tool.RECTANGLE]


def test_eraser_works_per_screen_and_with_zoom(overlay):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    drag(window, [(100, 100), (300, 100)])
    window.set_snapshot(_snapshot(window))
    window.set_zoom(4.0, QPointF(0, 0))  # canvas (100, 100) is now at view (400, 400)
    overlay.set_tool(Tool.ERASER)
    # 30 view px below the line = 7.5 canvas px: out of the eraser's on-screen reach.
    drag(window, [(500, 430), (700, 430)])
    assert len(overlay.shapes()) == 1
    drag(window, [(500, 405), (700, 405)])  # 5 view px below: hit
    assert overlay.shapes() == []


def test_hiding_mid_erase_keeps_the_erase_undoable(overlay):
    window = overlay.windows[0]
    overlay.set_tool(Tool.RECTANGLE)
    drag(window, [(100, 100), (300, 300)])
    overlay.set_tool(Tool.ERASER)
    _mouse(window, QEvent.Type.MouseButtonPress, QPoint(150, 50), True)
    _mouse(window, QEvent.Type.MouseMove, QPoint(150, 150), True)
    assert overlay.shapes() == []
    overlay.hide()
    overlay.show()
    overlay.undo()
    assert len(overlay.shapes()) == 1


def test_sketch_switches_background_and_keeps_shapes(overlay, qtbot):
    overlay.set_screens(_recorded_screens())
    window = overlay.windows[0]
    toolbar = overlay._toolbar
    assert overlay.background() == "screen" and not overlay.sketch
    assert toolbar.sketch_button.text() == "Sketch (S)" and not toolbar.sketch_button.isChecked()
    drag(window, [(10, 10), (60, 60)])  # on the screen
    toolbar.sketch_button.click()
    assert overlay.sketch and overlay.background() == "sketch"
    # The button stays pressed while sketching; its text never changes.
    assert toolbar.sketch_button.isChecked() and toolbar.sketch_button.text() == "Sketch (S)"
    assert len(overlay.shapes()) == 1  # switching removes nothing
    drag(window, [(100, 100), (160, 160)])  # on the canvas
    assert [d["background"] for d in overlay.drawings()] == ["screen", "sketch"]

    # The canvas is opaque white (the screen does not show through); shapes on top.
    image = window.grab().toImage()
    assert image.pixelColor(400, 400) == QColor("#FFFFFF")
    assert image.pixelColor(60, 35) == QColor("#FF0000")

    # Zoom works on the canvas without a screen picture.
    window.set_snapshot(None)
    _wheel(window, QPoint(100, 100), 2)
    assert window.zoom > 1.0
    overlay.undo()  # undo is unaffected by the background
    assert len(overlay.shapes()) == 1
    overlay.toggle_sketch()
    assert not overlay.sketch and not toolbar.sketch_button.isChecked()
    assert window.zoom == 1.0  # no screen picture to zoom into here
    assert len(overlay.shapes()) == 1
    image = window.grab().toImage()
    assert image.pixelColor(400, 400).alpha() < 10  # see-through again

    # Every drawing session starts on the screens.
    overlay.toggle_sketch()
    overlay.hide()
    assert overlay.background() is None
    overlay.show()
    assert not overlay.sketch and overlay.background() == "screen"
    assert not toolbar.sketch_button.isChecked()
