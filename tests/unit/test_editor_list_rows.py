"""Editor UI feedback: thin keypoint lines with a ruler arrow, readable light theme,
recordings list rows with thumbnail, screen dots, separators and hover scrub
(offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from test_editor import DURATION_MS, NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402
from test_editor_features import _with_keypoints  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.editor.preview import SUBTITLE_TEXT_LIGHT  # noqa: E402
from video_story.editor.recordings_view import (  # noqa: E402
    MIN_LIST_WIDTH,
    SEPARATOR_DARK,
    SEPARATOR_LIGHT,
    elided_lines,
)
from video_story.editor.timeline.view import (  # noqa: E402
    COLOR_KEYPOINT,
    KEYPOINT_ARROW_H,
    KEYPOINT_W,
)
from video_story.editor.window import EditorWindow  # noqa: E402


@pytest.fixture
def editor(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER, first_text="older one")
    _with_keypoints(tmp_path, NEWEST, [1000, 2000])
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    yield window
    window.set_theme(True)  # leave the application dark for the other tests


def _move(widget, pos: QPoint) -> None:
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


# Keypoints ----------------------------------------------------------------------------


def test_keypoint_is_a_thin_line_with_a_green_arrow_on_the_ruler(editor, qtbot):
    view = editor.timeline
    editor.model.set_playhead(0)
    view.update()
    x = view.keypoint_x(1000)
    assert x is not None and KEYPOINT_W == 1
    image = view.grab().toImage()
    green = COLOR_KEYPOINT.name()
    columns = [
        c for c in range(int(x) - 4, int(x) + 5)
        if image.pixelColor(c, view.audio_rect().center().y()).name() == green
    ]  # fmt: skip
    assert len(columns) == 1, "the line is as thin as the playhead"
    line_x = columns[0]
    assert image.pixelColor(line_x, 1).name() == green  # the arrow at the top of the ruler
    assert image.pixelColor(line_x - 4, 1).name() == green  # ... as wide as the red one
    assert image.pixelColor(line_x - 4, KEYPOINT_ARROW_H + 3).name() != green
    assert view.keypoint_at(QPoint(line_x, 3)) == 1000  # the arrow shows the tooltip too


# Light theme --------------------------------------------------------------------------


def test_light_theme_subtitle_text_and_edit_icon_are_dark(editor, qtbot):
    editor.set_theme(False)
    label = editor.preview.subtitle_label
    assert SUBTITLE_TEXT_LIGHT in label.styleSheet()
    image = editor.preview.edit_subtitle_button.icon().pixmap(20, 20).toImage()
    ink = [
        image.pixelColor(x, y)
        for x in range(20)
        for y in range(20)
        if image.pixelColor(x, y).alpha() > 200
    ]
    assert ink and max(c.lightness() for c in ink) < 128, "the pencil is dark on light"
    editor.set_theme(True)
    assert SUBTITLE_TEXT_LIGHT not in label.styleSheet()
    image = editor.preview.edit_subtitle_button.icon().pixmap(20, 20).toImage()
    ink = [
        image.pixelColor(x, y)
        for x in range(20)
        for y in range(20)
        if image.pixelColor(x, y).alpha() > 200
    ]
    assert ink and min(c.lightness() for c in ink) > 128, "the pencil is light on dark"


# Recordings list ----------------------------------------------------------------------


def test_list_collapses_below_its_minimum_width(editor):
    left = editor.main_splitter.widget(0)
    assert left.minimumWidth() == MIN_LIST_WIDTH
    assert editor.main_splitter.isCollapsible(0)
    editor.main_splitter.setSizes([MIN_LIST_WIDTH - 50, 1200])
    assert editor.main_splitter.sizes()[0] in (0, MIN_LIST_WIDTH)


def test_rows_have_separators_thumbnail_and_one_dot_per_screen(editor, qtbot):
    view = editor.list_view
    rect = view.visualRect(view.model().index(1, 0))  # the older, unselected row
    qtbot.waitUntil(lambda: view.thumbnails.pending == 0, timeout=5000)
    view.viewport().update()
    image = view.viewport().grab().toImage()
    separator = image.pixelColor(rect.center().x(), rect.bottom()).name()
    assert separator in (SEPARATOR_DARK.name(), SEPARATOR_LIGHT.name())
    thumb = view.delegate.thumb_rect(rect)
    assert image.pixelColor(thumb.center()).name() != "#101010", "a frame is drawn"
    line = QFontMetrics(view.font()).lineSpacing()
    dots = view.delegate.dot_rects(rect, 2, line)
    assert len(dots) == 2 and dots[-1].right() <= rect.right()
    assert view.delegate.dot_rects(rect, 5, line).__len__() == 4  # at most four


def test_clicking_a_dot_switches_the_screen_without_opening_the_recording(editor, qtbot):
    view = editor.list_view
    index = view.model().index(1, 0)
    item = view.model().item(1)
    assert view.screen_of(item) == "screen1"
    line = QFontMetrics(view.font()).lineSpacing()
    second = view.delegate.dot_rects(view.visualRect(index), 2, line)[1]
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=second.center())
    assert view.screen_of(item) == "screen2"
    assert editor.recording.session_id == NEWEST, "the dot does not select the row"


def test_a_dot_switches_every_row_to_the_screen_at_its_position(editor, qtbot):
    from dataclasses import replace

    view = editor.list_view
    item = view.model().item(1)
    one = replace(item, screen_ids=("screen1",))
    three = replace(item, screen_ids=("screen1", "screen2", "screen3"))
    four = replace(item, screen_ids=("screen1", "screen2", "screen3", "screen4"))
    other = view.model().item(0)
    line = QFontMetrics(view.font()).lineSpacing()
    second = view.delegate.dot_rects(view.visualRect(view.model().index(1, 0)), 2, line)[1]
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=second.center())
    assert view.screen_position == 1
    assert view.screen_of(item) == view.screen_of(other) == "screen2"
    assert view.screen_of(one) == "screen1"  # only one screen: stays on it
    assert view.screen_of(three) == view.screen_of(four) == "screen2"
    view.set_screen(four, "screen3")  # the third dot of a row with four screens
    assert view.screen_of(four) == view.screen_of(three) == "screen3"
    assert view.screen_of(item) == "screen2"  # two screens: the second (the last)
    assert view.screen_of(one) == "screen1"
    view.set_screen(three, "screen1")
    assert {view.screen_of(i) for i in (one, item, three, four)} == {"screen1"}


def test_hovering_the_text_scrubs_the_thumbnail_and_the_spoken_text(editor, qtbot):
    view = editor.list_view
    index = view.model().index(0, 0)
    item = view.model().item(0)
    area = view.delegate.scrub_rect(view.visualRect(index))
    assert view.hover_time(0, item) is None
    _move(view.viewport(), QPoint(area.left(), area.center().y()))
    assert view.hover_row == 0 and view.hover_time(0, item) == 0
    _move(view.viewport(), QPoint(area.left() + area.width() - 1, area.center().y()))
    assert view.hover_time(0, item) == DURATION_MS
    assert item.text_at(DURATION_MS) == "second line"
    middle = area.left() + (area.width() - 1) // 2
    _move(view.viewport(), QPoint(middle, area.center().y()))
    assert view.hover_time(0, item) == pytest.approx(DURATION_MS / 2, abs=DURATION_MS * 0.01)
    # Over the thumbnail the moment chosen last stays; leaving the list resets it.
    thumb = view.delegate.thumb_rect(view.visualRect(index))
    before = view.hover_time(0, item)
    _move(view.viewport(), thumb.center())
    assert view.hover_time(0, item) == before
    QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
    assert view.hover_row is None


def test_elided_lines_wraps_and_ends_with_an_ellipsis(qtbot):
    font = QApplication.font()
    width = QFontMetrics(font).horizontalAdvance("word " * 6)
    lines = elided_lines("word " * 40, font, width, 2)
    assert len(lines) == 2 and lines[1].endswith("…")
    assert elided_lines("", font, width, 2) == []
