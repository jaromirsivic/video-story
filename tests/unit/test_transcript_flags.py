"""Incomplete transcripts, hallucinated credits and what the host reports about them."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from video_story.core.manifest import Manifest, write_manifest
from video_story.core.screens import Screen
from video_story.core.session import (
    SessionPaths,
    SessionState,
    Status,
    now_iso,
    read_state,
    write_state,
)
from video_story.core.transcript import (
    Segment,
    Transcript,
    drop_credit_hallucinations,
    is_credit_hallucination,
    read_transcript,
    transcript_status,
    write_transcript,
)
from video_story.host import editor_session as es
from video_story.host.runner import describe_recording, session_status


def test_transcript_flags_round_trip_and_legacy_default(tmp_path: Path):
    partial = Transcript("cs", [Segment(0, 1000, "a")], complete=False, transcribed_ms=1000)
    txt, js = tmp_path / "t.txt", tmp_path / "t.json"
    write_transcript(txt, js, partial)
    data = json.loads(js.read_text(encoding="utf-8"))
    assert data["complete"] is False and data["transcribed_ms"] == 1000
    stored = read_transcript(js)
    assert stored.complete is False and stored.transcribed_ms == 1000
    status = transcript_status(js)
    assert status.exists and not status.complete and status.pending
    assert status.transcribed_ms == 1000

    # Files written before the flag existed are complete.
    data.pop("complete")
    data.pop("transcribed_ms")
    js.write_text(json.dumps(data), encoding="utf-8")
    assert read_transcript(js).complete is True
    assert transcript_status(js).complete is True

    missing = transcript_status(tmp_path / "nope.json")
    assert not missing.exists and missing.pending and missing.transcribed_ms == 0
    js.write_text("not json", encoding="utf-8")
    assert transcript_status(js).pending  # unreadable: transcribe it again


@pytest.mark.parametrize(
    "text",
    [
        "Titulky vytvořil JohnyX",
        " titulky vytvořila Anna. ",
        "Titulky: JohnyX",
        "Překlad a titulky: JohnyX",
        "Subtitles by the Amara.org community",
        "Subtitled by Amara",
        "Thank you for watching!",
        "Thanks for watching",
        "Untertitel von Stephanie Geiges",
        "Sous-titres par Jean",
        "Subtítulos realizados por la comunidad",
        "Napisy stworzone przez społeczność",
    ],
)
def test_credit_hallucinations_are_recognised(text):
    assert is_credit_hallucination(text)


@pytest.mark.parametrize(
    "text",
    [
        "Když edituje nějaký titulek, tak by neměl mít možnost upravovat datum a čas.",
        "Někdy se v titulcích zobrazí nesmysl.",
        "Add subtitles to the preview by default.",
        "Thank you, that is all for today.",
        "Překlad tohoto textu do angličtiny prosím.",
        "",
        "   ",
    ],
)
def test_normal_speech_about_subtitles_is_kept(text):
    assert not is_credit_hallucination(text)


def test_drop_credit_hallucinations_keeps_order():
    segments = [
        Segment(0, 1000, "first"),
        Segment(1000, 2000, "Titulky vytvořil JohnyX"),
        Segment(2000, 3000, "last"),
    ]
    assert drop_credit_hallucinations(segments) == [segments[0], segments[2]]


def test_update_recording_session_progress_only_while_editing(tmp_path: Path):
    paths = SessionPaths(tmp_path, "20261109-173745")
    es.update_recording_session_progress(paths, 0.5, "x")  # no state file: nothing
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(tmp_path),
            pid=os.getpid(),
        ),
    )
    es.update_recording_session_progress(paths, 0.42, "transcribing 42 %")
    state = read_state(paths.state_path)
    assert state.progress == pytest.approx(0.42) and state.message == "transcribing 42 %"
    assert state.status is Status.EDITING
    state.status = Status.PROCESSED
    write_state(paths.state_path, state)
    es.update_recording_session_progress(paths, 0.9, "late")
    assert read_state(paths.state_path).message == "transcribing 42 %"  # final: untouched


def _recording(tmp_path: Path, session_id: str = "20261109-173745") -> SessionPaths:
    paths = SessionPaths(tmp_path, session_id)
    final = paths.final_dir
    paths.frames_dir(final).mkdir(parents=True)
    write_manifest(
        paths.manifest_path(final),
        Manifest(
            session_id=session_id,
            started_at=now_iso(),
            finished_at=None,
            recorded_duration_ms=6000,
            capture_interval_ms=500,
            image_format="jpeg",
            jpeg_quality=70,
            screens=[Screen("screen1", 1, 0, 0, 320, 200, True, 1.0)],
        ),
    )
    return paths


def test_dead_editor_with_an_incomplete_transcript_tells_the_agent(tmp_path: Path):
    paths = _recording(tmp_path)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("cs", [Segment(0, 1000, "a")], complete=False, transcribed_ms=1000),
    )
    write_state(
        paths.state_path,
        SessionState(
            session_id=paths.session_id,
            status=Status.EDITING,
            recording_folder=str(tmp_path),
            pid=2**22 + 12345,
            recorded_duration_ms=6000,
            recording_dir=str(paths.final_dir),
        ),
    )
    data = session_status(paths)
    assert data["status"] == "processed" and data["crop"] == {"start_ms": 0, "end_ms": 6000}
    assert "incomplete" in data["message"] and "video-story transcribe" in data["message"]

    described = describe_recording(paths)
    assert described["transcript_complete"] is False
    assert described["transcribed_ms"] == 1000 and "incomplete" in described["transcript_note"]


def test_describe_reports_a_complete_transcript(tmp_path: Path):
    paths = _recording(tmp_path)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("cs", [Segment(0, 1000, "a")]),
    )
    described = describe_recording(paths)
    assert described["transcript_complete"] is True and "transcript_note" not in described
