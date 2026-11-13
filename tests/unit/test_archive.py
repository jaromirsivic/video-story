"""Archiving a recording: one MP4 per screen, stills, deleting frames and audio, resume."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import pathlib  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from test_editor import NEWEST, make_recording  # noqa: E402

from video_story.core.archive_state import (  # noqa: E402
    ArchiveStatus,
    read_archive_state,
    request_archive,
)
from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor.recording import LoadedRecording  # noqa: E402
from video_story.export import ExportError, ExportResult, ffmpeg_export  # noqa: E402
from video_story.export import archive as archive_mod  # noqa: E402
from video_story.export.archive import (  # noqa: E402
    ArchiveError,
    archive_recording,
    cleanup_leftovers,
    screen_outputs,
)
from video_story.host import cli  # noqa: E402
from video_story.host.events import build_timeline  # noqa: E402
from video_story.host.runner import FrameIndex, describe_recording, frames_at  # noqa: E402
from video_story.transcribe.ffmpeg import find_ffmpeg  # noqa: E402


class FakeExporter:
    """Writes a stand-in MP4 and SRT instead of running FFmpeg; ``fail_on`` makes the
    export of that screen fail once."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def __call__(self, request, config, progress=None, cancel=None):
        self.calls.append(request.screen_id)
        if request.screen_id == self.fail_on:
            self.fail_on = None
            raise ExportError("FFMPEG_FAILED", "simulated failure")
        assert request.start_ms == 0 and request.end_ms is None and request.write_srt
        request.output.write_bytes(b"mp4 of " + request.screen_id.encode())
        request.output.with_suffix(".srt").write_text("1\n", encoding="utf-8")
        if progress is not None:
            progress(1.0, "done")


class FakeVerifier:
    """Stands in for the FFmpeg check of a written MP4; ``reject`` makes it fail for
    that file name."""

    def __init__(self, reject: str | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.reject = reject

    def __call__(self, video, duration_ms, config, cancel=None):
        self.calls.append((video.name, duration_ms))
        if video.name == self.reject:
            raise ExportError("OUTPUT_TRUNCATED", "the video is cut short: simulated")


@pytest.fixture(autouse=True)
def verified(monkeypatch) -> FakeVerifier:
    """The stand-in MP4s of :class:`FakeExporter` are no videos: the check that
    ``archive_recording`` runs by default is replaced (and records its calls)."""
    verifier = FakeVerifier()
    monkeypatch.setattr(archive_mod, "verify_video", verifier)
    return verifier


@pytest.fixture
def recording(tmp_path: Path) -> SessionPaths:
    return make_recording(tmp_path, NEWEST)


def _images(paths: SessionPaths, screen_id: str) -> list[Path]:
    folder = paths.frames_dir(paths.final_dir)
    return sorted(folder.glob(f"*_{screen_id}.png")) if folder.is_dir() else []


def test_archive_renders_every_screen_and_deletes_frames_and_audio(
    recording: SessionPaths, verified: FakeVerifier
):
    exporter = FakeExporter()
    reported: list[float] = []
    state = archive_recording(
        recording, Config(), progress=lambda f, _t: reported.append(f), exporter=exporter
    )
    final = recording.final_dir
    assert exporter.calls == ["screen1", "screen2"]
    # Every MP4 was checked against the length of the recording before anything went.
    assert verified.calls == [("screen1.mp4", 2500), ("screen2.mp4", 2500)]
    assert state.status is ArchiveStatus.ARCHIVED and state.screens_done == ["screen1", "screen2"]
    for screen_id in ("screen1", "screen2"):
        mp4, jpg, srt = screen_outputs(recording, screen_id)
        assert mp4.is_file() and srt.is_file() and jpg.is_file()
    # The still comes from the frame closest to the middle (1250 ms -> the 1000 ms frame).
    with Image.open(screen_outputs(recording, "screen1")[1]) as still:
        assert still.format == "JPEG" and still.size == (320, 200)
        r, g, b = still.convert("RGB").getpixel((5, 5))
        assert abs(r - 100) < 12 and abs(g - 80) < 12 and abs(b - 40) < 12
    assert not recording.frames_dir(final).exists() and not recording.audio_dir(final).exists()
    assert len(list(recording.frames_info_dir(final).glob("*.json"))) == 10  # sidecars stay
    assert recording.transcript_json_path(final).is_file()
    assert recording.manifest_path(final).is_file()
    assert reported[-1] == 1.0 and reported == sorted(reported)
    assert read_archive_state(recording).archived
    # Archiving again does nothing.
    assert archive_recording(recording, Config(), exporter=exporter).archived
    assert exporter.calls == ["screen1", "screen2"]


def test_archive_deletes_frames_screen_by_screen_and_resumes(recording: SessionPaths):
    failing = FakeExporter(fail_on="screen2")
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=failing)
    assert error.value.code == "FFMPEG_FAILED"
    state = read_archive_state(recording)
    assert state is not None and state.status is ArchiveStatus.ARCHIVING
    assert state.screens_done == ["screen1"] and "simulated" in (state.error or "")
    assert _images(recording, "screen1") == []  # done: its frames are gone at once
    assert len(_images(recording, "screen2")) == 5  # not done: kept
    assert recording.audio_path(recording.final_dir).is_file()  # needed for screen2

    # A stale partial output of the screen that failed is replaced on resume.
    mp4, _jpg, _srt = screen_outputs(recording, "screen2")
    mp4.write_bytes(b"half")
    resumed = FakeExporter()
    state = archive_recording(recording, Config(), exporter=resumed)
    assert resumed.calls == ["screen2"]  # screen1 is not rendered again
    assert state.archived and state.error is None
    assert mp4.read_bytes() == b"mp4 of screen2"


