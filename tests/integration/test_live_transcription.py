"""Transcription while recording with the real model on a WAV that grows.

Runs only with ``VIDEO_STORY_INTEGRATION=1`` (like the other integration tests): a
thread writes the Czech sample three times (with pauses) into ``audio/audio.wav`` in
100 ms blocks, as the recorder does, while the live worker transcribes it.
"""

from __future__ import annotations

import os
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from video_story.core.config import Config
from video_story.core.session import SessionPaths
from video_story.transcribe.ffmpeg import find_ffmpeg
from video_story.transcribe.live_worker import run_live_worker

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "czech.wav"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")


def _sample() -> tuple[np.ndarray, int]:
    with wave.open(str(SAMPLE), "rb") as wf:
        rate = wf.getframerate()
        data = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
        if wf.getnchannels() > 1:
            data = data.reshape(-1, wf.getnchannels()).mean(axis=1).astype(np.int16)
    return data, rate


def test_live_worker_transcribes_a_growing_recording(tmp_path: Path) -> None:
    config = Config(recording_folder=tmp_path / "rec")
    if not config.resolved_model_path().is_file():
        pytest.skip("whisper model not downloaded (video-story setup)")
    if find_ffmpeg(config) is None:
        pytest.skip("FFmpeg not available")
    paths = SessionPaths(config.recording_folder, "20261111-120000")
    paths.create_work_dirs()
    sample, rate = _sample()
    pause = np.zeros(rate * 2, dtype=np.int16)
    audio = np.concatenate([sample, pause, sample, pause, sample, pause])

    def write() -> None:
        block = rate // 10
        with wave.open(str(paths.audio_path(paths.work_dir)), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            for i in range(0, len(audio), block):
                wf.writeframes(audio[i : i + block].tobytes())
                time.sleep(0.01)  # ten times faster than real time

    writer = threading.Thread(target=write)
    writer.start()
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r")
    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append(event)
        if event["event"] == "committed" and event["speech"]:
            os.close(write_fd)  # stdin EOF: the worker ends after this window

    done = run_live_worker(
        paths,
        config,
        "faster",
        emit=emit,
        stdin=stdin,
        on_eof=lambda: None,
        threads=min(8, os.cpu_count() or 4),
        lower_priority=False,
    )
    writer.join()
    committed = [e for e in events if e["event"] == "committed"]
    assert [e["event"] for e in events[:2]] == ["start", "ready"]
    assert committed and committed[0]["start_ms"] == 0
    assert committed[0]["cut"] in ("pause", "mic_pause")
    text = " ".join(s["text"] for s in committed[0]["segments"]).casefold()
    assert "dobrý den" in text
    assert committed[0]["language"] == "cs"
    assert done["transcribed_ms"] == committed[-1]["transcribed_ms"] > 12_000
    assert not paths.transcript_json_path(paths.work_dir).exists()  # the worker never writes
