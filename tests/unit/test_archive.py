"""Archiving a recording: every audio track as AAC, one MP4 per screen, stills, deleting
frames and audio, resume."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import wave  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from test_editor import NEWEST, make_recording  # noqa: E402

from video_story.core.archive_state import (  # noqa: E402
    ArchiveState,
    ArchiveStatus,
    archive_state_path,
    read_archive_state,
    record_archive_failure,
    request_archive,
    retry_archive,
    withdraw_archive_request,
    write_archive_state,
)
from video_story.core.audio_mix import TrackSelection, write_selection  # noqa: E402
from video_story.core.config import Config  # noqa: E402
from video_story.core.session import SessionPaths  # noqa: E402
from video_story.core.transcript import Segment, Transcript, write_transcript  # noqa: E402
from video_story.editor.recording import LoadedRecording  # noqa: E402
from video_story.export import ExportError, ExportResult, ffmpeg_export  # noqa: E402
from video_story.export import archive as archive_mod  # noqa: E402
from video_story.export.archive import (  # noqa: E402
    AUDIO_PROGRESS_SHARE,
    ArchiveError,
    archive_recording,
    audio_output,
    cancel_archive,
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


class FakeAudio:
    """Stands in for the FFmpeg encode of an audio track (``encode``) and for the check
    of the written file (``verify``); ``fail_on`` makes the encode of that WAV fail
    once, ``reject`` the check of the track whose file name starts with it."""

    def __init__(self, fail_on: str | None = None, reject: str | None = None) -> None:
        self.encoded: list[str] = []
        self.checked: list[tuple[str, int]] = []
        self.fail_on = fail_on
        self.reject = reject

    def encode(self, source, output, config, progress=None, cancel=None):
        self.encoded.append(source.name)
        if source.name == self.fail_on:
            self.fail_on = None
            raise ExportError("FFMPEG_FAILED", "simulated audio failure")
        output.write_bytes(b"aac of " + source.name.encode())
        if progress is not None:
            progress(1.0, "done")

    def verify(self, audio, duration_ms, config, cancel=None):
        self.checked.append((audio.name, duration_ms))
        if self.reject is not None and audio.name.startswith(self.reject):
            raise ExportError("OUTPUT_TRUNCATED", "the audio is cut short: simulated")


@pytest.fixture(autouse=True)
def verified(monkeypatch) -> FakeVerifier:
    """The stand-in MP4s of :class:`FakeExporter` are no videos: the check that
    ``archive_recording`` runs by default is replaced (and records its calls)."""
    verifier = FakeVerifier()
    monkeypatch.setattr(archive_mod, "verify_video", verifier)
    return verifier


@pytest.fixture(autouse=True)
def audio(monkeypatch) -> FakeAudio:
    """No FFmpeg for the audio tracks either: the encoder and the check that
    ``archive_recording`` uses by default are replaced (and record their calls)."""
    fake = FakeAudio()
    monkeypatch.setattr(archive_mod, "encode_audio", fake.encode)
    monkeypatch.setattr(archive_mod, "verify_audio", fake.verify)
    return fake


@pytest.fixture
def recording(tmp_path: Path) -> SessionPaths:
    return make_recording(tmp_path, NEWEST)


def _add_computer_track(paths: SessionPaths, ms: int = 1000, rate: int = 48000) -> Path:
    """A second audio track (``audio/computer.wav``) of ``ms`` milliseconds."""
    path = paths.computer_audio_path(paths.final_dir)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(2 * rate * ms // 1000))
    return path


def _archive_names(paths: SessionPaths) -> list[str]:
    return sorted(p.name for p in paths.archive_dir(paths.final_dir).iterdir())


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
    # Nothing was deleted: the recording is given back (failed), not left locked.
    assert state is not None and state.status is ArchiveStatus.FAILED
    assert state.failed_attempts == 1
    assert state.screens_done == [] and "cut short" in (state.error or "")
    assert len(_images(recording, "screen1")) == 5 and len(_images(recording, "screen2")) == 5
    assert recording.audio_path(recording.final_dir).is_file()
    assert not screen_outputs(recording, "screen1")[1].exists()  # no still either

    # A later screen that fails keeps its own frames; the screen before it is done,
    # its frames are gone: the archive stays locked (archiving) and is resumed.
    with pytest.raises(ArchiveError):
        archive_recording(
            recording, Config(), exporter=exporter, verifier=FakeVerifier(reject="screen2.mp4")
        )
    state = read_archive_state(recording)
    assert state.screens_done == ["screen1"] and state.status is ArchiveStatus.ARCHIVING
    assert state.failed_attempts == 1 and "cut short" in (state.error or "")
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


# Audio tracks -----------------------------------------------------------------------


def test_every_audio_track_is_archived_whatever_is_switched_on(
    recording: SessionPaths, audio: FakeAudio
):
    """The MP4 holds only the tracks switched on in the editor; ``audio/`` is deleted.
    So each track goes to ``archive/`` on its own first, the muted one included."""
    final = recording.final_dir
    _add_computer_track(recording, ms=1000)
    write_selection(recording, final, TrackSelection(microphone=True, computer=False))
    seen_by_exporter: list[list[str]] = []
    exporter = FakeExporter()

    def export(request, config, progress=None, cancel=None):
        seen_by_exporter.append(_archive_names(recording))
        return exporter(request, config, progress, cancel)

    reported: list[tuple[float, str]] = []
    state = archive_recording(
        recording, Config(), progress=lambda f, t: reported.append((f, t)), exporter=export
    )

    assert audio.encoded == ["audio.wav", "computer.wav"]
    # Each file was checked under its partial name against the length of its WAV.
    assert audio.checked == [("audio.part.m4a", 2500), ("computer.part.m4a", 1000)]
    assert state.archived and state.audio == {"microphone": "audio.m4a", "computer": "computer.m4a"}
    assert audio_output(recording, "microphone").read_bytes() == b"aac of audio.wav"
    assert audio_output(recording, "computer").read_bytes() == b"aac of computer.wav"
    selection = json.loads((recording.archive_dir(final) / "tracks.json").read_text("utf-8"))
    assert selection == {"microphone": True, "computer": False}
    # The audio was there (verified, no partial file) before the first screen was rendered.
    assert seen_by_exporter[0] == ["archive.json", "audio.m4a", "computer.m4a", "tracks.json"]
    assert not recording.audio_dir(final).exists()
    assert not [name for name in _archive_names(recording) if ".part" in name or ".tmp" in name]
    # The first slice of the progress belongs to the audio.
    fractions = [fraction for fraction, _text in reported]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    audio_part = [fraction for fraction, text in reported if text == "archiving audio"]
    assert audio_part and max(audio_part) <= AUDIO_PROGRESS_SHARE
    assert reported[len(audio_part)] == (AUDIO_PROGRESS_SHARE, "archiving screen1")
    # An agent finds the tracks.
    info = describe_recording(recording)["archive"]
    assert info["audio"] == {
        "microphone": str(audio_output(recording, "microphone")),
        "computer": str(audio_output(recording, "computer")),
    }
    assert info["tracks_file"] == str(recording.archive_dir(final) / "tracks.json")


def test_both_tracks_switched_off_are_archived_too(recording: SessionPaths, audio: FakeAudio):
    _add_computer_track(recording)
    write_selection(recording, recording.final_dir, TrackSelection(False, False))
    state = archive_recording(recording, Config(), exporter=FakeExporter())
    assert state.archived and sorted(state.audio) == ["computer", "microphone"]
    assert audio_output(recording, "microphone").is_file()
    assert audio_output(recording, "computer").is_file()


def test_audio_that_fails_the_check_keeps_everything(recording: SessionPaths, audio: FakeAudio):
    """An ``.m4a`` shorter than its WAV: no screen is rendered, nothing is deleted, the
    file does not get its name; the track that was fine is not converted again."""
    final = recording.final_dir
    computer = _add_computer_track(recording)
    exporter = FakeExporter()
    rejecting = FakeAudio(reject="computer")
    with pytest.raises(ArchiveError) as error:
        archive_recording(
            recording,
            Config(),
            exporter=exporter,
            audio_encoder=rejecting.encode,
            audio_verifier=rejecting.verify,
        )
    assert error.value.code == "OUTPUT_TRUNCATED"
    assert "computer.m4a is not a complete copy of computer.wav" in str(error.value)
    assert "nothing was deleted" in str(error.value)
    assert exporter.calls == []
    state = read_archive_state(recording)
    assert state is not None and state.status is ArchiveStatus.FAILED  # given back
    assert state.audio == {"microphone": "audio.m4a"} and state.screens_done == []
    assert "cut short" in (state.error or "")
    assert recording.audio_path(final).is_file() and computer.is_file()
    assert len(_images(recording, "screen1")) == 5 and len(_images(recording, "screen2")) == 5
    assert _archive_names(recording) == ["archive.json", "audio.m4a"]  # no partial file

    state = archive_recording(recording, Config(), exporter=exporter)
    assert audio.encoded == ["computer.wav"]  # the microphone is not converted twice
    assert state.archived and state.error is None
    assert state.audio == {"microphone": "audio.m4a", "computer": "computer.m4a"}
    assert not recording.audio_dir(final).exists()


def test_audio_that_cannot_be_encoded_or_read_keeps_everything(
    recording: SessionPaths, audio: FakeAudio
):
    final = recording.final_dir
    audio.fail_on = "audio.wav"
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter())
    assert error.value.code == "FFMPEG_FAILED"
    state = read_archive_state(recording)
    assert state.audio == {} and "simulated audio failure" in (state.error or "")
    assert recording.audio_path(final).is_file() and len(_images(recording, "screen1")) == 5
    assert _archive_names(recording) == ["archive.json"]

    # A track whose WAV header cannot be read has no length to compare with.
    recording.computer_audio_path(final).write_bytes(b"not a wav file")
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter())
    assert error.value.code == "AUDIO_UNREADABLE" and "computer.wav" in str(error.value)
    state = read_archive_state(recording)
    assert state.audio == {"microphone": "audio.m4a"} and state.screens_done == []
    assert recording.computer_audio_path(final).is_file()
    assert len(_images(recording, "screen1")) == 5


def test_resumed_archive_does_not_convert_the_audio_again(
    recording: SessionPaths, audio: FakeAudio
):
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=FakeExporter(fail_on="screen2"))
    assert audio.encoded == ["audio.wav"]
    assert read_archive_state(recording).audio == {"microphone": "audio.m4a"}
    assert recording.audio_path(recording.final_dir).is_file()  # screen2 still needs it
    state = archive_recording(recording, Config(), exporter=FakeExporter())
    assert state.archived and audio.encoded == ["audio.wav"]
    assert audio_output(recording, "microphone").is_file()
    assert not recording.audio_dir(recording.final_dir).exists()


def test_archive_left_half_way_by_an_older_version_converts_the_audio(
    recording: SessionPaths, audio: FakeAudio
):
    """``archive.json`` written before the tracks were archived has no ``audio``: such a
    file loads, and the audio is converted before ``audio/`` goes."""
    final = recording.final_dir
    for image in _images(recording, "screen1"):
        image.unlink()  # screen1 was done: its frames are gone
    old = {
        "status": "archiving",
        "requested_at": "2026-11-01T10:00:00.000+01:00",
        "reason": "age",
        "screens": ["screen1", "screen2"],
        "screens_done": ["screen1"],
        "started_at": "2026-11-01T10:00:01.000+01:00",
        "finished_at": None,
        "leftovers": [],
        "error": "simulated failure",
    }
    path = archive_state_path(recording, final)
    path.parent.mkdir()
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = read_archive_state(recording)
    assert loaded is not None and loaded.audio == {} and loaded.screens_done == ["screen1"]

    exporter = FakeExporter()
    state = archive_recording(recording, Config(), exporter=exporter)
    assert audio.encoded == ["audio.wav"] and exporter.calls == ["screen2"]
    assert state.archived and state.audio == {"microphone": "audio.m4a"}
    assert audio_output(recording, "microphone").is_file()
    assert not recording.audio_dir(final).exists()


def test_recording_archived_by_an_older_version_stays_as_it_is(
    recording: SessionPaths, audio: FakeAudio
):
    final = recording.final_dir
    archive_recording(recording, Config(), exporter=FakeExporter())
    audio_output(recording, "microphone").unlink()
    path = archive_state_path(recording, final)
    data = json.loads(path.read_text("utf-8"))
    del data["audio"]  # as the older version wrote it
    path.write_text(json.dumps(data), encoding="utf-8")
    audio.encoded.clear()

    exporter = FakeExporter()
    state = archive_recording(recording, Config(), exporter=exporter)
    assert state.archived and state.audio == {}
    assert audio.encoded == [] and exporter.calls == []
    info = describe_recording(recording)["archive"]
    assert info["audio"] == {} and "tracks_file" not in info


def test_recording_without_audio_archives_as_before(tmp_path: Path, audio: FakeAudio):
    paths = make_recording(tmp_path, NEWEST)
    final = paths.final_dir
    paths.audio_path(final).unlink()
    reported: list[float] = []
    state = archive_recording(
        paths, Config(), progress=lambda f, _t: reported.append(f), exporter=FakeExporter()
    )
    assert state.archived and state.audio == {}
    assert audio.encoded == [] and audio.checked == []
    assert reported[0] == 0.0  # no slice of the progress for audio that is not there
    assert not [name for name in _archive_names(paths) if name.endswith((".m4a", "tracks.json"))]
    info = describe_recording(paths)["archive"]
    assert info["audio"] == {} and "tracks_file" not in info
    assert not paths.audio_dir(final).exists()


def test_track_without_a_single_sample_is_left_out(recording: SessionPaths, audio: FakeAudio):
    """FFmpeg writes no audio stream for an empty WAV; there is nothing to keep, and
    the recording must still archive (its MP4 gets silence, as before)."""
    final = recording.final_dir
    _add_computer_track(recording, ms=0)
    state = archive_recording(recording, Config(), exporter=FakeExporter())
    assert state.archived and state.audio == {"microphone": "audio.m4a"}
    assert audio.encoded == ["audio.wav"]
    assert not audio_output(recording, "computer").exists()
    assert not recording.audio_dir(final).exists()


def test_audio_folder_stays_while_a_track_has_no_archived_copy(
    recording: SessionPaths, audio: FakeAudio
):
    """The deletion of ``audio/`` relies on the ``.m4a`` files being there at the end,
    not on what happened earlier in the run."""
    final = recording.final_dir
    exporter = FakeExporter()

    def export(request, config, progress=None, cancel=None):
        audio_output(recording, "microphone").unlink(missing_ok=True)  # lost meanwhile
        return exporter(request, config, progress, cancel)

    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=export)
    assert error.value.code == "ARCHIVE_FAILED" and "audio.wav" in str(error.value)
    assert recording.audio_path(final).is_file()
    state = read_archive_state(recording)
    assert not state.archived and state.screens_done == ["screen1", "screen2"]

    state = archive_recording(recording, Config(), exporter=exporter)  # converts it again
    assert state.archived and audio.encoded == ["audio.wav", "audio.wav"]
    assert exporter.calls == ["screen1", "screen2"]  # no screen is rendered again
    assert audio_output(recording, "microphone").is_file()
    assert not recording.audio_dir(final).exists()


def test_cleanup_keeps_a_track_that_has_no_archived_copy(recording: SessionPaths):
    """An archive of an older version that could not delete a WAV then: that WAV is the
    only copy of the track, a later cleanup must not delete it."""
    final = recording.final_dir
    wav = recording.audio_path(final)
    shutil.rmtree(recording.frames_dir(final))
    old = ArchiveState(
        status=ArchiveStatus.ARCHIVED,
        screens=["screen1", "screen2"],
        screens_done=["screen1", "screen2"],
        leftovers=[str(wav)],
    )
    write_archive_state(recording, old, final)
    assert cleanup_leftovers(recording) == [str(wav)]
    assert wav.is_file() and read_archive_state(recording).leftovers == [str(wav)]


def test_archive_state_round_trips_the_audio_tracks():
    state = ArchiveState(audio={"microphone": "audio.m4a"})
    assert ArchiveState.from_dict(state.to_dict()).audio == {"microphone": "audio.m4a"}
    assert ArchiveState.from_dict({"status": "queued", "audio": None}).audio == {}
    assert ArchiveState.from_dict({"status": "queued", "audio": ["audio.m4a"]}).audio == {}


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


# A failed archive gives the recording back ------------------------------------


def test_archive_state_of_an_older_version_has_no_failed_attempts():
    old = {"status": "archiving", "reason": "age", "screens": ["screen1"], "error": "x"}
    state = ArchiveState.from_dict(old)
    assert state.failed_attempts == 0 and state.status is ArchiveStatus.ARCHIVING
    assert state.pending and not state.failed and state.error == "x"
    assert ArchiveState.from_dict({"failed_attempts": None}).failed_attempts == 0
    assert ArchiveState.from_dict({"failed_attempts": "2"}).failed_attempts == 0
    failed = ArchiveState(status=ArchiveStatus.FAILED, failed_attempts=2, error="why")
    again = ArchiveState.from_dict(json.loads(json.dumps(failed.to_dict())))
    assert again.failed and not again.pending and not again.archived
    assert again.failed_attempts == 2 and again.error == "why"


def test_failed_archive_with_nothing_deleted_gives_the_recording_back(recording: SessionPaths):
    """Status ``failed``, the attempt counted: the recording loads like any other, and
    the next attempt reuses the audio copy that was already verified."""
    request_archive(recording, reason="age")
    exporter = FakeExporter(fail_on="screen1")
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=exporter)
    state = read_archive_state(recording)
    assert state.status is ArchiveStatus.FAILED and state.failed_attempts == 1
    assert state.reason == "age" and "simulated" in state.error and state.screens_done == []
    assert state.audio == {"microphone": "audio.m4a"}  # verified: kept for the next attempt
    assert _archive_names(recording) == ["archive.json", "audio.m4a"]  # no partial video
    loaded = LoadedRecording.load(recording)
    assert not loaded.archived and loaded.audio is not None
    assert len(loaded.frame_times("screen1")) == 5
    info = describe_recording(recording)["archive"]
    assert info["status"] == "failed" and "simulated" in info["error"]
    assert "nothing was deleted" in info["note"]

    # Every further attempt that fails the same way is counted.
    exporter.fail_on = "screen1"
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=exporter)
    assert read_archive_state(recording).failed_attempts == 2

    # The attempt that works leaves no trace of the failures but the counter.
    state = archive_recording(recording, Config(), exporter=exporter)
    assert state.archived and state.error is None and state.failed_attempts == 2


def test_request_archive_starts_a_failed_archive_over(recording: SessionPaths, audio: FakeAudio):
    request_archive(recording, reason="age")
    for _ in range(2):
        with pytest.raises(ArchiveError):
            archive_recording(recording, Config(), exporter=FakeExporter(fail_on="screen1"))
    assert read_archive_state(recording).failed_attempts == 2
    state = request_archive(recording, reason="user")  # Archive Video in the editor
    assert state.status is ArchiveStatus.QUEUED and state.reason == "user"
    assert state.failed_attempts == 0 and state.error is None
    assert state.audio == {"microphone": "audio.m4a"}  # kept
    assert read_archive_state(recording).to_dict() == state.to_dict()
    assert request_archive(recording, reason="age").reason == "user"  # queued: kept as it is
    audio.encoded.clear()
    assert archive_recording(recording, Config(), exporter=FakeExporter()).archived
    assert audio.encoded == []  # the verified copy was reused


def test_retry_keeps_the_counter_and_a_failure_is_counted_once(recording: SessionPaths):
    assert retry_archive(recording) is None  # never asked
    assert record_archive_failure(recording, "x") is None
    request_archive(recording, reason="age")
    assert retry_archive(recording) is None  # queued, not failed
    # The worker died without a word: whoever notices records it - once.
    state = record_archive_failure(recording, "worker crashed")
    assert state.failed and state.failed_attempts == 1 and state.error == "worker crashed"
    again = record_archive_failure(recording, "said again by the editor")
    assert again.failed_attempts == 1 and again.error == "worker crashed"
    retried = retry_archive(recording)
    assert retried.status is ArchiveStatus.QUEUED and retried.failed_attempts == 1
    assert retried.reason == "age" and retried.error == "worker crashed"
    # An attempt that recorded its failure itself is not counted again either.
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=FakeExporter(fail_on="screen1"))
    assert record_archive_failure(recording, "the editor, after the worker").failed_attempts == 2
    assert "simulated" in read_archive_state(recording).error


def test_failure_after_a_screen_is_done_stays_locked(recording: SessionPaths):
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=FakeExporter(fail_on="screen2"))
    state = read_archive_state(recording)
    assert state.status is ArchiveStatus.ARCHIVING and state.failed_attempts == 0
    # Recording it again (the worker, then the editor) changes nothing but the reason.
    state = record_archive_failure(recording, "exit code 1")
    assert state.status is ArchiveStatus.ARCHIVING and state.failed_attempts == 0
    assert state.pending and state.screens_done == ["screen1"]
    # Not to be asked for again or taken back: it is resumed.
    assert request_archive(recording).status is ArchiveStatus.ARCHIVING
    assert retry_archive(recording) is None
    assert withdraw_archive_request(recording) is False


def test_a_stopped_archive_is_not_a_failure(recording: SessionPaths):
    def verifier(video, duration_ms, config, cancel=None):
        raise ExportError("CANCELLED", "export cancelled")

    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=FakeExporter(), verifier=verifier)
    state = read_archive_state(recording)
    assert state.status is ArchiveStatus.ARCHIVING and state.failed_attempts == 0
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), cancel=lambda: True, exporter=FakeExporter())
    assert read_archive_state(recording).pending


def test_withdraw_takes_back_only_an_automatic_request_that_has_not_started(
    recording: SessionPaths,
):
    assert withdraw_archive_request(recording) is False  # nothing to take back
    request_archive(recording, reason="user")
    assert withdraw_archive_request(recording) is False  # the user asked for it
    archive_state_path(recording).unlink()
    request_archive(recording, reason="age")
    assert withdraw_archive_request(recording) is True
    assert read_archive_state(recording) is None
    assert not recording.archive_dir(recording.final_dir).exists()  # made for the request
    # Work that has started is not taken back.
    request_archive(recording, reason="age")
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), cancel=lambda: True, exporter=FakeExporter())
    assert read_archive_state(recording).status is ArchiveStatus.ARCHIVING
    assert withdraw_archive_request(recording) is False
    # An automatic retry goes back to failed: its attempts and audio are remembered.
    record_archive_failure(recording, "why")
    assert retry_archive(recording).status is ArchiveStatus.QUEUED
    assert withdraw_archive_request(recording) is True
    state = read_archive_state(recording)
    assert state.failed and state.failed_attempts == 1 and state.error == "why"


# Cancel Archiving ---------------------------------------------------------------


def test_cancel_archive_gives_the_recording_back_for_good(
    recording: SessionPaths, audio: FakeAudio
):
    """A run stopped in its first screen: the status becomes ``cancelled`` (no failure,
    the counter as it was when the user cancelled), the partial video goes, the verified
    audio copy stays and is reused by the user's Archive Video."""
    request_archive(recording, reason="age")
    record_archive_failure(recording, "first")
    retry_archive(recording)
    mp4, jpg, srt = screen_outputs(recording, "screen1")

    def exporter(request, config, progress=None, cancel=None):
        ffmpeg_export.partial_output(request.output).write_bytes(b"half")
        request.output.with_suffix(".srt").write_text("1\n", encoding="utf-8")
        raise ExportError("FFMPEG_FAILED", "killed while cancelling")  # counted by _fail

    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=exporter)
    assert read_archive_state(recording).failed_attempts == 2  # what the stop ended in
    partial_audio = ffmpeg_export.partial_output(audio_output(recording, "microphone"))
    partial_audio.write_bytes(b"half")

    state = cancel_archive(recording, failed_attempts=1)  # 1 when the user cancelled

    assert state.status is ArchiveStatus.CANCELLED and state.cancelled
    assert not state.pending and not state.failed and not state.cancellable
    assert state.failed_attempts == 1 and state.error is None and state.reason == "age"
    assert state.audio == {"microphone": "audio.m4a"}
    assert read_archive_state(recording).to_dict() == state.to_dict()
    assert _archive_names(recording) == ["archive.json", "audio.m4a"]
    assert not ffmpeg_export.partial_output(mp4).exists() and not srt.exists()
    assert len(_images(recording, "screen1")) == 5
    loaded = LoadedRecording.load(recording)
    assert not loaded.archived and loaded.audio is not None
    info = describe_recording(recording)["archive"]
    assert info["status"] == "cancelled" and "cancelled" in info["note"]
    assert "error" not in info and info["videos"] == {}

    # Nothing but the user brings it back: no failure, no retry, no withdrawal.
    assert record_archive_failure(recording, "late").cancelled
    assert read_archive_state(recording).failed_attempts == 1
    assert retry_archive(recording) is None
    assert withdraw_archive_request(recording) is False
    assert read_archive_state(recording).cancelled
    state = request_archive(recording, reason="user")  # Archive Video in the editor
    assert state.status is ArchiveStatus.QUEUED and state.reason == "user"
    assert state.failed_attempts == 0 and state.audio == {"microphone": "audio.m4a"}
    audio.encoded.clear()
    assert archive_recording(recording, Config(), exporter=FakeExporter()).archived
    assert audio.encoded == []  # the verified copy was reused


