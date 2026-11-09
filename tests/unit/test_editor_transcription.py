"""Background transcription in the editor, the progress arrow, disabled buttons, Auto
process and read-only subtitle times (offscreen Qt; the whisper worker is a script)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from test_editor import NEWEST, OLDER, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.manifest import read_manifest  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, read_transcript, write_transcript  # noqa: E402
from video_story.editor.recordings_model import scan_recordings  # noqa: E402
from video_story.editor.subtitle_dialog import SubtitleDialog  # noqa: E402
from video_story.editor.timeline.view import COLOR_TRANSCRIBING  # noqa: E402
from video_story.editor.transcription import TranscriptionManager  # noqa: E402
from video_story.editor.window import AUTO_PROCESS_SECONDS, EditorWindow  # noqa: E402
from video_story.host import editor_session as es  # noqa: E402

FAKE_WORKER = r"""
import json, sys, time
spec_path, from_ms, session_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]
with open(spec_path, encoding="utf-8") as f:
    spec = json.load(f)
with open(spec["log"], "a", encoding="utf-8") as f:
    f.write(session_id + " " + str(from_ms) + "\n")
for event in spec["events"]:
    if "sleep" in event:
        time.sleep(event["sleep"])
        continue
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()
if spec.get("fail"):
    sys.stderr.write(json.dumps({"ok": False, "code": "X", "message": spec["fail"]}) + "\n")
    sys.exit(1)
