"""Offscreen tests for drawing with Shift and for lines and arrows across screens."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.ui.overlay import (
    DrawingOverlay,
    OverlayWindow,
    Shape,
    Tool,
    clip_segment,
    constrain_square,
    shape_to_json,
)

SHIFT = Qt.KeyboardModifier.ShiftModifier
NONE = Qt.KeyboardModifier.NoModifier
LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton


def _send(
    window: OverlayWindow,
    kind: QEvent.Type,
    pos: tuple[float, float],
    *,
    button: Qt.MouseButton = Qt.MouseButton.NoButton,
    buttons: Qt.MouseButton = Qt.MouseButton.NoButton,
    shift: bool = False,
) -> None:
    local = QPointF(*pos)
    event = QMouseEvent(
        kind, local, window.mapToGlobal(local), button, buttons, SHIFT if shift else NONE
    )
    QApplication.sendEvent(window, event)


def press(window, pos, *, shift=False, button=LEFT):
    _send(window, QEvent.Type.MouseButtonPress, pos, button=button, buttons=button, shift=shift)


def move(window, pos, *, shift=False, held=Qt.MouseButton.NoButton):
    _send(window, QEvent.Type.MouseMove, pos, buttons=held, shift=shift)


def release(window, pos, *, shift=False, button=LEFT):
    _send(window, QEvent.Type.MouseButtonRelease, pos, button=button, shift=shift)


def drag(window, points, *, shift=False, button=LEFT):
    first, *rest = points
    press(window, first, shift=shift, button=button)
    for point in rest:
        move(window, point, shift=shift, held=button)
    release(window, points[-1], shift=shift, button=button)


class Keyboard:
    """What the overlay's Shift polling reads (the real keyboard is not used)."""

    def __init__(self, overlay: DrawingOverlay) -> None:
        self.shift = False
        overlay._shift_source = lambda: SHIFT if self.shift else NONE


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


def _second_screen(overlay, qtbot) -> OverlayWindow:
    """A second overlay window right of the first, as on a second monitor."""
    first = overlay.windows[0]
    second = OverlayWindow(overlay, 1)
    qtbot.addWidget(second)
    geometry = first.geometry()
    second.setGeometry(QRect(geometry.right() + 1, geometry.top(), 800, geometry.height()))
    second.show()
    overlay._windows.append(second)
    return second


def _points(shape: Shape) -> list[tuple[float, float]]:
    return [(p.x(), p.y()) for p in shape.points]


# -- helpers -------------------------------------------------------------------------


def test_constrain_square_grows_into_the_cursor_quadrant():
    start = QPointF(100, 100)
    assert constrain_square(start, QPointF(200, 150)) == QPointF(200, 200)
    assert constrain_square(start, QPointF(80, 20)) == QPointF(20, 20)
    assert constrain_square(start, QPointF(130, 40)) == QPointF(160, 40)


def test_clip_segment():
    rect = QRectF(0, 0, 100, 100)
    assert clip_segment(QPointF(10, 10), QPointF(50, 50), rect) == (0.0, 1.0)
    assert clip_segment(QPointF(50, 50), QPointF(150, 50), rect) == (0.0, 0.5)
    assert clip_segment(QPointF(-50, 50), QPointF(150, 50), rect) == (0.25, 0.75)
    assert clip_segment(QPointF(150, 50), QPointF(250, 50), rect) is None
    assert clip_segment(QPointF(100, 50), QPointF(200, 50), rect) is None  # touches only


# -- Clear and Undo --------------------------------------------------------------------


