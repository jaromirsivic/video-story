"""Plugin configuration (`config.json`).

The configuration lives in the plugin installation directory (the repository root)
as required by the specification. When that directory is not writable the user
configuration directory is used instead and the chosen path is logged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_config_dir, user_videos_dir

from video_story.core.fileio import atomic_write_text

log = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.json"
CONFIG_PATH_ENV = "VIDEO_STORY_CONFIG"

CAPTURE_INTERVAL_MIN_MS = 1
CAPTURE_INTERVAL_MAX_MS = 1000
CAPTURE_INTERVAL_DEFAULT_MS = 500

JPEG_QUALITY_MIN = 20
JPEG_QUALITY_MAX = 100
JPEG_QUALITY_DEFAULT = 70

AUTO_STOP_MIN_MINUTES = 1
AUTO_STOP_MAX_MINUTES = 1440
AUTO_STOP_DEFAULT_MINUTES = 120

# Recordings older than this are archived (MP4 per screen) when the editor starts.
ARCHIVE_AFTER_MIN_DAYS = 1
ARCHIVE_AFTER_MAX_DAYS = 3650
ARCHIVE_AFTER_DEFAULT_DAYS = 30

EXPORT_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow")
EXPORT_PRESET_DEFAULT = "medium"
EXPORT_CRF_DEFAULT = 18
EXPORT_AUDIO_BITRATE_K_DEFAULT = 192
EXPORT_FPS = 24

IMAGE_FORMATS = ("png", "jpeg")
IMAGE_FORMAT_DEFAULT = "jpeg"
WHISPER_BACKENDS = ("pywhispercpp", "whisper-cli")
MODEL_FILE_NAME = "ggml-large-v3-turbo-q5_0.bin"

ImageFormat = Literal["png", "jpeg"]
WhisperBackend = Literal["pywhispercpp", "whisper-cli"]
LiveTranscriptionMode = Literal["off", "light", "faster"]
LIVE_TRANSCRIPTION_MODES = ("off", "light", "faster")
LIVE_TRANSCRIPTION_DEFAULT = "light"


class ConfigError(ValueError):
    """Raised when a configuration value is invalid."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(f"{field_name}: {message}")
        self.field_name = field_name
        self.message = message


def plugin_root() -> Path:
    """Return the plugin installation directory (repository root).

    The package is installed in editable mode from ``src/video_story``; the root is
    therefore three levels above this file. ``VIDEO_STORY_ROOT`` overrides it.
    """
    override = os.environ.get("VIDEO_STORY_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def default_recording_folder() -> Path:
    """Platform specific default for the Recording Folder setting."""
    base = Path(user_videos_dir())
    if sys.platform == "darwin":
        base = Path.home() / "Movies"
    return base / "VideoStory"


def default_model_path() -> Path:
    return plugin_root() / "models" / MODEL_FILE_NAME


def default_whisper_threads() -> int:
    """All logical CPUs up to 32: large-v3-turbo scales well (32 threads: 1.6x faster than 8)."""
    return max(1, min(32, os.cpu_count() or 4))


@dataclass(frozen=True)
class Config:
    """Validated plugin configuration."""

    recording_folder: Path = field(default_factory=default_recording_folder)
    capture_interval_ms: int = CAPTURE_INTERVAL_DEFAULT_MS
    image_format: ImageFormat = IMAGE_FORMAT_DEFAULT  # type: ignore[assignment]
    jpeg_quality: int = JPEG_QUALITY_DEFAULT
    audio_input: str | None = None
    computer_audio: bool = True  # also record what this computer plays (Windows only)
    # Mouse actions plus everything about them: window titles, the elements under the
    # mouse and their content (at most 4096 characters, never password fields).
    mouse_actions: bool = True
    # Transcribe while recording: "off" (only in the editor), "light" (low priority, a
    # quarter of the CPUs) or "faster" (half of the CPUs, slightly below normal).
    live_transcription: LiveTranscriptionMode = LIVE_TRANSCRIPTION_DEFAULT  # type: ignore[assignment]
    auto_stop_minutes: int = AUTO_STOP_DEFAULT_MINUTES
    archive_after_days: int = ARCHIVE_AFTER_DEFAULT_DAYS
    export_preset: str = EXPORT_PRESET_DEFAULT
    export_crf: int = EXPORT_CRF_DEFAULT
    export_audio_bitrate_k: int = EXPORT_AUDIO_BITRATE_K_DEFAULT
    editor_draw_cursor: bool = True
    whisper_model_path: Path | None = None
    whisper_backend: WhisperBackend = "pywhispercpp"
    whisper_cli_path: Path | None = None
    whisper_threads: int = field(default_factory=default_whisper_threads)
    ffmpeg_path: Path | None = None

    # Derived helpers -------------------------------------------------------

    @property
    def auto_stop_ms(self) -> int:
        return self.auto_stop_minutes * 60_000

    @property
    def image_extension(self) -> str:
        return "png" if self.image_format == "png" else "jpg"

    def resolved_model_path(self) -> Path:
        return self.whisper_model_path or default_model_path()

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, Path):
                data[key] = str(value)
        return data

    def with_values(self, **changes: Any) -> Config:
        """Return a validated copy with ``changes`` applied."""
        merged = self.to_json_dict()
        merged.update(changes)
        return config_from_dict(merged)


