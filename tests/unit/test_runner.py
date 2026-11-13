"""Tests for session following and frame lookup (no GUI, no devices)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from video_story.core.config import Config
from video_story.core.manifest import Manifest, write_manifest
from video_story.core.naming import frame_filename, sidecar_filename
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import SessionPaths, SessionState, Status, write_state
from video_story.host import runner


def make_recording(root: Path, session_id: str = "20260101-120000") -> SessionPaths:
    paths = SessionPaths(root, session_id)
    paths.create_work_dirs()
    frames = paths.frames_dir(paths.work_dir)
    info = paths.frames_info_dir(paths.work_dir)
    screens = assign_screen_ids(
        [
            RawScreen(0, 0, 100, 100, primary=True),
            RawScreen(100, 0, 50, 50),
        ]
    )
    for t in (0, 250, 500, 750):
        cursor = {"screen": "screen2" if t >= 500 else "screen1", "x": t % 50, "y": 1}
        for sid in ("screen1", "screen2"):
            (frames / frame_filename(t, sid, "png")).write_bytes(b"\x89PNG")
            (info / sidecar_filename(t, sid)).write_text(
                json.dumps({"time_ms": t, "screen": sid, "cursor": cursor}), encoding="utf-8"
            )
    write_manifest(
        paths.manifest_path(paths.work_dir),
        Manifest(
            session_id=session_id,
            started_at="x",
            finished_at="y",
            recorded_duration_ms=900,
            capture_interval_ms=250,
            image_format="png",
            jpeg_quality=None,
            screens=screens,
        ),
    )
    write_state(
        paths.state_path,
        SessionState(session_id=session_id, status=Status.PROCESSED, recording_folder=str(root)),
    )
    paths.finalize()
    return paths


def test_frames_at_cursor_screen(tmp_path: Path):
    paths = make_recording(tmp_path)
    result = runner.frames_at(paths, 600)  # latest frame at/before 600 => 500, cursor on screen2
    assert result["cursor_screen"] == "screen2"
    assert [f["screen"] for f in result["frames"]] == ["screen2"]
    assert result["frames"][0]["time_ms"] == 500
    assert result["frames"][0]["cursor"] == {"screen": "screen2", "x": 0, "y": 1}
    assert result["screen_bounds"]["screen2"]["x"] == 100


def test_frames_at_all_and_window(tmp_path: Path):
    paths = make_recording(tmp_path)
    result = runner.frames_at(paths, 300, screen="all", window_ms=260)
    assert [f["time_ms"] for f in result["frames"]] == [250, 250]
    assert [f["time_ms"] for f in result["window"]["screen1"]] == [250, 500]
    before_start = runner.frames_at(paths, -5, screen="screen1")
    assert before_start["frames"][0]["time_ms"] == 0
    with pytest.raises(runner.SessionError):
        runner.frames_at(paths, 0, screen="screen9")


def many_frames(root: Path, sidecar: dict, count: int = 41) -> SessionPaths:
    """A saved recording with ``count`` ticks (every 500 ms) of two screens."""
    paths = SessionPaths(root, "20260101-120000")
    final = paths.final_dir
    paths.frames_dir(final).mkdir(parents=True)
    paths.frames_info_dir(final).mkdir()
    for i in range(count):
        t = i * 500
        for sid in ("screen1", "screen2"):
            (paths.frames_dir(final) / frame_filename(t, sid, "png")).write_bytes(b"\x89PNG")
            data = {"time_ms": t, "screen": sid, "cursor": {"screen": "screen1", "x": 1, "y": 2}}
            (paths.frames_info_dir(final) / sidecar_filename(t, sid)).write_text(
                json.dumps({**data, **sidecar}), encoding="utf-8"
            )
    return paths


def chars(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def test_frames_window_lists_the_nearest_frames_and_says_so(tmp_path: Path):
    paths = many_frames(tmp_path, {})
    small = runner.frames_at(paths, 10_000, screen="all", window_ms=1000)
    assert small["truncated"] is False and "truncated_note" not in small
    assert [f["time_ms"] for f in small["window"]["screen1"]] == [9000, 9500, 10000, 10500, 11000]

    result = runner.frames_at(paths, 10_000, screen="all", window_ms=10_000)
    times = [f["time_ms"] for f in result["window"]["screen2"]]
    assert len(times) == runner.FRAMES_WINDOW_MAX // 2 == 12
    assert times == sorted(times) and 10_000 in times  # in time order, around at_ms
    assert all(abs(t - 10_000) <= 3000 for t in times)
    assert (result["window_total"], result["window_shown"]) == (82, 24)
    assert result["truncated"] is True and "(24 of 82)" in result["truncated_note"]
    assert chars(result) < runner.FRAMES_MAX_CHARS
    one = runner.frames_at(paths, 10_000, screen="screen1", window_ms=10_000)
    assert len(one["window"]["screen1"]) == 24 and one["window_total"] == 41


def test_frames_stay_small_with_many_shapes_and_a_long_copied_text(tmp_path: Path):
    shapes = [
        {"id": i, "tool": "line", "color": "#FF0000", "screen": "screen1", "path": [[i, i]] * 20}
        for i in range(1, 41)
    ]
    paths = many_frames(tmp_path, {"drawings": shapes})
    plain = runner.frames_at(paths, 10_000, screen="screen1")  # one frame: nothing to cut
    assert plain["truncated"] is False and len(plain["frames"][0]["drawings"]) == 40

    result = runner.frames_at(paths, 10_000, screen="screen1", window_ms=2000)
    assert result["truncated"] is True and chars(result) < runner.FRAMES_MAX_CHARS
    assert "window_total" not in result  # all nine frames of the window are listed
    assert "window frames list drawing_ids" in result["truncated_note"]
    entry = result["window"]["screen1"][0]
    assert entry["drawing_ids"] == list(range(1, 41)) and "drawings" not in entry
    assert len(result["frames"][0]["drawings"]) == 40  # the asked frame keeps its shapes

    # Both screens with 300 shapes and 64 KB of copied text: every step is needed.
    many = [dict(shape, id=i) for i, shape in enumerate(shapes * 8, start=1)][:300]
    huge = many_frames(tmp_path / "huge", {"drawings": many, "clipboard": "c" * 65_535})
    result = runner.frames_at(huge, 10_000, screen="all", window_ms=10_000)
    assert chars(result) < runner.FRAMES_MAX_CHARS
    slim = result["window"]["screen1"][0]
    assert set(slim) == {"screen", "time_ms", "image", "sidecar", "cursor"}
    frame = result["frames"][0]
    assert len(frame["drawing_ids"]) == 300 and "drawings" not in frame
    assert frame["clipboard_chars"] == 65_535
    assert len(frame["clipboard"]) == runner.FRAMES_CLIPBOARD_CHARS + 3
    for part in ("only time, image", "frames list drawing_ids", "clipboard is cut"):
        assert part in result["truncated_note"]


def test_session_summary_is_short_and_survives_a_broken_state(tmp_path: Path):
    paths = make_recording(tmp_path)
    summary = runner.session_summary(SessionPaths(tmp_path, paths.session_id, "default"))
    assert summary["status"] == "processed" and summary["folder"] == "default"
    assert summary["archived"] is False and "message" not in summary
    assert len(json.dumps(summary)) < 500
    paths.state_path.write_text("{ not json", encoding="utf-8")
    broken = runner.session_summary(paths)
    assert broken["status"] == "unknown" and "cannot read state file" in broken["message"]


def test_resolve_session_accepts_id_and_paths(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    paths = make_recording(tmp_path)
    assert runner.resolve_session(cfg, paths.session_id) == paths
    assert runner.resolve_session(cfg, str(paths.final_dir)) == paths
    assert runner.resolve_session(cfg, str(paths.work_dir)) == paths
    with pytest.raises(runner.SessionError):
        runner.resolve_session(cfg, "not-a-session")


def test_status_and_wait(tmp_path: Path, monkeypatch):
    paths = SessionPaths(tmp_path, "20260101-120001")
    paths.work_dir.mkdir(parents=True)
    state = SessionState(
        session_id=paths.session_id, status=Status.RECORDING, recording_folder=str(tmp_path), pid=1
    )
    write_state(paths.state_path, state)
    monkeypatch.setattr(runner, "pid_alive", lambda pid: True)
    data = runner.wait_for_session(paths, timeout_s=0)
    assert data["status"] == "recording" and data["timed_out"] is True
    # Window died: the status becomes failed.
    monkeypatch.setattr(runner, "pid_alive", lambda pid: False)
    data = runner.session_status(paths)
    assert data["status"] == "failed" and data["error_code"] == "WINDOW_EXITED"
    assert runner.wait_for_session(paths, timeout_s=0)["final"] is True


def test_active_session_detection(tmp_path: Path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    assert runner.active_session(cfg) is None
    paths = SessionPaths(tmp_path, "20260101-120002")
    paths.work_dir.mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id, status=Status.READY, recording_folder=str(tmp_path), pid=4
        ),
    )
    monkeypatch.setattr(runner, "pid_alive", lambda pid: pid == 4)
    found = runner.active_session(cfg)
    assert found is not None and found[0] == paths
    monkeypatch.setattr(runner, "pid_alive", lambda pid: False)
    assert runner.active_session(cfg) is None
