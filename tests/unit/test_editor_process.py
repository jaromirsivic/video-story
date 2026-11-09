"""Tests for the editor's Process button, crop persistence and agent sessions (offscreen Qt)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from test_editor import DURATION_MS, NEWEST, OLDER, _mouse_move, _wait_loaded, make_recording  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.crop import Crop, read_crop, write_crop  # noqa: E402
from video_story.core.session import (  # noqa: E402
    SessionPaths,
    SessionState,
    Status,
    read_state,
    write_state,
)
from video_story.editor import window as window_mod  # noqa: E402
from video_story.editor.window import EditorWindow  # noqa: E402
from video_story.host import editor_session as es  # noqa: E402


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    make_recording(tmp_path, OLDER, first_text="older one")
    make_recording(tmp_path, NEWEST)
    return tmp_path


def _crop_file(folder: Path, session_id: str = NEWEST) -> Path:
    return SessionPaths(folder, session_id).crop_path(folder / session_id)


def _read_crop_json(folder: Path, session_id: str = NEWEST) -> dict:
    return json.loads(_crop_file(folder, session_id).read_text(encoding="utf-8"))


def _editing_session(folder: Path, session_id: str = NEWEST) -> SessionPaths:
    """A recording session handed to the editor by the Stop dialog (status ``editing``)."""
    paths = SessionPaths(folder, session_id)
    write_state(
        paths.state_path,
        SessionState(
            session_id=session_id,
            status=Status.EDITING,
            recording_folder=str(folder),
            pid=1,
            recorded_duration_ms=DURATION_MS,
        ),
    )
    return paths


def _open(qtbot, folder: Path, **kwargs) -> EditorWindow:
    window = EditorWindow(Config(recording_folder=folder), **kwargs)
    qtbot.addWidget(window)
    window.show()
    _wait_loaded(qtbot, window, kwargs.get("session_id") or NEWEST)
    return window


# Process button -------------------------------------------------------------------


def test_process_button_is_green_enabled_and_next_to_render(qtbot, folder: Path):
    window = _open(qtbot, folder)
    button = window.process_button
    assert button.text() == "Process" and button.isEnabled()
    assert "agent" in button.toolTip() and "cropped range" in button.toolTip()
    assert button.minimumSize() == window.render_button.minimumSize()
    assert button.font().bold() and button.font().pointSizeF() == pytest.approx(
        window.render_button.font().pointSizeF()
    )
    assert "#1e9e3e" in button.styleSheet() and "color: white" in button.styleSheet()
    image = button.grab().toImage()
    centre = image.pixelColor(image.width() // 2, image.height() // 4)  # away from the text
    assert centre.green() > centre.red() + 60 and centre.green() > centre.blue() + 60
    assert QKeySequence("Ctrl+Return") in window.process_action.shortcuts()
    assert window.render_button.parentWidget() is button.parentWidget()
    # Order: Auto process | Process | Instructions || Render (Process before Render).
    assert window.auto_process_checkbox.geometry().right() < button.geometry().left()
    assert button.geometry().right() < window.instructions_button.geometry().left()
    assert (
        window.instructions_button.geometry().right()
        < window.render_separator.geometry().left()
        < window.render_button.geometry().left()
    )
    # Smaller than the old 180x44 buttons, all three the same size.
    assert button.minimumSize().height() < 44 and button.minimumSize().width() < 180
    assert window.instructions_button.minimumSize() == button.minimumSize()

    window.list_view.clearSelection()
    window._set_recording(None)
    assert not button.isEnabled()
    assert not window.instructions_button.isEnabled()


def test_process_without_sessions_stores_crop_shows_feedback_and_closes(qtbot, folder: Path):
    window = _open(qtbot, folder)
    sent: list[tuple[str, Crop]] = []
    window.processed.connect(lambda sid, crop: sent.append((sid, crop)))
    window.model.set_in(500)
    window.model.set_out(2000)
    window._crop_updated()
    assert not _crop_file(folder).exists()
    assert window.process() == Crop(500, 2000)
    assert _read_crop_json(folder) == {
        "start_ms": 500,
        "end_ms": 2000,
        "updated_at": _read_crop_json(folder)["updated_at"],
    }
    assert sent == [(NEWEST, Crop(500, 2000))]
    assert window.process_button.text() == "Sent to agent"
    assert not window.process_button.isEnabled()
    assert "Sent to agent" in window.statusBar().currentMessage()
    assert NEWEST in window.statusBar().currentMessage()
    # The work continues in the agent: the editor closes by itself.
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)


def test_shortcut_cannot_bypass_the_sent_to_agent_feedback(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    window = _open(qtbot, folder, editor_session=editor_session)
    sent: list[str] = []
    window.processed.connect(lambda sid, _crop: sent.append(sid))
    qtbot.keyClick(window.timeline, Qt.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert sent == [NEWEST] and window.process_button.text() == "Sent to agent"
    stamp = es.read_editor_event(editor_session)["updated_at"]
    assert not window.process_action.isEnabled()
    qtbot.keyClick(window.timeline, Qt.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert window.process() is None  # a direct call during the feedback is ignored too
    assert sent == [NEWEST]
    assert es.read_editor_event(editor_session)["updated_at"] == stamp
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)
    assert sent == [NEWEST]
    assert es.read_editor_event(editor_session)["event"] == "process"


def test_process_and_render_commit_a_typed_in_value_first(qtbot, folder: Path, monkeypatch):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    window = _open(qtbot, folder, editor_session=editor_session)
    window.in_edit.setFocus()
    window.in_edit.selectAll()
    qtbot.keyClicks(window.in_edit, "00:00:00.700")
    assert window.model.in_ms == 0  # typed, not committed
    # Ctrl+Enter inside the editor commits the value and then triggers Process.
    qtbot.keyClick(window.in_edit, Qt.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert window.model.in_ms == 700
    event = es.read_editor_event(editor_session)
    assert event["event"] == "process" and event["crop"]["start_ms"] == 700
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)

    editor_session = es.new_editor_session(cfg)
    window = _open(qtbot, folder, editor_session=editor_session)
    assert window.model.in_ms == 700  # restored from crop.json
    # Render: the request carries the committed In.
    window.in_edit.setFocus()
    window.in_edit.selectAll()
    qtbot.keyClicks(window.in_edit, "00:00:00.900")
    seen: list[tuple[int, int]] = []

    def cancelled_dialog(parent, suggested):
        seen.append((window.model.in_ms, window.model.out_ms))
        return None

    monkeypatch.setattr(window_mod, "ask_output_path", cancelled_dialog)
    assert window.render() is None
    assert seen == [(900, DURATION_MS)]
    # A value typed into Out and Process called by a direct call: committed.
    window.out_edit.setFocus()
    window.out_edit.selectAll()
    qtbot.keyClicks(window.out_edit, "00:00:02.000")
    assert window.model.out_ms == DURATION_MS
    assert window.process() == Crop(900, 2000)
    assert es.read_editor_event(editor_session)["crop"] == {"start_ms": 900, "end_ms": 2000}


def test_process_failure_keeps_the_recording_session_and_the_button(
    qtbot, folder: Path, monkeypatch
):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    paths = _editing_session(folder)
    window = _open(qtbot, folder, editor_session=editor_session, recording_session=NEWEST)
    sent: list[str] = []
    window.processed.connect(lambda sid, _crop: sent.append(sid))

    def failing_finish(paths, crop, shown_session_id=None, instructions=None):
        raise RuntimeError("state.json is locked")

    monkeypatch.setattr(window_mod, "finish_recording_session", failing_finish)
    assert window.process() is None
    assert window.recording_session == NEWEST  # not lost
    assert window.process_button.text() == "Process" and window.process_button.isEnabled()
    assert window.process_action.isEnabled()
    assert sent == []
    assert "Process failed" in window.statusBar().currentMessage()
    assert "state.json is locked" in window.statusBar().currentMessage()
    assert read_state(paths.state_path).status is Status.EDITING
    assert window.editor_session == editor_session  # the editor event itself went out
    # The retry works once the session can be written again.
    monkeypatch.setattr(window_mod, "finish_recording_session", es.finish_recording_session)
    assert window.process() is not None
    assert window.recording_session is None
    assert read_state(paths.state_path).status is Status.PROCESSED
    assert sent == [NEWEST]


def test_process_failure_then_close_cancels_the_recording_session(qtbot, folder: Path, monkeypatch):
    paths = _editing_session(folder)
    window = _open(qtbot, folder, recording_session=NEWEST)
    monkeypatch.setattr(
        window_mod,
        "finish_recording_session",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert window.process() is None and window.recording_session == NEWEST
    window.close()
    assert read_state(paths.state_path).status is Status.CANCELLED


# Crop persistence (chapter 7) ------------------------------------------------------


def test_crop_json_written_on_io_keys_editors_and_reset(qtbot, folder: Path):
    window = _open(qtbot, folder)
    assert not _crop_file(folder).exists()  # loading never writes
    window.model.set_playhead(700)
    qtbot.keyClick(window.timeline, Qt.Key_I)
    assert _read_crop_json(folder)["start_ms"] == 700
    assert _read_crop_json(folder)["end_ms"] == DURATION_MS
    window.model.set_playhead(2200)
    qtbot.keyClick(window.timeline, Qt.Key_O)
    data = _read_crop_json(folder)
    assert (data["start_ms"], data["end_ms"]) == (700, 2200) and "updated_at" in data
    window.in_edit.selectAll()
    qtbot.keyClicks(window.in_edit, "00:00:01.000")
    qtbot.keyClick(window.in_edit, Qt.Key_Return)
    assert _read_crop_json(folder)["start_ms"] == 1000
    window.reset_crop_button.click()
    data = _read_crop_json(folder)
    assert (data["start_ms"], data["end_ms"]) == (0, DURATION_MS)
    # Reset Crop on an unchanged crop still stores the whole range.
    _crop_file(folder).unlink()
    window.reset_crop_button.click()
    assert read_crop(_crop_file(folder), DURATION_MS) == Crop(0, DURATION_MS)


def test_crop_handle_drag_writes_once_after_release(qtbot, folder: Path):
    window = _open(qtbot, folder)
    view, model = window.timeline, window.model
    model.set_in(1000)
    window._crop_edited()
    before = _crop_file(folder).stat().st_mtime_ns
    y = view.audio_rect().center().y()
    x_in = int(view._x(1000))
    qtbot.mousePress(view, Qt.MouseButton.LeftButton, pos=QPoint(x_in, y))
    assert view._drag == "in"
    for dx in (3, 6, 9):
        _mouse_move(view, QPoint(x_in - dx, y))
    moved_to = model.in_ms
    assert moved_to < 1000
    assert _crop_file(folder).stat().st_mtime_ns == before  # not per mouse move
    qtbot.mouseRelease(view, Qt.MouseButton.LeftButton, pos=QPoint(x_in - 9, y))
    qtbot.waitUntil(lambda: _read_crop_json(folder)["start_ms"] == moved_to, timeout=5000)
    assert not window._crop_save_timer.isActive()


def test_crop_restored_on_reload_and_clamped(qtbot, folder: Path):
    write_crop(_crop_file(folder), Crop(600, 2100))
    write_crop(_crop_file(folder, OLDER), Crop(-5, 99_999))
    window = _open(qtbot, folder)
    assert (window.model.in_ms, window.model.out_ms) == (600, 2100)
    assert window.in_edit.text() == "00:00:00.600" and window.out_edit.text() == "00:00:02.100"
    assert "length 00:00:01.500" in window.crop_label.text()
    stamp = _read_crop_json(folder)["updated_at"]
    # Switching recordings restores the other crop (clamped) without writing anything.
    window.list_view.setCurrentIndex(window.recordings.index(1))
    _wait_loaded(qtbot, window, OLDER)
    assert (window.model.in_ms, window.model.out_ms) == (0, DURATION_MS)
    assert _read_crop_json(folder)["updated_at"] == stamp
    window.model.set_playhead(900)
    qtbot.keyClick(window.timeline, Qt.Key_I)
    assert _read_crop_json(folder, OLDER)["start_ms"] == 900
    window.list_view.setCurrentIndex(window.recordings.index(0))
    _wait_loaded(qtbot, window, NEWEST)
    assert (window.model.in_ms, window.model.out_ms) == (600, 2100)
    window.close()
    # A fresh window sees the stored crops.
    again = _open(qtbot, folder, session_id=OLDER)
    assert (again.model.in_ms, again.model.out_ms) == (900, DURATION_MS)


# Process with agent sessions (chapter 6) ------------------------------------------------


def test_process_writes_editor_event_and_finishes_recording_session(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    paths = _editing_session(folder)
    window = _open(qtbot, folder, editor_session=editor_session, recording_session=paths.session_id)
    adopted = read_state(paths.state_path)
    assert adopted.status is Status.EDITING and adopted.pid == os.getpid()
    assert f"waiting for Process (recording {NEWEST})" in window.status_label.text()
    assert es.read_editor_event(editor_session)["event"] == "open"

    window.model.set_in(500)
    window.model.set_out(2000)
    window._crop_updated()
    qtbot.keyClick(window.timeline, Qt.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert window.process_button.text() == "Sent to agent"
    event = es.read_editor_event(editor_session)
    assert event["event"] == "process" and event["session_id"] == NEWEST
    assert event["crop"] == {"start_ms": 500, "end_ms": 2000}
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED and state.status.final
    assert state.crop == {"start_ms": 500, "end_ms": 2000}
    assert state.processed_via == "editor"
    assert state.recording_dir == str(paths.final_dir)
    assert _read_crop_json(folder)["start_ms"] == 500
    assert window.recording_session is None and window.editor_event_sent
    assert "sent to agent" in window.status_label.text()

    # The window closes by itself; closing neither rewrites the event nor touches the
    # finished session.
    qtbot.waitUntil(lambda: not window.isVisible(), timeout=5000)
    assert es.read_editor_event(editor_session)["event"] == "process"
    assert read_state(paths.state_path).status is Status.PROCESSED


def test_process_on_another_recording_still_finishes_the_recording_session(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    paths = _editing_session(folder, NEWEST)
    window = _open(qtbot, folder, editor_session=editor_session, recording_session=NEWEST)
    window.list_view.setCurrentIndex(window.recordings.index(1))
    _wait_loaded(qtbot, window, OLDER)
    window.model.set_in(1000)
    window._crop_updated()
    window.process_button.click()
    event = es.read_editor_event(editor_session)
    assert event["session_id"] == OLDER and event["crop"]["start_ms"] == 1000
    # The recording session keeps its own range (no crop.json of its own = whole
    # recording); the crop made on the other recording went into the editor event only.
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED and state.crop["start_ms"] == 0
    assert state.crop["end_ms"] == state.recorded_duration_ms
    assert OLDER in (state.message or "")
    assert state.recording_dir == str(paths.final_dir)


def test_close_without_process_reports_closed_and_cancels(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    editor_session = es.new_editor_session(cfg)
    paths = _editing_session(folder)
    window = _open(qtbot, folder, editor_session=editor_session, recording_session=paths.session_id)
    window.close()
    assert es.read_editor_event(editor_session)["event"] == "closed"
    state = read_state(paths.state_path)
    assert state.status is Status.CANCELLED and state.status.final
    assert "Process" in (state.message or "")
    assert state.recording_dir == str(paths.final_dir)
    assert state.processed_via == "editor"


def test_close_flushes_a_pending_crop_drag(qtbot, folder: Path):
    window = _open(qtbot, folder)
    window.model.set_in(800)
    window._on_crop_changed()  # as the timeline does while a handle is dragged
    assert window._crop_save_timer.isActive() and not _crop_file(folder).exists()
    window.close()
    assert _read_crop_json(folder)["start_ms"] == 800


# Attach requests (spec 5.3) ---------------------------------------------------------


def test_attach_request_selects_recording_adopts_sessions_and_raises(
    qtbot, folder: Path, monkeypatch
):
    cfg = Config(recording_folder=folder)
    first = es.new_editor_session(cfg)
    window = _open(qtbot, folder, editor_session=first)
    assert window._attach_timer.isActive() and window._attach_timer.interval() == 1000
    window._attach_timer.stop()  # the slot is triggered by hand below
    raised: list[str] = []
    monkeypatch.setattr(window, "raise_", lambda: raised.append("raise_"))
    monkeypatch.setattr(window, "activateWindow", lambda: raised.append("activateWindow"))
    window.showMaximized()
    assert window.isMaximized()

    # A recording that appeared after the window started, handed over by the Stop dialog.
    third = "20260103-100000"
    make_recording(folder, third, first_text="newest of all")
    paths = _editing_session(folder, third)
    second = es.new_editor_session(cfg)
    es.write_attach_request(cfg, second, third, recording_session=third)

    handled = window.check_attach_requests()
    assert [(r.editor_session, r.session_id) for r in handled] == [(second, third)]
    _wait_loaded(qtbot, window, third)
    assert window.list_view.currentIndex().row() == 0
    assert window.editor_session == second and not window.editor_event_sent
    assert window.recording_session == third
    assert read_state(paths.state_path).pid == os.getpid()
    assert raised == ["raise_", "activateWindow"]
    assert window.isMaximized() and not window.isMinimized()  # not dropped to normal
    assert f"waiting for Process (recording {third})" in window.status_label.text()
    # The previous editor session was told the editor moved on; its folder is empty.
    assert es.read_editor_event(first)["event"] == "closed"
    assert not list(es.requests_dir(cfg).glob("*.json"))

    # Process now serves the attached sessions.
    window.process_button.click()
    assert es.read_editor_event(second)["session_id"] == third
    assert read_state(paths.state_path).status is Status.PROCESSED

    # A request without a session id keeps the shown recording.
    fourth = es.new_editor_session(cfg)
    es.write_attach_request(cfg, fourth, None)
    window.check_attach_requests()
    assert window.recording.session_id == third and window.editor_session == fourth
    assert es.read_editor_event(second)["event"] == "process"  # a sent session stays sent
    window.close()
    assert es.read_editor_event(fourth)["event"] == "closed"


def test_attach_timer_serves_requests_on_its_own(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    window = _open(qtbot, folder)
    session = es.new_editor_session(cfg)
    es.write_attach_request(cfg, session, OLDER)
    qtbot.waitUntil(lambda: window.editor_session == session, timeout=5000)
    _wait_loaded(qtbot, window, OLDER)
    assert "waiting for Process" in window.status_label.text()


def test_attach_un_minimizes_without_touching_a_normal_window(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    window = _open(qtbot, folder)
    window._attach_timer.stop()
    window.showMinimized()
    assert window.isMinimized()
    es.write_attach_request(cfg, es.new_editor_session(cfg), None)
    window.check_attach_requests()
    assert not window.isMinimized() and not window.isMaximized()


def test_requests_left_by_a_previous_editor_are_declined_at_start(qtbot, folder: Path):
    """A request for an editor that was closing must not hijack the next editor."""
    cfg = Config(recording_folder=folder)
    stale = es.new_editor_session(cfg)
    stale_paths = _editing_session(folder, OLDER)
    es.write_attach_request(cfg, stale, OLDER, recording_session=OLDER)
    own = es.new_editor_session(cfg)
    window = _open(qtbot, folder, editor_session=own)
    assert not list(es.requests_dir(cfg).glob("*.json"))
    assert window.editor_session == own and window.recording_session is None
    assert es.read_editor_event(own)["event"] == "open"  # the live agent keeps waiting
    assert es.read_editor_event(stale)["event"] == "closed"
    assert read_state(stale_paths.state_path).status is Status.CANCELLED
    assert window.recording.session_id == NEWEST  # not switched to the stale request
    qtbot.wait(50)
    assert window.recording.session_id == NEWEST and window.editor_session == own


def test_request_arriving_during_shutdown_is_answered_closed(qtbot, folder: Path):
    cfg = Config(recording_folder=folder)
    window = _open(qtbot, folder)
    window._attach_timer.stop()  # the request lands after the last tick
    late = es.new_editor_session(cfg)
    late_paths = _editing_session(folder, OLDER)
    es.write_attach_request(cfg, late, OLDER, recording_session=OLDER)
    window.close()
    assert not list(es.requests_dir(cfg).glob("*.json"))
    assert es.read_editor_event(late)["event"] == "closed"
    assert read_state(late_paths.state_path).status is Status.CANCELLED


def test_attach_requests_wait_while_a_modal_dialog_or_a_load_is_running(
    qtbot, folder: Path, monkeypatch
):
    cfg = Config(recording_folder=folder)
    window = _open(qtbot, folder)
    window._attach_timer.stop()
    session = es.new_editor_session(cfg)
    request = es.write_attach_request(cfg, session, OLDER)
    dialog = QDialog(window)
    dialog.open()  # window-modal, as the render progress dialog is
    assert QApplication.activeModalWidget() is dialog
    assert window.check_attach_requests() == []
    assert request.exists() and window.editor_session is None
    dialog.close()
    assert QApplication.activeModalWidget() is None
    monkeypatch.setattr(EditorWindow, "loading", property(lambda self: True))
    assert window.check_attach_requests() == []
    assert request.exists() and window.editor_session is None
    monkeypatch.undo()
    handled = window.check_attach_requests()
    assert [r.path for r in handled] == [request] and not request.exists()
    assert window.editor_session == session
    _wait_loaded(qtbot, window, OLDER)
