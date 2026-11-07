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
    assert dialog.interval_spin.value() == 250 and dialog.format_combo.currentData() == "png"
    assert dialog.interval_spin.minimum() == 1 and dialog.interval_spin.maximum() == 1000
    assert not form.isRowVisible(3)
    dialog.format_combo.setCurrentIndex(1)
    assert form.isRowVisible(3)
    dialog.interval_spin.setValue(1000)
    dialog.quality_spin.setValue(20)
    dialog.auto_stop_spin.setValue(45)
    dialog._save()
    assert dialog.result_config is not None
    assert dialog.result_config.capture_interval_ms == 1000
    assert dialog.result_config.image_format == "jpeg" and dialog.result_config.jpeg_quality == 20
    assert dialog.result_config.auto_stop_minutes == 45
    reloaded = cfg_mod.load_config()
    assert reloaded == dialog.result_config


def test_app_instance_available():
    assert QApplication.instance() is not None
    QTimer.singleShot(0, lambda: None)
    assert QMessageBox is not None
