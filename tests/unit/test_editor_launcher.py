"""``video-story edit --detach``: the editor lock and launching the editor process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from video_story.core.config import Config
from video_story.core.manifest import Manifest, write_manifest
from video_story.core.screens import Screen
from video_story.core.session import SessionPaths, now_iso
from video_story.host import editor_launcher as launcher
from video_story.host import editor_session as es
from video_story.host import mcp_server
from video_story.host.editor_lock import EditorLock, lock_holder_pid
from video_story.host.runner import SessionError


@pytest.fixture(autouse=True)
def isolated_launcher(monkeypatch):
    # These tests own the spawn/lock contract, not a real GUI. Startup is tested separately.
    monkeypatch.setattr(launcher, "require_ready", lambda cfg: {})
    monkeypatch.setattr(launcher.startup, "wait_ready", lambda *a, **k: {"phase": "ready"})


def _processed(root: Path, session_id: str, valid: bool = True) -> SessionPaths:
    paths = SessionPaths(root, session_id)
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    manifest_path = paths.manifest_path(paths.final_dir)
    if not valid:
        manifest_path.write_text("{}", encoding="utf-8")  # unreadable: no screens etc.
        return paths
    write_manifest(
        manifest_path,
        Manifest(
            session_id=session_id,
            started_at=now_iso(),
            finished_at=now_iso(),
            recorded_duration_ms=1000,
            capture_interval_ms=500,
            image_format="png",
            jpeg_quality=None,
            screens=[Screen("screen1", 1, 0, 0, 320, 200, True, 1.0)],
        ),
    )
    return paths


# Lock -----------------------------------------------------------------------


def test_lock_is_free_after_release_and_after_a_stale_file(tmp_path: Path):
    path = tmp_path / ".work" / "editor.lock"
    assert lock_holder_pid(path) is None
    lock = EditorLock(path)
    assert lock.acquire() and lock.held
    assert path.read_text(encoding="ascii") == str(os.getpid())
    lock.release()
    assert not lock.held and lock_holder_pid(path) is None
    # A pid file left behind without an OS lock (impossible for a live editor) is free.
    path.parent.mkdir(exist_ok=True)
    path.write_text("123456", encoding="ascii")
    assert lock_holder_pid(path) is None
    with EditorLock(path):
        assert lock_holder_pid(path) == os.getpid() or lock_holder_pid(path) is None
    assert lock_holder_pid(path) is None


def test_lock_held_by_another_process_is_detected(tmp_path: Path):
    """A second process holds the lock; the probe sees it (and its pid) until it exits."""
    path = tmp_path / ".work" / "editor.lock"
    script = textwrap.dedent(
        f"""
        import os, sys, pathlib
        from video_story.host.editor_lock import EditorLock
        lock = EditorLock(pathlib.Path({str(path)!r}))
        assert lock.acquire()
        print("locked", os.getpid(), flush=True)
        sys.stdin.readline()
        lock.release()
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        word, child_pid = proc.stdout.readline().split()  # the venv launcher may wrap it
        assert word == "locked"
        assert lock_holder_pid(path) == int(child_pid)
        assert not EditorLock(path).acquire()
        cfg = Config(recording_folder=tmp_path)
        _processed(tmp_path, "20261001-090000")
        attached = launcher.launch_editor(cfg)
        assert attached["attached"] is True and attached["pid"] == int(child_pid)
        es.take_attach_requests(cfg)
    finally:
        assert proc.stdin is not None
        proc.stdin.write("\n")
        proc.stdin.close()
        proc.wait(timeout=20)
    assert lock_holder_pid(path) is None


# Launcher -------------------------------------------------------------------


def test_launch_requires_processed_recordings(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    with pytest.raises(SessionError) as info:
        launcher.launch_editor(cfg)
    assert info.value.code == "NO_RECORDINGS"
    # A manifest the editor cannot read does not count either (same rule as the list).
    _processed(tmp_path, "20261001-090000", valid=False)
    assert launcher.processed_recordings(cfg) == []
    with pytest.raises(SessionError) as info:
        launcher.launch_editor(cfg, "20261001-090000")
    assert info.value.code == "NO_RECORDINGS"


def test_launch_rejects_unknown_or_invalid_session(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    _processed(tmp_path, "20261001-090000")
    with pytest.raises(SessionError) as info:
        launcher.launch_editor(cfg, "20261002-090000")
    assert info.value.code == "UNKNOWN_SESSION"
    with pytest.raises(SessionError) as info:
        launcher.launch_editor(cfg, "not-an-id")
    assert info.value.code == "INVALID_SESSION"


def test_launch_spawns_detached_process(tmp_path: Path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    _processed(tmp_path, "20261001-090000")
    _processed(tmp_path, "20261002-090000")
    spawned: list[list[str]] = []

    def fake_spawn(args, log_path, cwd=None):
        spawned.append(list(args))
        assert log_path == tmp_path / ".work" / launcher.EDITOR_LOG_NAME
        return 4242

    monkeypatch.setattr(launcher, "spawn_detached", fake_spawn)
    result = launcher.launch_editor(cfg, config_file=tmp_path / "config.json")
    assert result["pid"] == 4242 and result["session_id"] == "20261002-090000"
    assert result["recordings"] == 2 and result["attached"] is False
    assert spawned[0][spawned[0].index("--session") + 1] == "20261002-090000"
    assert str(tmp_path / "config.json") in spawned[0]
    editor_session = Path(result["editor_session"])
    assert editor_session.exists() and spawned[0][-2:] == ["--editor-session", str(editor_session)]
    assert "--recording-session" not in spawned[0]

    result = launcher.launch_editor(cfg, "20261001-090000", recording_session="20261001-090000")
    assert result["session_id"] == "20261001-090000"
    assert "--session" in spawned[1] and spawned[1][-2:] == [
        "--recording-session",
        "20261001-090000",
    ]


def test_launch_refuses_second_instance(tmp_path: Path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    _processed(tmp_path, "20261001-090000")
    monkeypatch.setattr(launcher, "spawn_detached", lambda *a, **k: 1)
    with EditorLock(launcher.editor_lock_path(cfg)):
        # A running editor is reused: an attach request is written, no process spawned.
        result = launcher.launch_editor(cfg, "20261001-090000")
        assert result["attached"] is True and result["pid"] == os.getpid()
        requests = es.take_attach_requests(cfg)
        assert len(requests) == 1 and requests[0].session_id == "20261001-090000"
        assert requests[0].editor_session == Path(result["editor_session"])
    assert launcher.launch_editor(cfg)["pid"] == 1


# MCP tool ---------------------------------------------------------------------


def test_tool_edit_wraps_launcher(tmp_path: Path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"recording_folder": str(tmp_path)}), encoding="utf-8")
    result = mcp_server.tool_edit(None, config_file)
    assert result["ok"] is False and result["code"] == "NO_RECORDINGS"
    _processed(tmp_path, "20261001-090000")
    calls: list[tuple] = []
    monkeypatch.setattr(
        launcher,
        "spawn_detached",
        lambda args, log_path, cwd=None: calls.append(tuple(args)) or 77,
    )
    result = mcp_server.tool_edit("20261001-090000", config_file)
    assert result["ok"] is True and result["pid"] == 77
    assert calls and "--session" in calls[0]
    assert calls[0][calls[0].index("--session") + 1] == "20261001-090000"
    assert "--editor-session" in calls[0] and result["editor_session"] in calls[0]
    assert str(config_file) in calls[0]
    assert mcp_server.tool_edit("20261002-090000", config_file)["code"] == "UNKNOWN_SESSION"
