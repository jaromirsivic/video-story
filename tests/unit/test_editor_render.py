"""Tests for Render Video... in the recordings list: the dialog, the background render,
Cancel Rendering, the RENDERING badge, the green fresh recording and the middle-button
pan of the timeline (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu  # noqa: E402
from test_editor import DURATION_MS, NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.crop import Crop, write_crop  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.editor.recordings_view import (  # noqa: E402
    BADGE_RENDERING,
    FRESH_DARK,
    FRESH_LIGHT,
    fresh_background,
)
from video_story.editor.render_dialog import (  # noqa: E402
    SELECT_FILE_PLACEHOLDER,
    RenderVideoDialog,
)
from video_story.editor.timeline.view import HEADER_W  # noqa: E402
from video_story.editor.window import (  # noqa: E402
    BEING_RENDERED_REASON,
    CANCEL_RENDER_LABEL,
    RENDER_BUSY_NOTE,
    RENDER_ONE_NOTE,
    RENDER_VIDEO_LABEL,
    EditorWindow,
)
from video_story.export import ExportError, ExportRequest, ExportResult  # noqa: E402
from video_story.host import editor_session as es  # noqa: E402


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    return tmp_path


def _open(qtbot, folder: Path, **kwargs) -> EditorWindow:
    window = EditorWindow(Config(recording_folder=folder), **kwargs)
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, kwargs.get("session_id") or NEWEST)
    return window


def _item(window: EditorWindow, session_id: str):
    return window.recordings.item(window.recordings.row_of(session_id))


def _result(request: ExportRequest) -> ExportResult:
    return ExportResult(
        output=request.output,
        srt=request.output.with_suffix(".srt"),
        screen_id=request.screen_id,
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        duration_ms=request.end_ms - request.start_ms,
        width=160,
        height=100,
        fps=24,
        frames_used=3,
        ffmpeg="ffmpeg",
    )


class GatedExport:
    """An export that reports progress and waits until released or cancelled."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.ended = threading.Event()
        self.requests: list[ExportRequest] = []
        self.saw_cancel = False

    def __call__(self, request, config, progress=None, cancel=None):
        self.requests.append(request)
        try:
            progress(0.25, "working")
            deadline = time.perf_counter() + 10  # never hang the suite
            while not self.release.is_set():
                if cancel():
                    self.saw_cancel = True
                    raise ExportError("CANCELLED", "export cancelled")
                if time.perf_counter() > deadline:
                    raise ExportError("TIMEOUT", "never released")
                time.sleep(0.01)
            return _result(request)
        finally:
            self.ended.set()


@pytest.fixture
def window(qtbot, folder: Path) -> EditorWindow:
    win = _open(qtbot, folder)
    win.notes: list[tuple[str, str]] = []
    win._notify_render = lambda icon, title, text: win.notes.append((title, text))
    return win


def _start(
    window: EditorWindow, export: GatedExport, session_id: str, output: Path, screen="screen1"
):
    window._export_fn = export
    asked: list[str] = []

    def ask(item, default_screen):
        asked.append(default_screen)
        return screen, output

    window._ask_render = ask
    assert window.render_video(_item(window, session_id)) is True
    return asked


# Menu -------------------------------------------------------------------------------


def _render_action(window: EditorWindow, clicked: str, selected: list[str]):
    menu = QMenu(window)
    action = window._add_render_action(
        menu, _item(window, clicked), [_item(window, sid) for sid in selected]
    )
    return action


def test_render_video_menu_item_for_one_video_only(window):
    action = _render_action(window, NEWEST, [NEWEST])
    assert action.text() == RENDER_VIDEO_LABEL and action.isEnabled()
    action = _render_action(window, NEWEST, [NEWEST, OLDER])
    assert action.text() == RENDER_VIDEO_LABEL and not action.isEnabled()
    assert action.toolTip() == RENDER_ONE_NOTE


