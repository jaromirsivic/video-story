"""Qt tests for the control window state machine (offscreen, no real recording)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths, Status, read_state  # noqa: E402
from video_story.core.timeline import RecordingClock  # noqa: E402
from video_story.host.controller import SessionController  # noqa: E402
from video_story.platform.base import Platform, SupportStatus  # noqa: E402
from video_story.ui import control_window as cw  # noqa: E402
from video_story.ui.settings_dialog import SettingsDialog  # noqa: E402
from video_story.ui.stop_dialog import StopChoice  # noqa: E402


class FakePlatform(Platform):
    name = "fake"

    def prepare_process(self) -> None:
        pass

    def support_status(self) -> SupportStatus:
        return SupportStatus(True)

    def list_screens(self):
        return []

    def cursor_position(self):
        return None


class FakeRecorder:
    """Stands in for capture.Recorder: no threads, no devices."""

    instances: list[FakeRecorder] = []

    def __init__(self, config, platform, paths, on_event=None, record_audio=True):
        self.config = config
        self.paths = paths
        self.clock = RecordingClock()
        self.screens = []
        self.started_at = "2026-01-01T00:00:00.000+00:00"
        self.auto_stopped = False
        self.running = False
        self.frame_count = 0
        self.stopped = False
        FakeRecorder.instances.append(self)

    @property
    def elapsed_ms(self) -> int:
        return self.clock.elapsed_ms()

    def start(self) -> None:
        self.paths.create_work_dirs()
        (self.paths.frames_dir(self.paths.work_dir) / "0_000_screen1.png").write_bytes(b"x")
        self.clock.start()
        self.running = True

    def stop(self):
        from video_story.capture.recorder import RecordingSummary

        self.running = False
        self.stopped = True
        return RecordingSummary(
            session_id=self.paths.session_id,
            started_at=self.started_at,
            finished_at="2026-01-01T00:00:01.000+00:00",
            recorded_duration_ms=self.clock.stop(),
            screens=[],
            frame_count=1,
            dropped_ticks=0,
            audio=None,
            auto_stopped=self.auto_stopped,
        )


@pytest.fixture
def controller(tmp_path: Path, monkeypatch):
    import video_story.host.controller as ctl

    monkeypatch.setattr(ctl, "Recorder", FakeRecorder)
    FakeRecorder.instances.clear()
    paths = SessionPaths(tmp_path, "20260101-120000")
    return SessionController(Config(recording_folder=tmp_path), FakePlatform(), paths)


@pytest.fixture
def window(qtbot, controller):
    finished: list[str] = []
    w = cw.ControlWindow(controller, on_finished=finished.append)
    w.finished_statuses = finished  # type: ignore[attr-defined]
    w.show()
    yield w
    # Close ourselves (not via qtbot.addWidget, whose finalizer would run first and
    # open the Stop dialog while a fake recording is still active).
    w._done = True
    w.close()


def _auto_answer_stop(monkeypatch, choice: StopChoice, confirm: bool | None = None):
    """Make the Stop dialog return ``choice`` and the discard confirmation ``confirm``."""

    def fake_exec(self):
        self.choice = choice
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(cw.StopDialog, "exec", fake_exec)
    if confirm is not None:
        monkeypatch.setattr(cw, "confirm_discard", lambda parent=None: confirm)


def test_initial_state(window, controller):
    assert window.record_button.text() == "Record"
    assert window.settings_button.isEnabled()
    assert not window.draw_button.isEnabled()
    assert read_state(controller.paths.state_path).status is Status.READY


def test_record_then_continue_keeps_recording(window, controller, monkeypatch, qtbot):
    window.record_button.click()
    assert controller.recording
    assert window.record_button.text() == "Stop"
    assert not window.settings_button.isEnabled()
    assert window.draw_button.isEnabled()
    assert read_state(controller.paths.state_path).status is Status.RECORDING

    _auto_answer_stop(monkeypatch, StopChoice.CONTINUE)
    window.record_button.click()
    assert controller.recording and not FakeRecorder.instances[-1].stopped
    assert window.record_button.text() == "Stop"


def test_discard_not_confirmed_keeps_files(window, controller, monkeypatch):
    window.record_button.click()
    answers = iter([(StopChoice.DISCARD, False), (StopChoice.CONTINUE, None)])

    def fake_exec(self):
        choice, confirm = next(answers)
        self.choice = choice
        if confirm is not None:
            monkeypatch.setattr(cw, "confirm_discard", lambda parent=None: confirm)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(cw.StopDialog, "exec", fake_exec)
    window.record_button.click()
    assert controller.recording
    assert (controller.paths.frames_dir(controller.paths.work_dir) / "0_000_screen1.png").exists()


def test_discard_confirmed_deletes_and_resets(window, controller, monkeypatch):
    window.record_button.click()
    frames = controller.paths.frames_dir(controller.paths.work_dir)
    assert frames.exists()
    _auto_answer_stop(monkeypatch, StopChoice.DISCARD, confirm=True)
    window.record_button.click()
    assert not controller.recording
    assert not frames.exists()
    assert controller.paths.state_path.exists()
    assert read_state(controller.paths.state_path).status is Status.READY
    assert window.record_button.text() == "Record"
    assert window.settings_button.isEnabled() and not window.draw_button.isEnabled()
    # A second recording can start in the same session.
    window.record_button.click()
    assert controller.recording and len(FakeRecorder.instances) == 2


def test_process_runs_transcription_and_finishes(window, controller, monkeypatch, qtbot):
    import video_story.transcribe.pipeline as pipeline_mod
    from video_story.core.manifest import TranscriptInfo
    from video_story.core.transcript import Segment, Transcript, write_transcript

    class Result:
        def __init__(self):
            self.transcript = Transcript("en", [Segment(0, 500, "hello")])
            self.info = TranscriptInfo(language="en", segments=1)

    def fake_transcribe(paths, config, *, base_dir=None, progress=None):
        base = base_dir or paths.work_dir
        if progress:
            progress(0.5, "Transcribing audio")
        write_transcript(
            paths.transcript_txt_path(base), paths.transcript_json_path(base), Result().transcript
        )
        return Result()

    monkeypatch.setattr(pipeline_mod, "transcribe_recording", fake_transcribe, raising=False)
    window.record_button.click()
    _auto_answer_stop(monkeypatch, StopChoice.PROCESS)
    window.record_button.click()
    qtbot.waitUntil(lambda: window.finished_statuses == ["processed"], timeout=10000)
    state = read_state(controller.paths.state_path)
    assert state.status is Status.PROCESSED and state.status.final
    final = controller.paths.final_dir
    assert final.exists() and (final / "manifest.json").exists()
    assert (
        (final / "transcript.txt")
        .read_text(encoding="utf-8")
        .startswith("[00:00:00.000 --> 00:00:00.500] hello")
    )
    assert state.recording_dir == str(final)
    assert state.crop == {"start_ms": 0, "end_ms": state.recorded_duration_ms}
    assert state.processed_via == "stop_dialog"


def _fake_transcription(monkeypatch):
    import video_story.transcribe.pipeline as pipeline_mod
    from video_story.core.manifest import TranscriptInfo
    from video_story.core.transcript import Segment, Transcript, write_transcript

    class Result:
        def __init__(self):
            self.transcript = Transcript("en", [Segment(0, 500, "hello")])
            self.info = TranscriptInfo(language="en", segments=1)

    def fake_transcribe(paths, config, *, base_dir=None, progress=None):
        base = base_dir or paths.work_dir
        write_transcript(
            paths.transcript_txt_path(base), paths.transcript_json_path(base), Result().transcript
        )
        return Result()

    monkeypatch.setattr(pipeline_mod, "transcribe_recording", fake_transcribe, raising=False)


def test_video_editor_choice_processes_and_hands_over(window, controller, monkeypatch, qtbot):
    """Stop -> Video Editor: transcribe, then open the editor; the session stays non-final."""
    _fake_transcription(monkeypatch)
    launched: list[dict] = []

    def fake_launch(config, session_id=None, config_file=None, recording_session=None):
        launched.append({"session_id": session_id, "recording_session": recording_session})
        return {"pid": 4321, "session_id": session_id, "editor_session": "x", "attached": False}

    monkeypatch.setattr(cw, "launch_editor", fake_launch)
    window.record_button.click()
    _auto_answer_stop(monkeypatch, StopChoice.EDITOR)
    window.record_button.click()
    qtbot.waitUntil(lambda: window.finished_statuses == ["editing"], timeout=10000)
    assert launched == [
        {
            "session_id": controller.paths.session_id,
            "recording_session": controller.paths.session_id,
        }
    ]
    state = read_state(controller.paths.state_path)
    assert state.status is Status.EDITING and not state.status.final
    assert state.pid == 4321 and state.crop is None
    assert state.recording_dir == str(controller.paths.final_dir)
    assert (controller.paths.final_dir / "transcript.txt").exists()


def test_video_editor_choice_falls_back_to_processed(window, controller, monkeypatch, qtbot):
    """When the editor cannot start, the session is processed with the whole range."""
    _fake_transcription(monkeypatch)

    def failing_launch(config, session_id=None, config_file=None, recording_session=None):
        raise RuntimeError("no editor today")

    monkeypatch.setattr(cw, "launch_editor", failing_launch)
    monkeypatch.setattr(cw.QMessageBox, "warning", lambda *a, **k: None)
    window.record_button.click()
    _auto_answer_stop(monkeypatch, StopChoice.EDITOR)
    window.record_button.click()
    qtbot.waitUntil(lambda: window.finished_statuses == ["processed"], timeout=10000)
    state = read_state(controller.paths.state_path)
    assert state.status is Status.PROCESSED and state.processed_via == "stop_dialog"
    assert state.crop == {"start_ms": 0, "end_ms": state.recorded_duration_ms}
    assert "no editor today" in (state.message or "")


def test_auto_stop_triggers_processing(window, controller, monkeypatch, qtbot):
    import video_story.transcribe.pipeline as pipeline_mod
    from video_story.core.manifest import TranscriptInfo
    from video_story.core.transcript import Transcript, write_transcript

    class Result:
        transcript = Transcript("en", [])
        info = TranscriptInfo(language="en", segments=0)

    def fake_transcribe(paths, config, *, base_dir=None, progress=None):
        base = base_dir or paths.work_dir
        write_transcript(
            paths.transcript_txt_path(base), paths.transcript_json_path(base), Result.transcript
        )
        return Result()

    monkeypatch.setattr(pipeline_mod, "transcribe_recording", fake_transcribe, raising=False)
    window.record_button.click()
    FakeRecorder.instances[-1].auto_stopped = True
    window._tick()
    qtbot.waitUntil(lambda: window.finished_statuses == ["processed"], timeout=10000)
    assert read_state(controller.paths.state_path).status is Status.PROCESSED


def test_close_in_ready_state_cancels(window, controller):
    window.close()
    assert window.finished_statuses == ["cancelled"]
    assert read_state(controller.paths.state_path).status is Status.CANCELLED


def test_close_while_recording_opens_stop_dialog(window, controller, monkeypatch):
    window.record_button.click()
    _auto_answer_stop(monkeypatch, StopChoice.CONTINUE)
    window.close()
    assert controller.recording and window.isVisible()


def test_settings_dialog_order_and_values(qtbot, tmp_path: Path, monkeypatch):
    import video_story.core.config as cfg_mod

    monkeypatch.setenv(cfg_mod.CONFIG_PATH_ENV, str(tmp_path / "config.json"))
    dialog = SettingsDialog(Config(recording_folder=tmp_path))
    qtbot.addWidget(dialog)
    form = dialog._form
    labels = [form.itemAt(i, form.ItemRole.LabelRole).widget().text() for i in range(5)]
    assert labels == [
        "Recording Folder",
        "Capture Interval",
        "Image Format",
        "JPEG Quality",
        "Audio Input",
    ]
    assert form.itemAt(5, form.ItemRole.LabelRole).widget().text() == "Auto Stop Recording"
    assert form.itemAt(6, form.ItemRole.LabelRole).widget().text() == "Keep Recordings"
    assert form.itemAt(7, form.ItemRole.LabelRole).widget().text() == "Recordings"
    assert dialog.interval_spin.value() == 500 and dialog.format_combo.currentData() == "jpeg"
    assert dialog.quality_spin.value() == 70
    assert dialog.auto_stop_spin.value() == 120 and dialog.retention_spin.value() == 30
    assert dialog.interval_spin.minimum() == 1 and dialog.interval_spin.maximum() == 1000
    assert form.isRowVisible(3)  # JPEG is the default, so the quality row shows
    dialog.format_combo.setCurrentIndex(0)
    assert not form.isRowVisible(3)
    dialog.format_combo.setCurrentIndex(1)
    assert form.isRowVisible(3)
    dialog.interval_spin.setValue(1000)
    dialog.quality_spin.setValue(20)
    dialog.auto_stop_spin.setValue(45)
    dialog.retention_spin.setValue(7)
    dialog._save()
    assert dialog.result_config is not None
    assert dialog.result_config.capture_interval_ms == 1000
    assert dialog.result_config.image_format == "jpeg" and dialog.result_config.jpeg_quality == 20
    assert dialog.result_config.auto_stop_minutes == 45
    assert dialog.result_config.retention_days == 7
    reloaded = cfg_mod.load_config()
    assert reloaded == dialog.result_config


def test_settings_delete_all_asks_and_keeps_current_session(qtbot, tmp_path: Path, monkeypatch):
    from video_story.core.session import SessionPaths, SessionState, Status, write_state
    from video_story.ui import settings_dialog as sd

    old = SessionPaths(tmp_path, "20261001-090000")
    old.frames_dir(old.final_dir).mkdir(parents=True)
    (old.frames_dir(old.final_dir) / "0_000_screen1.png").write_bytes(b"x")
    current = SessionPaths(tmp_path, "20261107-120000")
    current.create_work_dirs()
    write_state(
        current.state_path,
        SessionState(session_id=current.session_id, status=Status.READY, recording_folder="x"),
    )
    dialog = SettingsDialog(
        Config(recording_folder=tmp_path), current_session_id=current.session_id
    )
    qtbot.addWidget(dialog)
    questions: list[str] = []

    def answer_no(*args, **kwargs):
        questions.append(args[2])
        return sd.QMessageBox.StandardButton.No

    monkeypatch.setattr(sd.QMessageBox, "question", answer_no)
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    dialog.delete_all_button.click()
    assert len(questions) == 1 and "1 recording(s)" in questions[0]
    assert old.final_dir.exists()

    monkeypatch.setattr(
        sd.QMessageBox, "question", lambda *a, **k: sd.QMessageBox.StandardButton.Yes
    )
    dialog.delete_all_button.click()
    assert not old.final_dir.exists()
    assert current.state_path.exists()


def test_app_instance_available():
    assert QApplication.instance() is not None
    QTimer.singleShot(0, lambda: None)
    assert QMessageBox is not None