def test_archive_keeps_the_frames_of_a_video_that_fails_verification(recording: SessionPaths):
    """FFmpeg exits with 0 and writes an MP4 with full-length audio even when it cut the
    video at a frame it could not read: a written file alone must not delete anything."""
    exporter = FakeExporter()
    with pytest.raises(ArchiveError) as error:
        archive_recording(
            recording, Config(), exporter=exporter, verifier=FakeVerifier(reject="screen1.mp4")
        )
    assert error.value.code == "OUTPUT_TRUNCATED"
    assert "screen1.mp4" in str(error.value) and "nothing was deleted" in str(error.value)
    assert exporter.calls == ["screen1"]  # stopped at the first screen
    state = read_archive_state(recording)
    assert state is not None and state.status is ArchiveStatus.ARCHIVING
    assert state.screens_done == [] and "cut short" in (state.error or "")
    assert len(_images(recording, "screen1")) == 5 and len(_images(recording, "screen2")) == 5
    assert recording.audio_path(recording.final_dir).is_file()
    assert not screen_outputs(recording, "screen1")[1].exists()  # no still either

    # A later screen that fails keeps its own frames; the screen before it is done.
    with pytest.raises(ArchiveError):
        archive_recording(
            recording, Config(), exporter=exporter, verifier=FakeVerifier(reject="screen2.mp4")
        )
    assert read_archive_state(recording).screens_done == ["screen1"]
    assert _images(recording, "screen1") == [] and len(_images(recording, "screen2")) == 5
    assert recording.audio_path(recording.final_dir).is_file()

    # Once the video is whole (the frame is readable again) the archive finishes.
    state = archive_recording(recording, Config(), exporter=exporter, verifier=FakeVerifier())
    assert state.archived and state.error is None
    assert exporter.calls == ["screen1", "screen1", "screen2", "screen2"]
    assert not recording.frames_dir(recording.final_dir).exists()


