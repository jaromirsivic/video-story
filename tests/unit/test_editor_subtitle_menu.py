"""The narration's context menu (Unlock / Add New Subtitle / Delete Subtitle / Lock),
Ctrl+Z / Ctrl+Y of subtitle changes, a double click on a subtitle (Edit for that one,
the playhead stays) and the small look fixes of the editor (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent, QMouseEvent, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from test_editor import NEWEST, _mouse_move, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import (  # noqa: E402
    LOWER_TRACK,
    Segment,
    Transcript,
    read_transcript,
)
from video_story.editor.subtitle_dialog import SubtitleDialog  # noqa: E402
from video_story.editor.theme import PROCESS_GREEN  # noqa: E402
from video_story.editor.timeline.view import DARK_COLORS, LIGHT_COLORS  # noqa: E402
from video_story.editor.window import (  # noqa: E402
    ADD_SUBTITLE_LABEL,
    DELETE_SUBTITLE_LABEL,
    LOCK_NARRATION_LABEL,
    NO_ROOM_FOR_SUBTITLE,
    UNLOCK_NARRATION_LABEL,
    EditorWindow,
)

A = Segment(0, 1000, "hello world")
B = Segment(1500, 2500, "second line")


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, NEWEST)  # A and B, 2.5 s
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path) -> EditorWindow:
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    return window


def _segments(folder: Path) -> list[Segment]:
    paths = SessionPaths(folder, NEWEST)
    return read_transcript(paths.transcript_json_path(paths.final_dir)).segments


def _transcript_bytes(folder: Path) -> bytes:
    paths = SessionPaths(folder, NEWEST)
    return paths.transcript_json_path(paths.final_dir).read_bytes()


def _point(editor: EditorWindow, time_ms: int, track: int = 0) -> QPoint:
    view = editor.timeline
    return QPoint(int(round(view._x(time_ms))), view.narration_lane_rect(track).center().y())


def _unlock(editor: EditorWindow) -> None:
    assert editor.timeline.set_narration_unlocked(True)


def _labels(menu) -> list[tuple[str, bool]]:
    return [(a.text(), a.isEnabled()) for a in menu.actions() if not a.isSeparator()]


def _trigger(menu, label: str) -> None:
    next(a for a in menu.actions() if a.text() == label).trigger()


def _context_menu(view, pos: QPoint) -> None:
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos, view.mapToGlobal(pos))
    QApplication.sendEvent(view, event)


class _TypingDialog(SubtitleDialog):
    text = "typed"
    accept_it = True
    seen: list[tuple[str, list[Segment]]] = []

    def exec(self) -> int:  # type: ignore[override]
        type(self).seen.append((self.windowTitle(), [p.segment for p in self.parts]))
        if not type(self).accept_it:
            return QDialog.DialogCode.Rejected
        self.text_edit.setPlainText(type(self).text)
        self.accept()
        return QDialog.DialogCode.Accepted


@pytest.fixture
def dialog(editor, monkeypatch):
    _TypingDialog.seen = []
    _TypingDialog.accept_it = True
    _TypingDialog.text = "typed"
    monkeypatch.setattr(editor, "_subtitle_dialog", lambda s, d: _TypingDialog(s, d))
    return _TypingDialog


# The context menu -----------------------------------------------------------------


def test_locked_menu_offers_only_unlock_disabled_while_transcribing(editor):
    menu = editor.narration_menu(0, 500, A)
    assert _labels(menu) == [(UNLOCK_NARRATION_LABEL, True)]
    _trigger(menu, UNLOCK_NARRATION_LABEL)
    assert editor.timeline.narration_unlocked
    assert "unlocked" in editor.statusBar().currentMessage().lower()
    editor.timeline.set_narration_unlocked(False)
    editor.recording.transcript.complete = False
    editor._update_buttons()
    assert _labels(editor.narration_menu(0, 500, A)) == [(UNLOCK_NARRATION_LABEL, False)]


def test_unlocked_menu_add_delete_and_lock(editor):
    _unlock(editor)
    assert _labels(editor.narration_menu(0, 1200, None)) == [
        (ADD_SUBTITLE_LABEL, True),
        (LOCK_NARRATION_LABEL, True),
    ]
    assert _labels(editor.narration_menu(0, 500, A)) == [
        (ADD_SUBTITLE_LABEL, True),
        (DELETE_SUBTITLE_LABEL, True),
        (LOCK_NARRATION_LABEL, True),
    ]
    header = editor.narration_menu(0, None, None)  # over the track header
    assert _labels(header) == [(LOCK_NARRATION_LABEL, True)]
    _trigger(header, LOCK_NARRATION_LABEL)
    assert not editor.timeline.narration_unlocked


def test_right_click_on_the_narration_asks_for_the_menu(editor):
    view = editor.timeline
    requests: list[tuple] = []
    view.narration_menu_requested.disconnect()
    view.narration_menu_requested.connect(lambda *args: requests.append(args))
    _context_menu(view, _point(editor, 500))
    lane, time_ms, segment, _global = requests[-1]
    assert (lane, segment) == (0, A) and abs(time_ms - 500) <= 20
    _context_menu(view, _point(editor, 1200, LOWER_TRACK))
    assert requests[-1][0] == LOWER_TRACK and requests[-1][2] is None
    _context_menu(view, QPoint(20, view.narration_lane_rect(0).center().y()))  # the header
    assert requests[-1][1] is None and requests[-1][2] is None
    _context_menu(view, QPoint(_point(editor, 500).x(), view.audio_rect().center().y()))
    assert len(requests) == 3  # not on the narration: no menu


# Add / Delete ---------------------------------------------------------------------


def test_add_new_subtitle_types_its_text_and_saves(editor, dialog, folder):
    _unlock(editor)
    _trigger(editor.narration_menu(LOWER_TRACK, 500, None), ADD_SUBTITLE_LABEL)
    title, drafts = dialog.seen[-1]
    assert title == "New Subtitle" and drafts == [Segment(500, 1500, "", LOWER_TRACK)]
    assert Segment(500, 1500, "typed", LOWER_TRACK) in _segments(folder)
    assert "Subtitle added" in editor.statusBar().currentMessage()
    assert editor.timeline.narration_unlocked


def test_add_cancelled_adds_nothing(editor, dialog, folder):
    _unlock(editor)
    before = _transcript_bytes(folder)
    dialog.accept_it = False
    assert not editor.add_subtitle(LOWER_TRACK, 500)
    assert _transcript_bytes(folder) == before


def test_add_without_a_free_second_shows_a_message(editor, dialog, folder, monkeypatch):
    _unlock(editor)
    shown: list[str] = []
    monkeypatch.setattr(editor, "_inform", lambda title, text: shown.append(text))
    before = _transcript_bytes(folder)
    assert not editor.add_subtitle(0, 500)  # inside A
    assert not editor.add_subtitle(0, 1200)  # the gap between A and B is 0.5 s
    assert shown == [NO_ROOM_FOR_SUBTITLE, NO_ROOM_FOR_SUBTITLE]
    assert dialog.seen == [] and _transcript_bytes(folder) == before


def test_delete_needs_no_question_and_ctrl_z_brings_it_back(editor, folder, qtbot):
    _unlock(editor)
    before = _transcript_bytes(folder)
    _trigger(editor.narration_menu(0, 500, A), DELETE_SUBTITLE_LABEL)
    assert _segments(folder) == [B]
    assert "Ctrl+Z" in editor.statusBar().currentMessage()
    editor.activateWindow()
    editor.timeline.setFocus()
    qtbot.keyClick(editor.timeline, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert _transcript_bytes(folder) == before
    assert editor.statusBar().currentMessage() == "Undone: subtitle deleted."
    qtbot.keyClick(editor.timeline, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert _segments(folder) == [B]
    assert editor.statusBar().currentMessage() == "Redone: subtitle deleted."


def test_add_and_delete_need_the_unlocked_track(editor, dialog, folder):
    before = _transcript_bytes(folder)
    assert not editor.add_subtitle(LOWER_TRACK, 500)
    assert not editor.delete_subtitle(A)
    assert _transcript_bytes(folder) == before


# Undo / Redo ----------------------------------------------------------------------


def _drag(editor, qtbot, start: QPoint, end: QPoint) -> None:
    view = editor.timeline
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    _mouse_move(view, QPoint((start.x() + end.x()) // 2, end.y()))
    _mouse_move(view, end)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)


def test_undo_and_redo_a_drag(editor, qtbot, folder):
    _unlock(editor)
    before = _transcript_bytes(folder)
    px = editor.model.zoom / 1000.0
    start = _point(editor, 500)
    _drag(editor, qtbot, start, QPoint(start.x() + int(200 * px), start.y()))
    moved = _segments(folder)
    assert moved == [Segment(200, 1200, "hello world"), B]
    assert editor.undo_subtitles()
    assert _transcript_bytes(folder) == before
    assert editor.redo_subtitles()
    assert _segments(folder) == moved
    assert not editor.redo_subtitles()
    assert editor.statusBar().currentMessage() == "Nothing to redo."


def test_several_steps_undo_in_reverse_order(editor, dialog, folder):
    _unlock(editor)
    before = _transcript_bytes(folder)
    assert editor.add_subtitle(LOWER_TRACK, 500)
    assert editor.delete_subtitle(B)
    assert editor.undo_subtitles()  # B back
    assert B in _segments(folder)
    assert editor.undo_subtitles()  # the added one gone
    assert _transcript_bytes(folder) == before
    assert not editor.undo_subtitles()
    assert editor.statusBar().currentMessage() == "Nothing to undo."


def test_undo_does_nothing_while_locked(editor, folder):
    _unlock(editor)
    assert editor.delete_subtitle(A)
    editor.timeline.set_narration_unlocked(False)
    deleted = _transcript_bytes(folder)
    assert not editor.undo_subtitles()
    assert not editor.redo_subtitles()
    assert _transcript_bytes(folder) == deleted
    assert editor.statusBar().currentMessage() == "Narration is locked: unlock it to redo."
    _unlock(editor)  # the step survived the lock
    assert editor.undo_subtitles()
    assert A in _segments(folder)


def test_an_edit_saved_while_locked_is_undone_after_unlocking(editor, dialog, folder):
    editor.model.set_playhead(500)
    dialog.text = "changed words"
    assert editor.edit_subtitle()
    assert Segment(0, 1000, "changed words") in _segments(folder)
    _unlock(editor)
    assert editor.undo_subtitles()
    assert A in _segments(folder)


def test_history_is_cleared_for_another_transcript(editor):
    _unlock(editor)
    assert editor.delete_subtitle(A)
    assert editor.subtitle_history.can_undo
    editor._on_transcription_segment(NEWEST, None)  # the transcript changed underneath
    assert not editor.subtitle_history.can_undo
    assert editor.delete_subtitle(B)
    editor._set_recording(editor.recording)
    assert not editor.subtitle_history.can_undo


def test_a_changed_transcript_makes_a_stale_step_fail_safely(editor):
    _unlock(editor)
    assert editor.delete_subtitle(A)
    editor.recording._store_transcript(Transcript("en", [A, B]))  # A back behind its back
    assert not editor.undo_subtitles()  # adding A again would overlap A
    assert not editor.subtitle_history.can_undo
    assert editor.statusBar().currentMessage().startswith("Cannot undo")


# Double click ---------------------------------------------------------------------


def _double_click(view, pos: QPoint) -> None:
    for kind in (
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.MouseButtonRelease,
    ):
        down = kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
        event = QMouseEvent(
            kind,
            QPointF(pos),
            QPointF(view.mapToGlobal(pos)),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton if down else Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(view, event)


@pytest.mark.parametrize("unlocked", [False, True])
def test_double_click_edits_that_subtitle_only_and_the_playhead_stays(
    editor, dialog, folder, qtbot, unlocked
):
    lower = Segment(1800, 2400, "dole", LOWER_TRACK)
    editor.recording._store_transcript(Transcript("en", [A, B, lower]))
    if unlocked:
        _unlock(editor)
    view = editor.timeline
    editor.model.set_playhead(300)
    editor._sync_playhead()
    moves: list[int] = []
    view.playhead_moved.connect(moves.append)
    dialog.text = "SECOND"
    _double_click(view, _point(editor, 2000))  # upper B, overlapped by the lower one
    assert dialog.seen[-1] == ("Edit Subtitle", [B])
    assert Segment(1500, 2500, "SECOND") in _segments(folder)
    assert lower in _segments(folder)
    qtbot.wait(QApplication.doubleClickInterval() + 100)
    assert moves == [] and editor.model.playhead_ms == 300


def test_a_single_click_on_a_subtitle_moves_the_playhead_after_the_double_click_time(editor, qtbot):
    view = editor.timeline
    editor.model.set_playhead(2400)
    target = _point(editor, 500)
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=target)
    assert view.click_pending and editor.model.playhead_ms == 2400
    qtbot.waitUntil(lambda: not view.click_pending, timeout=3000)
    assert abs(editor.model.playhead_ms - view._time(target.x())) <= 20
    # Outside the subtitles the playhead moves at once, as before.
    gap = _point(editor, 1250)
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=gap)
    assert not view.click_pending and editor.model.playhead_ms == view._time(gap.x())


def test_dragging_from_a_subtitle_still_scrubs(editor, qtbot):
    view = editor.timeline
    start = _point(editor, 300)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    end = QPoint(start.x() + 40, start.y())
    _mouse_move(view, end)
    assert editor.model.playhead_ms == view._time(end.x())
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)
    assert not view.click_pending


# Look -----------------------------------------------------------------------------


def test_open_padlock_is_process_green_in_the_light_theme():
    assert LIGHT_COLORS["lock"].name() == PROCESS_GREEN
    assert DARK_COLORS["lock"].name() == "#2fd36f"


def test_time_under_the_player_has_hundredths(editor):
    editor.preview.set_time(694_701, 1_202_067)
    assert editor.preview.time_label.text() == "00:11:34.70 / 00:20:02.06"


@pytest.mark.parametrize("dark", [False, True])
def test_folders_menu_button_shows_dots_in_the_text_colour(editor, dark):
    editor.set_theme(dark)
    button = editor.folders_menu_button
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    image = button.icon().pixmap(button.iconSize()).toImage()
    ink = QApplication.palette().color(QPalette.ColorRole.Text)
    inked = [
        (x, y)
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 200
    ]
    assert inked
    x, y = inked[len(inked) // 2]
    assert abs(image.pixelColor(x, y).lightness() - ink.lightness()) < 40


def test_ctrl_z_in_a_text_field_stays_with_the_field(editor, qtbot, folder):
    _unlock(editor)
    assert editor.delete_subtitle(A)
    deleted = _transcript_bytes(folder)
    editor.activateWindow()
    editor.search_edit.setFocus()
    qtbot.keyClicks(editor.search_edit, "abc")
    qtbot.keyClick(editor.search_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert editor.search_edit.text() == ""  # the field's own undo
    assert _transcript_bytes(folder) == deleted  # the subtitles stay as they are
