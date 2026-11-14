"""Resumable transcription for the editor's background worker (no model, no audio devices)."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_transcribe import FakeBackend, write_silence

from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript, read_transcript
from video_story.transcribe import pipeline
from video_story.transcribe import whisper_backend as wb


@pytest.fixture
def session(tmp_path: Path, monkeypatch) -> tuple[SessionPaths, Config]:
    """A work directory with one second of audio; ffmpeg is replaced by a silent WAV."""
    cfg = Config(
        recording_folder=tmp_path / "rec",
        whisper_model_path=tmp_path / "ggml-small.bin",
        whisper_threads=2,
    )
    paths = SessionPaths(cfg.recording_folder, "20260101-120000")
    paths.create_work_dirs()
    write_silence(paths.audio_path(paths.work_dir), 1.0)
    monkeypatch.setattr(pipeline, "ensure_ffmpeg", lambda config, download=False: Path("ffmpeg"))
    monkeypatch.setattr(
        pipeline, "convert_to_16k_mono", lambda ffmpeg, src, dst: write_silence(dst, 1.0, 16000)
    )
    return paths, cfg


class RecordingBackend(FakeBackend):
    """Remembers how long the WAV handed to whisper was (it is deleted afterwards)."""

    def transcribe(self, wav16k, *, language, threads, progress=None, on_segment=None):
        self.calls.append({"wav_ms": wb.wav_duration_ms(wav16k)})
        if on_segment:
            for segment in self.segments:
                on_segment(segment)
        if progress:
            progress(1.0)
        return Transcript(language=self.language, segments=list(self.segments))


def test_trim_wav_start_keeps_the_tail(tmp_path: Path):
    path = tmp_path / "a.wav"
    write_silence(path, 2.0, 16000)
    pipeline.trim_wav_start(path, 1500)
    assert wb.wav_duration_ms(path) == 500
    pipeline.trim_wav_start(path, 0)  # no-op
    assert wb.wav_duration_ms(path) == 500
    pipeline.trim_wav_start(path, 99_000)  # past the end: empty, not an error
    assert wb.wav_duration_ms(path) == 0


def test_transcribe_audio_resumes_at_start_ms_with_absolute_times(session, monkeypatch):
    paths, cfg = session
    monkeypatch.setattr(
        pipeline, "convert_to_16k_mono", lambda ffmpeg, src, dst: write_silence(dst, 4.0, 16000)
    )
    backend = RecordingBackend(
        [
            Segment(0, 800, " druhá  věta "),
            Segment(900, 1000, "Titulky vytvořil JohnyX"),  # hallucinated credits: dropped
            Segment(1000, 1500, "třetí"),
        ]
    )
    seen: list[Segment] = []
    events: list[float] = []
    result = pipeline.transcribe_audio(
        paths,
        cfg,
        start_ms=2500,
        backend=backend,
        on_segment=seen.append,
        progress=lambda f, _m: events.append(f),
    )
    assert backend.calls == [{"wav_ms": 1500}], "whisper only sees the audio after start_ms"
    assert result.audio_duration_ms == 4000
    assert result.transcript.segments == [
        Segment(2500, 3300, "druhá  věta"),
        Segment(3500, 4000, "třetí"),
    ]
    assert seen == result.transcript.segments  # streamed with absolute times, credits dropped
    assert not paths.audio_16k_path(paths.work_dir).exists()
    assert events[-1] == pytest.approx(pipeline.PROGRESS_TRANSCRIBE_END)
    # Nothing left after the end of the audio: whisper is not even called.
    backend = RecordingBackend([Segment(0, 100, "x")])
    result = pipeline.transcribe_audio(paths, cfg, start_ms=4000, backend=backend)
    assert backend.calls == [] and result.transcript.segments == []


def test_transcribe_audio_delivers_segments_a_non_streaming_backend_returns(session):
    """whisper-cli reports only at the end: listeners still see every segment."""
    paths, cfg = session

    class EndOnly(FakeBackend):
        def transcribe(self, wav16k, *, language, threads, progress=None, on_segment=None):
            return Transcript(language="en", segments=list(self.segments))

    seen: list[Segment] = []
    result = pipeline.transcribe_audio(
        paths,
        cfg,
        backend=EndOnly([Segment(0, 500, "one"), Segment(500, 900, "two")]),
        on_segment=seen.append,
    )
    expected = [Segment(0, 500, "one"), Segment(500, 900, "two")]
    assert seen == expected and result.transcript.segments == expected


def test_transcribe_recording_writes_a_complete_transcript_and_drops_credits(session):
    paths, cfg = session
    backend = FakeBackend([Segment(0, 500, "hello"), Segment(600, 900, "Subtitles by Amara.org")])
    result = pipeline.transcribe_recording(paths, cfg, backend=backend)
    stored = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert stored.complete is True and stored.transcribed_ms is None
    assert stored.segments == result.transcript.segments == [Segment(0, 500, "hello")]


def test_stream_transcription_reports_events_in_order(session, monkeypatch):
    paths, cfg = session
    monkeypatch.setattr(
        pipeline, "convert_to_16k_mono", lambda ffmpeg, src, dst: write_silence(dst, 3.0, 16000)
    )
    events: list[dict] = []
    backend = RecordingBackend([Segment(0, 700, "first"), Segment(800, 1200, "second")])
    done = pipeline.stream_transcription(
        paths, cfg, start_ms=1000, emit=events.append, backend=backend
    )
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and events[0]["start_ms"] == 1000
    assert kinds.count("segment") == 2 and "progress" in kinds and "done" not in kinds
    segments = [e for e in events if e["event"] == "segment"]
    assert (segments[0]["start_ms"], segments[0]["end_ms"], segments[0]["text"]) == (
        1000,
        1700,
        "first",
    )
    assert segments[1]["start_ms"] == 1800 and segments[1]["text"] == "second"
    last_progress = [e for e in events if e["event"] == "progress"][-1]
    assert last_progress["fraction"] == pytest.approx(1.0)
    assert last_progress["transcribed_ms"] == 2200  # the end of the last segment
    assert done["event"] == "done" and done["segments"] == 2
    assert done["audio_duration_ms"] == 3000 and done["language"] == "cs"
    assert done["backend"] == "fake" and done["seconds"] >= 0
    # Nothing was written: the editor owns the transcript files.
    assert not paths.transcript_json_path(paths.work_dir).exists()
