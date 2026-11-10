"""``manifest.json`` - everything an agent needs to interpret a recording."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text
from .screens import Screen
from .session import AUDIO_DIR_NAME, AUDIO_FILE_NAME, FRAMES_DIR_NAME, FRAMES_INFO_DIR_NAME

SCHEMA_VERSION = 2  # 2: audio/audio.wav, frames/ images only, sidecars in frames_info/

AUDIO_FILE = f"{AUDIO_DIR_NAME}/{AUDIO_FILE_NAME}"


@dataclass
class AudioInfo:
    file: str = AUDIO_FILE
    device: str | None = None
    sample_rate: int = 0
    channels: int = 1
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "device": self.device,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "duration_ms": self.duration_ms,
        }


@dataclass
class FramesInfo:
    dir: str = FRAMES_DIR_NAME
    info_dir: str = FRAMES_INFO_DIR_NAME
    count: int = 0
    dropped_ticks: int = 0
    naming: str = (
        "<seconds>_<millis>_<screenId>.<png|jpg> in dir, its sidecar "
        "<seconds>_<millis>_<screenId>.json in info_dir"
    )
    # Grabs of one screen that failed (lock screen, UAC prompt): no frame of that
    # screen exists for that tick; the frame before it is the latest one.
    failed_grabs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir": self.dir,
            "info_dir": self.info_dir,
            "count": self.count,
            "dropped_ticks": self.dropped_ticks,
            "failed_grabs": self.failed_grabs,
            "naming": self.naming,
        }


@dataclass
class TranscriptInfo:
    txt: str = "transcript.txt"
    json_file: str = "transcript.json"
    language: str | None = None
    model: str | None = None
    backend: str | None = None
    segments: int = 0
    duration_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "txt": self.txt,
            "json": self.json_file,
            "language": self.language,
            "model": self.model,
            "backend": self.backend,
            "segments": self.segments,
            "transcription_seconds": self.duration_s,
        }


@dataclass
class Manifest:
    session_id: str
    started_at: str
    finished_at: str | None
    recorded_duration_ms: int
    capture_interval_ms: int
    image_format: str
    jpeg_quality: int | None
    screens: list[Screen]
    audio: AudioInfo = field(default_factory=AudioInfo)
    frames: FramesInfo = field(default_factory=FramesInfo)
    transcript: TranscriptInfo | None = None
    auto_stopped: bool = False
    auto_stop_minutes: int | None = None
    platform: str = ""
    plugin_version: str = ""
    notes: list[str] = field(default_factory=list)
    # Moments the user marked with **Keypoint** (Ctrl+Alt+K) while recording (ms, ascending).
    keypoints: list[int] = field(default_factory=list)
    # Why the recording ended by itself: "auto_stop", "low_disk" (less than 1 GB free),
    # "capture_failed" or "recovered" (the window died; finished afterwards). None when
    # the user stopped it.
    stop_reason: str | None = None

    @property
    def in_progress(self) -> bool:
        """A recording that was never finished (written at Record, ``finished_at`` null)."""
        return self.finished_at is None

    def keypoints_to_dict(self) -> list[dict[str, int]]:
        times = sorted({int(t) for t in self.keypoints if int(t) >= 0})
        return [{"index": i + 1, "time_ms": t} for i, t in enumerate(times)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "recorded_duration_ms": self.recorded_duration_ms,
            "timeline": "all times are milliseconds since the recording started; "
            "frame names and transcript segments share this timeline",
            "auto_stopped": self.auto_stopped,
            "auto_stop_minutes": self.auto_stop_minutes,
            "stop_reason": self.stop_reason,
            "capture_interval_ms": self.capture_interval_ms,
            "image_format": self.image_format,
            "jpeg_quality": self.jpeg_quality,
            "screens": [s.to_manifest() for s in self.screens],
            "cursor": "each frame sidecar JSON holds {screen, x, y}: the screen the cursor was on "
            "and its position in pixels of that screen's image, origin top-left",
            "audio": self.audio.to_dict(),
            "frames": self.frames.to_dict(),
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "keypoints": self.keypoints_to_dict(),
            "keypoints_note": "moments the user marked as important while recording "
            "(Keypoint button, Ctrl+Alt+K); the editor shows them as green lines and snaps "
            "the crop to them",
            "platform": self.platform,
            "plugin_version": self.plugin_version,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Manifest:
        audio = data.get("audio") or {}
        frames = data.get("frames") or {}
        transcript = data.get("transcript")
        return cls(
            session_id=data["session_id"],
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at"),
            recorded_duration_ms=int(data.get("recorded_duration_ms", 0)),
            capture_interval_ms=int(data.get("capture_interval_ms", 0)),
            image_format=data.get("image_format", "png"),
            jpeg_quality=data.get("jpeg_quality"),
            screens=[Screen.from_manifest(s) for s in data.get("screens", [])],
            audio=AudioInfo(
                file=audio.get("file", AUDIO_FILE),
                device=audio.get("device"),
                sample_rate=int(audio.get("sample_rate", 0)),
                channels=int(audio.get("channels", 1)),
                duration_ms=int(audio.get("duration_ms", 0)),
            ),
            frames=FramesInfo(
                dir=frames.get("dir", FRAMES_DIR_NAME),
                info_dir=frames.get("info_dir", FRAMES_INFO_DIR_NAME),
                count=int(frames.get("count", 0)),
                dropped_ticks=int(frames.get("dropped_ticks", 0)),
                failed_grabs=int(frames.get("failed_grabs", 0)),
            ),
            transcript=TranscriptInfo(
                txt=transcript.get("txt", "transcript.txt"),
                json_file=transcript.get("json", "transcript.json"),
                language=transcript.get("language"),
                model=transcript.get("model"),
                backend=transcript.get("backend"),
                segments=int(transcript.get("segments", 0)),
                duration_s=transcript.get("transcription_seconds"),
            )
            if transcript
            else None,
            auto_stopped=bool(data.get("auto_stopped", False)),
            auto_stop_minutes=data.get("auto_stop_minutes"),
            platform=data.get("platform", ""),
            plugin_version=data.get("plugin_version", ""),
            notes=list(data.get("notes", [])),
            keypoints=_keypoints_from(data.get("keypoints")),
            stop_reason=data.get("stop_reason"),
        )


def _keypoints_from(raw: Any) -> list[int]:
    """``[{"index", "time_ms"}, ...]`` or plain ``[ms, ...]``; sorted, unique, >= 0."""
    if not isinstance(raw, list):
        return []
    times: set[int] = set()
    for item in raw:
        value = item.get("time_ms") if isinstance(item, dict) else item
        try:
            ms = int(value)
        except (TypeError, ValueError):
            continue
        if ms >= 0:
            times.add(ms)
    return sorted(times)


def write_manifest(path: Path, manifest: Manifest) -> None:
    """Atomic: the manifest is rewritten while recording (keypoints) and must never be
    left half written when the process dies."""
    atomic_write_text(path, json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False) + "\n")


def read_manifest(path: Path) -> Manifest:
    return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


__all__ = [
    "SCHEMA_VERSION",
    "AudioInfo",
    "FramesInfo",
    "Manifest",
    "TranscriptInfo",
    "read_manifest",
    "write_manifest",
]