def test_undo_does_nothing_while_shift_is_held(overlay, keyboard, qtbot):
    window = overlay.windows[0]
    drag(window, [(10, 10), (60, 60)])
    keyboard.shift = True
    overlay.undo()
    qtbot.keyClick(
        window,
        Qt.Key.Key_Z,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    assert len(overlay.shapes()) == 1
    keyboard.shift = False
    qtbot.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert overlay.shapes() == []


# -- square, circle ----------------------------------------------------------------------


def test_shift_draws_a_square_and_a_circle(overlay, keyboard):
    overlay.set_screens(_screens())
    window = overlay.windows[0]
    keyboard.shift = True
    drag(window, [(100, 100), (200, 150)], shift=True)
    overlay.set_tool(Tool.ELLIPSE)
    drag(window, [(300, 300), (260, 220)], shift=True)
    square, circle = overlay.shapes()
    assert _points(square) == [(100, 100), (200, 200)] and square.shift
    assert _points(circle) == [(300, 300), (220, 220)] and circle.shift
    data = overlay.drawings()
    assert [d.get("shift") for d in data] == [True, True]
    assert "closed" not in data[0] and "group" not in data[0]


def test_square_follows_shift_while_dragging(overlay, keyboard):
    window = overlay.windows[0]
    press(window, (100, 100))
    move(window, (200, 150), held=LEFT)
    assert _points(window.current_shape)[-1] == (200, 150)
    keyboard.shift = True
    move(window, (200, 150), held=LEFT, shift=True)
    assert _points(window.current_shape)[-1] == (200, 200)
    # Shift released without moving the mouse: the poll turns it back into a rectangle.
    keyboard.shift = False
    overlay._poll_shift()
    assert _points(window.current_shape)[-1] == (200, 150)
    release(window, (200, 150))
    (rect,) = overlay.shapes()
    assert _points(rect) == [(100, 100), (200, 150)] and not rect.shift


def test_rectangle_without_shift_and_right_button_arrow_with_shift_is_double(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(100, 100), (200, 150)])
    keyboard.shift = True
    drag(window, [(300, 100), (400, 150)], shift=True, button=RIGHT)  # not chained: double
    rect, arrow = overlay.shapes()
    assert _points(rect) == [(100, 100), (200, 150)]
    assert arrow.tool is Tool.ARROW and _points(arrow) == [(300, 100), (400, 150)]
    assert overlay._span is None and arrow.shift and arrow.heads == "both"


# -- closed brush ----------------------------------------------------------------------------


def test_brush_with_shift_before_the_press_is_closed(overlay, keyboard):
    overlay.set_screens(_screens())
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
    keyboard.shift = True
    drag(window, [(100, 100), (300, 100), (300, 300)], shift=True)
    keyboard.shift = False
    drag(window, [(500, 100), (600, 100), (600, 200)])  # no Shift: an open stroke
    closed, open_stroke = overlay.shapes()
    assert closed.closed and not open_stroke.closed
    data = overlay.drawings()
    assert data[0]["closed"] is True and data[0]["shift"] is True
    assert "closed" not in data[1]

    # The joining stroke (300,300 -> 100,100) is painted and hit by the eraser.
    image = window.grab().toImage()
    assert image.pixelColor(200, 200) == QColor("#FF0000")
    overlay.set_tool(Tool.ERASER)
    drag(window, [(180, 220), (220, 180)])
    assert overlay.shapes() == [open_stroke]


