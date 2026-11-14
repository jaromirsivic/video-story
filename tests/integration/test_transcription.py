"""Real offline transcription of the Czech sample with the bundled model.

Runs only with ``VIDEO_STORY_INTEGRATION=1``. Proxies are pointed at a dead port so
any attempt to download something fails loudly.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from video_story.core.config import Config, default_model_path
from video_story.core.session import SessionPaths
from video_story.core.transcript import read_transcript
from video_story.transcribe.ffmpeg import find_ffmpeg
from video_story.transcribe.pipeline import transcribe_recording
from video_story.transcribe.whisper_backend import WhisperCliBackend

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "czech.wav"
EXPECTED_WORDS = ["dobrý den", "přepis", "schůzk", "úkol"]
# Optional second backend: point VIDEO_STORY_WHISPER_CLI at a whisper-cli executable.
WHISPER_CLI = Path(os.environ.get("VIDEO_STORY_WHISPER_CLI", "whisper-cli-not-configured"))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")


@pytest.fixture
def session(tmp_path: Path) -> tuple[SessionPaths, Config]:
    cfg = Config(recording_folder=tmp_path / "rec")
    if not cfg.resolved_model_path().is_file():
        pytest.skip(f"model missing: {default_model_path()}")
    if find_ffmpeg(cfg) is None:
        pytest.skip("ffmpeg not available")
    paths = SessionPaths(cfg.recording_folder, "20260101-120000")
    paths.create_work_dirs()
    shutil.copy(SAMPLE, paths.audio_path(paths.work_dir))
    return paths, cfg


def _check_transcript(transcript, label: str) -> None:
    text = transcript.text.lower()
    print(f"\n[{label}] language={transcript.language} segments={len(transcript.segments)}")
    for seg in transcript.segments:
        print(f"  {seg.start_ms:6d} -> {seg.end_ms:6d}  {seg.text}")
    assert transcript.language == "cs"
    # large-v3-turbo keeps this 14 s sample as one segment; small split it in two.
    assert len(transcript.segments) >= 1, transcript.segments
    assert "zkou" in transcript.text.lower() and "sch" in transcript.text.lower()
    last = 0
    for seg in transcript.segments:
        assert 0 <= seg.start_ms <= seg.end_ms <= 15000, seg
        assert seg.start_ms >= last, "segments must be chronological"
        last = seg.start_ms
    hits = [w for w in EXPECTED_WORDS if w in text]
    assert len(hits) >= 2, f"expected at least two of {EXPECTED_WORDS} in {text!r}"


def test_pywhispercpp_pipeline_on_czech_sample(session):
    paths, cfg = session
    events: list[tuple[float, str]] = []
    started = time.perf_counter()
    result = transcribe_recording(paths, cfg, progress=lambda f, m: events.append((f, m)))
    elapsed = time.perf_counter() - started
    print(f"\n[pywhispercpp] wall time {elapsed:.2f} s, info={result.info.to_dict()}")

    _check_transcript(result.transcript, "pywhispercpp")
    assert 13000 <= result.audio_duration_ms <= 15000
    assert result.info.backend == "pywhispercpp"
    assert result.info.model == "ggml-large-v3-turbo-q5_0"
    assert result.info.segments == len(result.transcript.segments)
    assert not paths.audio_16k_path(paths.work_dir).exists()
    stored = read_transcript(paths.transcript_json_path(paths.work_dir))
    assert stored.segments == result.transcript.segments and stored.language == "cs"
    txt = paths.transcript_txt_path(paths.work_dir).read_text(encoding="utf-8")
    assert txt.startswith("[00:00:00.") and txt.count("\n") == len(stored.segments)
    fractions = [f for f, _ in events]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert any(0.05 < f < 0.98 for f in fractions), "backend progress reached the caller"


@pytest.mark.skipif(not WHISPER_CLI.is_file(), reason="whisper-cli.exe not available")
def test_whisper_cli_backend_on_czech_sample(session):
    paths, cfg = session
    cfg = cfg.with_values(whisper_backend="whisper-cli", whisper_cli_path=str(WHISPER_CLI))
    started = time.perf_counter()
    result = transcribe_recording(
        paths, cfg, backend=WhisperCliBackend(WHISPER_CLI, cfg.resolved_model_path())
    )
    elapsed = time.perf_counter() - started
    print(f"\n[whisper-cli] wall time {elapsed:.2f} s")
    _check_transcript(result.transcript, "whisper-cli")
    assert result.info.backend == "whisper-cli"
