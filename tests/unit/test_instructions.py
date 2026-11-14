"""Master instructions: file, editor session events, describe and the manifest keypoints."""

from __future__ import annotations

import json
from pathlib import Path

from video_story.core.crop import Crop
from video_story.core.instructions import (
    MAX_INSTRUCTIONS_CHARS,
    normalize_instructions,
    read_instructions,
    write_instructions,
)
from video_story.core.manifest import Manifest, read_manifest, write_manifest
from video_story.core.screens import RawScreen, assign_screen_ids
from video_story.core.session import SessionPaths, SessionState, Status, read_state, write_state
from video_story.host import editor_session as es
from video_story.host.runner import FrameRef, describe_recording, session_status


def _editing_state(paths: SessionPaths, pid: int = 1) -> None:
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(paths.recording_folder),
            pid=pid,
            recorded_duration_ms=44075,
        ),
    )


def test_instructions_file_round_trip(tmp_path: Path):
    path = tmp_path / "instructions.txt"
    assert read_instructions(path) == ""
    stored = write_instructions(path, "  Create an implementation plan.\r\n\r\n")
    assert stored == "Create an implementation plan."
    assert path.read_text(encoding="utf-8") == "Create an implementation plan.\n"
    assert read_instructions(path) == "Create an implementation plan."
    assert write_instructions(path, "   \n") == "" and not path.exists()
    assert normalize_instructions(None) == ""
    assert len(normalize_instructions("x" * (MAX_INSTRUCTIONS_CHARS + 5))) == MAX_INSTRUCTIONS_CHARS


def test_editor_event_and_recording_session_carry_the_instructions(tmp_path: Path):
    path = tmp_path / "editor-abc.json"
    es.write_editor_event(path, "open")
    es.write_editor_event(
        path,
        "process",
        session_id="20261107-122147",
        crop=Crop(0, 5000),
        instructions=" Plan it \n",
    )
    result = es.wait_for_editor(path, timeout_s=0)
    assert result["event"] == "process" and result["instructions"] == "Plan it"
    es.write_editor_event(tmp_path / "editor-def.json", "process", session_id="x", crop=Crop(0, 9))
    assert es.wait_for_editor(tmp_path / "editor-def.json", timeout_s=0)["instructions"] == ""

    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    _editing_state(paths)
    es.finish_recording_session(paths, Crop(1000, 20000), instructions="Answer in Czech.")
    state = read_state(paths.state_path)
    assert state.status is Status.PROCESSED and state.instructions == "Answer in Czech."
    assert "instructions" in (state.message or "")
    assert state.to_dict()["instructions"] == "Answer in Czech."


def test_finish_on_another_recording_reads_the_sessions_own_instructions(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    write_instructions(paths.instructions_path(paths.final_dir), "Mine")
    _editing_state(paths)
    es.finish_recording_session(
        paths, Crop(1000, 2000), shown_session_id="20261107-130000", instructions="Not mine"
    )
    state = read_state(paths.state_path)
    assert state.instructions == "Mine" and state.crop == {"start_ms": 0, "end_ms": 44075}


def test_finish_without_instructions_reads_the_file_and_defaults_to_empty(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    _editing_state(paths)
    es.finish_recording_session(paths, Crop(0, 44075))
    assert read_state(paths.state_path).instructions == ""


def test_a_dead_editor_session_hands_no_instructions_over(tmp_path: Path):
    """Without Process nothing reaches the agent, typed instructions included (they
    stay in ``instructions.txt`` for the next time the recording is opened)."""
    paths = SessionPaths(tmp_path, "20261107-122147")
    paths.frames_dir(paths.final_dir).mkdir(parents=True)
    write_instructions(paths.instructions_path(paths.final_dir), "Keep it short")
    _editing_state(paths, pid=999_999_999)
    data = session_status(paths)
    assert data["status"] == "cancelled" and data["instructions"] is None
    assert paths.instructions_path(paths.final_dir).read_text(encoding="utf-8").strip() == (
        "Keep it short"
    )


def _manifest(session_id: str, keypoints: list[int]) -> Manifest:
    return Manifest(
        session_id=session_id,
        started_at="2026-01-01T12:00:00+01:00",
        finished_at=None,
        recorded_duration_ms=30_000,
        capture_interval_ms=500,
        image_format="png",
        jpeg_quality=None,
        screens=assign_screen_ids([RawScreen(0, 0, 320, 200, primary=True)]),
        keypoints=keypoints,
    )


def test_describe_reports_instructions_and_keypoints(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261107-122147")
    final = paths.final_dir
    paths.frames_dir(final).mkdir(parents=True)
    write_manifest(paths.manifest_path(final), _manifest(paths.session_id, [12_000, 4_000, 4_000]))
    data = describe_recording(paths)
    assert data["instructions"] == ""
    assert data["keypoints"] == [{"index": 1, "time_ms": 4000}, {"index": 2, "time_ms": 12000}]
    write_instructions(paths.instructions_path(final), "Summarise the bugs I show.")
    assert describe_recording(paths)["instructions"] == "Summarise the bugs I show."
    # The manifest round trip keeps the keypoints sorted and unique; old manifests have none.
    assert read_manifest(paths.manifest_path(final)).keypoints == [4000, 12000]
    raw = json.loads(paths.manifest_path(final).read_text(encoding="utf-8"))
    del raw["keypoints"]
    paths.manifest_path(final).write_text(json.dumps(raw), encoding="utf-8")
    assert read_manifest(paths.manifest_path(final)).keypoints == []
    assert describe_recording(paths)["keypoints"] == []


def test_frame_ref_reports_the_clipboard(tmp_path: Path):
    image = tmp_path / "0_500_screen1.png"
    sidecar = tmp_path / "0_500_screen1.json"
    image.write_bytes(b"x")
    sidecar.write_text(
        json.dumps({"cursor": {"screen": "screen1", "x": 1, "y": 2}, "clipboard": "hello"}),
        encoding="utf-8",
    )
    data = FrameRef("screen1", 500, image, sidecar).to_dict()
    assert data["clipboard"] == "hello"
    assert data["cursor"] == {"screen": "screen1", "x": 1, "y": 2}
    sidecar.write_text(json.dumps({"cursor": None}), encoding="utf-8")
    assert FrameRef("screen1", 500, image, sidecar).to_dict()["clipboard"] is None
