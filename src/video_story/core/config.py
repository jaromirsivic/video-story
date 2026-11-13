"""Plugin configuration (`config.json`) and the two roots the plugin works with.

The *code root* holds the sources (``pyproject.toml``); in a distribution it may be
read-only and is replaced by every update. The *state root* holds everything the
plugin keeps between runs: ``config.json``, ``models/`` (the whisper model) and
``bin/ffmpeg/`` (a downloaded FFmpeg). See :func:`resolve_state_root` for which folder
that is. Recordings live elsewhere (``recording_folder``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from platformdirs import user_videos_dir

from video_story.core.fileio import atomic_write_text

log = logging.getLogger(__name__)

CONFIG_FILE_NAME = "config.json"
CONFIG_PATH_ENV = "VIDEO_STORY_CONFIG"
CODE_ROOT_ENV = "VIDEO_STORY_ROOT"  # overrides the code root (as for the launchers)
STATE_HOME_ENV = "VIDEO_STORY_HOME"  # overrides the state root (tests, special setups)
STATE_DIR_NAME = ".video-story"  # the state root in the user's home folder

CAPTURE_INTERVAL_MIN_MS = 1
CAPTURE_INTERVAL_MAX_MS = 1000
CAPTURE_INTERVAL_DEFAULT_MS = 500

JPEG_QUALITY_MIN = 20
JPEG_QUALITY_MAX = 100
JPEG_QUALITY_DEFAULT = 70

AUTO_STOP_MIN_MINUTES = 1
AUTO_STOP_MAX_MINUTES = 1440
AUTO_STOP_DEFAULT_MINUTES = 120

# Recordings older than this are archived (MP4 per screen) when the editor starts;
# 0 = never (nothing is archived automatically).
ARCHIVE_AFTER_NEVER = 0
ARCHIVE_AFTER_MIN_DAYS = ARCHIVE_AFTER_NEVER
ARCHIVE_AFTER_MAX_DAYS = 3650
ARCHIVE_AFTER_DEFAULT_DAYS = 30

# Ctrl while drawing snaps points to a grid with this spacing (logical pixels); the grid
# is shown on the white sketch canvas.
DRAWING_GRID_MIN_PX = 8
DRAWING_GRID_MAX_PX = 128
DRAWING_GRID_DEFAULT_PX = 16

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


class StateFolderError(RuntimeError):
    """The state root (or the file asked for) cannot be created or written."""

    code = "STATE_NOT_WRITABLE"


StateMode = Literal["environment", "development", "user"]

_STATE_REASONS: dict[str, str] = {
    "environment": f"{STATE_HOME_ENV} is set",
    "development": f"development: {CONFIG_FILE_NAME} is in the code folder",
    "user": "user folder",
}


@dataclass(frozen=True)
class StateRoot:
    """The folder with the plugin's own state and why it was chosen."""

    path: Path
    mode: StateMode

    @property
    def reason(self) -> str:
        return _STATE_REASONS[self.mode]


def code_root() -> Path:
    """The folder with the code (``pyproject.toml``), possibly read-only.

    The package is installed in editable mode from ``src/video_story``; the root is
    therefore three levels above this file. ``VIDEO_STORY_ROOT`` overrides it.
    """
    override = os.environ.get(CODE_ROOT_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]


def resolve_state_root() -> StateRoot:
    """Where ``config.json``, ``models/`` and ``bin/`` live, in this order:

    1. ``VIDEO_STORY_HOME`` when set;
    2. the code root when it contains ``config.json`` (a development checkout keeps
       its whole state next to the code, as before);
    3. ``~/.video-story`` (the same rule on Windows, Linux and macOS).

    Nothing is created here.
    """
    override = os.environ.get(STATE_HOME_ENV)
    if override:
        return StateRoot(Path(override).expanduser().resolve(), "environment")
    code = code_root()
    if (code / CONFIG_FILE_NAME).is_file():
        return StateRoot(code, "development")
    return StateRoot(Path.home() / STATE_DIR_NAME, "user")


def state_root() -> Path:
    return resolve_state_root().path


def ensure_state_root() -> Path:
    """Create the state root when needed and check that it is writable."""
    root = resolve_state_root()
    try:
        root.path.mkdir(parents=True, exist_ok=True)
        probe = root.path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StateFolderError(
            f"cannot write the Video Story state folder {root.path} ({root.reason}): {exc}. "
            f"Make it writable or set {STATE_HOME_ENV} to a writable folder."
        ) from exc
    return root.path


def default_recording_folder() -> Path:
    """Platform specific default for the Recording Folder setting."""
    base = Path(user_videos_dir())
    if sys.platform == "darwin":
        base = Path.home() / "Movies"
    return base / "VideoStory"


def default_model_path() -> Path:
    """Where ``setup`` downloads the model to: always the state root."""
    return state_root() / "models" / MODEL_FILE_NAME


def model_search_paths() -> list[Path]:
    """The model in the state root first, then in the code root (a bundled copy)."""
    paths = [default_model_path(), code_root() / "models" / MODEL_FILE_NAME]
    return list(dict.fromkeys(paths))


def find_default_model() -> Path:
    """The first existing model of :func:`model_search_paths`, else the download target."""
    return next((p for p in model_search_paths() if p.is_file()), default_model_path())


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
    archive_after_days: int = ARCHIVE_AFTER_DEFAULT_DAYS  # 0: never archive automatically
    drawing_grid_px: int = DRAWING_GRID_DEFAULT_PX
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
        """``whisper_model_path``, else the model in the state root, else the code root."""
        return self.whisper_model_path or find_default_model()

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
        drawing_grid_px=_require_int(
            data,
            "drawing_grid_px",
            DRAWING_GRID_MIN_PX,
            DRAWING_GRID_MAX_PX,
            DRAWING_GRID_DEFAULT_PX,
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


def config_path() -> Path:
    """Location of ``config.json``: ``VIDEO_STORY_CONFIG``, else the state root.

    Nothing is created here; a state root that cannot be written makes
    :func:`save_config` fail instead of writing somewhere else.
    """
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return state_root() / CONFIG_FILE_NAME


def config_location(
    explicit: Path | None = None, explicit_source: str = "--config"
) -> dict[str, Any]:
    """Where ``config.json`` is and why, with the state folder in use (``config path``).

    ``explicit`` is a file given by the caller (``--config``), named ``explicit_source``.
    """
    path = explicit or config_path()
    if explicit is not None:
        source = explicit_source
    elif os.environ.get(CONFIG_PATH_ENV):
        source = CONFIG_PATH_ENV
    else:
        source = "state folder"
    state = resolve_state_root()
    return {
        "path": str(path),
        "exists": path.exists(),
        "source": source,
        "state_root": str(state.path),
        "state_reason": state.reason,
    }


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
    try:
        atomic_write_text(path, json.dumps(config.to_json_dict(), indent=2) + "\n")
    except OSError as exc:
        raise StateFolderError(
            f"cannot write the configuration {path}: {exc}. Make the folder writable or set "
            f"{STATE_HOME_ENV} to a writable folder."
        ) from exc
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
    "ARCHIVE_AFTER_NEVER",
    "CODE_ROOT_ENV",
    "STATE_DIR_NAME",
    "STATE_HOME_ENV",
    "Config",
    "ConfigError",
    "StateFolderError",
    "StateRoot",
    "code_root",
    "config_from_dict",
    "config_location",
    "config_path",
    "default_model_path",
    "default_recording_folder",
    "ensure_state_root",
    "find_default_model",
    "load_config",
    "model_search_paths",
    "replace",
    "resolve_state_root",
    "save_config",
    "state_root",
    "update_config",
]