# Validation ------------------------------------------------------------------


def _require_int(data: dict[str, Any], key: str, lo: int, hi: int, default: int) -> int:
    value = data.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise ConfigError(key, f"must be an integer between {lo} and {hi}")
    if not lo <= value <= hi:
        raise ConfigError(key, f"must be between {lo} and {hi} (got {value})")
    return value


def _require_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ConfigError(key, "must be true or false")


def _optional_path(data: dict[str, Any], key: str) -> Path | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str | os.PathLike):
        raise ConfigError(key, "must be a path string or null")
    return Path(value).expanduser()


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(key, "must be a string or null")
    return value


def _choice(data: dict[str, Any], key: str, choices: tuple[str, ...], default: str) -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or value.lower() not in choices:
        raise ConfigError(key, f"must be one of {', '.join(choices)}")
    return value.lower()


def config_from_dict(data: dict[str, Any]) -> Config:
    """Validate a raw dictionary (from JSON or the UI) into a :class:`Config`."""
    if not isinstance(data, dict):
        raise ConfigError("config", "must be a JSON object")
    folder = _optional_path(data, "recording_folder") or default_recording_folder()
    threads_default = default_whisper_threads()
    return Config(
        recording_folder=folder,
        capture_interval_ms=_require_int(
            data,
            "capture_interval_ms",
            CAPTURE_INTERVAL_MIN_MS,
            CAPTURE_INTERVAL_MAX_MS,
            CAPTURE_INTERVAL_DEFAULT_MS,
        ),
        image_format=_choice(data, "image_format", IMAGE_FORMATS, IMAGE_FORMAT_DEFAULT),  # type: ignore[arg-type]
        jpeg_quality=_require_int(
            data, "jpeg_quality", JPEG_QUALITY_MIN, JPEG_QUALITY_MAX, JPEG_QUALITY_DEFAULT
        ),
        audio_input=_optional_str(data, "audio_input"),
        computer_audio=_require_bool(data, "computer_audio", True),
        mouse_actions=_require_bool(data, "mouse_actions", True),
        live_transcription=_choice(  # type: ignore[arg-type]
            data, "live_transcription", LIVE_TRANSCRIPTION_MODES, LIVE_TRANSCRIPTION_DEFAULT
        ),
        auto_stop_minutes=_require_int(
            data,
            "auto_stop_minutes",
            AUTO_STOP_MIN_MINUTES,
            AUTO_STOP_MAX_MINUTES,
            AUTO_STOP_DEFAULT_MINUTES,
        ),
        archive_after_days=_require_int(
            data,
            "archive_after_days",
            ARCHIVE_AFTER_MIN_DAYS,
            ARCHIVE_AFTER_MAX_DAYS,
            ARCHIVE_AFTER_DEFAULT_DAYS,
        ),
        export_preset=_choice(data, "export_preset", EXPORT_PRESETS, EXPORT_PRESET_DEFAULT),
        export_crf=_require_int(data, "export_crf", 0, 51, EXPORT_CRF_DEFAULT),
        export_audio_bitrate_k=_require_int(
            data, "export_audio_bitrate_k", 32, 512, EXPORT_AUDIO_BITRATE_K_DEFAULT
        ),
        editor_draw_cursor=_require_bool(data, "editor_draw_cursor", True),
        whisper_model_path=_optional_path(data, "whisper_model_path"),
        whisper_backend=_choice(data, "whisper_backend", WHISPER_BACKENDS, "pywhispercpp"),  # type: ignore[arg-type]
        whisper_cli_path=_optional_path(data, "whisper_cli_path"),
        whisper_threads=_require_int(data, "whisper_threads", 1, 128, threads_default),
        ffmpeg_path=_optional_path(data, "ffmpeg_path"),
    )