def test_render_video_sits_right_above_archive_video(window):
    item = _item(window, NEWEST)
    menu = window.recording_menu(item, [item])
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts.index(RENDER_VIDEO_LABEL) + 1 == texts.index("Archive Video")
    menu.deleteLater()


# Dialog -------------------------------------------------------------------------------


def test_render_dialog_needs_a_file_name(qtbot, tmp_path: Path, monkeypatch):
    suggested: list[str] = []

    def save_as(parent, title, path, filt):
        suggested.append(path)
        assert filt == "MP4 video (*.mp4)"
        return (str(tmp_path / "clip"), filt)

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(save_as))
    dialog = RenderVideoDialog(
        [("screen1", "screen1 2560x1600"), ("screen2", "screen2 1920x1080")],
        "screen2",
        lambda sid: tmp_path / f"rec_{sid}.mp4",
    )
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.screen_id == "screen2"
    assert dialog.file_edit.text() == "" and dialog.file_edit.isReadOnly()
    assert dialog.file_edit.placeholderText() == SELECT_FILE_PLACEHOLDER
    assert not dialog.render_button.isEnabled()
    qtbot.mouseClick(dialog.file_edit, Qt.MouseButton.LeftButton)
    assert suggested == [str(tmp_path / "rec_screen2.mp4")]
    assert dialog.output == tmp_path / "clip.mp4"  # .mp4 appended
    assert dialog.file_edit.text() == str(tmp_path / "clip.mp4")
    assert dialog.render_button.isEnabled()


