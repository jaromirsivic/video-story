"""Offscreen tests for Ctrl while drawing: points snap to the grid, the grid on the sketch."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.ui.overlay import (
    GRID_COLOR,
    DrawingOverlay,
    OverlayWindow,
    Shape,
    Tool,
    snap_square,
    snap_to_grid,
)

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton


class Keyboard:
    """What the overlay's modifier polling reads (the real keyboard is not used)."""

    def __init__(self, overlay: DrawingOverlay) -> None:
        self.ctrl = False
        self.shift = False
        overlay._shift_source = self.modifiers

    def modifiers(self) -> Qt.KeyboardModifier:
        mods = NONE
        if self.ctrl:
            mods |= CTRL
        if self.shift:
            mods |= SHIFT
        return mods


def _send(window, kind, pos, keyboard, *, button=Qt.MouseButton.NoButton, buttons=None):
    local = QPointF(*pos)
    held = button if buttons is None else buttons
    event = QMouseEvent(kind, local, window.mapToGlobal(local), button, held, keyboard.modifiers())
    QApplication.sendEvent(window, event)


def press(window, pos, keyboard, button=LEFT):
    _send(window, QEvent.Type.MouseButtonPress, pos, keyboard, button=button)


def move(window, pos, keyboard, held=Qt.MouseButton.NoButton):
    _send(window, QEvent.Type.MouseMove, pos, keyboard, buttons=held)


def release(window, pos, keyboard, button=LEFT):
    _send(
        window,
        QEvent.Type.MouseButtonRelease,
        pos,
        keyboard,
        button=button,
        buttons=Qt.MouseButton.NoButton,
    )


def drag(window, points, keyboard, button=LEFT):
    first, *rest = points
    press(window, first, keyboard, button)
    for point in rest:
        move(window, point, keyboard, held=button)
    release(window, points[-1], keyboard, button)


def _points(shape: Shape) -> list[tuple[float, float]]:
    return [(p.x(), p.y()) for p in shape.points]


def _screens():
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


@pytest.fixture
def overlay(qtbot):
    overlay = DrawingOverlay()
    overlay.show()
    for window in overlay.windows:
        qtbot.addWidget(window)
    toolbar = overlay._toolbar
    area = QGuiApplication.primaryScreen().geometry()
    toolbar.move(area.bottomRight() - QPoint(toolbar.width(), toolbar.height()))
    toolbar.user_moved = True
    yield overlay
    overlay.close()


@pytest.fixture
def keyboard(overlay):
    return Keyboard(overlay)


def _second_screen(overlay, qtbot) -> OverlayWindow:
    first = overlay.windows[0]
    second = OverlayWindow(overlay, 1)
    qtbot.addWidget(second)
    geometry = first.geometry()
    second.setGeometry(QRect(geometry.right() + 1, geometry.top(), 800, geometry.height()))
    second.show()
    overlay._windows.append(second)
    return second


def _on_grid(points, step=16):
    return all(x % step == 0 and y % step == 0 for x, y in points)


# -- pure helpers ----------------------------------------------------------------------------


def test_snap_to_grid_rounds_to_the_nearest_grid_point():
    assert snap_to_grid(QPointF(7.9, 8.0), 16) == QPointF(0, 16)  # half way rounds up
    assert snap_to_grid(QPointF(25, 41), 16) == QPointF(32, 48)
    assert snap_to_grid(QPointF(100, 100), 128) == QPointF(128, 128)
    assert snap_to_grid(QPointF(13, 3), 8) == QPointF(16, 0)


def test_snap_square_keeps_a_square_with_a_side_on_the_grid():
    assert snap_square(QPointF(32, 32), QPointF(70, 50), 16) == QPointF(64, 64)
    assert snap_square(QPointF(32, 32), QPointF(0, 10), 16) == QPointF(0, 0)  # up-left
    assert snap_square(QPointF(32, 32), QPointF(40, 80), 16) == QPointF(80, 80)


# -- snapping --------------------------------------------------------------------------------


def test_rectangle_and_ellipse_snap_both_corners_with_ctrl(overlay, keyboard):
    overlay.set_screens(_screens())
    window = overlay.windows[0]
    keyboard.ctrl = True
    drag(window, [(37, 21), (150, 90), (203, 117)], keyboard)
    overlay.set_tool(Tool.ELLIPSE)
    drag(window, [(250, 250), (395, 333)], keyboard)
    rectangle, ellipse = overlay.shapes()
    assert _points(rectangle) == [(32, 16), (208, 112)]
    assert _points(ellipse) == [(256, 256), (400, 336)]
    assert rectangle.grid == ellipse.grid == 16
    data = overlay.drawings()
    assert data[0]["grid"] == 16 and data[0]["from"]["x"] == 32


def test_ctrl_pressed_during_the_drag_snaps_only_the_end(overlay, keyboard):
    window = overlay.windows[0]
    press(window, (37, 21), keyboard)
    move(window, (100, 100), keyboard, held=LEFT)
    keyboard.ctrl = True
    move(window, (203, 117), keyboard, held=LEFT)
    assert _points(window.current_shape)[-1] == (208, 112)
    release(window, (203, 117), keyboard)
    (shape,) = overlay.shapes()
    assert _points(shape) == [(37, 21), (208, 112)] and shape.grid == 16