def test_cancel_archive_too_late_leaves_the_work_to_finish(recording: SessionPaths):
    assert cancel_archive(recording) is None  # never asked
    with pytest.raises(ArchiveError):
        archive_recording(recording, Config(), exporter=FakeExporter(fail_on="screen2"))
    before = read_archive_state(recording)
    assert not before.cancellable  # screen1 is done: its frames are gone
    state = cancel_archive(recording, failed_attempts=0)
    assert state.status is ArchiveStatus.ARCHIVING and state.screens_done == ["screen1"]
    assert read_archive_state(recording).to_dict() == before.to_dict()
    assert screen_outputs(recording, "screen1")[0].is_file()
    archived = archive_recording(recording, Config(), exporter=FakeExporter())
    assert cancel_archive(recording).archived and archived.archived


def test_cancel_a_request_that_has_not_started(recording: SessionPaths):
    queued = request_archive(recording, reason="user")
    assert queued.cancellable
    state = cancel_archive(recording)
    assert state.cancelled and state.failed_attempts == 0 and state.screens == []
    assert _archive_names(recording) == ["archive.json"]


def test_cancelled_archive_state_round_trips():
    state = ArchiveState(status=ArchiveStatus.CANCELLED, reason="age", failed_attempts=2)
    again = ArchiveState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert again.cancelled and again.failed_attempts == 2 and again.reason == "age"
    assert not again.pending and not again.archived and not again.failed
    old = ArchiveState.from_dict({"status": "queued", "reason": "user"})  # no newer fields
    assert old.cancellable and old.audio == {} and old.failed_attempts == 0


