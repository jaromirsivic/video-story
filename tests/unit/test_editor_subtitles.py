"""Tests for editing a subtitle from the editor (Edit button, dialog, transcript files)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, read_transcript  # noqa: E402
from video_story.editor.subtitle_dialog import SubtitleDialog  # noqa: E402
from video_story.editor.window import EditorWindow  # noqa: E402


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path) -> EditorWindow:
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    return window


def _at(editor: EditorWindow, ms: int) -> None:
    editor.model.set_playhead(ms)
    editor._sync_playhead()


class _ScriptedDialog(SubtitleDialog):
    """Fills the fields and presses Save (or Cancel) instead of waiting for the user."""

    script: dict[str, object] = {}
    seen: list[Segment] = []

    def __init__(self, segment: Segment, duration_ms: int = 0, parent=None):
        super().__init__(segment, duration_ms, parent)
        type(self).seen.append(segment)

    def exec(self) -> int:  # type: ignore[override]
        script = type(self).script
        if script.get("cancel"):
            self.reject()
            return QDialog.DialogCode.Rejected
        if "start" in script:
            self.start_edit.setText(str(script["start"]))
        if "end" in script:
            self.end_edit.setText(str(script["end"]))
        if "text" in script:
            self.text_edit.setPlainText(str(script["text"]))
        self.accept()
        return self.result()


@pytest.fixture
def scripted(editor: EditorWindow, monkeypatch):
    _ScriptedDialog.script = {}
    _ScriptedDialog.seen = []
    monkeypatch.setattr(
        editor, "_subtitle_dialog", lambda segment, duration: _ScriptedDialog(segment, duration)
    )
    return _ScriptedDialog


def _transcript(folder: Path, session_id: str = NEWEST):
    paths = SessionPaths(folder, session_id)
    return read_transcript(paths.transcript_json_path(paths.final_dir))


def test_edit_button_beside_subtitle_enabled_only_with_a_subtitle(editor):
    button = editor.preview.edit_subtitle_button
    assert button.text() == "Edit" and not button.icon().isNull()
    assert button.parentWidget() is editor.preview
    _at(editor, 500)
    assert button.isEnabled()
    label = editor.preview.subtitle_label
    assert button.geometry().left() >= label.geometry().right()  # to the right of the text
    _at(editor, 1200)
    assert not button.isEnabled()
    _at(editor, 1800)
    assert button.isEnabled()


def test_save_rewrites_transcript_files_and_subtitle(editor, scripted, folder: Path):
    _at(editor, 500)
    scripted.script = {"start": "00:00:00.100", "end": "00:00:01.200", "text": "hello  there\nall"}
    editor.preview.edit_subtitle_button.click()
    assert scripted.seen == [Segment(0, 1000, "hello world")]
    assert editor.preview.subtitle_label.text() == "hello there all"
    transcript = _transcript(folder)
    assert transcript.segments == [
        Segment(100, 1200, "hello there all"),
        Segment(1500, 2500, "second line"),
    ]
    assert transcript.language == "en"
    paths = SessionPaths(folder, NEWEST)
    txt = paths.transcript_txt_path(paths.final_dir).read_text(encoding="utf-8")
    assert "[00:00:00.100 --> 00:00:01.200] hello there all" in txt
    assert "Subtitle saved" in editor.statusBar().currentMessage()
    # The other recording is untouched.
    assert _transcript(folder, OLDER).segments[0].text == "older one"


def test_cancel_changes_nothing(editor, scripted, folder: Path):
    _at(editor, 1800)
    scripted.script = {"cancel": True}
    assert editor.edit_subtitle() is False
    assert _transcript(folder).segments[1] == Segment(1500, 2500, "second line")
    assert editor.preview.subtitle_label.text() == "second line"


def test_no_subtitle_at_playhead(editor, scripted):
    _at(editor, 1200)
    assert editor.edit_subtitle() is False
    assert scripted.seen == []


def test_moving_a_segment_keeps_order(editor, scripted, folder: Path):
    _at(editor, 500)
    scripted.script = {"start": "00:00:01.100", "end": "00:00:01.400"}
    assert editor.edit_subtitle()
    assert [s.text for s in _transcript(folder).segments] == ["hello world", "second line"]
    assert editor.recording.subtitle_at(200) == ""
    assert editor.recording.subtitle_at(1200) == "hello world"
    assert editor.preview.subtitle_label.text() == ""  # the playhead (500) is now empty


@pytest.mark.parametrize(
    ("start", "end", "text", "message"),
    [
        ("0:00", "00:00:01.000", "x", "From"),
        ("00:00:01.000", "00:00:01.000", "x", "later than From"),
        ("00:00:00.000", "99:00:00.000", "x", "end of the recording"),
        ("00:00:00.000", "00:00:01.000", "   ", "must not be empty"),
    ],
)
def test_dialog_validation_keeps_it_open(qtbot, start, end, text, message):
    dialog = SubtitleDialog(Segment(0, 1000, "hello"), duration_ms=3000)
    qtbot.addWidget(dialog)
    assert dialog.start_edit.text() == "00:00:00.000"
    assert dialog.end_edit.text() == "00:00:01.000"
    assert dialog.text_edit.toPlainText() == "hello"
    dialog.start_edit.setText(start)
    dialog.end_edit.setText(end)
    dialog.text_edit.setPlainText(text)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted and dialog.segment() is None
    assert message in dialog.error_label.text()


def test_dialog_accepts_time_without_milliseconds(qtbot):
    dialog = SubtitleDialog(Segment(0, 1000, "hello"), duration_ms=3000)
    qtbot.addWidget(dialog)
    dialog.end_edit.setText("00:00:02")
    dialog.accept()
    assert dialog.segment() == Segment(0, 2000, "hello")