def test_archive_verifies_the_length_the_exporter_rendered(recording: SessionPaths):
    """The verifier gets the duration the exporter reports and the caller's cancel check."""
    seen: list[tuple[str, int, object]] = []

    def exporter(request, config, progress=None, cancel=None):
        request.output.write_bytes(b"mp4")
        return ExportResult(
            output=request.output,
            srt=None,
            screen_id=request.screen_id,
            start_ms=0,
            end_ms=2400,
            duration_ms=2400,
            width=2,
            height=2,
            fps=24,
            frames_used=5,
            ffmpeg="ffmpeg",
        )

    def cancel() -> bool:
        return False

    archive_recording(
        recording,
        Config(),
        cancel=cancel,
        exporter=exporter,
        verifier=lambda video, ms, config, stop: seen.append((video.name, ms, stop)),
    )
    assert seen == [("screen1.mp4", 2400, cancel), ("screen2.mp4", 2400, cancel)]


def test_archive_verification_cancelled(recording: SessionPaths):
    def verifier(video, duration_ms, config, cancel=None):
        raise ExportError("CANCELLED", "export cancelled")

    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter(), verifier=verifier)
    assert error.value.code == "CANCELLED"
    assert read_archive_state(recording).screens_done == []
    assert len(_images(recording, "screen1")) == 5


def test_archive_checks_with_ffmpeg_by_default(recording: SessionPaths, monkeypatch):
    """Without a verifier the real check runs: a file that is no video deletes nothing."""
    if find_ffmpeg(Config()) is None:
        pytest.skip("ffmpeg not available")
    monkeypatch.setattr(archive_mod, "verify_video", ffmpeg_export.verify_video)
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter())
    assert error.value.code == "OUTPUT_UNVERIFIED"
    assert read_archive_state(recording).screens_done == []
    assert len(_images(recording, "screen1")) == 5
    assert recording.audio_path(recording.final_dir).is_file()


def test_archive_needs_a_complete_transcript(tmp_path: Path):
    paths = make_recording(tmp_path, NEWEST)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("cs", [Segment(0, 500, "x")], complete=False, transcribed_ms=500),
    )
    with pytest.raises(ArchiveError) as error:
        archive_recording(paths, Config(), exporter=FakeExporter())
    assert error.value.code == "NOT_TRANSCRIBED"
    assert len(_images(paths, "screen1")) == 5


def test_archive_cancelled_keeps_what_is_left(recording: SessionPaths):
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), cancel=lambda: True, exporter=FakeExporter())
    assert error.value.code == "CANCELLED"
    state = read_archive_state(recording)
    assert state is not None and not state.archived and state.screens_done == []
    assert len(_images(recording, "screen1")) == 5


def test_archive_refuses_a_nearly_full_disk(recording: SessionPaths, monkeypatch):
    monkeypatch.setattr(archive_mod, "_free_bytes", lambda _paths: 10)
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter())
    assert error.value.code == "LOW_DISK"
    assert len(_images(recording, "screen1")) == 5


