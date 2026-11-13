"""Lifetime of recordings: old ones are due for archiving (never deleted), Delete All."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from video_story.core.archive_state import (
    MAX_AUTOMATIC_ATTEMPTS,
    read_archive_state,
    record_archive_failure,
    request_archive,
    retry_archive,
)
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


def test_old_recordings_are_due_for_archive_never_deleted(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    old = _make_final(tmp_path, "20261001-090000")  # 37 days old
    recent = _make_final(tmp_path, "20261101-090000")  # 6 days old
    # Old session that failed in the work folder: no saved recording, nothing to archive.
    old_work = _make_work(tmp_path, "20260901-090000", Status.FAILED, pid=2**22)
    # Old but still open window (live pid): left alone.
    active = _make_work(tmp_path, "20260801-090000", Status.READY, pid=os.getpid())
    _make_final(tmp_path, "20260801-090000")
    # Already asked to be archived: not again.
    queued = _make_final(tmp_path, "20260701-090000")
    request_archive(queued)

    due = retention.recordings_due_for_archive(tmp_path, 30, now=now)

    assert [p.session_id for p in due] == ["20261001-090000"]
    assert old.final_dir.exists() and recent.final_dir.exists()  # nothing deleted
    assert old_work.work_dir.exists() and active.work_dir.exists()


def test_archive_boundary_uses_session_timestamp(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    _make_final(tmp_path, "20261008-120001")  # 29 days 23:59:59 old
    _make_final(tmp_path, "20261008-115959")  # 30 days 00:00:01 old
    due = retention.recordings_due_for_archive(tmp_path, 30, now=now)
    assert [p.session_id for p in due] == ["20261008-115959"]


def test_zero_days_means_never(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    _make_final(tmp_path, "20200101-090000")  # years old
    assert retention.recordings_due_for_archive(tmp_path, 0, now=now) == []
    assert not retention.older_than("20200101-090000", 0, now)
    assert retention.older_than("20200101-090000", 1, now)
    assert not retention.older_than("garbage", 30, now)


def _fail_archive(paths: SessionPaths, reason: str, times: int = 1) -> None:
    """``times`` archive attempts of ``paths`` that failed with nothing deleted."""
    request_archive(paths, reason=reason)
    for attempt in range(times):
        if attempt:
            assert retry_archive(paths) is not None
        assert record_archive_failure(paths, "simulated").failed


def test_failed_automatic_archive_is_retried_only_three_times(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    old = _make_final(tmp_path, "20261001-090000")
    assert not retention.archive_retry_due(old, 30, now)  # never asked: not a retry
    _fail_archive(old, "age")
    assert read_archive_state(old).failed_attempts == 1
    assert retention.archive_retry_due(old, 30, now)
    assert not retention.archive_retry_due(old, 0, now)  # automatic archiving is off
    assert not retention.archive_retry_due(old, 60, now)  # not older than the limit any more
    # A failed recording is not "due" either: only the retry rule brings it back.
    assert retention.recordings_due_for_archive(tmp_path, 30, now=now) == []
    assert retry_archive(old).failed_attempts == 1  # queued again, the counter stays
    assert not retention.archive_retry_due(old, 30, now)  # queued, not failed
    record_archive_failure(old, "again")
    assert retry_archive(old) is not None
    record_archive_failure(old, "and again")
    state = read_archive_state(old)
    assert state.failed and state.failed_attempts == MAX_AUTOMATIC_ATTEMPTS == 3
    assert not retention.archive_retry_due(old, 30, now)  # left alone from now on
    # Archive Video by hand starts over: queued, counter 0 - and then it is the user's.
    again = request_archive(old, reason="user")
    assert again.status == "queued" and again.failed_attempts == 0 and again.reason == "user"


def test_failed_archive_of_the_user_is_never_retried(tmp_path: Path):
    now = datetime(2026, 11, 7, 12, 0, 0)
    old = _make_final(tmp_path, "20261001-090000")
    _fail_archive(old, "user")
    assert not retention.archive_retry_due(old, 30, now)
    # Nor is one whose window still runs.
    active = _make_final(tmp_path, "20260801-090000")
    _make_work(tmp_path, "20260801-090000", Status.READY, pid=os.getpid())
    _fail_archive(active, "age")
    assert not retention.archive_retry_due(active, 30, now)


def test_cancelled_archive_is_never_due_nor_retried(tmp_path: Path):
    """The user cancelled it: a permanent exemption from the automatic archiving,
    whatever the limit; only Archive Video brings it back."""
    from video_story.export.archive import cancel_archive

    now = datetime(2026, 11, 7, 12, 0, 0)
    old = _make_final(tmp_path, "20261001-090000")
    _fail_archive(old, "age")
    assert retry_archive(old) is not None  # the automatic retry was queued ...
    assert cancel_archive(old, failed_attempts=1).cancelled  # ... and the user cancelled it
    for days in (1, 30, 365):
        assert retention.recordings_due_for_archive(tmp_path, days, now=now) == []
        assert not retention.archive_retry_due(old, days, now)
    state = read_archive_state(old)
    assert state.cancelled and state.failed_attempts == 1 and state.reason == "age"


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
    assert FrameRef("screen1", 1000, image, sidecar).to_dict()["drawing_now"] is None
    now = {"tool": "brush", "color": "#FF0000", "screen": "screen1", "started_ms": 900}
    sidecar.write_text(
        json.dumps({"time_ms": 1000, "screen": "screen1", "drawings": [], "drawing_now": now}),
        encoding="utf-8",
    )
    assert FrameRef("screen1", 1000, image, sidecar).to_dict()["drawing_now"] == now
