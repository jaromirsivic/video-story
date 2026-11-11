"""``manifest.json`` - everything an agent needs to interpret a recording."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text
from .input_events import MAX_ELEMENT_TEXT_CHARS, MouseCapabilities, MouseSystemParams
from .screens import Screen
from .session import (
    AUDIO_DIR_NAME,
    AUDIO_FILE_NAME,
    COMPUTER_AUDIO_FILE_NAME,
    FRAMES_DIR_NAME,
    FRAMES_INFO_DIR_NAME,
    INPUT_DIR_NAME,
    MOUSE_FILE_NAME,
)

SCHEMA_VERSION = 2  # 2: audio/audio.wav, frames/ images only, sidecars in frames_info/

AUDIO_FILE = f"{AUDIO_DIR_NAME}/{AUDIO_FILE_NAME}"
COMPUTER_AUDIO_FILE = f"{AUDIO_DIR_NAME}/{COMPUTER_AUDIO_FILE_NAME}"
MOUSE_FILE = f"{INPUT_DIR_NAME}/{MOUSE_FILE_NAME}"


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

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_file: str = AUDIO_FILE) -> AudioInfo:
        return cls(
            file=data.get("file", default_file),
            device=data.get("device"),
            sample_rate=int(data.get("sample_rate", 0)),
            channels=int(data.get("channels", 1)),
            duration_ms=int(data.get("duration_ms", 0)),
        )


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
class MouseInfo:
    """``input/mouse.jsonl``: what the platform could record and how much was recorded."""

    file: str = MOUSE_FILE
    platform: str = ""  # e.g. "windows-ll-hook"
    capabilities: MouseCapabilities = field(default_factory=MouseCapabilities)
    params: MouseSystemParams = field(default_factory=MouseSystemParams)
    max_content_chars: int = MAX_ELEMENT_TEXT_CHARS
    events: int = 0
    elements: int = 0
    contents: int = 0
    windows: int = 0
    element_timeouts: int = 0
    hook_restarts: int = 0
    error: str | None = None  # the source could not start; the file may be missing

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "file": self.file,
            "platform": self.platform,
            "capabilities": self.capabilities.to_dict(),
            "params": self.params.to_dict(),
            "max_content_chars": self.max_content_chars,
            "events": self.events,
            "elements": self.elements,
            "contents": self.contents,
            "windows": self.windows,
            "element_timeouts": self.element_timeouts,
            "hook_restarts": self.hook_restarts,
        }
        if self.error:
            data["error"] = self.error
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MouseInfo:
        def count(key: str) -> int:
            try:
                return max(0, int(data.get(key, 0)))
            except (TypeError, ValueError):
                return 0

        return cls(
            file=str(data.get("file") or MOUSE_FILE),
            platform=str(data.get("platform") or ""),
            capabilities=MouseCapabilities.from_dict(data.get("capabilities")),
            params=MouseSystemParams.from_dict(data.get("params")),
            max_content_chars=count("max_content_chars") or MAX_ELEMENT_TEXT_CHARS,
            events=count("events"),
            elements=count("elements"),
            contents=count("contents"),
            windows=count("windows"),
            element_timeouts=count("element_timeouts"),
            hook_restarts=count("hook_restarts"),
            error=data.get("error") or None,
        )


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
    # What this computer played meanwhile (``audio/computer.wav``); None when it was not
    # recorded (switched off, not Windows, no output device).
    computer_audio: AudioInfo | None = None
    frames: FramesInfo = field(default_factory=FramesInfo)
    transcript: TranscriptInfo | None = None
    # Mouse actions and the elements under the mouse (``input/mouse.jsonl``); None when
    # Mouse was switched off or the platform cannot record it.
    mouse: MouseInfo | None = None
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
    # Transcription while recording (mode, threads, priority, windows, how far it got at
    # Stop, ...; see host.live_transcription); None when it did not run.
    live_transcription: dict[str, Any] | None = None

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
            "computer_audio": self.computer_audio.to_dict() if self.computer_audio else None,
            "audio_note": "audio is the microphone; computer_audio (when recorded) is what "
            "this computer played; both start at 0 ms. The transcript is made from the "
            "tracks switched on in the editor (audio/tracks.json, both by default)",
            "frames": self.frames.to_dict(),
            "transcript": self.transcript.to_dict() if self.transcript else None,
            "mouse": self.mouse.to_dict() if self.mouse else None,
            "mouse_note": "input/mouse.jsonl (when mouse is not null): clicks, drags, wheel "
            "and two-finger touchpad gestures with the window, the element under the mouse "
            "and its content (at most max_content_chars, never password fields); times on "
            "the recording timeline, screen/x/y like the cursor in the frame sidecars",
            "live_transcription": self.live_transcription,
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
            audio=AudioInfo.from_dict(audio),
            computer_audio=AudioInfo.from_dict(data["computer_audio"], COMPUTER_AUDIO_FILE)
            if isinstance(data.get("computer_audio"), dict)
            else None,
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
            mouse=MouseInfo.from_dict(data["mouse"])
            if isinstance(data.get("mouse"), dict)
            else None,
            keypoints=_keypoints_from(data.get("keypoints")),
            stop_reason=data.get("stop_reason"),
            live_transcription=data["live_transcription"]
            if isinstance(data.get("live_transcription"), dict)
            else None,
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
    "AUDIO_FILE",
    "COMPUTER_AUDIO_FILE",
    "MOUSE_FILE",
    "SCHEMA_VERSION",
    "AudioInfo",
    "FramesInfo",
    "Manifest",
    "MouseInfo",
    "TranscriptInfo",
    "read_manifest",
    "write_manifest",
]