def test_releasing_shift_finishes_a_closed_brush_shape(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
    keyboard.shift = True
    press(window, (100, 100), shift=True)
    move(window, (200, 100), held=LEFT, shift=True)
    move(window, (200, 200), held=LEFT, shift=True)
    keyboard.shift = False
    move(window, (150, 250), held=LEFT)  # Shift up: finished before this point
    assert window.current_shape is None
    (shape,) = overlay.shapes()
    assert shape.closed and _points(shape) == [(100, 100), (100, 100), (200, 100), (200, 200)]
    move(window, (120, 260), held=LEFT)
    release(window, (120, 260))  # the button's release draws nothing more
    assert overlay.shapes() == [shape]


def test_shift_pressed_during_a_brush_stroke_closes_it_at_once(overlay, keyboard):
    overlay.set_screens(_screens())
    now = [1000]
    overlay.set_clock(lambda: now[0])
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
    press(window, (100, 100))
    move(window, (300, 100), held=LEFT)
    move(window, (300, 300), held=LEFT)
    assert not window.current_shape.closed
    now[0] = 1500
    keyboard.shift = True
    move(window, (310, 300), held=LEFT, shift=True)  # Shift down mid-stroke
    shape = window.current_shape
    assert shape.closed and shape.closed_ms == 1500
    # The end joins the start right away, while the button is still held.
    image = window.grab().toImage()
    assert image.pixelColor(205, 200) == QColor("#FF0000")
    move(window, (320, 320), held=LEFT, shift=True)
    keyboard.shift = False
    move(window, (330, 330), held=LEFT)  # releasing Shift finishes it, as if held from the press
    assert window.current_shape is None
    (finished,) = overlay.shapes()
    assert finished.closed and _points(finished)[-1] == (320, 320)
    data = overlay.drawings()[0]
    assert data["closed"] is True and data["closed_ms"] == 1500 and data["shift"] is True


def test_shift_pressed_during_a_brush_stroke_and_the_button_released_first(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.BRUSH)
    press(window, (100, 100))
    move(window, (300, 100), held=LEFT)
    keyboard.shift = True
    move(window, (300, 300), held=LEFT, shift=True)
    release(window, (300, 300), shift=True)  # the button's release finishes it closed
    (shape,) = overlay.shapes()
    assert shape.closed


# -- connected lines -----------------------------------------------------------------------


def test_connected_lines(overlay, keyboard):
    overlay.set_screens(_screens())
    times = iter(range(1000, 2000, 100))
    overlay.set_clock(lambda: next(times))
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    keyboard.shift = True
    press(window, (100, 100), shift=True)
    release(window, (150, 100), shift=True)  # Shift held: the line goes on
    assert overlay.shapes() == [] and overlay._span.waiting
    move(window, (200, 150), shift=True)  # no button: the end follows the cursor
    assert overlay.drawing_now()["tool"] == "line"
    press(window, (200, 150), shift=True)  # ends the line here, starts the next
    release(window, (200, 150), shift=True)
    move(window, (300, 150), shift=True)
    press(window, (300, 150), shift=True)
    release(window, (300, 150), shift=True)
    move(window, (300, 250), shift=True)
    keyboard.shift = False
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyRelease, Qt.Key.Key_Shift, NONE))
    assert overlay._span is None  # Shift up without a button: the last line ends at once
    lines = overlay.shapes()
    assert [_points(s) for s in lines] == [
        [(100, 100), (200, 150)],
        [(200, 150), (300, 150)],
        [(300, 150), (300, 250)],
    ]
    assert {s.group for s in lines} == {1} and [s.segment for s in lines] == [1, 2, 3]
    assert all(s.shift and s.tool is Tool.LINE for s in lines)
    data = overlay.drawings()
    assert [(d["group"], d["segment"], d["shift"]) for d in data] == [
        (1, 1, True),
        (1, 2, True),
        (1, 3, True),
    ]
    # Every line runs from the press that started it to the one that ended it.
    assert data[1]["started_ms"] == data[0]["finished_ms"]
    # Each line is undone on its own.
    keyboard.shift = False
    overlay.undo()
    assert len(overlay.shapes()) == 2