def test_render_dialog_cancelled_save_as_keeps_render_disabled(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    dialog = RenderVideoDialog([("screen1", "screen1")], "screen1", lambda sid: tmp_path / "x.mp4")
    qtbot.addWidget(dialog)
    assert dialog.choose_file() is None
    assert dialog.output is None and not dialog.render_button.isEnabled()


# Background render ------------------------------------------------------------------------


def test_render_runs_in_the_background_with_screen_and_crop(window, qtbot, tmp_path: Path):
    window.select_screen("screen2")
    window.model.set_in(700)
    window.model.set_out(2200)
    window._crop_updated()
    export = GatedExport()
    output = tmp_path / "exports" / "clip.mp4"
    asked = _start(window, export, NEWEST, output, screen="screen2")
    assert asked == ["screen2"]  # the screen selected on the timeline is offered
    assert window.renderer.running == NEWEST
    qtbot.waitUntil(lambda: window.list_view.activity(NEWEST) == ("rendering", 0.25))
    badge = window.list_view.badge_of(_item(window, NEWEST))
    assert badge == (BADGE_RENDERING, "25.0 %")
    # The editor stays usable: another recording can be shown meanwhile.
    window.list_view.setCurrentIndex(window.recordings.index(window.recordings.row_of(OLDER)))
    _wait_loaded(qtbot, window, OLDER)
    # One render at a time.
    assert window.render_block_reason([_item(window, OLDER)]) == RENDER_BUSY_NOTE
    action = _render_action(window, NEWEST, [NEWEST])
    assert action.text() == CANCEL_RENDER_LABEL and action.isEnabled()

    export.release.set()
    qtbot.waitUntil(lambda: window.renderer.running is None, timeout=10_000)
    request = export.requests[0]
    assert request.paths.session_id == NEWEST and request.output == output
    assert request.screen_id == "screen2"
    assert (request.start_ms, request.end_ms) == (700, 2200)
    assert request.draw_cursor is True and request.write_srt is True
    assert window.list_view.activity(NEWEST) is None
    assert window.last_export_result is not None and window.last_export_result.duration_ms == 1500
    assert window.notes and window.notes[0][0] == "Render finished"
    assert str(output) in window.notes[0][1]
    assert "Rendered" in window.statusBar().currentMessage()


def test_render_of_another_recording_uses_its_stored_crop(window, qtbot, folder, tmp_path):
    paths = SessionPaths(folder, OLDER)
    write_crop(paths.crop_path(paths.final_dir), Crop(500, 1500))
    export = GatedExport()
    export.release.set()
    asked = _start(window, export, OLDER, tmp_path / "older.mp4")
    assert asked == ["screen1"]
    qtbot.waitUntil(lambda: window.renderer.running is None, timeout=10_000)
    request = export.requests[0]
    assert request.paths.session_id == OLDER
    assert (request.start_ms, request.end_ms) == (500, 1500)
    assert window.recording.session_id == NEWEST  # the shown recording did not change


def test_cancel_rendering_stops_the_export(window, qtbot, tmp_path):
    export = GatedExport()
    _start(window, export, NEWEST, tmp_path / "c.mp4")
    assert window.cancel_rendering() is True
    qtbot.waitUntil(lambda: window.renderer.running is None, timeout=10_000)
    assert export.saw_cancel
    assert window.list_view.activity(NEWEST) is None
    assert window.notes == []  # no box for a cancel
    assert "cancelled" in window.statusBar().currentMessage()
    assert window.render_block_reason([_item(window, NEWEST)]) is None  # can render again


def test_render_failure_is_reported(window, qtbot, tmp_path):
    def failing(request, config, progress=None, cancel=None):
        raise ExportError("FFMPEG_FAILED", "ffmpeg exploded")

    window._export_fn = failing
    window._ask_render = lambda item, screen: ("screen1", tmp_path / "f.mp4")
    assert window.render_video(_item(window, NEWEST)) is True
    qtbot.waitUntil(lambda: window.renderer.running is None, timeout=10_000)
    assert window.notes and window.notes[0][0] == "Render failed"
    assert "ffmpeg exploded" in window.notes[0][1] and "FFMPEG_FAILED" in window.notes[0][1]


def test_a_rendering_video_cannot_be_deleted_moved_archived_or_retranscribed(
    window, qtbot, tmp_path
):
    export = GatedExport()
    _start(window, export, NEWEST, tmp_path / "r.mp4")
    item = _item(window, NEWEST)
    assert window.archivable([item]) == [] and window.movable([item]) == []
    failures: list[list[str]] = []
    window._report_delete_failures = failures.append
    assert window.delete_recordings([item], confirm=False) == []
    assert failures and BEING_RENDERED_REASON in failures[0][0]
    assert window.recordings.row_of(NEWEST) >= 0
    window._confirm_transcribe_again = lambda recording: pytest.fail("must not be asked")
    assert window.request_transcribe_again() is False
    export.release.set()
    qtbot.waitUntil(lambda: window.renderer.running is None, timeout=10_000)


# Closing while rendering ------------------------------------------------------------------


def test_closing_while_rendering_asks_and_stops_the_render(window, qtbot, tmp_path):
    export = GatedExport()
    _start(window, export, NEWEST, tmp_path / "r.mp4")
    answers = [False, True]
    asked: list[bool] = []

    def confirm() -> bool:
        asked.append(True)
        return answers.pop(0)

    window._confirm_stop_render = confirm
    window.close()
    assert window.isVisible() and window.renderer.running == NEWEST  # kept
    window.close()
    assert not window.isVisible() and len(asked) == 2
    assert window.renderer.running is None
    assert export.ended.wait(5) and export.saw_cancel


def test_process_while_rendering_asks_first(qtbot, folder, tmp_path):
    editor_session = es.new_editor_session(Config(recording_folder=folder))
    window = _open(qtbot, folder, editor_session=editor_session)
    window._notify_render = lambda *a: None
    export = GatedExport()
    _start(window, export, NEWEST, tmp_path / "r.mp4")
    window._confirm_stop_render = lambda: False
    assert window.process() is None
    assert es.read_editor_event(editor_session)["event"] != "process"
    assert window.renderer.running == NEWEST
    window._confirm_stop_render = lambda: True
    assert window.process() == Crop(0, DURATION_MS)
    assert es.read_editor_event(editor_session)["event"] == "process"
    assert window.renderer.running is None and export.saw_cancel
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)


