"""Pause / Resume and Mute Mic / Mute PC: the clock, the recorders, the manifest and the
text timeline."""

from __future__ import annotations

import json
import queue
import time
from pathlib import Path

import numpy as np
from test_recorder_sidecars import FakeGrabber, FakePlatform

from video_story.capture import computer_audio as ca
from video_story.capture import recorder as recorder_mod
from video_story.capture.audio_recorder import AudioRecorder
from video_story.capture.recorder import Recorder
from video_story.core.config import Config
from video_story.core.crop import Crop
from video_story.core.manifest import Manifest, read_manifest, write_manifest
from video_story.core.session import SessionPaths
from video_story.core.timeline import RecordingClock
from video_story.host import events

# -- the clock -----------------------------------------------------------------------------


def _clock_at(monkeypatch, now: list[int]) -> RecordingClock:
    monkeypatch.setattr(time, "perf_counter_ns", lambda: now[0])
    clock = RecordingClock()
    clock.start()
    return clock


def test_clock_leaves_out_the_paused_time(monkeypatch):
    now = [1_000_000_000]
    clock = _clock_at(monkeypatch, now)
    now[0] += 2_000_000_000
    assert clock.elapsed_ms() == 2000
    assert clock.pause() and clock.paused and not clock.pause()  # a second pause: nothing
    now[0] += 5_000_000_000
    assert clock.elapsed_ms() == 2000  # frozen while paused
    assert clock.resume() == 5_000_000_000 and not clock.paused
    assert clock.resume() is None
    now[0] += 1_000_000_000
    assert clock.elapsed_ms() == 3000
    # perf_counter stamps: before, inside and after the pause.
    assert clock.ms_at(1_000_000_000 + 1_500_000_000) == 1500
    assert clock.ms_at(1_000_000_000 + 4_000_000_000) is None
    assert clock.ms_at(1_000_000_000 + 7_500_000_000) == 2500
    assert clock.perf_counter_at(2_500_000_000) == (1_000_000_000 + 7_500_000_000) / 1e9


def test_clock_stop_while_paused_ends_the_pause(monkeypatch):
    now = [0]
    clock = _clock_at(monkeypatch, now)
    now[0] += 1_000_000_000
    clock.pause()
    now[0] += 3_000_000_000
    assert clock.ms_at(now[0]) is None  # the pause still goes on
    assert clock.stop() == 1000 and not clock.paused and not clock.running
    assert not clock.pause()  # a stopped clock cannot be paused


# -- the audio tracks ------------------------------------------------------------------------


def _bare_mic() -> AudioRecorder:
    mic = AudioRecorder.__new__(AudioRecorder)
    mic._queue = queue.Queue()
    mic._dropped_blocks = 0
    mic.paused = False
    mic.muted = False
    return mic


def test_microphone_drops_paused_blocks_and_writes_silence_while_muted():
    mic = _bare_mic()
    block = np.full((4, 1), 1000, dtype=np.int16)
    mic._callback(block, 4, None, None)
    mic.paused = True
    mic._callback(block, 4, None, None)  # cut out
    mic.paused = False
    mic.muted = True
    mic._callback(block, 4, None, None)  # same length, silent
    first, second = mic._queue.get_nowait(), mic._queue.get_nowait()
    assert mic._queue.empty()
    assert first.tolist() == block.tolist() and not second.any() and len(second) == 4


class _FakeWav:
    def __init__(self) -> None:
        self.data = bytearray()

    def writeframes(self, raw: bytes) -> None:
        self.data += raw

    @property
    def samples(self) -> np.ndarray:
        return np.frombuffer(bytes(self.data), dtype="<i2")


def test_computer_audio_leaves_out_the_pause_and_writes_silence_while_muted(
    tmp_path: Path, monkeypatch
):
    recorder = ca.ComputerAudioRecorder(tmp_path / "computer.wav", sample_rate=1000)
    wav = _FakeWav()
    recorder._origin = 0.0
    block = np.full((100, 1), 0.5, dtype=np.float32)
    recorder._write(wav, block, 0.1)
    clock = [0.1]
    monkeypatch.setattr(ca.time, "monotonic", lambda: clock[0])
    recorder.pause()
    clock[0] = 5.1  # five seconds paused: not a gap to fill
    recorder.resume()
    recorder.muted = True
    recorder._write(wav, block, 5.2)
    assert recorder.frames_written == 200  # no silence for the pause
    assert wav.samples[:100].all() and not wav.samples[100:].any()  # muted: silence
    recorder._pad_to(wav, 5.5)
    assert recorder.frames_written == 500


# -- the recorder ----------------------------------------------------------------------------


def _wait_frames(recorder: Recorder, count: int) -> None:
    deadline = time.monotonic() + 3.0
    while recorder.frame_count < count and time.monotonic() < deadline:
        time.sleep(0.01)