def test_eraser_removes_all_connected_lines_and_undo_brings_them_back(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    drag(window, [(100, 400), (300, 400)])  # a separate line stays
    keyboard.shift = True
    press(window, (100, 100), shift=True)
    release(window, (200, 100), shift=True)
    for point in ((200, 100), (200, 200), (300, 200)):
        move(window, point, shift=True)
        press(window, point, shift=True)
        release(window, point, shift=True)
    keyboard.shift = False
    overlay._poll_shift()
    lone, *chain = overlay.shapes()
    assert len(chain) == 3 and {s.group for s in chain} == {chain[0].group}
    overlay.set_tool(Tool.ERASER)
    drag(window, [(190, 150), (210, 150)])  # across the middle line only
    assert overlay.shapes() == [lone]
    overlay.undo()  # one step brings back the whole group, in its order
    assert overlay.shapes() == [lone, *chain]
    assert [s.id for s in overlay.shapes()] == [lone.id] + [s.id for s in chain]


def test_shift_pressed_during_a_line_drag_goes_on_and_shift_up_ends_it(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    press(window, (100, 100))
    move(window, (200, 100), held=LEFT)
    keyboard.shift = True
    release(window, (200, 100), shift=True)
    assert overlay._span is not None and overlay.shapes() == []
    move(window, (250, 200), shift=True)
    keyboard.shift = False
    overlay._poll_shift()
    (line,) = overlay.shapes()
    assert _points(line) == [(100, 100), (250, 200)]
    assert line.group is None and line.segment is None and not line.shift


def test_shift_up_with_the_button_held_ends_the_line_at_the_release(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    keyboard.shift = True
    press(window, (100, 100), shift=True)
    release(window, (100, 100), shift=True)
    move(window, (200, 100), shift=True)
    press(window, (200, 100), shift=True)  # first line done, the second one starts
    keyboard.shift = False
    move(window, (250, 150), held=LEFT)  # Shift up, button down: still drawing
    assert overlay._span is not None
    release(window, (300, 200))
    assert overlay._span is None
    assert [_points(s) for s in overlay.shapes()] == [
        [(100, 100), (200, 100)],
        [(200, 100), (300, 200)],
    ]


def test_press_on_the_start_and_other_tools_while_connecting(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    keyboard.shift = True
    press(window, (100, 100), shift=True)
    release(window, (100, 100), shift=True)
    press(window, (100, 100), shift=True)  # a press on the start: nothing to store
    release(window, (100, 100), shift=True)
    assert overlay.shapes() == [] and overlay._span.waiting
    press(window, (150, 150), shift=True, button=RIGHT)  # ignored meanwhile
    assert overlay.shapes() == []
    move(window, (200, 100), shift=True)
    press(window, (200, 100), shift=True)
    release(window, (200, 100), shift=True)
    overlay.set_tool(Tool.RECTANGLE)  # another tool: the line being drawn is dropped
    assert overlay._span is None
    assert [_points(s) for s in overlay.shapes()] == [[(100, 100), (200, 100)]]


# -- across screens --------------------------------------------------------------------------


def test_line_across_screens_is_split_at_the_edge(overlay, qtbot):
    first = overlay.windows[0]
    second = _second_screen(overlay, qtbot)
    width = first.width()
    overlay.set_tool(Tool.LINE)
    drag(first, [(width - 100, 100), (width + 100, 100)])  # the button keeps the first window
    left, right = overlay.shapes()
    assert left.screen_index == 0 and right.screen_index == 1
    assert _points(left) == [(width - 100, 100), (width, 100)]
    assert _points(right) == [(0, 100), (100, 100)]
    assert left.group == right.group is not None and left.split == right.split != 0
    assert left.segment is None and left.part_of is None and not left.shift
    overlay.undo()  # one step takes back both pieces
    assert overlay.shapes() == []
    assert second.shapes == []


def test_arrow_across_screens_keeps_its_head_on_the_last_piece(overlay, qtbot):
    overlay.set_screens(_screens())
    first = overlay.windows[0]
    second = _second_screen(overlay, qtbot)
    width = first.width()
    drag(first, [(width - 50, 200), (width + 150, 200)], button=RIGHT)
    shaft, head = overlay.shapes()
    assert shaft.tool is Tool.LINE and shaft.part_of is Tool.ARROW
    assert head.tool is Tool.ARROW and head.part_of is None and head.screen_index == 1
    data = shape_to_json(shaft, _screens()[0], 1.0)
    assert data["part_of"] == "arrow" and data["group"] == shaft.group

    # The eraser on one piece removes both; Undo brings both back.
    overlay.set_tool(Tool.ERASER)
    drag(second, [(100, 150), (100, 250)])
    assert overlay.shapes() == []
    overlay.undo()
    assert [s.tool for s in overlay.shapes()] == [Tool.LINE, Tool.ARROW]


def test_connected_lines_go_on_on_another_screen(overlay, keyboard, qtbot):
    first = overlay.windows[0]
    second = _second_screen(overlay, qtbot)
    width = first.width()
    overlay.set_tool(Tool.LINE)
    keyboard.shift = True
    press(first, (width - 100, 100), shift=True)
    release(first, (width - 100, 100), shift=True)
    move(second, (100, 100), shift=True)  # the cursor is over the other screen now
    press(second, (100, 100), shift=True)
    release(second, (100, 100), shift=True)
    move(second, (100, 300), shift=True)
    keyboard.shift = False
    overlay._poll_shift()
    a, b, c = overlay.shapes()
    assert (a.screen_index, b.screen_index, c.screen_index) == (0, 1, 1)
    assert _points(a) == [(width - 100, 100), (width, 100)]
    assert _points(b) == [(0, 100), (100, 100)]
    assert _points(c) == [(100, 100), (100, 300)]
    assert a.group == b.group == c.group and [a.segment, b.segment, c.segment] == [1, 1, 2]
    assert a.split == b.split != 0 and c.split == 0

    # The eraser on the last line (second screen only) removes the whole group on both
    # screens; Undo brings every piece back.
    overlay.set_tool(Tool.ERASER)
    drag(second, [(90, 200), (110, 200)])
    assert overlay.shapes() == []
    overlay.undo()
    assert overlay.shapes() == [a, b, c]


# -- double-headed arrows ------------------------------------------------------------------


def test_arrow_tool_with_shift_draws_a_double_headed_arrow(overlay, keyboard):
    overlay.set_screens(_screens())
    window = overlay.windows[0]
    overlay.set_tool(Tool.ARROW)
    keyboard.shift = True
    drag(window, [(100, 300), (400, 300)], shift=True)
    keyboard.shift = False
    drag(window, [(100, 500), (400, 500)])
    double, single = overlay.shapes()
    assert double.heads == "both" and double.shift
    assert single.heads == "end" and not single.shift
    data = overlay.drawings()
    assert data[0]["heads"] == "both" and data[0]["shift"] is True
    assert "heads" not in data[1] and "shift" not in data[1]
    # A head is painted at both ends of the double arrow, only at the end of the other.
    image = window.grab().toImage()
    red = QColor("#FF0000")
    assert image.pixelColor(103, 300) == red and image.pixelColor(397, 300) == red
    assert image.pixelColor(397, 500) == red
    assert image.pixelColor(100, 506) != red  # no head at the tail: the shaft only

    # The eraser hits the second head too.
    overlay.set_tool(Tool.ERASER)
    drag(window, [(108, 280), (108, 320)])
    assert overlay.shapes() == [single]


def test_shift_pressed_and_released_while_dragging_an_arrow(overlay, keyboard):
    window = overlay.windows[0]
    press(window, (100, 100), button=RIGHT)
    move(window, (200, 100), held=RIGHT)
    keyboard.shift = True
    overlay._poll_shift()
    assert overlay._span.double  # the second head appears at once
    move(window, (300, 100), shift=True, held=RIGHT)
    keyboard.shift = False
    overlay._poll_shift()
    release(window, (300, 100), button=RIGHT)  # released without Shift: one head
    (arrow,) = overlay.shapes()
    assert arrow.heads == "end" and not arrow.shift


def test_double_arrow_across_screens_has_a_head_on_both_outer_pieces(overlay, keyboard, qtbot):
    overlay.set_screens(_screens())
    first = overlay.windows[0]
    _second_screen(overlay, qtbot)
    width = first.width()
    keyboard.shift = True
    drag(first, [(width - 50, 200), (width + 150, 200)], shift=True, button=RIGHT)
    start, end = overlay.shapes()
    assert start.tool is Tool.ARROW and start.heads == "start" and start.part_of is None
    assert end.tool is Tool.ARROW and end.heads == "end" and end.screen_index == 1
    assert start.group == end.group and start.shift and end.shift
    assert shape_to_json(start, _screens()[0], 1.0)["heads"] == "start"
    assert "heads" not in shape_to_json(end, _screens()[0], 1.0)


# -- frozen drawing (the recording is paused) ------------------------------------------------


def test_frozen_drawing_keeps_the_shapes_and_reports_every_attempt(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(100, 100), (200, 200)])
    blocked: list[int] = []
    overlay.blocked.connect(lambda: blocked.append(1))
    press(window, (300, 300), button=RIGHT)  # an arrow half drawn when Pause comes
    overlay.set_frozen(True)
    assert overlay._span is None and len(overlay.shapes()) == 1
    assert not overlay._toolbar._tool_buttons[Tool.LINE].isEnabled()

    drag(window, [(400, 400), (500, 500)])
    assert len(overlay.shapes()) == 1 and len(blocked) == 1
    QApplication.sendEvent(
        window, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert overlay.is_visible() and len(blocked) == 2  # Esc does not leave a frozen drawing
    overlay._user_close()  # the toolbar's cross, Alt+F4
    assert len(blocked) == 3 and len(overlay.shapes()) == 1

    overlay.set_frozen(False)
    assert overlay._toolbar._tool_buttons[Tool.LINE].isEnabled()
    drag(window, [(400, 400), (500, 500)])
    assert len(overlay.shapes()) == 2 and len(blocked) == 3
