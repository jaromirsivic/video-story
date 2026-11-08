"""Retention: automatic deletion of old recordings and Delete All."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from video_story.core.session import SessionPaths, SessionState, Status, write_state
from video_story.host import retention
from video_story.host.runner import FrameRef


def _make_final(root: Path, session_id: str, size: int = 10) -> SessionPaths:
    paths = SessionPaths(root, session_id)
    frames = paths.frames_dir(paths.final_dir)
    frames.mkdir(parents=True)
    (frames / "0_000_screen1.png").write_bytes(b"x" * size)
    paths.manifest_path(paths.final_dir).write_text("{}", encoding="utf-8")
    return paths


def _make_work(root: Path, session_id: str, status: Status, pid: int) -> SessionPaths:
    paths = SessionPaths(root, session_id)
    paths.create_work_dirs()
    write_state(
        paths.state_path,
        SessionState(session_id=session_id, status=status, recording_folder=str(root), pid=pid),
    )
    return paths


def test_session_started_at():
    assert retention.session_started_at("20261107-122147") == datetime(2026, 11, 7, 12, 21, 47)
    assert retention.session_started_at("20261107-122147-2") == datetime(2026, 11, 7, 12, 21, 47)
    assert retention.session_started_at("garbage") is None


def test_purge_deletes_only_old_inactive_sessions(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    old = _make_final(tmp_path, "20261001-090000")  # 37 days old
    recent = _make_final(tmp_path, "20261101-090000")  # 6 days old
    # Old session that failed in the work folder (final state, dead pid): deleted too.
    old_work = _make_work(tmp_path, "20260901-090000", Status.FAILED, pid=2**22)
    # Old but still open window (live pid): kept.
    active = _make_work(tmp_path, "20260801-090000", Status.READY, pid=os.getpid())

    deleted = retention.purge_old_recordings(tmp_path, 30, now=now)

    assert sorted(deleted) == ["20260901-090000", "20261001-090000"]
    assert not old.final_dir.exists() and not old_work.work_dir.exists()
    assert recent.final_dir.exists() and active.work_dir.exists()


def test_purge_boundary_uses_session_timestamp(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    just_inside = _make_final(tmp_path, "20261008-120001")  # 29 days 23:59:59 old
    just_outside = _make_final(tmp_path, "20261008-115959")  # 30 days 00:00:01 old
    deleted = retention.purge_old_recordings(tmp_path, 30, now=now)
    assert deleted == ["20261008-115959"]
    assert just_inside.final_dir.exists() and not just_outside.final_dir.exists()


def test_summarize_and_delete_all_keep_current_session(tmp_path: Path):
    a = _make_final(tmp_path, "20261001-090000", size=1000)
    b = _make_final(tmp_path, "20261002-090000", size=24)
    # The open window's session: a work folder with state.json and ui.log.
    current = _make_work(tmp_path, "20261107-120000", Status.READY, pid=os.getpid())
    (current.work_dir / "ui.log").write_text("log", encoding="utf-8")

    summary = retention.summarize_recordings(tmp_path, exclude_session_id=current.session_id)
    assert summary.count == 2 and summary.total_bytes == 1024 + 4  # two manifests "{}"
    assert summary.total_text == "1.0 KB"

    deleted = retention.delete_all_recordings(tmp_path, exclude_session_id=current.session_id)
    assert sorted(deleted) == [a.session_id, b.session_id]
    assert not a.final_dir.exists() and not b.final_dir.exists()
    assert current.state_path.exists() and (current.work_dir / "ui.log").exists()
    assert retention.summarize_recordings(tmp_path, current.session_id).count == 0


def test_frame_ref_exposes_drawings(tmp_path: Path):
    image = tmp_path / "1_000_screen1.png"
    sidecar = tmp_path / "1_000_screen1.json"
    image.write_bytes(b"x")
    drawing = {
        "tool": "rectangle",
        "color": "#FF0000",
        "screen": "screen1",
        "stroke_width": 6,
        "from": {"screen": "screen1", "x": 10, "y": 20},
        "to": {"screen": "screen1", "x": 110, "y": 220},
    }
    sidecar.write_text(
        json.dumps({"time_ms": 1000, "screen": "screen1", "cursor": None, "drawings": [drawing]}),
        encoding="utf-8",
    )
    data = FrameRef("screen1", 1000, image, sidecar).to_dict()
    assert data["drawings"] == [drawing] and data["cursor"] is None
    # Sidecars written before drawings existed still work.
    sidecar.write_text(json.dumps({"time_ms": 1000, "screen": "screen1"}), encoding="utf-8")
    assert FrameRef("screen1", 1000, image, sidecar).to_dict()["drawings"] == []
    assert FrameRef("screen1", 1000, image, sidecar).to_dict()["zoom"] == []
    zoom = {"screen": "screen1", "factor": 2.0, "x": 100, "y": 50, "width": 400, "height": 300}
    sidecar.write_text(
        json.dumps({"time_ms": 1000, "screen": "screen1", "drawings": [], "zoom": [zoom]}),
        encoding="utf-8",
    )
    assert FrameRef("screen1", 1000, image, sidecar).to_dict()["zoom"] == [zoom]