def test_closing_without_a_render_does_not_ask(window):
    window._confirm_stop_render = lambda: pytest.fail("nothing renders")
    window.close()
    assert not window.isVisible()


# Fresh recording ----------------------------------------------------------------------


def test_newest_recording_is_fresh_and_painted_green_when_not_selected(window, qtbot):
    assert window.fresh_recording == NEWEST
    view = window.list_view
    row = window.recordings.row_of(NEWEST)
    view.setCurrentIndex(window.recordings.index(window.recordings.row_of(OLDER)))
    _wait_loaded(qtbot, window, OLDER)
    rect = view.visualRect(window.recordings.index(row))
    image = view.viewport().grab().toImage()
    probe = image.pixelColor(rect.left() + 2, rect.center().y())
    green = fresh_background(view.palette()).name()  # light or dark theme
    assert green in (FRESH_LIGHT.name(), FRESH_DARK.name())
    assert probe.name() == green
    # Selected again: the selection colour wins.
    view.setCurrentIndex(window.recordings.index(row))
    image = view.viewport().grab().toImage()
    assert image.pixelColor(rect.left() + 2, rect.center().y()).name() != green
    # The other recording is not green.
    other = view.visualRect(window.recordings.index(window.recordings.row_of(OLDER)))
    assert image.pixelColor(other.left() + 2, other.center().y()).name() != green


def test_recording_from_the_stop_dialog_is_fresh(qtbot, folder):
    window = _open(qtbot, folder, session_id=OLDER, recording_session=OLDER)
    assert window.fresh_recording == OLDER
    window.refresh_recordings()  # stays after a rescan
    assert window.fresh_recording == OLDER


# Middle-button pan ------------------------------------------------------------------------


def _mouse(widget, kind: QEvent.Type, pos: QPoint, button, buttons) -> None:
    event = QMouseEvent(
        kind,
        QPointF(pos),
        QPointF(widget.mapToGlobal(pos)),
        button,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def test_middle_button_drags_the_timeline(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST, duration_ms=120_000)  # long: there is room to scroll
    window = _open(qtbot, tmp_path)
    view = window.timeline
    model = window.model
    model.set_zoom(model.max_zoom())  # zoomed in: there is something to scroll
    assert model.max_scroll_ms() > 0
    model.set_scroll(model.max_scroll_ms() / 2)
    view._after_view_change()
    playhead, crop = model.playhead_ms, (model.in_ms, model.out_ms)
    start = model.scroll_ms
    step = 0.2 * min(start, model.max_scroll_ms() - start) * model.zoom / 1000.0  # px
    y = view.track_rects()[0][1].center().y()
    x0 = HEADER_W + 300
    middle, none = Qt.MouseButton.MiddleButton, Qt.MouseButton.NoButton
    _mouse(view, QEvent.Type.MouseButtonPress, QPoint(x0, y), middle, middle)
    assert view.panning
    _mouse(view, QEvent.Type.MouseMove, QPoint(x0 + int(step), y), none, middle)
    # Dragged to the right: earlier times come into view.
    assert model.scroll_ms == pytest.approx(start - int(step) * 1000.0 / model.zoom, abs=1)
    _mouse(view, QEvent.Type.MouseMove, QPoint(x0 - int(step), y), none, middle)
    assert model.scroll_ms == pytest.approx(start + int(step) * 1000.0 / model.zoom, abs=1)
    _mouse(view, QEvent.Type.MouseButtonRelease, QPoint(x0 - int(step), y), middle, none)
    assert not view.panning
    assert view.scrollbar.value() == model.scroll_ms
    assert model.playhead_ms == playhead and (model.in_ms, model.out_ms) == crop
    # Moving without the button afterwards does not scroll any more.
    _mouse(view, QEvent.Type.MouseMove, QPoint(x0, y), none, none)
    assert view.scrollbar.value() == model.scroll_ms != start