def test_ctrl_without_moving_the_mouse_snaps_the_dragged_corner(overlay, keyboard):
    window = overlay.windows[0]
    press(window, (32, 32), keyboard)
    move(window, (203, 117), keyboard, held=LEFT)
    keyboard.ctrl = True
    overlay._poll_shift()  # Ctrl seen by polling, the mouse did not move
    assert _points(window.current_shape)[-1] == (208, 112)
    keyboard.ctrl = False
    overlay._poll_shift()
    assert _points(window.current_shape)[-1] == (203, 117)


def test_ctrl_shift_square_has_a_side_on_the_grid(overlay, keyboard):
    window = overlay.windows[0]
    keyboard.ctrl = keyboard.shift = True
    drag(window, [(35, 30), (110, 70)], keyboard)
    (square,) = overlay.shapes()
    assert square.shift and _points(square) == [(32, 32), (112, 112)]


def test_line_and_connected_lines_snap_every_vertex(overlay, keyboard):
    overlay.set_screens(_screens())
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    keyboard.ctrl = True
    drag(window, [(37, 21), (203, 117)], keyboard)
    keyboard.shift = True  # Ctrl+Shift: connected lines on the grid
    press(window, (300, 300), keyboard)
    release(window, (350, 310), keyboard)
    move(window, (395, 333), keyboard)
    press(window, (395, 333), keyboard)
    release(window, (395, 333), keyboard)
    move(window, (459, 401), keyboard)
    keyboard.shift = False
    overlay._poll_shift()  # Shift up without a button ends the last line
    line, first, second = overlay.shapes()
    assert _points(line) == [(32, 16), (208, 112)]
    assert _points(first) == [(304, 304), (400, 336)]
    assert _points(second) == [(400, 336), (464, 400)]
    assert all(s.grid == 16 for s in (line, first, second))
    assert [d["grid"] for d in overlay.drawings()] == [16, 16, 16]


def test_arrow_across_screens_snaps_its_ends_but_not_the_screen_edge(overlay, keyboard, qtbot):
    first = overlay.windows[0]
    _second_screen(overlay, qtbot)
    width = first.width()
    keyboard.ctrl = True
    drag(first, [(width - 101, 205), (width + 99, 299)], keyboard, button=RIGHT)
    shaft, head = overlay.shapes()
    tail = _points(shaft)[0]
    tip = _points(head)[-1]
    assert _on_grid([tail]) and _on_grid([tip])
    assert tip == (96, 304)  # on the grid of the second screen (its own 0,0)
    edge_x = _points(shaft)[-1][0]
    assert edge_x == width and _points(head)[0][0] == 0  # cut at the edge, not snapped
    assert shaft.grid == head.grid == 16


def test_brush_and_eraser_ignore_ctrl(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
    keyboard.ctrl = True
    drag(window, [(37, 21), (101, 23), (203, 117)], keyboard)
    (stroke,) = overlay.shapes()
    assert _points(stroke)[0] == (37, 21) and (203, 117) in _points(stroke)
    assert stroke.grid is None


def test_grid_size_comes_from_the_setting(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_grid_size(32)
    keyboard.ctrl = True
    drag(window, [(37, 21), (203, 117)], keyboard)
    (shape,) = overlay.shapes()
    assert _points(shape) == [(32, 32), (192, 128)] and shape.grid == 32


# -- the grid on the sketch canvas and in the sidecars -----------------------------------


def test_grid_is_painted_on_the_sketch_canvas_only_while_ctrl_is_held(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_sketch(True)
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) == QColor("#FFFFFF")  # no Ctrl: no grid
    keyboard.ctrl = True
    overlay._poll_shift()
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) == GRID_COLOR  # a vertical line at x = 32
    assert image.pixelColor(40, 48) == GRID_COLOR  # a horizontal line at y = 48
    assert image.pixelColor(40, 40) == QColor("#FFFFFF")  # inside a cell
    overlay.set_sketch(False)
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) != GRID_COLOR  # on the screen the grid is invisible


def test_ctrl_key_events_show_and_hide_the_grid(overlay):
    window = overlay.windows[0]
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Control, CTRL))
    assert overlay.ctrl_held
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Control, NONE))
    assert not overlay.ctrl_held


def test_grid_for_the_sidecars(overlay, keyboard):
    overlay.set_screens(_screens())
    assert overlay.grid() is None
    keyboard.ctrl = True
    overlay._poll_shift()
    screen_id = _screens()[0].id
    assert overlay.grid()[screen_id] == 16
    keyboard.ctrl = False
    overlay._poll_shift()
    assert overlay.grid() is None


def test_modifiers_are_polled_while_the_overlay_is_shown(overlay):
    assert overlay._shift_timer.isActive()
    overlay.hide()
    assert not overlay._shift_timer.isActive()
    assert not overlay.ctrl_held
