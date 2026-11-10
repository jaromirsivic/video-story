"""Editor features: keypoints, play marker, zoom to playhead, ETA, Instructions, theme,
icon transport and the scrollable subtitle (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from test_editor import (  # noqa: E402
    DURATION_MS,
    NEWEST,
    OLDER,
    _mouse_move,
    _wait_loaded,
    make_recording,
)

from video_story.core.config import Config  # noqa: E402
from video_story.core.crop import Crop  # noqa: E402
from video_story.core.instructions import read_instructions  # noqa: E402
from video_story.core.session import (  # noqa: E402
    SessionPaths,
    SessionState,
    Status,
    read_state,
    write_state,
)
from video_story.editor.instructions_dialog import InstructionsDialog  # noqa: E402
from video_story.editor.preview import SUBTITLE_MAX_LINES, PreviewPanel, SubtitleBox  # noqa: E402
from video_story.editor.timeline.model import TimelineModel  # noqa: E402
from video_story.editor.timeline.view import (  # noqa: E402
    COLOR_KEYPOINT,
    DARK_COLORS,
    HEADER_W,
    LIGHT_COLORS,
    RULER_H,
    format_eta,
    transcribing_label,
)
from video_story.editor.window import (  # noqa: E402
    INSTRUCTIONS_LABEL,
    INSTRUCTIONS_SET_LABEL,
    EditorWindow,
)
from video_story.host import editor_session as es  # noqa: E402


def _with_keypoints(root: Path, session_id: str, keypoints: list[int]) -> SessionPaths:
    paths = make_recording(root, session_id)
    manifest = paths.manifest_path(paths.final_dir)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["keypoints"] = [{"index": i + 1, "time_ms": t} for i, t in enumerate(keypoints)]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return paths


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    _with_keypoints(tmp_path, NEWEST, [1000, 2000])
    return tmp_path


@pytest.fixture
def editor(qtbot, folder: Path):
    window = EditorWindow(Config(recording_folder=folder))
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    return window


# Model --------------------------------------------------------------------------------


def test_model_keypoints_snap_and_stop_position():
    model = TimelineModel(duration_ms=10_000, viewport_width=1000)
    model.set_keypoints([7000, 2000, 2000, 12_000, -5])
    assert model.keypoints == [2000, 7000]
    assert model.snap_to_keypoint(2100, tolerance_ms=200) == 2000
    assert model.snap_to_keypoint(2300, tolerance_ms=200) == 2300
    assert model.nearest_keypoint(5000) == 7000
    model.set_in(1000)
    model.set_out(9000)
    assert model.stop_position() == 1000  # no marker: In
    model.set_play_marker(3000)
    assert model.stop_position() == 3000
    model.set_play_marker(9500)  # outside the crop: Stop goes to In
    assert model.stop_position() == 1000
    model.set_duration(5000)
    assert model.keypoints == [] and model.play_marker_ms is None


def test_model_zoom_centered_keeps_the_time_in_the_middle():
    model = TimelineModel(duration_ms=100_000, viewport_width=1000)
    model.zoom_centered(4.0, 50_000)
    assert model.time_to_x(50_000) == pytest.approx(500, abs=1)
    model.zoom_centered(2.0, 50_000)
    assert model.time_to_x(50_000) == pytest.approx(500, abs=1)
    model.zoom_centered(1.0, 1000)  # near the start: clamped, cannot centre
    assert model.scroll_ms == 0
    model.set_zoom_fraction(1.0, center_ms=70_000)
    assert model.time_to_x(70_000) == pytest.approx(500, abs=1)


def test_eta_formatting():
    assert format_eta(None) == "" and format_eta(-1) == ""
    assert format_eta(0) == "0:00" and format_eta(65) == "1:05" and format_eta(3725) == "1:02:05"
    assert transcribing_label(None) == "transcribing"
    assert transcribing_label(125) == "transcribing (ETA: 2:05)"


# Timeline -----------------------------------------------------------------------------


def test_keypoints_are_green_lines_with_tooltips_that_attract_the_handles(editor, qtbot):
    view = editor.timeline
    assert editor.model.keypoints == [1000, 2000]
    x = view.keypoint_x(1000)
    assert x is not None
    image = view.grab().toImage()
    y = view.audio_rect().center().y()
    assert image.pixelColor(int(x), y).name() == COLOR_KEYPOINT.name()
    assert image.pixelColor(int(x) + 12, y).name() != COLOR_KEYPOINT.name()
    assert view.keypoint_at(QPoint(int(x) + 2, y)) == 1000
    assert view.keypoint_at(QPoint(int(x) + 40, y)) is None
    assert view.keypoint_tooltip(2, 2000) == "Keypoint 2  (00:00:02.000)"

    # Dragging the Out handle close to the keypoint snaps onto it (magnet).
    x_out = min(int(view._x(editor.model.out_ms)), view.width() - 2)
    y_track = view.track_rects()[0][1].center().y()
    target = QPoint(int(view._x(2000)) + 5, y_track)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x_out, y_track))
    _mouse_move(view, target)
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=target)
    assert editor.model.out_ms == 2000
    # A click on the ruler near a keypoint lands exactly on it.
    click = QPoint(int(view._x(1000)) + 4, RULER_H // 2)
    qtbot.mouseClick(view, Qt.MouseButton.LeftButton, pos=click)
    assert editor.model.playhead_ms == 1000


def test_play_marker_is_painted_on_the_ruler_and_zoom_centres_the_playhead(editor, qtbot):
    view = editor.timeline
    assert view.play_marker_x() is None
    editor.model.set_playhead(1500)
    editor.toggle_play()
    editor.toggle_play()
    assert editor.model.play_marker_ms == 1500
    x = view.play_marker_x()
    assert x == pytest.approx(view._x(1500))
    editor.model.set_playhead(2200)  # away from the marker, so the red line does not cover it
    editor._sync_playhead()
    image = view.grab().toImage()
    colour = image.pixelColor(int(x), RULER_H - 4)
    assert colour.lightness() > 240  # the white arrow (antialiased edges allowed)
    assert DARK_COLORS["play_marker"].name() == "#ffffff"
    # Zooming with the buttons keeps the red line in the middle of the view. The sample
    # recording is 2.5 s long: the timeline must be narrow enough for it to overflow.
    editor.model.set_viewport_width(360)  # as if the timeline were 360 px wide
    editor.model.set_playhead(1250)
    editor._sync_playhead()
    editor.zoom_in()
    assert editor.model.max_scroll_ms() > 0
    body_centre = HEADER_W + editor.model.viewport_width / 2
    assert view._x(editor.model.playhead_ms) == pytest.approx(body_centre, abs=2)
    editor.zoom_slider.setValue(editor.zoom_slider.maximum() // 2)
    if editor.model.max_scroll_ms() > 0:
        assert view._x(editor.model.playhead_ms) == pytest.approx(body_centre, abs=2)
    editor.zoom_slider.setValue(editor.zoom_slider.maximum())
    assert view._x(editor.model.playhead_ms) == pytest.approx(body_centre, abs=2)
    editor.zoom_out()
    if editor.model.max_scroll_ms() > 0:
        assert view._x(editor.model.playhead_ms) == pytest.approx(body_centre, abs=2)


def test_transcribing_label_shows_the_eta(editor, qtbot, monkeypatch):
    view = editor.timeline
    editor.recording.transcript.complete = False
    editor.recording.transcript.transcribed_ms = 500
    session = editor.recording.session_id
    clock = {"now": 100.0}
    monkeypatch.setattr("video_story.editor.window.time.monotonic", lambda: clock["now"])
    editor._on_transcription_progress(session, 0.2, 500)
    assert editor.transcription_eta(session) is None  # the first event only sets the origin
    clock["now"] = 110.0
    editor._on_transcription_progress(session, 0.4, 1000)
    # 10 s for 20 % -> 30 s for the remaining 60 %.
    assert editor.transcription_eta(session) == pytest.approx(30.0)
    assert view.transcription == ("transcribing", 1000) and view.eta_s == pytest.approx(30.0)
    assert "(ETA: 0:30)" in editor.status_label.text()
    assert not view.grab().toImage().isNull()


# Instructions -------------------------------------------------------------------------


def test_instructions_button_dialog_file_and_process_event(
    editor, qtbot, folder: Path, monkeypatch
):
    assert editor.instructions_button.text() == INSTRUCTIONS_LABEL
    assert editor.instructions == "" and editor.instructions_button.isEnabled()
    assert "whole recording" in editor.instructions_button.toolTip()

    class FakeDialog:
        def __init__(self, text):
            self.text = text

        def exec(self):
            return QDialog.DialogCode.Accepted

        def instructions(self):
            return "Create an implementation plan.\n"

        def deleteLater(self):
            pass

    monkeypatch.setattr(editor, "_instructions_dialog", lambda text: FakeDialog(text))
    assert editor.edit_instructions()
    assert editor.instructions == "Create an implementation plan."
    assert editor.instructions_button.text() == INSTRUCTIONS_SET_LABEL
    assert "Create an implementation plan." in editor.instructions_button.toolTip()
    path = SessionPaths(folder, NEWEST).instructions_path(folder / NEWEST)
    assert read_instructions(path) == "Create an implementation plan."

    # Process hands them to the agent: editor event and the recording session.
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    paths = SessionPaths(folder, NEWEST)
    write_state(
        paths.state_path,
        SessionState(
            session_id=NEWEST,
            status=Status.EDITING,
            recording_folder=str(folder),
            pid=1,
            recorded_duration_ms=DURATION_MS,
        ),
    )
    editor.editor_session = editor_session
    editor.recording_session = NEWEST
    assert editor.process() == Crop(0, DURATION_MS)
    event = es.wait_for_editor(editor_session, timeout_s=0)
    assert event["event"] == "process"
    assert event["instructions"] == "Create an implementation plan."
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED
    assert state.instructions == "Create an implementation plan."
    assert "with instructions" in editor.statusBar().currentMessage()


def test_instructions_reload_with_the_recording_and_clear(editor, qtbot, folder: Path):
    assert editor.set_instructions("Only the second half matters")
    editor.list_view.setCurrentIndex(editor.recordings.index(1))
    _wait_loaded(qtbot, editor, OLDER)
    assert editor.instructions == "" and editor.instructions_button.text() == INSTRUCTIONS_LABEL
    editor.list_view.setCurrentIndex(editor.recordings.index(0))
    _wait_loaded(qtbot, editor, NEWEST)
    assert editor.instructions == "Only the second half matters"
    assert editor.set_instructions("")
    assert editor.instructions == ""
    assert not SessionPaths(folder, NEWEST).instructions_path(folder / NEWEST).exists()


def test_instructions_dialog_contents(qtbot):
    dialog = InstructionsDialog("  hello\n", None)
    qtbot.addWidget(dialog)
    assert dialog.text_edit.toPlainText() == "  hello\n"
    assert dialog.instructions() == "hello"
    assert "Instructions" in dialog.windowTitle()
    assert "characters" in dialog.counter.text()


# Theme --------------------------------------------------------------------------------


def test_theme_toggle_switches_colours_icons_and_is_remembered(qtbot, folder: Path):
    settings = QSettings(str(folder / "editor.ini"), QSettings.Format.IniFormat)
    window = EditorWindow(Config(recording_folder=folder), settings=settings)
    qtbot.addWidget(window)
    window.restore_settings()
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    start = window.dark
    assert window.preview.dark == start
    assert window.timeline.colors["bg"] == DARK_COLORS["bg"]  # dark canvas until a theme is chosen
    window.preview.theme_button.click()
    assert window.dark is (not start) and window.preview.dark is (not start)
    expected = LIGHT_COLORS if not window.dark else DARK_COLORS
    assert window.timeline.colors["bg"] == expected["bg"]
    palette = QApplication.instance().palette()
    assert (palette.window().color().lightness() < 128) == window.dark
    image = window.timeline.grab().toImage()
    lane = window.timeline.progress_rect()  # empty lane: shows the canvas background
    assert image.pixelColor(window.timeline.width() - 5, lane.center().y()).name() == (
        expected["bg"].name()
    )
    assert image.pixelColor(2, RULER_H + 40).name() == expected["header"].name()
    window.save_settings()
    again = EditorWindow(
        Config(recording_folder=folder),
        settings=QSettings(str(folder / "editor.ini"), QSettings.Format.IniFormat),
    )
    qtbot.addWidget(again)
    again.restore_settings()
    assert again.dark is window.dark
    again.set_theme(True)  # leave the application dark for the other tests
    window.set_theme(True)


# Preview ------------------------------------------------------------------------------


def test_transport_buttons_are_icons_and_the_theme_button_sits_above_edit(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    panel.resize(1000, 600)
    panel.show()
    qtbot.waitExposed(panel)
    buttons = (
        panel.in_button,
        panel.back_button,
        panel.play_button,
        panel.stop_button,
        panel.forward_button,
        panel.out_button,
        panel.theme_button,
    )
    for button in buttons:
        assert not button.icon().isNull()
        assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        assert button.width() <= 40  # narrow: icons instead of words
    theme = panel.theme_button.geometry()
    edit = panel.edit_subtitle_button.geometry()
    assert theme.right() > panel.out_button.geometry().right()
    assert theme.bottom() < edit.top()
    assert abs(theme.right() - edit.right()) <= 4  # right above Edit, flush with it
    assert theme.right() >= panel.width() - 12
    panel.set_playing(True)
    assert panel.play_button.text() == "Pause" and not panel.play_button.icon().isNull()
    panel.set_playing(False)
    assert panel.play_button.text() == "Play"


def test_subtitle_box_grows_to_five_lines_then_scrolls(qtbot):
    panel = PreviewPanel()
    qtbot.addWidget(panel)
    panel.resize(600, 500)
    panel.show()
    qtbot.waitExposed(panel)
    box: SubtitleBox = panel.subtitle_label
    panel.set_subtitle("")
    empty_height = box.height()
    panel.set_subtitle("one line")
    assert box.text() == "one line"
    one_line_height = box.height()
    panel.set_subtitle("first\nsecond")
    # One line of text takes the room of two: the preview above does not jump.
    assert box.height() == one_line_height == empty_height == box.min_height()
    panel.set_subtitle("\n".join(f"line {i}" for i in range(4)))
    four_height = box.height()
    assert four_height > one_line_height
    panel.set_subtitle("\n".join(f"line {i}" for i in range(12)))
    QApplication.processEvents()
    assert box.height() <= box.max_height()
    assert box.height() >= four_height
    assert box.visible_lines() <= SUBTITLE_MAX_LINES
    assert box.verticalScrollBar().maximum() > 0  # the rest scrolls
    panel.set_subtitle("short again")
    QApplication.processEvents()
    assert box.verticalScrollBar().maximum() == 0
    assert box.alignment() & Qt.AlignmentFlag.AlignHCenter


def test_play_marker_follows_the_playhead_on_mouse_release(editor, qtbot):
    view = editor.timeline
    y = RULER_H // 2
    start = QPoint(int(view._x(500)), y)
    end = QPoint(int(view._x(1600)), y)
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=start)
    _mouse_move(view, end)
    assert editor.model.play_marker_ms is None  # not while the button is held
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=end)
    marker = editor.model.play_marker_ms
    assert marker == editor.model.playhead_ms and abs(marker - 1600) < 50
    # Play from there and Stop: the red line returns to the marker.
    editor.toggle_play()
    editor.stop_playback()
    assert editor.model.playhead_ms == marker


def test_status_bar_and_tools_row_after_the_cleanup(editor):
    text = editor.status_label.text()
    assert NEWEST in text and "crop" not in text and "waiting for Process" not in text
    assert not hasattr(editor, "fit_button")
    assert editor.in_edit.text().count(".") == 1 and len(editor.in_edit.text()) == 11
    assert editor.crop_label.text().startswith("length ") and len(editor.crop_label.text()) == 18