# The still image --------------------------------------------------------------


def test_still_skips_an_empty_frame_in_the_middle(recording: SessionPaths):
    """The frame nearest the middle (1000 ms of 2500) is a zero-byte file: the next
    nearest one (1500 ms) is used instead of failing the archive."""
    middle = recording.frames_dir(recording.final_dir) / "1_000_screen1.png"
    assert middle.is_file()
    middle.write_bytes(b"")
    state = archive_recording(recording, Config(), exporter=FakeExporter())
    assert state.archived
    with Image.open(screen_outputs(recording, "screen1")[1]) as still:
        r, g, b = still.convert("RGB").getpixel((5, 5))
        assert abs(r - 150) < 12 and abs(g - 80) < 12 and abs(b - 40) < 12  # the 1500 ms frame
    with Image.open(screen_outputs(recording, "screen2")[1]) as still:
        r, g, b = still.convert("RGB").getpixel((5, 5))
        assert abs(b - 100) < 12  # untouched screen: still the 1000 ms frame


def test_still_searches_outwards_past_unusable_frames(recording: SessionPaths):
    frames = _images(recording, "screen1")
    for path in frames[1:4]:  # 500, 1000 and 1500 ms
        path.write_bytes(b"" if path != frames[1] else b"not an image")
    assert archive_recording(recording, Config(), exporter=FakeExporter()).archived
    with Image.open(screen_outputs(recording, "screen1")[1]) as still:
        r, _g, _b = still.convert("RGB").getpixel((5, 5))
        assert abs(r - 200) < 12  # 2000 ms is as near as 500 ms, which cannot be read


