"""Recordings search, narration track, blinking ARCHIVED label and theme colours
(offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QFont, QTextCursor  # noqa: E402
from test_editor import NEWEST, OLDER, _mouse_move, _wait_loaded, make_recording  # noqa: E402

from video_story.core.archive_state import ArchiveState, ArchiveStatus  # noqa: E402
from video_story.core.config import Config  # noqa: E402
from video_story.core.transcript import Segment  # noqa: E402
from video_story.editor.preview import (  # noqa: E402
    BADGE_BLINK_MS,
    SUBTITLE_MATCH_DARK,
    SUBTITLE_MATCH_LIGHT,
    WATERMARK_DARK,
    watermark_colors,
)
from video_story.editor.recordings_view import BADGE_ARCHIVED  # noqa: E402
from video_story.editor.search import (  # noqa: E402
    match_spans,
    normalize,
    normalize_query,
    recording_matches,
    text_matches,
)
from video_story.editor.timeline.view import (  # noqa: E402
    AUDIO_H,
    COLOR_MATCH,
    COLOR_TRANSCRIBING,
    COLOR_TRANSCRIBING_LIGHT,
    DARK_COLORS,
    HEADER_W,
    LIGHT_COLORS,
    NARRATION_H,
    NARRATION_TEXT_MIN_ZOOM,
)
from video_story.editor.window import EditorWindow  # noqa: E402
from video_story.ui.icons import refresh_icon, search_icon  # noqa: E402

THIRD = "20251231-080000"


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(
        tmp_path,
        NEWEST,
        segments=[
            Segment(0, 800, "Tohle je příliš světlé"),
            Segment(900, 1400, "   "),  # no words: no box
            Segment(1500, 2400, "Second line"),
        ],
    )
    make_recording(tmp_path, OLDER, first_text="Je to PRILIS tmave")
    make_recording(tmp_path, THIRD, first_text="nothing to see")
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    return window


def _search(qtbot, window: EditorWindow, text: str) -> None:
    window.search_edit.setText(text)
    window.search_recordings()
    qtbot.waitUntil(lambda: not window.search_running, timeout=10_000)
    qtbot.wait(50)  # the queued match signals reach the window


# Matching ------------------------------------------------------------------------------


def test_matching_ignores_case_diacritics_and_spacing():
    assert normalize("  Příliš   ŽLUŤOUČKÝ kůň ") == "prilis zlutoucky kun"
    folded = normalize_query("prilis  zlutoucky")
    assert text_matches("Byl to příliš žluťoučký kůň.", folded)
    assert text_matches("PŘÍLIŠ ŽLUŤOUČKÝ", folded)
    assert not text_matches("příliš kůň", folded)  # the phrase as typed, not single words
    assert not text_matches("anything", normalize_query("   "))  # empty query: no match


def test_match_spans_point_into_the_original_text():
    text = "Byl to  PŘÍLIŠ   žluťoučký kůň, příliš!"
    spans = match_spans(text, normalize_query("prilis zlutoucky"))
    assert [text[a:b] for a, b in spans] == ["PŘÍLIŠ   žluťoučký"]
    found = [text[a:b] for a, b in match_spans(text, normalize_query("prilis"))]
    assert found == ["PŘÍLIŠ", "příliš"]
    assert match_spans(text, normalize_query("")) == []
    assert match_spans("", normalize_query("x")) == []
    # A letter that folds to two (German sharp s) still maps back to whole characters.
    assert [("Straße groß"[a:b]) for a, b in match_spans("Straße groß", "gross")] == ["groß"]


def test_recording_matches_reads_the_transcript(tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST, first_text="Dobrý den, světe")
    assert recording_matches(paths, normalize_query("dobry DEN"))
    assert not recording_matches(paths, normalize_query("nashledanou"))
    bare = make_recording(tmp_path, OLDER, with_transcript=False)
    assert not recording_matches(bare, normalize_query("dobry"))


# Window --------------------------------------------------------------------------------


def test_search_box_and_icon_buttons_sit_under_the_list(editor):
    assert editor.search_edit.placeholderText()
    assert editor.search_button.text() == "Search" and not editor.search_button.icon().isNull()
    assert editor.refresh_button.text() == "Refresh" and not editor.refresh_button.icon().isNull()
    edit = editor.search_edit.geometry()
    search = editor.search_button.geometry()
    refresh = editor.refresh_button.geometry()
    assert edit.right() < search.left() < refresh.left()  # box | magnifier | refresh
    assert edit.width() > 3 * refresh.width()
    assert abs(search.center().y() - edit.center().y()) <= 2
    assert not search_icon(24).isNull() and not refresh_icon(24).isNull()


def test_search_marks_the_shown_recording_first_then_the_others(editor, qtbot):
    _search(qtbot, editor, "prilis")
    assert editor.search_query == "prilis"
    assert editor.list_view.search_matches == {NEWEST, OLDER}  # diacritics and case ignored
    assert editor.timeline.search == "prilis"
    message = editor.statusBar().currentMessage()
    assert "found in 2 recordings" in message and "1 subtitle in this one" in message
    # The shown recording's matching narration box is yellow, the other one is not.
    hits = [
        editor.timeline.segment_matches(seg.text) for _, seg in editor.timeline.narration_boxes()
    ]
    assert hits == [True, False]


def test_matched_recording_shows_yellow_boxes_after_clicking_it(editor, qtbot):
    _search(qtbot, editor, "PŘÍLIŠ tmavé")
    assert editor.list_view.search_matches == {OLDER}
    row = editor.recordings.row_of(OLDER)
    editor.list_view.setCurrentIndex(editor.recordings.index(row))
    _wait_loaded(qtbot, editor, OLDER)
    boxes = editor.timeline.narration_boxes()
    assert [editor.timeline.segment_matches(seg.text) for _, seg in boxes] == [True, False]
    image = editor.timeline.grab().toImage()
    rect = boxes[0][0]
    assert image.pixelColor(int(rect.center().x()), int(rect.center().y())) == COLOR_MATCH


def test_list_row_of_a_match_gets_a_yellow_frame(editor, qtbot):
    _search(qtbot, editor, "prilis")
    view = editor.list_view
    rect = view.visualRect(editor.recordings.index(editor.recordings.row_of(OLDER)))
    image = view.viewport().grab().toImage()
    assert image.pixelColor(rect.left() + 1, rect.center().y()) == COLOR_MATCH
    third = view.visualRect(editor.recordings.index(editor.recordings.row_of(THIRD)))
    assert image.pixelColor(third.left() + 1, third.center().y()) != COLOR_MATCH


def test_emptying_the_box_clears_the_search(editor, qtbot):
    _search(qtbot, editor, "svetle")
    assert editor.list_view.search_matches == {NEWEST}
    editor.search_edit.clear()
    assert editor.search_query == ""
    assert editor.list_view.search_matches == set()
    assert editor.timeline.search == ""


def _char_format(box, position: int):
    cursor = QTextCursor(box.document())
    cursor.setPosition(position + 1)  # the format of the character before the cursor
    return cursor.charFormat()


def _show(window: EditorWindow, position_ms: int) -> None:
    window.model.set_playhead(position_ms)
    window._sync_playhead()


def test_searched_phrase_is_bold_and_red_in_the_subtitle(editor, qtbot):
    editor.set_theme(True)
    box = editor.preview.subtitle_label
    assert box.text() == "Tohle je příliš světlé" and box.highlights == []
    _search(qtbot, editor, "PRILIS")
    assert [box.text()[a:b] for a, b in box.highlights] == ["příliš"]
    start, _ = box.highlights[0]
    hit = _char_format(box, start)
    assert hit.fontWeight() == QFont.Weight.Bold
    assert hit.foreground().color().name() == SUBTITLE_MATCH_DARK
    assert _char_format(box, 0).fontWeight() != QFont.Weight.Bold
    editor.set_theme(False)
    try:
        assert _char_format(box, start).foreground().color().name() == SUBTITLE_MATCH_LIGHT
    finally:
        editor.set_theme(True)
    # Another subtitle at the playhead is highlighted by the same search.
    _show(editor, 1600)
    assert box.text() == "Second line" and box.highlights == []
    _show(editor, 100)
    assert len(box.highlights) == 1
    editor.search_edit.clear()
    assert box.highlights == []
    assert _char_format(box, start).fontWeight() != QFont.Weight.Bold


# Narration track -----------------------------------------------------------------------


def test_narration_track_is_half_the_audio_track_under_it(editor):
    view = editor.timeline
    audio, narration, lane = view.audio_rect(), view.narration_rect(), view.progress_rect()
    assert NARRATION_H == AUDIO_H // 2 and narration.height() == NARRATION_H
    assert audio.bottom() < narration.top() and narration.bottom() < lane.top()
    lock = view.lock_rect()
    assert lock.right() < HEADER_W and narration.contains(lock)
    assert view.tracks_bottom() == narration.bottom()
    assert view.minimumHeight() >= lane.bottom()


def test_narration_boxes_only_where_text_is_and_words_when_zoomed(editor):
    view = editor.timeline
    boxes = view.narration_boxes()
    assert [seg.text for _, seg in boxes] == ["Tohle je příliš světlé", "Second line"]
    first, second = boxes[0][0], boxes[1][0]
    assert first.right() < second.left()  # the pause between them stays empty
    pause = view.narration_rect().center().y()
    assert view.narration_segment_at(QPoint(int(view._x(1200)), pause)) is None
    assert view.narration_segment_at(QPoint(int(first.center().x()), pause)) is boxes[0][1]
    editor.model.zoom = NARRATION_TEXT_MIN_ZOOM - 1  # a long recording at fit
    assert not view.narration_shows_text()
    editor.model.zoom = NARRATION_TEXT_MIN_ZOOM
    assert view.narration_shows_text()


def test_narration_cannot_be_dragged_a_click_only_moves_the_playhead(editor, qtbot):
    view = editor.timeline
    crop_before = (editor.model.in_ms, editor.model.out_ms)
    y = view.narration_rect().center().y()
    x = int(view._x(1800))
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x, y))
    _mouse_move(view, QPoint(x + 40, y))
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x + 40, y))
    assert (editor.model.in_ms, editor.model.out_ms) == crop_before
    assert [seg.start_ms for _, seg in view.narration_boxes()] == [0, 1500]


# ARCHIVED label and colours ------------------------------------------------------------


def test_archived_recording_blinks_archived_in_the_preview(editor, qtbot, monkeypatch):
    preview = editor.preview.view
    assert preview.badge is None
    recording = editor.recording
    monkeypatch.setattr(
        recording,
        "archive",
        ArchiveState(status=ArchiveStatus.ARCHIVED, screens=["screen1", "screen2"]),
    )
    editor._set_recording(recording)
    assert preview.badge == BADGE_ARCHIVED and preview.badge_visible
    assert BADGE_BLINK_MS == 1000
    qtbot.waitUntil(lambda: not preview.badge_visible, timeout=3000)
    qtbot.waitUntil(lambda: preview.badge_visible, timeout=3000)
    editor._set_recording(None)
    assert preview.badge is None and not preview.badge_visible


def test_light_theme_uses_a_darker_transcription_green():
    assert DARK_COLORS["transcribing"] == COLOR_TRANSCRIBING
    assert LIGHT_COLORS["transcribing"] == COLOR_TRANSCRIBING_LIGHT
    assert COLOR_TRANSCRIBING_LIGHT.lightness() < COLOR_TRANSCRIBING.lightness() - 40


def test_dark_screen2_watermark_is_lighter_than_the_other_dark_ones():
    background, text = watermark_colors("screen2", dark=True)
    others = [WATERMARK_DARK[i][0].lightness() for i in (0, 2, 3)]
    assert background.lightness() > max(others)
    assert text.lightness() > background.lightness()
    assert background.blue() > background.red()  # still blue