def test_locked_files_stay_and_archiving_still_finishes(recording: SessionPaths, monkeypatch):
    """A file another program holds open cannot be deleted: it stays (listed in
    ``leftovers``), everything else is deleted, and a later cleanup removes it."""
    monkeypatch.setattr(archive_mod, "DELETE_DELAY_S", 0.0)
    locked = _images(recording, "screen2")[2]
    real_unlink = pathlib.Path.unlink

    def unlink(self, missing_ok=False):
        if self == locked:
            raise PermissionError(13, "in use by another process", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", unlink)
    state = archive_recording(recording, Config(), exporter=FakeExporter())
    assert state.archived and state.leftovers == [str(locked)]
    assert locked.is_file()
    assert [p.name for p in recording.frames_dir(recording.final_dir).iterdir()] == [locked.name]
    assert not recording.audio_dir(recording.final_dir).exists()

    monkeypatch.setattr(pathlib.Path, "unlink", real_unlink)  # the other program let go
    assert cleanup_leftovers(recording) == []
    assert not recording.frames_dir(recording.final_dir).exists()
    assert read_archive_state(recording).leftovers == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete open files")
def test_really_open_file_is_left_behind_on_windows(recording: SessionPaths, monkeypatch):
    monkeypatch.setattr(archive_mod, "DELETE_DELAY_S", 0.0)
    locked = _images(recording, "screen1")[0]
    with open(locked, "rb"):
        state = archive_recording(recording, Config(), exporter=FakeExporter())
        assert state.archived and str(locked) in state.leftovers and locked.exists()
    assert archive_recording(recording, Config(), exporter=FakeExporter()).leftovers == []
    assert not recording.frames_dir(recording.final_dir).exists()


def test_archived_recording_loads_stills_without_audio(recording: SessionPaths):
    archive_recording(recording, Config(), exporter=FakeExporter())
    loaded = LoadedRecording.load(recording)
    assert loaded.archived and loaded.audio is None
    assert loaded.frame_times("screen1") == [0] and loaded.frame_times("screen2") == [0]
    ref = loaded.frame_at("screen2", 2000)
    assert ref is not None and ref.image == screen_outputs(recording, "screen2")[1]
    assert loaded.cursor_at("screen1", 500) is None
    assert loaded.subtitle_at(1600) == "second line"  # the transcript is still there


def test_agent_sees_an_archived_recording(recording: SessionPaths):
    archive_recording(recording, Config(), exporter=FakeExporter())
    index = FrameIndex.of(recording, recording.final_dir)
    assert len(index.by_screen["screen1"]) == 5  # from the sidecars
    found = frames_at(recording, 1200, screen="screen1")
    assert found["frames"][0]["image"] is None and found["frames"][0]["cursor"] is not None
    assert found["archive"]["status"] == "archived"
    assert found["archive"]["videos"]["screen1"].endswith("screen1.mp4")
    timeline = build_timeline(recording)
    assert "SAY" in timeline["timeline"] and timeline["archive"]["status"] == "archived"
    assert describe_recording(recording)["archive"]["stills"]["screen2"].endswith("screen2.jpg")


def test_request_archive_queues_once(recording: SessionPaths):
    first = request_archive(recording, reason="age")
    assert first.status is ArchiveStatus.QUEUED and first.reason == "age"
    again = request_archive(recording, reason="user")
    assert again.reason == "age"  # an existing request is kept


def test_archive_stream_transcribes_first_then_archives(tmp_path: Path, monkeypatch, capsys):
    """The editor's worker: an incomplete transcript is completed (segments stored as
    they come), then the recording is archived; one JSON event per line."""
    paths = make_recording(tmp_path, NEWEST)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("en", [Segment(0, 1000, "hello")], complete=False, transcribed_ms=1000),
    )
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"recording_folder": str(tmp_path)}), encoding="utf-8")

    def fake_stream(paths_, cfg, *, base_dir, start_ms, emit, backend=None):
        assert start_ms == 1000
        emit({"event": "segment", "start_ms": 1500, "end_ms": 2400, "text": "and more"})
        emit({"event": "progress", "fraction": 1.0, "transcribed_ms": 2400})
        return {"event": "done", "language": "en", "model": "m", "backend": "fake"}

    monkeypatch.setattr("video_story.transcribe.pipeline.stream_transcription", fake_stream)
    exporter = FakeExporter()
    real = archive_mod.archive_recording
    monkeypatch.setattr(
        archive_mod,
        "archive_recording",
        lambda p, c, **kw: real(p, c, exporter=exporter, **kw),
    )
    code = cli.main(["--config", str(config_file), "archive-stream", str(paths.final_dir)])
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    phases = [e["phase"] for e in lines if e.get("event") == "phase"]
    assert phases == ["transcribing", "archiving"]
    assert lines[-1]["status"] == "archived" and lines[-1]["ok"] is True
    loaded = LoadedRecording.load(paths)
    assert loaded.transcript_complete and loaded.subtitle_at(2000) == "and more"
    assert loaded.archived and exporter.calls == ["screen1", "screen2"]