def test_recorder_pause_resume_and_mutes_end_up_in_the_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "ScreenGrabber", FakeGrabber)
    cfg = Config(
        recording_folder=tmp_path, capture_interval_ms=20, image_format="png", computer_audio=False
    )
    paths = SessionPaths(tmp_path, "20260101-130000")
    recorder = Recorder(cfg, FakePlatform(), paths, record_audio=False)
    assert recorder.is_muted("computer") and not recorder.is_muted("mic")
    recorder.start()
    _wait_frames(recorder, 3)
    recorder.set_muted("mic", True)
    assert recorder.pause() and recorder.paused and not recorder.pause()
    at_ms = recorder.elapsed_ms
    frames = recorder.frame_count
    assert recorder.add_keypoint() is None  # no keypoint while paused
    time.sleep(0.3)
    assert recorder.frame_count <= frames + 1  # at most a tick already under way
    assert recorder.elapsed_ms == at_ms
    recorder.set_muted("computer", False)  # unmuted while paused: from the pause on
    assert recorder.resume() and not recorder.paused
    _wait_frames(recorder, recorder.frame_count + 3)
    summary = recorder.stop()

    assert summary.recorded_duration_ms < at_ms + 250  # the 300 ms pause is cut out
    (pause,) = summary.pauses
    assert pause["at_ms"] == at_ms and pause["duration_ms"] >= 300
    mic = [m for m in summary.mutes if m["track"] == "mic"]
    computer = [m for m in summary.mutes if m["track"] == "computer"]
    assert len(mic) == 1 and mic[0]["to_ms"] == summary.recorded_duration_ms
    assert computer == [{"track": "computer", "from_ms": 0, "to_ms": at_ms}]

    write_manifest(paths.manifest_path(paths.work_dir), summary.to_manifest(cfg, "fake"))
    raw = json.loads(paths.manifest_path(paths.work_dir).read_text(encoding="utf-8"))
    assert raw["pauses"] == summary.pauses and "cut out" in raw["pauses_note"]
    manifest = read_manifest(paths.manifest_path(paths.work_dir))
    assert manifest.pauses == summary.pauses and manifest.mutes == summary.mutes

    # Frame times run on without a gap where the pause was.
    times = sorted(
        int(p.stem.split("_")[0]) * 1000 + int(p.stem.split("_")[1])
        for p in paths.frames_dir(paths.work_dir).glob("*.png")
    )
    assert max(b - a for a, b in zip(times, times[1:], strict=False)) < 200


def test_stop_while_paused_closes_the_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder_mod, "ScreenGrabber", FakeGrabber)
    cfg = Config(recording_folder=tmp_path, capture_interval_ms=20, image_format="png")
    recorder = Recorder(
        cfg, FakePlatform(), SessionPaths(tmp_path, "20260101-130001"), record_audio=False
    )
    recorder.start()
    _wait_frames(recorder, 2)
    recorder.pause()
    time.sleep(0.1)
    summary = recorder.stop()
    (pause,) = summary.pauses
    assert pause["duration_ms"] >= 100
    assert summary.recorded_duration_ms == pause["at_ms"] or (
        summary.recorded_duration_ms - pause["at_ms"] < 50
    )


def test_manifest_reads_pauses_and_mutes_leniently():
    manifest = Manifest.from_dict(
        {
            "session_id": "x",
            "screens": [],
            "pauses": [{"at_ms": 900, "duration_ms": 50}, {"at_ms": "x"}, {"at_ms": 100}],
            "mutes": [{"track": "speaker", "from_ms": 0}, {"track": "mic", "from_ms": 5}],
        }
    )
    assert manifest.pauses == [
        {"at_ms": 100, "duration_ms": None},
        {"at_ms": 900, "duration_ms": 50},
    ]
    assert manifest.mutes == [{"track": "mic", "from_ms": 5, "to_ms": None}]


# -- the text timeline -----------------------------------------------------------------------


def test_timeline_lines_for_pauses_and_mutes():
    manifest = {
        "recorded_duration_ms": 90_000,
        "pauses": [
            {"at_ms": 5000, "duration_ms": 42_400},
            {"at_ms": 60_000, "duration_ms": 185_000},
            {"at_ms": 95_000, "duration_ms": 1000},  # outside the range
        ],
        "mutes": [
            {"track": "computer", "from_ms": 0, "to_ms": 90_000},
            {"track": "mic", "from_ms": 10_000, "to_ms": 20_000},
            {"track": "mic", "from_ms": 1000, "to_ms": 30_000},
        ],
    }
    counts: dict[str, int] = {}
    window = Crop(2000, 90_000)
    pauses = [e.text for e in events._pause_events(manifest, window, counts)]
    assert pauses == [
        "5000 PAUSE 42 s (the user paused the recording here; the 42 s are cut out, "
        "nothing was recorded meanwhile)",
        "60000 PAUSE 3 min 05 s (the user paused the recording here; the 3 min 05 s are cut "
        "out, nothing was recorded meanwhile)",
    ]
    mutes = [e.text for e in events._mute_events(manifest, window, counts)]
    assert mutes == [
        "2000 MUTE computer (muted since 0)",
        "10000 MUTE mic",
        "20000 UNMUTE mic",
        "2000 MUTE mic (muted since 1000)",
        "30000 UNMUTE mic",
    ]
    whole = [e.text for e in events._mute_events(manifest, Crop(0, 90_000), {})]
    assert whole[0] == "0 MUTE computer (muted from the start)"
    assert counts["pauses"] == 2 and counts["mutes"] == 3
    assert "PAUSE" in events.LEGEND and "MUTE" in events.LEGEND


def test_double_headed_arrow_in_the_timeline():
    double = {"tool": "arrow", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}, "heads": "both"}
    double["shift"] = True
    assert events._geometry(double) == "ends 1,2 3,4"
    assert events._shape_note(double) == " (double-headed, Shift)"
    start = {"tool": "arrow", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}}
    start.update(heads="start", shift=True, group=4)
    assert events._geometry(start) == "head 1,2 to 3,4"
    assert "start head part of an arrow split at a screen edge, group 4" in events._shape_note(
        start
    )
    plain = {"tool": "arrow", "from": {"x": 1, "y": 2}, "to": {"x": 3, "y": 4}}
    assert events._geometry(plain) == "tail 1,2 head 3,4" and events._shape_note(plain) == ""