"""

DONE = {"event": "done", "language": "cs", "model": "ggml-test", "backend": "fake"}


class ScriptedManager(TranscriptionManager):
    """Runs the fake worker above instead of ``video-story transcribe-stream``."""

    def __init__(self, config: Config, spec_path: Path) -> None:
        super().__init__(config)
        self.spec_path = spec_path

    def command(self, paths, start_ms):
        return [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            FAKE_WORKER,
            str(self.spec_path),
            str(start_ms),
            paths.session_id,
        ]


def _spec(tmp_path: Path, events: list[dict], fail: str | None = None) -> Path:
    spec = tmp_path / "worker-spec.json"
    log = tmp_path / "worker-log.txt"
    spec.write_text(
        json.dumps({"events": events, "log": str(log), "fail": fail}, ensure_ascii=False),
        encoding="utf-8",
    )
    return spec


def _starts(tmp_path: Path) -> list[tuple[str, int]]:
    """(session id, from_ms) of every worker that got as far as starting Python."""
    log = tmp_path / "worker-log.txt"
    if not log.exists():
        return []
    starts = []
    for line in log.read_text(encoding="utf-8").splitlines():
        session_id, from_ms = line.split()
        starts.append((session_id, int(from_ms)))
    return starts


def _user_key(widget, key: Qt.Key) -> None:
    """A key press as the platform delivers it (QTest's keyClick skips the application
    wide event filter the window relies on, sendEvent does not)."""
    QApplication.sendEvent(
        widget, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    )
    QApplication.sendEvent(
        widget, QKeyEvent(QEvent.Type.KeyRelease, key, Qt.KeyboardModifier.NoModifier)
    )


def _user_click(widget, pos: QPoint) -> None:
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(
            widget,
            QMouseEvent(
                kind,
                QPointF(pos),
                QPointF(widget.mapToGlobal(pos)),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )


def _open(qtbot, folder: Path, spec: Path, session_id: str = NEWEST, **kwargs) -> EditorWindow:
    cfg = Config(recording_folder=folder)
    manager = ScriptedManager(cfg, spec)
    window = EditorWindow(cfg, transcription=manager, **kwargs)
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, session_id)
    return window


def _transcript(folder: Path, session_id: str = NEWEST) -> Transcript:
    paths = SessionPaths(folder, session_id)
    return read_transcript(paths.transcript_json_path(paths.final_dir))


def _wait_complete(qtbot, window: EditorWindow) -> None:
    qtbot.waitUntil(
        lambda: window.recording is not None and window.recording.transcript_complete,
        timeout=20_000,
    )


# Background transcription ---------------------------------------------------------


def test_untranscribed_recording_is_transcribed_in_the_background(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(
        tmp_path,
        [
            {"event": "start", "start_ms": 0},
            {"event": "segment", "start_ms": 0, "end_ms": 1000, "text": "ahoj světe"},
            {"event": "progress", "fraction": 0.4, "transcribed_ms": 1000},
            {"sleep": 0.6},
            {"event": "segment", "start_ms": 1500, "end_ms": 2500, "text": "druhá věta"},
            {
                "event": "segment",
                "start_ms": 2400,
                "end_ms": 2500,
                "text": "Titulky vytvořil JohnyX",
            },
            {"event": "progress", "fraction": 1.0, "transcribed_ms": 2500},
            DONE,
        ],
    )
    window = _open(qtbot, tmp_path, spec)
    # Right after loading: the worker runs, the buttons wait, the timeline says so.
    assert window.transcription.is_running(NEWEST)
    assert not window.process_button.isEnabled() and not window.render_button.isEnabled()
    assert not window.process_action.isEnabled() and not window.render_action.isEnabled()
    assert "transcription" in window.process_button.toolTip().lower()
    assert "transcription" in window.render_button.toolTip().lower()
    assert window.timeline.transcription == ("transcribing", 0)
    assert "transcribing" in window.recordings.item(0).details
    assert "transcribing" in window.status_label.text()
    assert window.process() is None and window.render() is None  # direct calls refuse too

    # The first segment shows up as a subtitle while whisper is still working.
    qtbot.waitUntil(lambda: window.recording.subtitle_at(500) == "ahoj světe", timeout=10_000)
    assert not window.recording.transcript_complete
    partial = _transcript(tmp_path)
    assert partial.complete is False and partial.transcribed_ms == 1000
    assert partial.segments == [Segment(0, 1000, "ahoj světe")]
    qtbot.waitUntil(lambda: window.timeline.transcription == ("transcribing", 1000), timeout=5000)
    assert not window.process_button.isEnabled()
    window.model.set_playhead(500)
    window._sync_playhead()
    assert window.preview.subtitle_label.text() == "ahoj světe"

    _wait_complete(qtbot, window)
    stored = _transcript(tmp_path)
    assert stored.complete is True and stored.transcribed_ms is None
    assert stored.language == "cs" and stored.model == "ggml-test" and stored.backend == "fake"
    assert stored.segments == [
        Segment(0, 1000, "ahoj světe"),
        Segment(1500, 2500, "druhá věta"),  # the hallucinated credits line is gone
    ]
    paths = SessionPaths(tmp_path, NEWEST)
    manifest = read_manifest(paths.manifest_path(paths.final_dir))
    assert manifest.transcript is not None and manifest.transcript.language == "cs"
    assert manifest.transcript.segments == 2 and manifest.finished_at
    qtbot.waitUntil(lambda: window.process_button.isEnabled(), timeout=5000)
    assert window.render_button.isEnabled() and window.render_action.isEnabled()
    assert window.timeline.transcription is None
    assert "transcribing" not in window.recordings.item(0).details
    assert window.recordings.item(0).first_line == "ahoj světe"
    assert "Transcription complete: 2 subtitles" in window.statusBar().currentMessage()
    qtbot.waitUntil(lambda: window.transcription.running is None, timeout=5000)
    assert _starts(tmp_path) == [(NEWEST, 0)]


def test_interrupted_transcription_resumes_where_it_stopped(qtbot, tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST, with_transcript=False)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript(None, [Segment(0, 1000, "first")], complete=False, transcribed_ms=1000),
    )
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 1200, "end_ms": 2000, "text": "second"},
            {"event": "progress", "fraction": 1.0, "transcribed_ms": 2000},
            DONE,
        ],
    )
    window = _open(qtbot, tmp_path, spec)
    assert window.timeline.transcription == ("transcribing", 1000)
    _wait_complete(qtbot, window)
    assert _starts(tmp_path) == [(NEWEST, 1000)]  # resumed, not restarted
    assert _transcript(tmp_path).segments == [
        Segment(0, 1000, "first"),
        Segment(1200, 2000, "second"),
    ]


def test_closing_the_editor_kills_the_worker_and_keeps_the_partial_transcript(
    qtbot, tmp_path: Path
):
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 0, "end_ms": 900, "text": "only this"},
            {"event": "progress", "fraction": 0.3, "transcribed_ms": 900},
            {"sleep": 60},
            DONE,
        ],
    )
    window = _open(qtbot, tmp_path, spec)
    qtbot.waitUntil(lambda: window.recording.subtitle_at(100) == "only this", timeout=10_000)
    manager = window.transcription
    window.close()
    qtbot.waitUntil(lambda: manager.running is None, timeout=10_000)
    stored = _transcript(tmp_path)
    assert stored.complete is False and stored.transcribed_ms == 900
    assert stored.segments == [Segment(0, 900, "only this")]
    paths = SessionPaths(tmp_path, NEWEST)
    assert read_manifest(paths.manifest_path(paths.final_dir)).transcript is None


def test_transcription_failure_unlocks_the_buttons(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(tmp_path, [], fail="whisper model not found: ggml-large-v3-turbo-q5_0.bin")
    window = _open(qtbot, tmp_path, spec)
    qtbot.waitUntil(
        lambda: "Transcription failed" in window.statusBar().currentMessage(), timeout=10_000
    )
    assert "whisper model not found" in window.statusBar().currentMessage()
    assert window.timeline.transcription == ("failed", 0)
    assert window.process_button.isEnabled() and window.render_button.isEnabled()
    assert "transcription failed" in window.status_label.text()
    assert not window.recording.transcript_complete  # the files still say so


def test_showing_another_recording_moves_the_transcription(qtbot, tmp_path: Path):
    make_recording(tmp_path, OLDER, with_transcript=False)
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(tmp_path, [{"sleep": 60}, DONE])
    window = _open(qtbot, tmp_path, spec)
    assert window.transcription.running == NEWEST
    window.list_view.setCurrentIndex(window.recordings.index(1))
    _wait_loaded(qtbot, window, OLDER)
    assert window.transcription.running == OLDER  # queued until the killed worker ends
    assert window.timeline.transcription == ("transcribing", 0)
    # The killed worker may not even have reached Python; the new one does.
    qtbot.waitUntil(lambda: (OLDER, 0) in _starts(tmp_path), timeout=10_000)
    assert window.transcription.running == OLDER
    assert all(entry in ((NEWEST, 0), (OLDER, 0)) for entry in _starts(tmp_path))
    window.close()
    qtbot.waitUntil(lambda: window.transcription.running is None, timeout=10_000)


def test_recording_list_marks_untranscribed_recordings(tmp_path: Path):
    make_recording(tmp_path, OLDER, with_transcript=False)
    make_recording(tmp_path, NEWEST)
    items = scan_recordings(tmp_path)
    assert [i.transcript_complete for i in items] == [True, False]
    assert "transcribing" in items[1].details and "transcribing" not in items[0].details


# Timeline -------------------------------------------------------------------------


def test_timeline_paints_the_green_arrow_at_the_transcription_front(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    view = window.timeline
    assert view.transcription is None and view.transcription_arrow_x() is None
    lane = view.progress_rect()
    assert lane.top() > view.audio_rect().bottom()
    assert lane.bottom() < view.height() - view.scrollbar.height()
    view.set_transcription("transcribing", 1000)
    assert view.transcription == ("transcribing", 1000)
    x = view.transcription_arrow_x()
    assert x == pytest.approx(view._x(1000))
    image = view.grab().toImage()
    assert image.pixelColor(int(x), lane.top() + 7).name() == COLOR_TRANSCRIBING.name()
    # The audio whisper has not reached yet is shaded, the part before it is not.
    mid = view.audio_rect().center().y() + 12
    before = image.pixelColor(int(view._x(200)), mid)
    after = image.pixelColor(int(view._x(2000)), mid)
    assert after.lightness() < before.lightness()
    view.set_transcription(None)
    image = view.grab().toImage()
    assert image.pixelColor(int(x), lane.top() + 7).name() != COLOR_TRANSCRIBING.name()


# Buttons --------------------------------------------------------------------------


def test_render_button_is_blue_next_to_the_green_process_button(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    window = EditorWindow(Config(recording_folder=tmp_path))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    render, process = window.render_button, window.process_button
    assert "#1f6fd0" in render.styleSheet() and "color: white" in render.styleSheet()
    image = render.grab().toImage()
    colour = image.pixelColor(image.width() // 2, image.height() // 4)
    assert colour.blue() > colour.red() + 60 and colour.blue() > colour.green() + 40
    image = process.grab().toImage()
    colour = image.pixelColor(image.width() // 2, image.height() // 4)
    assert colour.green() > colour.red() + 60 and colour.green() > colour.blue() + 60
    assert window.auto_process_checkbox.geometry().right() < render.geometry().left()
    assert render.geometry().right() < process.geometry().left()
    assert window.auto_process_checkbox.text() == "Auto process"
    assert not window.auto_process_checkbox.isChecked()


# Auto process ---------------------------------------------------------------------


def test_auto_process_counts_down_and_presses_process(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    cfg = Config(recording_folder=tmp_path)
    editor_session = es.new_editor_session(cfg)
    window = EditorWindow(cfg, editor_session=editor_session)
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    window._auto_timer.stop()  # ticked by hand below
    assert window.countdown is None
    window.auto_process_checkbox.setChecked(True)
    assert window.countdown == AUTO_PROCESS_SECONDS == 5
    assert window.process_button.text() == "Process (5)" and window.process_button.isEnabled()
    assert "Auto process" in window.statusBar().currentMessage()
    window._auto_timer.stop()
    for left in (4, 3, 2, 1):
        window._on_auto_tick()
        assert window.countdown == left and window.process_button.text() == f"Process ({left})"
    assert es.read_editor_event(editor_session)["event"] == "open"
    window._on_auto_tick()
    assert window.countdown is None
    event = es.read_editor_event(editor_session)
    assert event["event"] == "process" and event["session_id"] == NEWEST
    assert window.process_button.text() == "Sent to agent"
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)


def test_auto_process_needs_an_agent_and_stops_on_any_click_or_key(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    cfg = Config(recording_folder=tmp_path)
    window = EditorWindow(cfg)  # nobody waits for Process
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    window.auto_process_checkbox.setChecked(True)
    assert window.countdown is None and window.process_button.text() == "Process"

    # An agent attaches: the countdown starts; a key press cancels it for good.
    session = es.new_editor_session(cfg)
    es.write_attach_request(cfg, session, None)
    window.check_attach_requests()
    assert window.editor_session == session and window.countdown == AUTO_PROCESS_SECONDS
    window._auto_timer.stop()
    _user_key(window.timeline, Qt.Key.Key_Right)
    assert window.countdown is None and window.process_button.text() == "Process"
    assert "cancelled" in window.statusBar().currentMessage()
    window._on_auto_tick()  # a late tick does nothing
    assert window.countdown is None
    assert es.read_editor_event(session)["event"] == "open"
    # Ticking the checkbox again arms it again; a mouse press cancels it too.
    window.auto_process_checkbox.setChecked(False)
    window.auto_process_checkbox.setChecked(True)
    assert window.countdown == AUTO_PROCESS_SECONDS
    window._auto_timer.stop()
    _user_click(window.timeline, window.timeline.audio_rect().center())
    assert window.countdown is None
    # Unticking while counting stops it quietly.
    window.auto_process_checkbox.setChecked(False)
    window.auto_process_checkbox.setChecked(True)
    assert window.countdown == AUTO_PROCESS_SECONDS
    window.auto_process_checkbox.setChecked(False)
    assert window.countdown is None and window.process_button.text() == "Process"


def test_auto_process_waits_for_the_transcription(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST, with_transcript=False)
    spec = _spec(
        tmp_path,
        [
            {"event": "segment", "start_ms": 0, "end_ms": 1000, "text": "ahoj"},
            {"sleep": 0.5},
            DONE,
        ],
    )
    cfg = Config(recording_folder=tmp_path)
    editor_session = es.new_editor_session(cfg)
    window = _open(qtbot, tmp_path, spec, editor_session=editor_session)
    window.auto_process_checkbox.setChecked(True)
    assert window.countdown is None  # still transcribing
    assert not window.process_button.isEnabled()
    _wait_complete(qtbot, window)
    qtbot.waitUntil(lambda: window.countdown == AUTO_PROCESS_SECONDS, timeout=5000)
    window._auto_timer.stop()
    assert window.process_button.isEnabled()
    assert window.process_button.text() == f"Process ({AUTO_PROCESS_SECONDS})"
    assert es.read_editor_event(editor_session)["event"] == "open"


def test_auto_process_countdown_holds_while_a_dialog_is_open(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    cfg = Config(recording_folder=tmp_path)
    window = EditorWindow(cfg, editor_session=es.new_editor_session(cfg))
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, NEWEST)
    window.auto_process_checkbox.setChecked(True)
    window._auto_timer.stop()
    dialog = QDialog(window)
    dialog.open()
    window._on_auto_tick()
    assert window.countdown == AUTO_PROCESS_SECONDS  # held
    dialog.close()
    window._on_auto_tick()
    assert window.countdown == AUTO_PROCESS_SECONDS - 1


def test_auto_process_setting_is_remembered(qtbot, tmp_path: Path):
    make_recording(tmp_path, NEWEST)
    cfg = Config(recording_folder=tmp_path)
    settings = QSettings(str(tmp_path / "editor.ini"), QSettings.Format.IniFormat)
    window = EditorWindow(cfg, settings=settings)
    qtbot.addWidget(window)
    window.restore_settings()
    _wait_loaded(qtbot, window, NEWEST)
    assert not window.auto_process_checkbox.isChecked()
    window.auto_process_checkbox.setChecked(True)
    window.save_settings()
    again = EditorWindow(
        cfg, settings=QSettings(str(tmp_path / "editor.ini"), QSettings.Format.IniFormat)
    )
    qtbot.addWidget(again)
    again.restore_settings()
    assert again.auto_process_checkbox.isChecked()


# Subtitle dialog ------------------------------------------------------------------


def test_subtitle_dialog_times_are_read_only(qtbot):
    dialog = SubtitleDialog(Segment(1000, 2000, "hello"), duration_ms=3000)
    qtbot.addWidget(dialog)
    assert dialog.start_edit.isReadOnly() and dialog.end_edit.isReadOnly()
    assert dialog.start_edit.text() == "00:00:01.000" and dialog.end_edit.text() == "00:00:02.000"
    assert "cannot be changed" in dialog.start_edit.toolTip()
    assert not dialog.text_edit.isReadOnly()
    dialog.show()
    qtbot.keyClicks(dialog.start_edit, "9")  # typing into a read-only field changes nothing
    assert dialog.start_edit.text() == "00:00:01.000"
    dialog.text_edit.setPlainText("hello there")
    dialog.accept()
    assert dialog.segment() == Segment(1000, 2000, "hello there")