def test_screen_without_a_usable_frame_fails_with_nothing_deleted(recording: SessionPaths):
    for path in _images(recording, "screen1"):
        path.write_bytes(b"")
    with pytest.raises(ArchiveError) as error:
        archive_recording(recording, Config(), exporter=FakeExporter())
    assert error.value.code == "NO_FRAMES"
    state = read_archive_state(recording)
    assert state.failed and state.failed_attempts == 1 and "screen1" in state.error
    assert len(_images(recording, "screen1")) == 5 and len(_images(recording, "screen2")) == 5
    assert recording.audio_path(recording.final_dir).is_file()


def test_frames_from_the_middle_go_outwards():
    order = list(archive_mod._frames_from_middle([0, 500, 1000, 1500, 2000], 1250))
    assert order == [2, 3, 1, 4, 0]  # 1000, 1500, 500, 2000, 0
    assert list(archive_mod._frames_from_middle([0, 500], 5000)) == [1, 0]
    assert list(archive_mod._frames_from_middle([700], 0)) == [0]
    assert list(archive_mod._frames_from_middle([], 100)) == []


# The worker (archive-stream) --------------------------------------------------


def _worker_config(folder: Path) -> Path:
    config_file = folder / "config.json"
    config_file.write_text(json.dumps({"recording_folder": str(folder)}), encoding="utf-8")
    return config_file


