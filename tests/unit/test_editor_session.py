"""Crop persistence, editor session events, attach requests and the editing state."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from video_story.core.config import Config
from video_story.core.crop import Crop, read_crop, write_crop
from video_story.core.session import SessionPaths, SessionState, Status, read_state, write_state
from video_story.core.transcript import Segment, Transcript
from video_story.host import editor_session as es


def test_crop_round_trip_and_clamping(tmp_path: Path):
    path = tmp_path / "crop.json"
    assert read_crop(path, 5000) == Crop(0, 5000)
    write_crop(path, Crop(1000, 4000))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["start_ms"] == 1000 and data["end_ms"] == 4000 and "updated_at" in data
    assert read_crop(path, 5000) == Crop(1000, 4000)
    assert read_crop(path, 3000) == Crop(1000, 3000)  # clamped to a shorter recording
    assert read_crop(path, 500) == Crop(0, 500)  # inverted after clamping -> whole
    path.write_text("not json", encoding="utf-8")
    assert read_crop(path, 5000) == Crop(0, 5000)
    assert Crop(2000, 1000).clamped(5000) == Crop(0, 5000)
    assert Crop(-5, 99999).clamped(5000) == Crop(0, 5000)
    assert Crop(100, 200).duration_ms == 100


def test_transcript_segments_in_range():
    t = Transcript(
        language="cs",
        segments=[Segment(0, 1000, "a"), Segment(1000, 3000, "b"), Segment(4000, 5000, "c")],
    )
    assert [s.text for s in t.segments_in_range(1500, 4000)] == ["b", "c"]
    assert [s.text for s in t.segments_in_range(3500, 3900)] == []
    assert [s.text for s in t.segments_in_range(0, 5000)] == ["a", "b", "c"]


def test_editor_session_events_and_wait(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    path = es.new_editor_session(cfg)
    assert path.parent == tmp_path / ".work" and path.name.startswith("editor-")
    assert es.read_editor_event(path)["event"] == "open"
    timed = es.wait_for_editor(path, timeout_s=0)
    assert timed["timed_out"] is True and timed["event"] == "open"

    def press_process() -> None:
        time.sleep(0.3)
        es.write_editor_event(path, "process", session_id="20261107-122147", crop=Crop(5000, 20000))

    threading.Thread(target=press_process, daemon=True).start()
    result = es.wait_for_editor(path, timeout_s=10)
    assert result["event"] == "process" and result["timed_out"] is False
    assert result["session_id"] == "20261107-122147"
    assert result["crop"] == {"start_ms": 5000, "end_ms": 20000}
    es.write_editor_event(path, "closed")
    assert es.wait_for_editor(path, timeout_s=0)["event"] == "closed"
    path.unlink()
    assert es.wait_for_editor(path, timeout_s=0)["event"] == "closed"


def test_attach_requests_are_taken_oldest_first(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    assert es.take_attach_requests(cfg) == []
    session_a = es.new_editor_session(cfg)
    session_b = es.new_editor_session(cfg)
    es.write_attach_request(cfg, session_a, "20261001-090000")
    time.sleep(0.002)
    es.write_attach_request(cfg, session_b, None, recording_session="20261002-090000")
    requests = es.take_attach_requests(cfg)
    assert [r.session_id for r in requests] == ["20261001-090000", None]
    assert requests[0].editor_session == session_a
    assert requests[1].recording_session == "20261002-090000"
    assert es.take_attach_requests(cfg) == []  # consumed
    assert not any(es.requests_dir(cfg).iterdir())


def test_recording_session_finish_and_cancel(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(tmp_path),
            pid=1,
            recorded_duration_ms=44075,
        ),
    )
    es.adopt_recording_session(paths)
    adopted = read_state(paths.state_path)
    assert adopted.status is Status.EDITING and adopted.pid != 1 and not adopted.status.final

    es.finish_recording_session(paths, Crop(5000, 20000))
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED and state.status.final
    assert state.crop == {"start_ms": 5000, "end_ms": 20000}
    assert state.processed_via == "editor"
    assert state.frames_dir == str(paths.frames_dir(paths.final_dir))
    assert state.to_dict()["crop"]["end_ms"] == 20000

    # Closing without Process on an editing session cancels it; a final one is untouched.
    es.cancel_recording_session(paths)
    assert read_state(paths.state_path).status is Status.PROCESSED
    write_state(
        paths.state_path,
        SessionState(session_id=paths.session_id, status=Status.EDITING, recording_folder="x"),
    )
    es.cancel_recording_session(paths)
    state = read_state(paths.state_path)
    assert state.status is Status.CANCELLED and "without Process" in (state.message or "")
    assert state.recording_dir == str(paths.final_dir)


def test_describe_reports_crop_and_transcript_in_range(tmp_path: Path):
    from video_story.core.manifest import Manifest, write_manifest
    from video_story.core.screens import Screen
    from video_story.core.session import now_iso
    from video_story.core.transcript import write_transcript
    from video_story.host.runner import describe_recording

    paths = SessionPaths(tmp_path, "20261107-122147")
    final = paths.final_dir
    paths.frames_dir(final).mkdir(parents=True)
    write_manifest(
        paths.manifest_path(final),
        Manifest(
            session_id=paths.session_id,
            started_at=now_iso(),
            finished_at=now_iso(),
            recorded_duration_ms=6000,
            capture_interval_ms=500,
            image_format="jpeg",
            jpeg_quality=70,
            screens=[Screen("screen1", 1, 0, 0, 320, 200, True, 1.0)],
        ),
    )
    transcript = Transcript(
        "cs",
        [Segment(0, 1000, "first"), Segment(2000, 3000, "second"), Segment(5000, 6000, "third")],
    )
    write_transcript(
        paths.transcript_txt_path(final), paths.transcript_json_path(final), transcript
    )

    whole = describe_recording(paths)
    assert whole["crop"] == {"start_ms": 0, "end_ms": 6000}
    assert whole["transcript_in_range"].count("\n") == 3

    write_crop(paths.crop_path(final), Crop(1500, 4000))
    cropped = describe_recording(paths)
    assert cropped["crop"] == {"start_ms": 1500, "end_ms": 4000}
    assert cropped["transcript_in_range"] == "[00:00:02.000 --> 00:00:03.000] second\n"
    assert cropped["transcript_in_range_inline"] is True
    assert (cropped["transcript_in_range_lines"], cropped["transcript_in_range_chars"]) == (1, 39)
    # The whole transcript is not repeated: describe names its files instead.
    assert "transcript" not in cropped
    assert Path(cropped["transcript_txt"]).read_text(encoding="utf-8").count("\n") == 3

    # A transcript longer than the limit is left out with a note (the size stays bounded).
    long = describe_recording(paths, inline_chars=38)
    assert "transcript_in_range" not in long and long["transcript_in_range_inline"] is False
    assert "transcript_txt" in long["transcript_in_range_note"]
    assert "timeline" in long["transcript_in_range_note"]
    assert "transcript_in_range" not in describe_recording(paths, inline_chars=0)


# Review fixes: liveness, stale requests, guards ------------------------------------


def test_wait_for_editor_ends_when_the_editor_process_is_dead(tmp_path: Path):
    cfg = Config(recording_folder=tmp_path)
    path = es.new_editor_session(cfg)
    es.write_editor_event(path, "open", pid=os.getpid())
    assert es.wait_for_editor(path, timeout_s=0)["event"] == "open"  # we are alive
    es.write_editor_event(path, "open", pid=2**22 + 12345)  # no such process
    result = es.wait_for_editor(path, timeout_s=0)
    assert result["event"] == "closed" and "ended without Process" in result["message"]


def test_stale_or_served_attach_requests_are_dropped(tmp_path: Path, monkeypatch):
    cfg = Config(recording_folder=tmp_path)
    fresh = es.new_editor_session(cfg)
    es.write_attach_request(cfg, fresh, "20261001-090000")
    old_session = es.new_editor_session(cfg)
    old_request = es.write_attach_request(cfg, old_session, "20261002-090000")
    past = time.time() - es.ATTACH_REQUEST_MAX_AGE_S - 5
    os.utime(old_request, (past, past))
    closed_session = es.new_editor_session(cfg)
    es.write_editor_event(closed_session, "closed")
    es.write_attach_request(cfg, closed_session, "20261003-090000")
    requests = es.take_attach_requests(cfg)
    assert [r.session_id for r in requests] == ["20261001-090000"]
    assert not any(es.requests_dir(cfg).iterdir())  # all three files consumed


def test_finish_recording_session_requires_editing_state(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(session_id=paths.session_id, status=Status.CANCELLED, recording_folder="x"),
    )
    es.finish_recording_session(paths, Crop(0, 1000))
    assert read_state(paths.state_path).status is Status.CANCELLED  # a final state stays final


def test_session_status_turns_a_dead_editor_into_cancelled(tmp_path: Path):
    """Nobody pressed Process: a crashed editor means the same as a closed one."""
    from video_story.host.runner import active_session, session_status

    cfg = Config(recording_folder=tmp_path)
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(tmp_path),
            pid=os.getpid(),
            recorded_duration_ms=44075,
            recording_dir=str(paths.final_dir),
        ),
    )
    assert active_session(cfg) is None  # a new recording may start meanwhile
    assert active_session(cfg, include_editing=True)[0] == paths
    assert session_status(paths)["status"] == "editing"  # alive: still waiting
    state = read_state(paths.state_path)
    state.pid = 2**22 + 12345
    write_state(paths.state_path, state)
    data = session_status(paths)
    assert data["status"] == "cancelled" and data["final"] is True
    assert data["crop"] is None  # nothing is handed to the agent
    assert "without Process" in data["message"] and "nothing should be done" in data["message"]
    assert data["recording_dir"] == str(paths.final_dir)  # the recording is kept


def test_crop_with_unknown_duration():
    assert Crop(9000, 3000).clamped(0) == Crop(0, 3000)
    assert Crop(1000, 3000).clamped(0) == Crop(1000, 3000)
    assert read_crop(Path("nonexistent/crop.json"), 0) == Crop(0, 0)