# Persistence -----------------------------------------------------------------


def _is_writable_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def config_path() -> Path:
    """Location of ``config.json``: env override, plugin root, or user config dir."""
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    root = plugin_root()
    candidate = root / CONFIG_FILE_NAME
    if candidate.exists() or _is_writable_dir(root):
        return candidate
    fallback = Path(user_config_dir("VideoStory", appauthor=False)) / CONFIG_FILE_NAME
    log.warning("Plugin root %s is not writable; using %s", root, fallback)
    return fallback


def load_config(path: Path | None = None) -> Config:
    """Load the configuration, returning defaults when the file does not exist."""
    path = path or config_path()
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("config", f"cannot read {path}: {exc}") from exc
    return config_from_dict(raw)


def save_config(config: Config, path: Path | None = None) -> Path:
    """Write the configuration atomically and return the path used."""
    path = path or config_path()
    atomic_write_text(path, json.dumps(config.to_json_dict(), indent=2) + "\n")
    return path


def update_config(path: Path | None = None, **changes: Any) -> Config:
    """Load, apply ``changes`` and save; returns the new configuration."""
    current = load_config(path)
    updated = current.with_values(**changes)
    save_config(updated, path)
    return updated


__all__ = [
    "AUTO_STOP_DEFAULT_MINUTES",
    "AUTO_STOP_MAX_MINUTES",
    "AUTO_STOP_MIN_MINUTES",
    "CAPTURE_INTERVAL_DEFAULT_MS",
    "CAPTURE_INTERVAL_MAX_MS",
    "CAPTURE_INTERVAL_MIN_MS",
    "CONFIG_FILE_NAME",
    "EXPORT_AUDIO_BITRATE_K_DEFAULT",
    "EXPORT_CRF_DEFAULT",
    "EXPORT_FPS",
    "EXPORT_PRESETS",
    "EXPORT_PRESET_DEFAULT",
    "IMAGE_FORMATS",
    "IMAGE_FORMAT_DEFAULT",
    "JPEG_QUALITY_DEFAULT",
    "JPEG_QUALITY_MAX",
    "JPEG_QUALITY_MIN",
    "ARCHIVE_AFTER_DEFAULT_DAYS",
    "ARCHIVE_AFTER_MAX_DAYS",
    "ARCHIVE_AFTER_MIN_DAYS",
    "Config",
    "ConfigError",
    "config_from_dict",
    "config_path",
    "default_model_path",
    "default_recording_folder",
    "load_config",
    "plugin_root",
    "replace",
    "save_config",
    "update_config",
]