def test_archive_stream_lowers_its_own_priority(tmp_path: Path, monkeypatch, capsys):
    """FFmpeg and whisper are children of the worker: they inherit what it sets, and
    below normal is the class a Windows child inherits."""
    paths = make_recording(tmp_path, NEWEST)
    modes: list[str] = []

    def lower(mode: str) -> str:
        modes.append(mode)
        return "below_normal"

    monkeypatch.setattr("video_story.platform.process.lower_own_priority", lower)
    started: list[list[str]] = []

    def fake_archive(p, c, **kw):
        started.append(list(modes))  # the priority was lowered before any work
        return ArchiveState(status=ArchiveStatus.ARCHIVED)

    monkeypatch.setattr(archive_mod, "archive_recording", fake_archive)
    args = ["--config", str(_worker_config(tmp_path)), "archive-stream", str(paths.final_dir)]
    assert cli.main(args) == 0
    assert modes == ["faster"] == [cli.ARCHIVE_PRIORITY_MODE] and started == [["faster"]]
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "archived"


def test_archive_stream_gives_the_recording_back_when_transcription_fails(
    tmp_path: Path, monkeypatch, capsys
):
    paths = make_recording(tmp_path, NEWEST)
    write_transcript(
        paths.transcript_txt_path(paths.final_dir),
        paths.transcript_json_path(paths.final_dir),
        Transcript("en", [Segment(0, 1000, "hello")], complete=False, transcribed_ms=1000),
    )
    request_archive(paths, reason="age")

    def broken_stream(paths_, cfg, **kwargs):
        raise RuntimeError("whisper model not found")

    monkeypatch.setattr("video_story.transcribe.pipeline.stream_transcription", broken_stream)
    args = ["--config", str(_worker_config(tmp_path)), "archive-stream", str(paths.final_dir)]
    assert cli.main(args) != 0
    assert "whisper model not found" in capsys.readouterr().err
    state = read_archive_state(paths)
    assert state.failed and state.failed_attempts == 1
    assert "whisper model not found" in state.error
    assert not LoadedRecording.load(paths).archived


