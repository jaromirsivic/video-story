"""Editing subtitles on the unlocked narration tracks in the editor window: the
padlock, dragging (move, resize, other track), the red preview, Escape, Edit with two
tracks (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402
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
from video_story.editor.timeline.narration_edit import Zone  # noqa: E402
from video_story.editor.timeline.view import (  # noqa: E402
    LOCK_TOOLTIP_LOCKED,
    LOCK_TOOLTIP_UNAVAILABLE,
    LOCK_TOOLTIP_UNLOCKED,
)
from video_story.editor.window import EditorWindow  # noqa: E402

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


def _transcript(folder: Path):
    paths = SessionPaths(folder, NEWEST)
    return read_transcript(paths.transcript_json_path(paths.final_dir))


def _point(editor: EditorWindow, time_ms: int, track: int = 0) -> QPoint:
    view = editor.timeline
    return QPoint(int(round(view._x(time_ms))), view.narration_lane_rect(track).center().y())


def _unlock(editor: EditorWindow, qtbot) -> None:
    view = editor.timeline
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.lock_rect().center())
    assert view.narration_unlocked


def _drag(editor, qtbot, start: QPoint, end: QPoint, release: bool = True) -> None:
    view = editor.timeline
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    _mouse_move(view, QPoint((start.x() + end.x()) // 2, end.y()))
    _mouse_move(view, end)
    if release:
        qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)


def test_padlock_opens_only_with_a_complete_transcript(editor, qtbot):
    view = editor.timeline
    assert view.narration_editable and not view.narration_unlocked
    assert view.lock_tooltip() == LOCK_TOOLTIP_LOCKED
    _unlock(editor, qtbot)
    assert view.lock_tooltip() == LOCK_TOOLTIP_UNLOCKED
    assert "unlocked" in editor.statusBar().currentMessage().lower()
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.lock_rect().center())
    assert not view.narration_unlocked
    # While the recording is (again) being transcribed the padlock stays closed.
    _unlock(editor, qtbot)
    editor.recording.transcript.complete = False
    editor._update_buttons()
    assert not view.narration_editable and not view.narration_unlocked
    assert view.lock_tooltip() == LOCK_TOOLTIP_UNAVAILABLE
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=view.lock_rect().center())
    assert not view.narration_unlocked


def test_zones_show_only_while_unlocked(editor, qtbot):
    view = editor.timeline
    middle = _point(editor, 500)
    assert view.segment_hit(middle).zone is Zone.NONE  # locked
    _unlock(editor, qtbot)
    assert view.segment_hit(middle).zone is Zone.MOVE
    assert view.segment_hit(_point(editor, 980)).zone is Zone.RESIZE_END
    assert view.segment_hit(_point(editor, 1200)).zone is Zone.NONE  # the pause
    assert view.segment_hit(_point(editor, 500, 1)).zone is Zone.NONE  # empty lower track


def test_dragging_a_subtitle_moves_it_and_saves(editor, qtbot, folder: Path):
    _unlock(editor, qtbot)
    px = editor.model.zoom / 1000.0  # px per ms
    start = _point(editor, 500)
    _drag(editor, qtbot, start, QPoint(start.x() + int(200 * px), start.y()))
    segments = _transcript(folder).segments
    assert segments == [Segment(200, 1200, "hello world"), B]
    assert "Subtitle moved" in editor.statusBar().currentMessage()


def test_moving_to_the_lower_track_may_overlap(editor, qtbot, folder: Path):
    _unlock(editor, qtbot)
    px = editor.model.zoom / 1000.0
    start = _point(editor, 500)
    end = QPoint(start.x() + int(1000 * px), editor.timeline.narration_lane_rect(1).center().y())
    _drag(editor, qtbot, start, end)
    segments = _transcript(folder).segments
    assert Segment(1000, 2000, "hello world", LOWER_TRACK) in segments
    # The preview shows both texts where they overlap.
    editor.model.set_playhead(1700)
    editor._sync_playhead()
    assert editor.preview.subtitle_label.text() == "second line hello world"


def test_overlap_in_one_track_is_red_and_changes_nothing(editor, qtbot, folder: Path):
    _unlock(editor, qtbot)
    px = editor.model.zoom / 1000.0
    start = _point(editor, 500)
    end = QPoint(start.x() + int(800 * px), start.y())
    _drag(editor, qtbot, start, end, release=False)
    proposal = editor.timeline.segment_proposal
    assert proposal is not None and not proposal.valid
    qtbot.mouseRelease(editor.timeline, Qt.MouseButton.LeftButton, pos=end)
    assert _transcript(folder).segments == [A, B]


def test_escape_cancels_only_the_drag(editor, qtbot, folder: Path):
    _unlock(editor, qtbot)
    editor.model.set_playhead(2000)
    px = editor.model.zoom / 1000.0
    start = _point(editor, 500)
    end = QPoint(start.x() + int(200 * px), start.y())
    _drag(editor, qtbot, start, end, release=False)
    assert editor.timeline.segment_dragging
    editor.stop_playback()  # what Escape does
    assert not editor.timeline.segment_dragging
    assert editor.model.playhead_ms == 2000  # Stop did not run
    qtbot.mouseRelease(editor.timeline, Qt.MouseButton.LeftButton, pos=end)
    assert _transcript(folder).segments == [A, B]


class _TwoFieldDialog(SubtitleDialog):
    texts: list[str] = []
    titles: list[str] = []

    def exec(self) -> int:  # type: ignore[override]
        type(self).titles = [part.box.title() for part in self.parts]
        for part, text in zip(self.parts, type(self).texts, strict=False):
            part.text_edit.setPlainText(text)
        self.accept()
        return QDialog.DialogCode.Accepted


def test_edit_shows_one_box_per_track_under_the_playhead(editor, qtbot, folder, monkeypatch):
    _unlock(editor, qtbot)
    lower = Segment(1800, 2400, "dole", LOWER_TRACK)
    editor.recording._store_transcript(Transcript("en", [A, B, lower]))
    monkeypatch.setattr(editor, "_subtitle_dialog", lambda s, d: _TwoFieldDialog(s, d))
    editor.model.set_playhead(2000)
    _TwoFieldDialog.texts = ["second LINE", "DOLE"]
    assert editor.edit_subtitle()
    assert _TwoFieldDialog.titles == ["Upper track", "Lower track"]
    segments = _transcript(folder).segments
    assert Segment(1500, 2500, "second LINE") in segments
    assert Segment(1800, 2400, "DOLE", LOWER_TRACK) in segments
    # Only the upper subtitle under the playhead: one box titled after its track.
    editor.model.set_playhead(1600)
    _TwoFieldDialog.texts = ["second line"]
    assert editor.edit_subtitle()
    assert _TwoFieldDialog.titles == ["Upper track"]
    # Only the lower one.
    editor.recording.apply_segment_edit(
        (Segment(1500, 2500, "second line"),), (Segment(1500, 1700, "second line"),)
    )
    editor.model.set_playhead(2200)
    _TwoFieldDialog.texts = ["dole"]
    assert editor.edit_subtitle()
    assert _TwoFieldDialog.titles == ["Lower track"]


def test_transcribe_again_warns_about_the_lower_track(editor):
    text = editor.transcribe_again_text(editor.recording)
    assert "lower track are lost" in text and "upper track again" in text


def test_escape_key_during_a_drag_and_the_preview_paints(editor, qtbot, folder: Path):
    _unlock(editor, qtbot)
    px = editor.model.zoom / 1000.0
    start = _point(editor, 500)
    end = QPoint(start.x() + int(200 * px), start.y())
    _drag(editor, qtbot, start, end, release=False)
    image = editor.timeline.grab().toImage()  # paints the green preview
    preview = editor.timeline.segment_box(editor.timeline.segment_proposal.new[0])
    color = image.pixelColor(int(preview.center().x()), int(preview.center().y()))
    assert color.green() > color.red() and color.green() > color.blue()
    qtbot.keyClick(editor.timeline, Qt.Key.Key_Escape)
    assert not editor.timeline.segment_dragging
    qtbot.mouseRelease(editor.timeline, Qt.MouseButton.LeftButton, pos=end)
    assert _transcript(folder).segments == [A, B]
