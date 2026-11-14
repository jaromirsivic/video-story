"""End-to-end: Stop -> Video Editor hands the session to a real editor process (Windows).

The control window runs in this process (like ``test_ui_e2e``), saves the recording
without transcribing it and launches the editor in a detached process, which starts the
background transcription. Closing that window without Process must cancel the session.
Requires a screen, a microphone, the whisper model and ``VIDEO_STORY_INTEGRATION=1``;
the window is closed through ``WM_CLOSE`` (Windows only).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("win"), reason="uses WM_CLOSE via ctypes"),
]


@pytest.fixture(scope="module")
def qapp_real():
    os.environ.pop("QT_QPA_PLATFORM", None)
    from video_story.platform import get_platform

    get_platform().prepare_process()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _wait(app, ms: int) -> None:
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _find_window(title: str, timeout_s: float, app) -> int:
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        handle = user32.FindWindowW(None, title)
        if handle:
            return handle
        _wait(app, 200)
    return 0


def test_video_editor_choice_then_close_cancels(qapp_real, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from video_story.core.config import Config, save_config
    from video_story.core.session import SessionPaths, Status, new_session_id, read_state
    from video_story.host.controller import SessionController
    from video_story.host.editor_launcher import editor_running_pid
    from video_story.host.editor_session import read_editor_event
    from video_story.platform import get_platform
    from video_story.ui import control_window as cw
    from video_story.ui.stop_dialog import StopChoice

    config = Config(recording_folder=tmp_path, capture_interval_ms=500)
    config_file = tmp_path / "config.json"
    save_config(config, config_file)
    paths = SessionPaths(tmp_path, new_session_id(tmp_path))
    controller = SessionController(config, get_platform(), paths)
    finished: list[str] = []
    window = cw.ControlWindow(controller, on_finished=finished.append, config_file=config_file)
    window.show()
    _wait(qapp_real, 300)

    window.record_button.click()
    assert controller.recording
    _wait(qapp_real, 1500)

    def fake_exec(self):
        self.choice = StopChoice.EDITOR
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(cw.StopDialog, "exec", fake_exec)
    window.record_button.click()
    deadline = time.monotonic() + 180
    while not finished and time.monotonic() < deadline:
        _wait(qapp_real, 100)
    assert finished == ["editing"], finished

    state = read_state(paths.state_path)
    assert state.status is Status.EDITING and not state.status.final
    assert state.pid > 0 and state.recording_dir == str(paths.final_dir)
    assert paths.manifest_path(paths.final_dir).exists()
    # The transcription happens in the editor, not before it opens.
    assert not paths.transcript_txt_path(paths.final_dir).exists()

    editor_sessions = sorted((tmp_path / ".work").glob("editor-*.json"))
    assert len(editor_sessions) == 1
    assert read_editor_event(editor_sessions[0])["event"] == "open"

    handle = _find_window("Video Story Editor", 30, qapp_real)
    assert handle, "the editor window did not appear"
    _wait(qapp_real, 1500)
    assert editor_running_pid(config) is not None
    adopted = read_state(paths.state_path)
    assert adopted.status is Status.EDITING  # the editor adopted it; still waiting

    ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)  # WM_CLOSE
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = read_state(paths.state_path)
        if state.status.final:
            break
        _wait(qapp_real, 200)
    assert state.status is Status.CANCELLED, state.to_dict()
    assert "without Process" in (state.message or "")
    assert state.recording_dir == str(paths.final_dir) and paths.final_dir.exists()
    assert read_editor_event(editor_sessions[0])["event"] == "closed"
    deadline = time.monotonic() + 10
    while editor_running_pid(config) is not None and time.monotonic() < deadline:
        _wait(qapp_real, 200)
    assert editor_running_pid(config) is None
    assert json.loads(paths.state_path.read_text(encoding="utf-8"))["final"] is True
