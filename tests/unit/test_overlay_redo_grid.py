"""Drawing layer: Redo (Ctrl+Y), the Snap to grid button (Ctrl+G), the Sketch button that
stays pressed, and what the timeline says about them (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402
from test_events import build_timeline, lines, make_recording, rect  # noqa: E402
from test_overlay_grid import (  # noqa: E402
    Keyboard,
    _on_grid,
    _points,
    _screens,
    _second_screen,
    drag,
    move,
    press,
    release,
)

from video_story.ui.overlay import GRID_COLOR, DrawingOverlay, Tool  # noqa: E402

CTRL = Qt.KeyboardModifier.ControlModifier


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


def _key(window, key: Qt.Key, modifiers=CTRL) -> None:
    QApplication.sendEvent(window, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


# -- Redo ----------------------------------------------------------------------------


def test_redo_draws_an_undone_shape_again_with_its_id(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(10, 10), (60, 60)], keyboard)
    drag(window, [(100, 100), (160, 160)], keyboard)
    ids = [s.id for s in overlay.shapes()]
    overlay.undo()
    assert [s.id for s in overlay.shapes()] == ids[:1]
    overlay.redo()
    assert [s.id for s in overlay.shapes()] == ids
    overlay.redo()  # nothing more to redo
    assert [s.id for s in overlay.shapes()] == ids


def test_redo_of_an_eraser_stroke_removes_the_shapes_again(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(10, 10), (60, 60)], keyboard)
    overlay.set_tool(Tool.ERASER)
    drag(window, [(10, 30), (12, 30)], keyboard)  # across the left edge
    assert overlay.shapes() == []
    overlay.undo()
    assert len(overlay.shapes()) == 1
    overlay.redo()
    assert overlay.shapes() == []
    overlay.undo()
    assert len(overlay.shapes()) == 1


def test_a_new_shape_or_clear_forgets_the_redo(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(10, 10), (60, 60)], keyboard)
    overlay.undo()
    drag(window, [(100, 100), (160, 160)], keyboard)
    overlay.redo()
    assert len(overlay.shapes()) == 1
    overlay.undo()
    overlay.clear()
    overlay.redo()
    assert overlay.shapes() == []


def test_redo_brings_back_every_piece_of_a_split_arrow(overlay, keyboard, qtbot):
    second = _second_screen(overlay, qtbot)
    first = overlay.windows[0]
    overlay.set_tool(Tool.ARROW)
    right = first.width() - 50
    press(first, (right, 100), keyboard)
    move(first, (first.width() + 100, 100), keyboard, held=Qt.MouseButton.LeftButton)
    release(second, (100, 100), keyboard)
    pieces = len(overlay.shapes())
    assert pieces == 2
    overlay.undo()
    assert overlay.shapes() == []
    overlay.redo()
    assert len(overlay.shapes()) == pieces


def test_ctrl_y_redoes_and_shift_blocks_it(overlay, keyboard):
    window = overlay.windows[0]
    drag(window, [(10, 10), (60, 60)], keyboard)
    _key(window, Qt.Key.Key_Z)
    assert overlay.shapes() == []
    keyboard.shift = True
    overlay.redo()
    assert overlay.shapes() == []
    keyboard.shift = False
    _key(window, Qt.Key.Key_Y)
    assert len(overlay.shapes()) == 1


def test_ctrl_y_and_ctrl_g_are_blocked_while_frozen(overlay):
    blocked: list[int] = []
    overlay.blocked.connect(lambda: blocked.append(1))
    overlay.set_frozen(True)
    window = overlay.windows[0]
    _key(window, Qt.Key.Key_Y)
    _key(window, Qt.Key.Key_G)
    assert len(blocked) == 2 and not overlay.grid_locked


# -- The toolbar -----------------------------------------------------------------------


def test_toolbar_has_sketch_snap_to_grid_and_clear_but_no_undo(overlay):
    toolbar = overlay._toolbar
    texts = [b.text() for b in toolbar.findChildren(QPushButton)]
    assert texts == ["Sketch (S)", "Snap to grid (Ctrl+G)", "Clear (Ctrl+Alt+C)"]
    assert toolbar.sketch_button.isCheckable() and toolbar.grid_button.isCheckable()
    assert not hasattr(toolbar, "undo_requested")


def test_sketch_button_stays_pressed_and_follows_the_s_key(overlay):
    toolbar = overlay._toolbar
    toolbar.sketch_button.click()
    assert overlay.sketch and toolbar.sketch_button.isChecked()
    overlay.toggle_sketch()  # the S key
    assert not overlay.sketch and not toolbar.sketch_button.isChecked()
    assert toolbar.sketch_button.text() == "Sketch (S)"


# -- Snap to grid ----------------------------------------------------------------------


def test_snap_to_grid_snaps_without_ctrl_and_releases_again(overlay, keyboard):
    window = overlay.windows[0]
    toolbar = overlay._toolbar
    toolbar.grid_button.click()
    assert overlay.grid_locked and overlay.snapping and toolbar.grid_button.isChecked()
    drag(window, [(21, 37), (83, 98)], keyboard)
    snapped = overlay.shapes()[-1]
    assert _on_grid(_points(snapped)) and snapped.grid == 16 and snapped.grid_toggle
    _key(window, Qt.Key.Key_G)  # Ctrl+G releases it
    assert not overlay.grid_locked and not toolbar.grid_button.isChecked()
    drag(window, [(21, 37), (83, 98)], keyboard)
    assert _points(overlay.shapes()[-1]) == [(21, 37), (83, 98)]


def test_snap_to_grid_works_for_lines(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_tool(Tool.LINE)
    overlay.set_grid_locked(True)
    drag(window, [(21, 37), (83, 98)], keyboard)
    line = overlay.shapes()[-1]
    assert _on_grid(_points(line)) and line.grid_toggle


def test_grid_shows_on_the_sketch_while_pressed_never_on_the_screen(overlay, keyboard):
    window = overlay.windows[0]
    overlay.set_grid_locked(True)
    overlay.set_sketch(True)
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) == GRID_COLOR
    overlay.set_grid_locked(False)
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) == QColor("#FFFFFF")
    overlay.set_grid_locked(True)
    overlay.set_sketch(False)
    image = window.grab().toImage()
    assert image.pixelColor(32, 40) != GRID_COLOR


def test_snap_to_grid_is_released_at_every_drawing_start(overlay):
    overlay.set_grid_locked(True)
    overlay.hide()
    assert not overlay.grid_locked
    overlay.show()
    assert not overlay.grid_locked and not overlay._toolbar.grid_button.isChecked()
    overlay.toggle_grid_lock()
    overlay.leave()
    assert not overlay.grid_locked
    overlay.hide()
    overlay.toggle_grid_lock()  # not drawing: nothing happens
    assert not overlay.grid_locked


def test_grid_source_for_the_sidecars(overlay, keyboard):
    overlay.set_screens(_screens())
    screen_id = _screens()[0].id
    assert overlay.grid() is None and overlay.grid_source() is None
    keyboard.ctrl = True
    overlay._poll_shift()
    assert overlay.grid()[screen_id] == 16 and overlay.grid_source() == "ctrl"
    overlay.set_grid_locked(True)
    assert overlay.grid_source() == "toggle"
    keyboard.ctrl = False
    overlay._poll_shift()
    assert overlay.grid()[screen_id] == 16 and overlay.grid_source() == "toggle"
    overlay.set_grid_locked(False)
    assert overlay.grid() is None and overlay.grid_source() is None


def test_shape_json_says_the_toggle_snapped_it(overlay, keyboard):
    overlay.set_screens(_screens())
    overlay.set_grid_locked(True)
    drag(overlay.windows[0], [(21, 37), (83, 98)], keyboard)
    data = overlay.drawings()[-1]
    assert data["grid"] == 16 and data["grid_source"] == "toggle"
    overlay.set_grid_locked(False)
    keyboard.ctrl = True
    overlay._poll_shift()
    drag(overlay.windows[0], [(121, 137), (183, 198)], keyboard)
    assert "grid_source" not in overlay.drawings()[-1]


# -- The timeline ----------------------------------------------------------------------


def test_timeline_names_the_snap_to_grid_button_and_keeps_short_periods(tmp_path: Path):
    snapped = dict(rect(1, 1200, 1400), grid=16, grid_source="toggle")
    ticks = {
        1000: {"grid": 16, "grid_source": "toggle"},
        1500: {"drawings": [snapped]},  # a short toggle period, nothing snapped in it
        3000: {"grid": 16, "drawings": [snapped]},  # a short Ctrl: left out
        3500: {"drawings": [snapped]},
    }
    paths = make_recording(tmp_path, ticks)
    text = lines(build_timeline(paths, end_ms=3500))
    assert (
        "1000 GRID on: Snap to grid switched on (Ctrl+G), points snap to a 16 px grid "
        "(pixels of the screen1 image; the grid shows on the sketch canvas only)"
    ) in text
    assert "1500 GRID off (Snap to grid switched off; snapping since 1000)" in text
    assert not any(line.startswith("3000 GRID") for line in text)
    draw = next(line for line in text if " DRAW #1 " in line)
    assert "(snapped to the 16 px grid, Snap to grid)" in draw


def test_timeline_restored_by_redo(tmp_path: Path):
    shape = rect(1, 1000, 1200)
    ticks = {1500: {"drawings": [shape]}, 2000: {}, 2500: {"drawings": [shape]}}
    paths = make_recording(tmp_path, ticks)
    text = lines(build_timeline(paths, end_ms=2500))
    assert any(line.startswith("2000 REMOVED #1") for line in text)
    assert "2500 RESTORED #1 (gone at 2000, back by Undo or Redo; same shapes as drawn)" in text


def test_frames_report_the_grid_source(tmp_path: Path):
    from video_story.host.runner import frames_at

    ticks = {1000: {"grid": 16, "grid_source": "toggle"}, 1500: {"grid": 16}}
    paths = make_recording(tmp_path, ticks)
    assert frames_at(paths, 1000, "screen1")["frames"][0]["grid_source"] == "toggle"
    assert frames_at(paths, 1500, "screen1")["frames"][0]["grid_source"] == "ctrl"  # older
    assert frames_at(paths, 2000, "screen1")["frames"][0]["grid_source"] is None