def test_archive_stream_counts_a_failed_archive_once(tmp_path: Path, monkeypatch, capsys):
    paths = make_recording(tmp_path, NEWEST)
    request_archive(paths, reason="age")
    real = archive_mod.archive_recording
    monkeypatch.setattr(
        archive_mod,
        "archive_recording",
        lambda p, c, **kw: real(p, c, exporter=FakeExporter(fail_on="screen1"), **kw),
    )
    args = ["--config", str(_worker_config(tmp_path)), "archive-stream", str(paths.final_dir)]
    assert cli.main(args) != 0
    state = read_archive_state(paths)
    assert state.failed and state.failed_attempts == 1  # by _fail, not again by the command
    assert "simulated failure" in state.error
    assert json.loads(capsys.readouterr().err.splitlines()[-1])["code"] == "FFMPEG_FAILED"


def test_archive_stream_that_was_stopped_records_no_failure(tmp_path: Path, monkeypatch, capsys):
    paths = make_recording(tmp_path, NEWEST)
    request_archive(paths, reason="age")

    def stopped(p, c, **kw):
        raise ArchiveError("CANCELLED", "archiving was stopped")

    monkeypatch.setattr(archive_mod, "archive_recording", stopped)
    args = ["--config", str(_worker_config(tmp_path)), "archive-stream", str(paths.final_dir)]
    assert cli.main(args) != 0
    state = read_archive_state(paths)
    assert state.status is ArchiveStatus.QUEUED and state.failed_attempts == 0


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
