"""Startup acknowledgement is distinct from PID creation and user completion."""

import os
import threading
import time

import pytest

from video_story.core.config import Config
from video_story.core.session import SessionPaths, Status, read_state, write_state
from video_story.host import runner, startup
from video_story.host.errors import HostError


def request(tmp_path, monkeypatch, timeout=1):
    monkeypatch.setattr(startup, "START_TIMEOUT_S", timeout)
    return startup.create_request(
        tmp_path / "startup.json", Config(recording_folder=tmp_path), "run", "20261113-120000"
    )


def test_wait_requires_ack_and_preserves_identity(tmp_path, monkeypatch):
    path = request(tmp_path, monkeypatch)
    thread = threading.Thread(target=lambda: (time.sleep(0.05), startup.acknowledge(path, "ready")))
    thread.start()
    try:
        assert startup.wait_ready(path, os.getpid(), tmp_path / "log")["phase"] == "ready"
    finally:
        thread.join()


def test_timeout_and_gui_failure(tmp_path, monkeypatch):
    path = request(tmp_path, monkeypatch, 0)
    with pytest.raises(HostError) as exc:
        startup.wait_ready(path, os.getpid(), tmp_path / "log", session_id="s")
    assert exc.value.details["reason"] == "readiness_timeout"
    assert exc.value.details["session_id"] == "s"
    startup.fail(path, HostError("ENVIRONMENT_INVALID", "missing", 23))
    with pytest.raises(HostError, match="missing") as exc:
        startup.wait_ready(path, os.getpid(), tmp_path / "log")
    assert exc.value.code == "ENVIRONMENT_INVALID"
    startup.cleanup_completed(path)
    assert path.exists(), "failed startup evidence must remain"
    startup.acknowledge(path, "ready")
    startup.cleanup_completed(path)
    assert not path.exists() and not startup.response_path(path).exists()


def test_gui_confirms_only_visible_window(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QWidget

    path = request(tmp_path, monkeypatch)
    window = QWidget()
    qtbot.addWidget(window)
    startup.watch_window(window, path)
    qtbot.wait(50)
    assert not startup.response_path(path).exists()
    window.show()
    qtbot.waitUntil(lambda: startup.response_path(path).exists())
    assert startup.wait_ready(path, os.getpid(), tmp_path / "log")["phase"] == "ready"


def test_no_side_effects_when_dependencies_missing(tmp_path, monkeypatch):
    cfg = Config(recording_folder=tmp_path / "recordings")

    def missing(config):
        raise HostError("SETUP_REQUIRED", "model", 4, missing=["model"])

    monkeypatch.setattr(runner, "require_ready", missing)
    with pytest.raises(HostError):
        runner.launch_control_window(cfg)
    assert not cfg.recording_folder.exists()


def test_parent_does_not_overwrite_gui_state(tmp_path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    monkeypatch.setattr(runner, "require_ready", lambda cfg: {})

    def spawn(args, log, cwd=None):
        session_id = args[args.index("--session") + 1]
        paths = SessionPaths(tmp_path, session_id)
        state = read_state(paths.state_path)
        state.status = Status.RECORDING
        state.pid = os.getpid()
        write_state(paths.state_path, state)
        startup.acknowledge(paths.work_dir / "startup.json", "ready")
        return os.getpid()

    monkeypatch.setattr(runner, "spawn_detached", spawn)
    result = runner.launch_control_window(cfg)
    assert read_state(result.state_file).status is Status.RECORDING
    assert result.pid == os.getpid()


def test_failed_spawn_does_not_leave_an_active_session(tmp_path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    monkeypatch.setattr(runner, "require_ready", lambda cfg: {})

    def fail(*args, **kwargs):
        raise OSError("cannot create process")

    monkeypatch.setattr(runner, "spawn_detached", fail)
    with pytest.raises(HostError) as exc:
        runner.launch_control_window(cfg)
    assert exc.value.code == "START_FAILED"
    assert runner.active_session(cfg) is None


def test_concurrent_start_is_not_a_second_window(tmp_path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    monkeypatch.setattr(runner, "require_ready", lambda cfg: {})
    monkeypatch.setattr(runner, "spawn_detached", lambda *a, **k: pytest.fail("duplicate spawn"))
    with startup.launch_guard(cfg, "recorder"):
        with pytest.raises(HostError) as exc:
            runner.launch_control_window(cfg)
        assert exc.value.details["reason"] == "start_in_progress"
    assert (tmp_path / ".work/start-recorder.lock").exists()
    with startup.launch_guard(cfg, "recorder"):
        pass  # the retained file is unlocked and reusable
