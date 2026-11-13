"""Qt tests for the control window state machine (offscreen, no real recording)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

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
        self.keypoints: list[int] = []
        self.clipboard_provider = None
        FakeRecorder.instances.append(self)

    @property
    def elapsed_ms(self) -> int:
        return self.clock.elapsed_ms()

    def add_keypoint(self):
        if not self.running:
            return None
        now = self.clock.elapsed_ms()
        if self.keypoints and now - self.keypoints[-1] < 3000:
            return None
        self.keypoints.append(now)
        return now

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
    assert window.record_button.text() == "Stop (Ctrl+Alt+S)"
    assert not window.settings_button.isEnabled()
    assert window.draw_button.isEnabled()
    assert read_state(controller.paths.state_path).status is Status.RECORDING

    _auto_answer_stop(monkeypatch, StopChoice.CONTINUE)
    window.record_button.click()
    assert controller.recording and not FakeRecorder.instances[-1].stopped
    assert window.record_button.text() == "Stop (Ctrl+Alt+S)"


def test_status_line_order_while_recording(window, controller):
    window.record_button.click()
    text = window.status_label.text()
    assert text.startswith("Recording ")
    assert text.index("auto stop in") < text.index("screen(s)") < text.index("frames")


def test_draw_icon_uses_button_text_color(qtbot):
    from PySide6.QtGui import QColor

    from video_story.ui.icons import draw_icon

    image = draw_icon(24, QColor("#FFFFFF")).pixmap(24, 24).toImage()
    opaque = [
        image.pixelColor(x, y)
        for x in range(24)
        for y in range(24)
        if image.pixelColor(x, y).alpha() == 255
    ]
    assert opaque and all(c.red() == c.green() == c.blue() == 255 for c in opaque)
    # Bristle tip bottom-left, handle top-right.
    assert image.pixelColor(5, 19).alpha() > 0 and image.pixelColor(19, 5).alpha() > 0
    assert image.pixelColor(19, 19).alpha() == 0 and image.pixelColor(4, 4).alpha() == 0


def test_stop_dialog_buttons_have_icons(qtbot):
    from video_story.ui.stop_dialog import StopDialog

    dialog = StopDialog()
    qtbot.addWidget(dialog)
    buttons = (
        dialog.editor_button,
        dialog.continue_button,
        dialog.discard_button,
    )
    keys = set()
    for button in buttons:
        icon = button.icon()
        assert not icon.isNull(), button.text()
        image = icon.pixmap(24, 24).toImage()
        assert any(image.pixelColor(x, y).alpha() > 0 for x in range(24) for y in range(24)), (
            button.text()
        )
        keys.add(icon.cacheKey())
    assert len(keys) == len(buttons)  # every choice has its own icon


def test_stop_dialog_video_editor_is_first_and_default(qtbot):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    from video_story.ui.stop_dialog import StopChoice, StopDialog

    dialog = StopDialog()
    qtbot.addWidget(dialog)
    layout = dialog.layout()
    widgets = [layout.itemAt(i).widget() for i in range(layout.count())]
    buttons = [w for w in widgets if isinstance(w, QPushButton)]
    assert buttons[0] is dialog.editor_button and dialog.editor_button.isDefault()
    # No direct processing: after Stop the user always goes through the editor.
    assert [b.text() for b in buttons] == [
        "Video Editor",
        "Continue Recording",
        "Discard Recording",
    ]
    assert not hasattr(dialog, "process_button")
    dialog.show()
    qtbot.keyClick(dialog, Qt.Key.Key_Return)
    assert dialog.choice is StopChoice.EDITOR


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


def _fake_editor_launch(monkeypatch) -> list[dict]:
    """Replace the editor launch (no real process); returns the recorded calls."""
    launched: list[dict] = []

    def fake_launch(config, session_id=None, config_file=None, recording_session=None):
        launched.append({"session_id": session_id, "recording_session": recording_session})
        return {"pid": 4321, "session_id": session_id, "editor_session": "x", "attached": False}

    monkeypatch.setattr(cw, "launch_editor", fake_launch)
    return launched


def _no_transcription(monkeypatch):
    """The control window never transcribes: the editor does."""
    import video_story.transcribe.pipeline as pipeline_mod

    def unexpected(*args, **kwargs):
        raise AssertionError("the control window transcribed instead of the editor")

    monkeypatch.setattr(pipeline_mod, "transcribe_recording", unexpected, raising=False)


def test_video_editor_choice_saves_and_hands_over(window, controller, monkeypatch, qtbot):
    """Stop -> Video Editor: save the recording and open the editor at once (it transcribes
    in the background); the session stays non-final and nothing is transcribed here."""
    _no_transcription(monkeypatch)
    launched = _fake_editor_launch(monkeypatch)
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
    assert "transcribing" in (state.message or "")
    final = controller.paths.final_dir
    assert (final / "manifest.json").exists()
    assert not (final / "transcript.txt").exists() and not (final / "transcript.json").exists()
    from video_story.core.manifest import read_manifest

    manifest = read_manifest(final / "manifest.json")
    assert manifest.transcript is None and not manifest.in_progress
    assert "Editor" in window.status_label.text()


def test_editor_that_cannot_open_hands_nothing_to_the_agent(window, controller, monkeypatch, qtbot):
    """Nobody pressed Process: the session ends ``cancelled`` and the recording is kept."""
    _no_transcription(monkeypatch)

    def failing_launch(config, session_id=None, config_file=None, recording_session=None):
        raise RuntimeError("no editor today")

    monkeypatch.setattr(cw, "launch_editor", failing_launch)
    monkeypatch.setattr(cw.QMessageBox, "warning", lambda *a, **k: None)
    window.record_button.click()
    _auto_answer_stop(monkeypatch, StopChoice.EDITOR)
    window.record_button.click()
    qtbot.waitUntil(lambda: window.finished_statuses == ["cancelled"], timeout=10000)
    state = read_state(controller.paths.state_path)
    assert state.status is Status.CANCELLED and state.crop is None
    assert "no editor today" in (state.message or "")
    assert (controller.paths.final_dir / "manifest.json").exists()  # the recording is kept


def test_auto_stop_opens_the_editor_and_processes_nothing(window, controller, monkeypatch, qtbot):
    """The auto stop ends like Stop -> Video Editor: only Process hands a recording over."""
    _no_transcription(monkeypatch)
    launched = _fake_editor_launch(monkeypatch)
    window.record_button.click()
    FakeRecorder.instances[-1].auto_stopped = True
    window._tick()
    qtbot.waitUntil(lambda: window.finished_statuses == ["editing"], timeout=10000)
    state = read_state(controller.paths.state_path)
    assert state.status is Status.EDITING and state.crop is None
    assert len(launched) == 1


def test_low_disk_stops_tells_the_user_and_opens_the_editor(window, controller, monkeypatch, qtbot):
    launched = _fake_editor_launch(monkeypatch)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cw.QMessageBox, "warning", lambda _p, title, text: shown.append((title, text))
    )
    window.record_button.click()
    recorder = FakeRecorder.instances[-1]
    recorder.low_disk = True
    recorder.running = False  # the capture loop left
    window._tick()
    assert recorder.stopped  # stopped before the message box, not after it
    assert len(shown) == 1 and "disk" in shown[0][0].lower()
    assert "At least 1 GB of free space" in shown[0][1] and "saved" in shown[0][1]
    qtbot.waitUntil(lambda: window.finished_statuses == ["editing"], timeout=10000)
    assert len(launched) == 1
    assert read_state(controller.paths.state_path).status is Status.EDITING


def test_write_error_stops_and_saves_what_there_is(window, controller, monkeypatch, qtbot):
    _fake_editor_launch(monkeypatch)
    shown: list[str] = []
    monkeypatch.setattr(cw.QMessageBox, "warning", lambda _p, title, text: shown.append(text))
    window.record_button.click()
    recorder = FakeRecorder.instances[-1]
    recorder.error = "frame write failed: disk went away"
    window._tick()
    assert recorder.stopped and "disk went away" in shown[0]
    qtbot.waitUntil(lambda: window.finished_statuses == ["editing"], timeout=10000)


def test_no_closing_while_a_stop_notice_is_shown(window, controller, monkeypatch, qtbot):
    """While the "recording stopped" notice is up, the window cannot be closed (closing
    would cancel the session instead of saving the recording)."""
    _fake_editor_launch(monkeypatch)
    seen: list[bool] = []

    def notice(_parent, _title, _text):
        window.close()
        seen.append(window.isVisible() and not window.finished_statuses)

    monkeypatch.setattr(cw.QMessageBox, "warning", notice)
    window.record_button.click()
    FakeRecorder.instances[-1].running = False  # the capture thread died
    window._tick()
    assert seen == [True]
    qtbot.waitUntil(lambda: window.finished_statuses == ["editing"], timeout=10000)


def test_record_reports_missing_transcription_parts(qtbot, controller, monkeypatch):
    answers = iter([cw.QMessageBox.StandardButton.No, cw.QMessageBox.StandardButton.Yes])
    asked: list[str] = []

    def question(_parent, _title, text, *_args):
        asked.append(text)
        return next(answers)

    monkeypatch.setattr(cw.QMessageBox, "question", question)
    w = cw.ControlWindow(controller, on_finished=lambda _s: None, preflight=lambda: ["FFmpeg"])
    w.show()
    try:
        w.record_button.click()
        assert not controller.recording and "FFmpeg" in asked[0] and "setup" in asked[0]
        w.record_button.click()
        assert controller.recording and len(asked) == 2
    finally:
        w._done = True
        w.close()


def test_new_recording_folder_applies_to_the_open_session(qtbot, controller, tmp_path: Path):
    """Settings -> Recording Folder: the session moves there at once, so the recording,
    the editor and the waiting agent (which resolves the session through the saved
    configuration) all use the new folder."""
    import video_story.core.config as cfg_mod
    from video_story.host.runner import resolve_session, session_status

    config_file = tmp_path / "config.json"
    old_paths = controller.paths
    new_folder = tmp_path / "bigger disk"
    dialog = SettingsDialog(
        controller.config,
        current_session_id=old_paths.session_id,
        config_file=config_file,
        save_guard=lambda config: controller.relocating(config.recording_folder),
    )
    qtbot.addWidget(dialog)
    dialog.folder_edit.setText(str(new_folder))
    dialog._save()
    assert dialog.result_config is not None
    controller.config = dialog.result_config
    assert controller.paths.recording_folder == new_folder
    assert controller.paths.session_id == old_paths.session_id
    assert not old_paths.state_path.exists()
    saved = cfg_mod.load_config(config_file)
    assert saved.recording_folder == new_folder
    status = session_status(resolve_session(saved, old_paths.session_id))
    assert status["status"] == "ready" and status["recording_folder"] == str(new_folder)
    controller.start_recording()
    assert (new_folder / ".work" / old_paths.session_id / "frames").is_dir()
    assert not (tmp_path / ".work" / old_paths.session_id / "frames").exists()
    controller.stop_recording()


def test_failed_settings_save_keeps_the_session_where_it_was(
    qtbot, controller, tmp_path: Path, monkeypatch
):
    from video_story.ui import settings_dialog as sd

    def cannot_save(config, path=None):
        raise OSError("read-only")

    monkeypatch.setattr(sd, "save_config", cannot_save)
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)
    old_paths = controller.paths
    new_folder = tmp_path / "elsewhere"
    dialog = SettingsDialog(
        controller.config,
        config_file=tmp_path / "config.json",
        save_guard=lambda config: controller.relocating(config.recording_folder),
    )
    qtbot.addWidget(dialog)
    dialog.folder_edit.setText(str(new_folder))
    dialog._save()
    assert dialog.result_config is None
    assert controller.paths == old_paths and old_paths.state_path.exists()
    assert not (new_folder / ".work" / old_paths.session_id).exists()
    assert read_state(old_paths.state_path).recording_folder == str(tmp_path)


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
    assert form.itemAt(5, form.ItemRole.LabelRole).widget().text() == "Computer Audio"
    assert form.itemAt(6, form.ItemRole.LabelRole).widget().text() == "Mouse"
    assert form.itemAt(7, form.ItemRole.LabelRole).widget().text() == "Live Transcription"
    assert form.itemAt(8, form.ItemRole.LabelRole).widget().text() == "Auto Stop Recording"
    assert form.itemAt(9, form.ItemRole.LabelRole).widget().text() == "Archive After"
    assert form.itemAt(10, form.ItemRole.LabelRole).widget().text() == "Drawing Grid"
    assert form.itemAt(11, form.ItemRole.LabelRole).widget().text() == "Recordings"
    assert dialog.grid_spin.value() == 16
    assert dialog.grid_spin.minimum() == 8 and dialog.grid_spin.maximum() == 128
    assert dialog.live_combo.currentData() == "light"  # the default
    assert [dialog.live_combo.itemData(i) for i in range(3)] == ["off", "light", "faster"]
    assert dialog.live_combo.itemText(1) == "Light – low CPU use (recommended)"
    assert dialog.computer_audio_check.isChecked()  # recorded by default
    assert dialog.mouse_check.isChecked()  # recorded by default
    assert dialog.interval_spin.value() == 500 and dialog.format_combo.currentData() == "jpeg"
    assert dialog.quality_spin.value() == 70
    assert dialog.auto_stop_spin.value() == 120 and dialog.archive_after_spin.value() == 30
    assert dialog.interval_spin.minimum() == 1 and dialog.interval_spin.maximum() == 1000
    assert form.isRowVisible(3)  # JPEG is the default, so the quality row shows
    dialog.format_combo.setCurrentIndex(0)
    assert not form.isRowVisible(3)
    dialog.format_combo.setCurrentIndex(1)
    assert form.isRowVisible(3)
    dialog.interval_spin.setValue(1000)
    dialog.quality_spin.setValue(20)
    dialog.auto_stop_spin.setValue(45)
    dialog.archive_after_spin.setValue(7)
    dialog.grid_spin.setValue(21)  # any number, not only multiples of 8
    dialog.computer_audio_check.setChecked(False)
    dialog.mouse_check.setChecked(False)
    dialog.live_combo.setCurrentIndex(2)
    dialog._save()
    assert dialog.result_config is not None
    assert dialog.result_config.capture_interval_ms == 1000
    assert dialog.result_config.image_format == "jpeg" and dialog.result_config.jpeg_quality == 20
    assert dialog.result_config.auto_stop_minutes == 45
    assert dialog.result_config.archive_after_days == 7
    assert dialog.result_config.drawing_grid_px == 21
    assert dialog.result_config.computer_audio is False
    assert dialog.result_config.mouse_actions is False
    assert dialog.result_config.live_transcription == "faster"
    reloaded = cfg_mod.load_config()
    assert reloaded == dialog.result_config


def test_settings_archive_after_zero_reads_never(qtbot, tmp_path: Path, monkeypatch):
    import video_story.core.config as cfg_mod

    monkeypatch.setenv(cfg_mod.CONFIG_PATH_ENV, str(tmp_path / "config.json"))
    dialog = SettingsDialog(Config(recording_folder=tmp_path))
    qtbot.addWidget(dialog)
    spin = dialog.archive_after_spin
    assert spin.minimum() == 0 and spin.maximum() == 3650
    assert spin.specialValueText() == "Never"
    assert spin.text() == "30 days"  # the default is unchanged
    spin.setValue(0)
    assert spin.text() == "Never"
    assert "Never" in spin.toolTip()
    dialog._save()
    assert dialog.result_config is not None and dialog.result_config.archive_after_days == 0
    assert cfg_mod.load_config().archive_after_days == 0
    # A stored 0 comes back as "Never".
    again = SettingsDialog(dialog.result_config)
    qtbot.addWidget(again)
    assert again.archive_after_spin.value() == 0 and again.archive_after_spin.text() == "Never"


def test_settings_live_transcription_is_off_and_disabled_with_whisper_cli(qtbot, tmp_path):
    dialog = SettingsDialog(Config(recording_folder=tmp_path, whisper_backend="whisper-cli"))
    qtbot.addWidget(dialog)
    assert dialog.live_combo.currentData() == "off"
    assert not dialog.live_combo.isEnabled()
    assert dialog.values()["live_transcription"] == "light"  # the stored value is kept


def test_settings_offers_no_delete_all(qtbot, tmp_path: Path):
    """Recordings are deleted in the editor (videos, folders), never from Settings."""
    dialog = SettingsDialog(Config(recording_folder=tmp_path))
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "delete_all_button")
    texts = [button.text() for button in dialog.findChildren(QPushButton)]
    assert not any("Delete" in text for text in texts)


def test_app_instance_available():
    assert QApplication.instance() is not None
    QTimer.singleShot(0, lambda: None)
    assert QMessageBox is not None


# Shortcuts while recording ---------------------------------------------------


def _activate(qtbot, widget) -> None:
    widget.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is widget, timeout=1000)


def test_buttons_name_their_shortcuts(window):
    assert window.draw_button.text() == "Draw (Ctrl+Space)"
    assert "Ctrl+Space" in window.draw_button.toolTip()
    assert "Ctrl+F1" in window.draw_button.toolTip()
    right = window.geometry().right()
    window.record_button.click()
    # The longer labels widen the window to the left: it stays in its top right spot.
    # (The offscreen test platform shifts a moved window by its 1 px frame.)
    assert abs(window.geometry().right() - right) <= 2
    assert window.record_button.text() == "Stop (Ctrl+Alt+S)"
    assert "Ctrl+Alt+S" in window.record_button.toolTip()
    assert window.keypoint_button.text() == "Keypoint (Ctrl+Alt+K)"
    assert "Ctrl+Alt+K" in window.keypoint_button.toolTip()
    window.insert_keypoint()
    assert window.keypoint_button.text() == "Keypoint (3)"
    window._reset_keypoint_button()
    assert window.keypoint_button.text() == "Keypoint (Ctrl+Alt+K)"


def test_shortcuts_are_active_only_while_recording(window, monkeypatch):
    # The offscreen test platform has no system-wide hotkeys: application-wide shortcuts.
    assert not window.hotkeys.enabled and window.hotkeys.application_wide == []
    window.record_button.click()
    assert window.hotkeys.enabled
    assert window.hotkeys.application_wide == [
        "Ctrl+Alt+S",
        "Ctrl+Alt+K",
        "Ctrl+Space",
        "Ctrl+F1",
    ]
    _auto_answer_stop(monkeypatch, StopChoice.DISCARD, confirm=True)
    window.record_button.click()
    assert not window.hotkeys.enabled and window.hotkeys.application_wide == []


def test_keypoint_and_stop_shortcuts(window, controller, monkeypatch, qtbot):
    from PySide6.QtCore import Qt

    ctrl_alt = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
    stops: list[int] = []

    def fake_exec(self):
        stops.append(1)
        self.choice = StopChoice.CONTINUE
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(cw.StopDialog, "exec", fake_exec)
    # Not recording: Ctrl+Alt+S must not start (or stop) anything.
    _activate(qtbot, window)
    qtbot.keyClick(window, Qt.Key.Key_S, ctrl_alt)
    qtbot.wait(50)
    assert not controller.recording and stops == []

    window.record_button.click()
    _activate(qtbot, window)
    qtbot.keyClick(window, Qt.Key.Key_K, ctrl_alt)
    qtbot.waitUntil(lambda: len(FakeRecorder.instances[-1].keypoints) == 1, timeout=1000)
    assert window.keypoint_countdown == 3
    qtbot.keyClick(window, Qt.Key.Key_S, ctrl_alt)
    qtbot.waitUntil(lambda: stops == [1], timeout=1000)
    assert controller.recording  # Continue Recording


def test_draw_shortcut_toggles_drawing(qtbot, controller):
    from PySide6.QtCore import QObject, Qt, Signal

    class FakeOverlay(QObject):
        closed = Signal()

        def __init__(self) -> None:
            super().__init__()
            self.visible = False
            self.shapes = 0
            self.clears = 0
            self.shapes_at_hide: list[int] = []

        def show(self) -> None:
            self.visible = True

        def hide(self) -> None:
            self.shapes_at_hide.append(self.shapes)
            self.visible = False

        def close(self) -> None:
            self.visible = False

        def clear(self) -> None:
            self.shapes = 0
            self.clears += 1

    overlays: list[FakeOverlay] = []

    def factory():
        overlays.append(FakeOverlay())
        return overlays[-1]

    w = cw.ControlWindow(controller, on_finished=lambda status: None, overlay_factory=factory)
    w.show()
    try:
        ctrl = Qt.KeyboardModifier.ControlModifier
        w.record_button.click()
        _activate(qtbot, w)
        qtbot.keyClick(w, Qt.Key.Key_Space, ctrl)
        qtbot.waitUntil(lambda: w.draw_button.isChecked(), timeout=1000)
        overlay = overlays[0]
        assert overlay.visible
        overlay.shapes = 2
        # Leaving drawing mode by the shortcut clears what was drawn.
        qtbot.keyClick(w, Qt.Key.Key_Space, ctrl)
        qtbot.waitUntil(lambda: not w.draw_button.isChecked(), timeout=1000)
        assert not overlay.visible and overlay.shapes == 0 and overlay.clears == 1
        # ... before hiding the overlay: hidden windows keep their last picture.
        assert overlay.shapes_at_hide == [0]
        # Ctrl+F1 toggles drawing too.
        qtbot.keyClick(w, Qt.Key.Key_F1, ctrl)
        qtbot.waitUntil(lambda: w.draw_button.isChecked(), timeout=1000)
        assert overlay.visible
        overlay.shapes = 3
        qtbot.keyClick(w, Qt.Key.Key_F1, ctrl)
        qtbot.waitUntil(lambda: not w.draw_button.isChecked(), timeout=1000)
        assert overlay.shapes == 0 and overlay.clears == 2
        # The button, then Esc on the overlay (its closed signal): cleared as well.
        w.draw_button.click()
        assert overlay.visible
        overlay.shapes = 1
        overlay.visible = False
        overlay.closed.emit()
        assert not w.draw_button.isChecked()
        assert overlay.shapes == 0 and overlay.clears == 3
    finally:
        w._done = True
        w.close()


def test_drawings_are_cleared_when_drawing_mode_ends(qtbot, controller):
    """With the real overlay: shapes drawn before Esc are gone at the next Draw."""
    from PySide6.QtCore import QPoint, Qt

    from video_story.ui.overlay import DrawingOverlay

    w = cw.ControlWindow(
        controller, on_finished=lambda status: None, overlay_factory=DrawingOverlay
    )
    w.show()
    try:
        w.record_button.click()
        w.draw_button.click()
        overlay = w._overlay
        assert overlay is not None and overlay.is_visible()
        window = overlay.windows[0]
        qtbot.mousePress(window, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))
        qtbot.mouseMove(window, QPoint(60, 60))
        qtbot.mouseRelease(window, Qt.MouseButton.LeftButton, pos=QPoint(60, 60))
        assert len(overlay.shapes()) == 1
        hidden_with: list[int] = []
        real_hide = overlay.hide
        overlay.hide = lambda: (hidden_with.append(len(overlay.shapes())), real_hide())
        overlay._user_close()  # Esc / Close / the cross
        assert overlay.shapes() == [] and overlay.leaving  # cleared at once
        qtbot.waitUntil(lambda: hidden_with == [0], timeout=2000)  # hidden after the flash
        assert not w.draw_button.isChecked()
        w.draw_button.click()
        assert overlay.is_visible() and overlay.shapes() == []
        w.draw_button.click()  # Draw / Ctrl+Space ends drawing with the green flash too
        assert not overlay.is_visible() and overlay.leaving
        qtbot.waitUntil(lambda: not overlay.leaving, timeout=2000)
    finally:
        w._close_overlay()  # its application-wide shortcuts must not outlive the test
        w._done = True
        w.close()


def test_theme_button_switches_and_remembers_the_theme(qtbot, controller, tmp_path):
    from PySide6.QtCore import QSettings

    app = QApplication.instance()
    palette, style = app.palette(), app.style().objectName()
    settings = QSettings(str(tmp_path / "recorder.ini"), QSettings.Format.IniFormat)
    w = cw.ControlWindow(controller, on_finished=lambda status: None, settings=settings)
    w.show()
    try:
        start = w.dark
        assert w.theme_button.text() == "Theme"
        assert ("light" if start else "dark") in w.theme_button.toolTip()
        w.theme_button.click()
        assert w.dark is (not start)
        assert settings.value(cw.THEME_SETTING, type=bool) is (not start)
        assert ("light" if w.dark else "dark") in w.theme_button.toolTip()
        dark_window = app.palette().color(app.palette().ColorRole.Window).lightness() < 128
        assert dark_window is w.dark
        # A new window starts in the remembered theme.
        other = cw.ControlWindow(controller, on_finished=lambda status: None, settings=settings)
        assert other.dark is w.dark
        other._done = True
        other.close()
    finally:
        w._done = True
        w.close()
        app.setStyle(style)
        app.setPalette(palette)


def test_hide_to_tray_and_back(window, monkeypatch, qtbot):
    from PySide6.QtWidgets import QSystemTrayIcon

    messages: list[str] = []
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    monkeypatch.setattr(
        QSystemTrayIcon, "showMessage", lambda self, title, text, *a: messages.append(text)
    )
    assert window.tray_button.text() == "Hide to Tray"
    window.record_button.click()
    window.tray_button.click()
    assert window.in_tray and not window.isVisible()
    assert messages and "Ctrl+Alt+S" in messages[0]
    assert window.hotkeys.enabled  # the shortcuts keep working while hidden
    # A left or a right click on the tray icon shows the window again.
    window._on_tray_activated(QSystemTrayIcon.ActivationReason.Context)
    assert window.isVisible() and not window.in_tray
    assert window.hide_to_tray()
    window._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert window.isVisible() and not window.in_tray
    # Discarding the recording from the tray brings the window back.
    assert window.hide_to_tray()
    _auto_answer_stop(monkeypatch, StopChoice.DISCARD, confirm=True)
    window._stop_by_shortcut()
    qtbot.waitUntil(lambda: window.isVisible(), timeout=1000)
    assert not window.in_tray and not window.controller.recording


def test_hide_to_tray_without_a_tray(window, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    notes: list[str] = []
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    monkeypatch.setattr(cw.QMessageBox, "information", lambda *a, **k: notes.append(a[2]))
    assert window.hide_to_tray() is False
    assert window.isVisible() and notes


def test_windows_hotkey_codes():
    from video_story.ui import hotkeys

    assert hotkeys.windows_hotkey("Ctrl+Alt+S") == (
        hotkeys.MOD_NOREPEAT | hotkeys.MOD_CONTROL | hotkeys.MOD_ALT,
        ord("S"),
    )
    assert hotkeys.windows_hotkey("Ctrl+Alt+K")[1] == ord("K")
    assert hotkeys.windows_hotkey("Ctrl+Space") == (
        hotkeys.MOD_NOREPEAT | hotkeys.MOD_CONTROL,
        hotkeys.VK_SPACE,
    )
    assert hotkeys.windows_hotkey("Ctrl+F1") == (
        hotkeys.MOD_NOREPEAT | hotkeys.MOD_CONTROL,
        hotkeys.VK_F1,
    )
    assert hotkeys.windows_hotkey("Ctrl+F5")[1] == hotkeys.VK_F1 + 4
    assert hotkeys.windows_hotkey("Ctrl+Home") is None  # not a supported key


@pytest.mark.skipif(not os.name == "nt", reason="WM_HOTKEY is a Windows message")
def test_windows_hotkey_message_reaches_the_shortcut(qtbot):
    import ctypes
    from ctypes import wintypes

    from PySide6.QtWidgets import QWidget
    from shiboken6 import VoidPtr

    from video_story.ui import hotkeys

    parent = QWidget()
    qtbot.addWidget(parent)
    hits: list[str] = []
    keys = hotkeys.RecordingHotkeys(
        parent, [hotkeys.Hotkey("Ctrl+Alt+S", lambda: hits.append("stop"))], system_wide=False
    )
    keys.enable()
    # Pretend the system registered it (no real RegisterHotKey in unit tests).
    keys._registered[hotkeys.HOTKEY_ID_BASE] = keys._hotkeys[0]
    event_filter = hotkeys._WindowsHotkeyFilter(keys._on_windows_hotkey)
    msg = wintypes.MSG()
    msg.message = hotkeys.WM_HOTKEY
    msg.wParam = hotkeys.HOTKEY_ID_BASE
    pointer = VoidPtr(ctypes.addressof(msg))  # what Qt passes
    handled, _ = event_filter.nativeEventFilter(b"windows_generic_MSG", pointer)
    assert handled
    qtbot.waitUntil(lambda: hits == ["stop"], timeout=1000)
    msg.wParam = hotkeys.HOTKEY_ID_BASE + 7  # someone else's hotkey
    assert event_filter.nativeEventFilter(b"windows_generic_MSG", pointer)[0] is False
    keys._registered.clear()  # nothing to unregister
    keys.disable()


def test_controller_transcribes_while_recording_and_saves_it(controller, monkeypatch):
    """Record starts the live transcription; saving stops it and keeps its transcript."""
    import sys as _sys
    import time

    import video_story.host.controller as ctl
    from video_story.core.manifest import read_manifest
    from video_story.core.transcript import read_transcript
    from video_story.host.live_transcription import LiveTranscriber

    event = {
        "event": "committed",
        "segments": [{"start_ms": 0, "end_ms": 2000, "text": "Dobrý den."}],
        "transcribed_ms": 2500,
        "language": "cs",
        "start_ms": 0,
        "end_ms": 2500,
        "cut": "pause",
        "speech": True,
        "seconds": 1.0,
    }
    script = (
        "import json, sys\n"
        "print(json.dumps({'event': 'start', 'threads': 4, 'priority': 'background'}), "
        "flush=True)\n"
        f"print(json.dumps({event!r}), flush=True)\n"
        "sys.stdin.read()\n"
    )
    monkeypatch.setattr(ctl, "live_transcription_available", lambda _c: None)
    created: list[LiveTranscriber] = []

    def create() -> LiveTranscriber:
        live = LiveTranscriber(
            controller.paths,
            controller.config,
            command=lambda _p, _m: [_sys.executable, "-c", script],
        )
        created.append(live)
        return live

    monkeypatch.setattr(controller, "create_live_transcriber", create)
    controller.start_recording()
    assert controller.live is created[0]
    deadline = time.monotonic() + 10
    while created[0].transcribed_ms < 2500 and time.monotonic() < deadline:
        time.sleep(0.02)
    final_dir = controller.process()
    assert controller.live is None and not created[0].running
    transcript = read_transcript(controller.paths.transcript_json_path(final_dir))
    assert not transcript.complete and transcript.transcribed_ms == 2500
    assert [s.text for s in transcript.segments] == ["Dobrý den."]
    manifest = read_manifest(controller.paths.manifest_path(final_dir))
    assert manifest.live_transcription["windows"] == 1
    assert manifest.live_transcription["priority"] == "background"


def test_discard_stops_the_live_transcription(controller, monkeypatch):
    import video_story.host.controller as ctl

    stopped: list[bool] = []

    class FakeLive:
        def start(self) -> bool:
            return True

        def stop(self) -> None:
            stopped.append(True)

        def info(self, recorded_ms=None):
            return {"mode": "light"}

    monkeypatch.setattr(ctl, "live_transcription_available", lambda _c: None)
    monkeypatch.setattr(controller, "create_live_transcriber", FakeLive)
    controller.start_recording()
    controller.discard()
    assert stopped == [True] and controller.live is None
