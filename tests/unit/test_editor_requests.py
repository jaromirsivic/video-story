"""Instruction templates, app icon, waveform fit, screen dot tooltips and the recordings
context menu (open folder, delete) of the editor (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QItemSelectionModel, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.session import (  # noqa: E402
    SessionPaths,
    SessionState,
    Status,
    write_state,
)
from video_story.editor.instructions_dialog import (  # noqa: E402
    CREATE_PLAN,
    DRAFT_INTENT,
    EXPLANATION,
    IMPLEMENT,
    TEMPLATES,
    InstructionsDialog,
)
from video_story.editor.window import EditorWindow  # noqa: E402
from video_story.host import retention  # noqa: E402
from video_story.ui.icons import APP_ICON_SIZES, app_icon  # noqa: E402

THIRD = "20260103-100000"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    make_recording(tmp_path, THIRD, first_text="third one")
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, THIRD)
    return window


# Instructions ---------------------------------------------------------------------


def test_instructions_button_has_no_icon_and_the_example_text_is_gone(editor):
    assert editor.instructions_button.icon().isNull()
    assert "Say what you want" not in EXPLANATION


def test_three_templates_fill_the_box(qtbot):
    dialog = InstructionsDialog("")
    qtbot.addWidget(dialog)
    assert list(dialog.template_buttons) == ["Draft Intent", "Create Plan", "Implement"]
    dialog.template_buttons["Create Plan"].click()
    assert dialog.text_edit.toPlainText() == CREATE_PLAN.text
    assert "implementation plan" in dialog.instructions()


def test_template_texts_say_what_the_ai_must_do():
    assert "formal brief" in DRAFT_INTENT.text and "ask me whether" in DRAFT_INTENT.text
    assert "Do not implement" in CREATE_PLAN.text
    assert "do not ask me" in IMPLEMENT.text
    assert all(len(t.text) < 1000 for t in TEMPLATES)


def test_replacing_other_text_asks_first(qtbot):
    dialog = InstructionsDialog("my own words")
    qtbot.addWidget(dialog)
    asked: list[str] = []
    answer = {"value": False}

    def confirm(template):
        asked.append(template.name)
        return answer["value"]

    dialog.confirm_replace = confirm
    dialog.template_buttons["Implement"].click()
    assert asked == ["Implement"]
    assert dialog.text_edit.toPlainText() == "my own words", "No keeps the text"
    answer["value"] = True
    dialog.template_buttons["Implement"].click()
    assert dialog.text_edit.toPlainText() == IMPLEMENT.text
    dialog.template_buttons["Implement"].click()  # the same template again: no question
    assert asked == ["Implement", "Implement"]


# Application icon -------------------------------------------------------------------


def test_app_icon_has_every_size(qtbot):
    icon = app_icon()
    sizes = {size.width() for size in icon.availableSizes()}
    assert set(APP_ICON_SIZES) <= sizes
    image = icon.pixmap(64, 64).toImage()
    assert image.pixelColor(32, 2).name() == "#1f6fd0"  # the blue background


# Waveform -------------------------------------------------------------------------


def test_fit_button_stretches_the_waveform_to_the_loudest_sound(editor, qtbot):
    timeline = editor.timeline
    qtbot.waitUntil(lambda: timeline.fit_button_rect() is not None, timeout=5000)
    assert not timeline.audio_fit and timeline.waveform_gain() == 1.0
    rect = timeline.fit_button_rect()
    assert rect.right() < 70 and timeline.audio_rect().contains(rect)
    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=rect.center())
    assert timeline.audio_fit
    # The sine in the test WAV peaks at 12000 / 32768.
    assert timeline.waveform_gain() == pytest.approx(32768 / 12000, rel=0.01)
    assert editor.model.playhead_ms == 0, "the toggle does not move the playhead"
    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=rect.center())
    assert not timeline.audio_fit


# Screen dots -------------------------------------------------------------------------


def test_screen_dot_tooltip_names_the_screen_and_its_size(editor):
    view = editor.list_view
    index = view.model().index(0, 0)
    line = QFontMetrics(view.font()).lineSpacing()
    first, second = view.delegate.dot_rects(view.visualRect(index), 2, line)
    assert view.dot_tooltip(first.center()) == "screen1 320x200"
    assert view.dot_tooltip(second.center()) == "screen2 160x100"
    assert view.dot_tooltip(QPoint(first.left() - 40, first.center().y())) is None


# Context menu: delete and open folder ------------------------------------------------


def _select_rows(editor, rows: list[int]) -> None:
    selection = editor.list_view.selectionModel()
    selection.clearSelection()
    for row in rows:
        selection.select(editor.recordings.index(row), QItemSelectionModel.SelectionFlag.Select)


def test_delete_one_recording_after_confirmation(editor, folder, qtbot):
    asked: list[int] = []
    editor._confirm_delete = lambda items: asked.append(len(items)) or True
    item = editor.recordings.item(2)  # the oldest, not the one shown
    assert editor.delete_recordings([item]) == [OLDER]
    assert asked == [1]
    assert not (folder / OLDER).exists()
    assert [i.session_id for i in editor.recordings.items] == [THIRD, NEWEST]
    assert editor.recording.session_id == THIRD


def test_declined_confirmation_deletes_nothing(editor, folder):
    editor._confirm_delete = lambda items: False
    assert editor.delete_recordings([editor.recordings.item(1)]) == []
    assert (folder / NEWEST).exists() and len(editor.recordings.items) == 3


def test_delete_several_selected_including_the_shown_one(editor, folder, qtbot):
    editor._confirm_delete = lambda items: True
    _select_rows(editor, [0, 1])
    items = editor.selected_items()
    assert [i.session_id for i in items] == [THIRD, NEWEST]
    assert sorted(editor.delete_recordings(items)) == sorted([THIRD, NEWEST])
    assert not (folder / THIRD).exists() and not (folder / NEWEST).exists()
    _wait_loaded(qtbot, editor, OLDER)  # the list refreshed and shows what is left


def test_recording_in_use_is_kept(editor, folder, monkeypatch):
    editor._confirm_delete = lambda items: True
    reported: list[list[str]] = []
    editor._report_delete_failures = reported.append
    paths = SessionPaths(folder, OLDER)
    paths.work_dir.mkdir(parents=True)
    state = SessionState(session_id=OLDER, status=Status.EDITING, recording_folder=str(folder))
    write_state(paths.state_path, state)  # the pid is this (running) process
    assert editor.delete_recordings([editor.recordings.item(2)]) == []
    assert (folder / OLDER).exists()
    assert len(reported) == 1 and retention.IN_USE_REASON in reported[0][0]


def test_open_folder_location_opens_the_recording_folder(editor, folder, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(
        "video_story.editor.window.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    assert editor.open_folder_location(editor.recordings.item(1))
    assert Path(opened[0]) == folder / NEWEST


def test_shift_click_selects_several_rows_and_shows_the_clicked_one(editor, qtbot):
    view = editor.list_view
    first = view.visualRect(editor.recordings.index(0)).center()
    last = view.visualRect(editor.recordings.index(2)).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    qtbot.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, last
    )
    assert len(editor.selected_items()) == 3
    _wait_loaded(qtbot, editor, OLDER)
