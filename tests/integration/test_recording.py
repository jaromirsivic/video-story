"""Real recording on the development machine (screens + microphone, no window)."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from video_story.capture.recorder import Recorder
from video_story.core.config import Config
from video_story.core.manifest import read_manifest, write_manifest
from video_story.core.naming import parse_frame_filename
from video_story.core.session import SessionPaths, new_session_id
from video_story.platform import get_platform

pytestmark = pytest.mark.integration


def _record(tmp_path: Path, seconds: float, **cfg_changes) -> tuple[SessionPaths, object, Config]:
    import time

    cfg = Config(recording_folder=tmp_path, **cfg_changes)
    platform = get_platform()
    paths = SessionPaths(cfg.recording_folder, new_session_id(cfg.recording_folder))
    recorder = Recorder(cfg, platform, paths)
    recorder.start()
    time.sleep(seconds)
    summary = recorder.stop()
    write_manifest(paths.manifest_path(paths.work_dir), summary.to_manifest(cfg, platform.name))
    paths.finalize()
    return paths, summary, cfg


def test_two_second_recording_produces_frames_sidecars_and_audio(tmp_path: Path):
    paths, summary, cfg = _record(tmp_path, 2.0, capture_interval_ms=250, image_format="png")
    final = paths.final_dir
    assert final.is_dir() and not paths.work_dir.joinpath("frames").exists()
    manifest = read_manifest(paths.manifest_path(final))
    screens = {s.id for s in manifest.screens}
    assert screens, "at least one screen"

    frames = sorted(p for p in paths.frames_dir(final).iterdir() if p.suffix == ".png")
    sidecars = {p.stem for p in paths.frames_dir(final).iterdir() if p.suffix == ".json"}
    assert len(frames) >= 5 * len(screens), f"expected >= 5 frames per screen, got {len(frames)}"
    assert {p.stem for p in frames} == sidecars, "every frame has a sidecar"

    per_screen: dict[str, list[int]] = {}
    for frame in frames:
        parsed = parse_frame_filename(frame.name)
        assert parsed is not None and parsed.screen_id in screens
        per_screen.setdefault(parsed.screen_id, []).append(parsed.time_ms)
        sidecar = json.loads(frame.with_suffix(".json").read_text(encoding="utf-8"))
        assert sidecar["screen"] == parsed.screen_id and sidecar["time_ms"] == parsed.time_ms
        assert sidecar["drawings"] == []  # no overlay in a headless recording
        assert sidecar["zoom"] == []
        cursor = sidecar["cursor"]
        if cursor is not None:
            assert cursor["screen"] in screens
            target = next(s for s in manifest.screens if s.id == cursor["screen"])
            assert 0 <= cursor["x"] < target.image_width and 0 <= cursor["y"] < target.image_height
    for times in per_screen.values():
        assert times == sorted(times) and len(set(times)) == len(times)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        assert all(200 <= g <= 400 for g in gaps), gaps

    with wave.open(str(paths.audio_path(final)), "rb") as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        duration_ms = wav.getnframes() * 1000 / wav.getframerate()
    assert 1500 <= duration_ms <= 3500, duration_ms
    assert abs(summary.recorded_duration_ms - 2000) < 400
    assert manifest.frames.count == len(frames)


def test_jpeg_format_and_auto_stop(tmp_path: Path):
    paths, summary, _ = _record(
        tmp_path, 1.0, capture_interval_ms=100, image_format="jpeg", jpeg_quality=50
    )
    frames = [p for p in paths.frames_dir(paths.final_dir).iterdir() if p.suffix == ".jpg"]
    assert frames and all(p.read_bytes()[:2] == b"\xff\xd8" for p in frames)
    assert summary.auto_stopped is False
