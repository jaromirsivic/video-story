"""Environment check (``video-story doctor``) and setup (``video-story setup``).

:func:`run_doctor` never prints; it returns a JSON-serialisable report that the CLI
emits. Downloads happen only when explicitly requested (``fix`` for FFmpeg,
``download_model`` for the whisper model).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from video_story import __version__
from video_story.capture.audio_recorder import AudioError, list_input_devices
from video_story.core.config import MODEL_FILE_NAME, Config, config_path
from video_story.core.screens import assign_screen_ids
from video_story.host.downloads import DownloadError, download_file, file_digest
from video_story.platform import get_platform
from video_story.transcribe.ffmpeg import (
    FfmpegNotFound,
    ensure_ffmpeg,
    ffmpeg_version,
    find_ffmpeg,
)

log = logging.getLogger(__name__)

# Whisper large-v3-turbo, 5-bit quantised (ggml); checksum as published by Hugging Face.
MODEL_SHA256 = "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
MODEL_SIZE = 574_041_195
MODEL_SIZE_TEXT = "548 MB"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
MIN_PYTHON = (3, 12)
SETUP_HINT = "Run `video-story setup`."


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "hint": self.hint}


@dataclass
class DoctorReport:
    ok: bool = True
    platform: str = ""
    checks: list[Check] = field(default_factory=list)
    ffmpeg: Path | None = None
    model: Path | None = None
    config_path: Path | None = None

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        if not check.ok:
            self.ok = False
        return check

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "version": __version__,
            "platform": self.platform,
            "checks": [c.to_dict() for c in self.checks],
            "ffmpeg": str(self.ffmpeg) if self.ffmpeg else None,
            "model": str(self.model) if self.model else None,
            "config_path": str(self.config_path) if self.config_path else None,
        }


# Individual checks --------------------------------------------------------------


def check_python() -> Check:
    version = ".".join(str(v) for v in sys.version_info[:3])
    ok = sys.version_info[:2] >= MIN_PYTHON
    return Check(
        "python",
        ok,
        f"Python {version} ({sys.executable})",
        "" if ok else f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.",
    )


def check_platform(report: DoctorReport) -> tuple[Check, Any]:
    try:
        platform = get_platform()
    except Exception as exc:
        report.platform = sys.platform
        return Check("platform", False, f"{sys.platform}: {exc}", "Unsupported platform."), None
    report.platform = platform.name
    status = platform.support_status()
    if status.supported:
        return Check("platform", True, f"{platform.name}: supported"), platform
    return (
        Check(
            "platform",
            False,
            f"{platform.name}: {status.reason or 'not supported'}",
            "Screen capture is not available in this session (see docs/installation.md).",
        ),
        platform,
    )


def check_screens(platform: Any) -> Check:
    if platform is None:
        return Check("screens", False, "platform unavailable", "Fix the platform check first.")
    try:
        platform.prepare_process()
        screens = assign_screen_ids(platform.list_screens())
    except Exception as exc:
        return Check("screens", False, f"cannot enumerate screens: {exc}", "")
    parts = [f"{s.id} {s.width}x{s.height}" + (" (primary)" if s.primary else "") for s in screens]
    return Check("screens", True, f"{len(screens)} screen(s): " + ", ".join(parts))


def check_audio(config: Config) -> Check:
    try:
        devices = list_input_devices()
    except AudioError as exc:
        return Check("audio", False, str(exc), "Check that an audio input device is connected.")
    except Exception as exc:  # sounddevice / PortAudio missing
        return Check(
            "audio", False, f"cannot query audio devices: {exc}", "Install PortAudio/sounddevice."
        )
    if not devices:
        return Check("audio", False, "no audio input device", "Connect a microphone.")
    default = next((d for d in devices if d.is_default), None)
    chosen = next((d for d in devices if d.name == config.audio_input), None)
    if config.audio_input and chosen is None:
        return Check(
            "audio",
            True,
            f"configured input {config.audio_input!r} not found; "
            f"default will be used: {(default or devices[0]).name}",
            "Choose an available device in Settings.",
        )
    used = chosen or default or devices[0]
    label = "configured" if chosen else ("default" if default else "first available")
    return Check(
        "audio",
        True,
        f"{len(devices)} input device(s); {label}: {used.name} ({used.host_api})",
    )


def check_computer_audio(config: Config) -> Check:
    """Recording what this computer plays is optional: never a failure."""
    if not config.computer_audio:
        return Check("computer_audio", True, "switched off (computer_audio false)")
    from video_story.capture.computer_audio import computer_audio_support

    support = computer_audio_support()
    if support.supported:
        return Check("computer_audio", True, f"recorded from {support.device} (loopback)")
    return Check("computer_audio", True, f"not recorded: {support.reason}")


def check_mouse(config: Config, platform: Any) -> Check:
    """Recording the mouse is optional: never a failure."""
    if not config.mouse_actions:
        return Check("mouse", True, "switched off (mouse_actions false)")
    try:
        offer = getattr(platform, "mouse_source", None)
        source = offer() if offer is not None else None
    except Exception as exc:  # noqa: BLE001
        return Check("mouse", True, f"not recorded: {exc}")
    if source is None:
        name = platform.name if platform is not None else "this platform"
        return Check("mouse", True, f"not recorded: not available on {name} yet")
    problem = source.probe()
    if problem:
        return Check("mouse", True, f"not recorded: {problem}")
    return Check("mouse", True, f"recorded ({source.name})")


def check_ffmpeg(config: Config, fix: bool, report: DoctorReport) -> Check:
    path = find_ffmpeg(config)
    if path is None and fix:
        try:
            path = ensure_ffmpeg(config, download=True)
        except (FfmpegNotFound, DownloadError) as exc:
            return Check("ffmpeg", False, str(exc), "Install ffmpeg and set ffmpeg_path.")
    if path is None:
        return Check(
            "ffmpeg",
            False,
            "ffmpeg not found (config ffmpeg_path, PATH, bundled copy)",
            f"{SETUP_HINT} It downloads a static FFmpeg build, "
            "or install ffmpeg and set ffmpeg_path in config.json.",
        )
    version = ffmpeg_version(path)
    if version is None:
        return Check("ffmpeg", False, f"{path} does not run", "Reinstall ffmpeg.")
    report.ffmpeg = path
    return Check("ffmpeg", True, f"ffmpeg {version} at {path}")


def model_status(path: Path) -> tuple[bool, str]:
    """``(ok, detail)`` for the model file: present and SHA-256 verified."""
    if not path.is_file():
        return False, f"model not found: {path}"
    size = path.stat().st_size
    digest = file_digest(path, "sha256")
    if digest != MODEL_SHA256:
        return (
            False,
            f"model {path} has SHA-256 {digest}, expected {MODEL_SHA256} ({size} bytes)",
        )
    return True, f"{MODEL_FILE_NAME} OK ({size} bytes, SHA-256 verified) at {path}"


def check_model(config: Config, download_model: bool, report: DoctorReport) -> Check:
    path = config.resolved_model_path()
    ok, detail = model_status(path)
    if not ok and download_model:
        try:
            log.info("Downloading whisper model to %s", path)
            download_file(MODEL_URL, path, expected_sha256=MODEL_SHA256)
        except DownloadError as exc:
            return Check("model", False, f"download failed: {exc}", "Retry `video-story setup`.")
        ok, detail = model_status(path)
    if ok:
        report.model = path
        return Check("model", True, detail)
    return Check(
        "model",
        False,
        detail,
        f"{SETUP_HINT} It downloads {MODEL_FILE_NAME} ({MODEL_SIZE_TEXT}) from Hugging Face, "
        "or set whisper_model_path to an existing copy.",
    )


def check_backend(config: Config) -> Check:
    if config.whisper_backend == "whisper-cli":
        cli = config.whisper_cli_path
        if cli is None:
            return Check(
                "backend", False, "whisper-cli selected but whisper_cli_path is not set", ""
            )
        if not Path(cli).is_file():
            return Check("backend", False, f"whisper-cli not found: {cli}", "Fix whisper_cli_path.")
        return Check("backend", True, f"whisper-cli at {cli}")
    try:
        spec = importlib.util.find_spec("pywhispercpp")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return Check(
            "backend",
            False,
            "pywhispercpp is not installed",
            "Run `uv sync` or set whisper_backend to whisper-cli.",
        )
    try:
        from importlib.metadata import version

        detail = f"pywhispercpp {version('pywhispercpp')} ({config.whisper_threads} threads)"
    except Exception:
        detail = f"pywhispercpp ({config.whisper_threads} threads)"
    return Check("backend", True, detail)


def check_recording_folder(config: Config) -> Check:
    folder = config.recording_folder
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "recording_folder",
            False,
            f"{folder}: {exc}",
            "Choose a writable Recording Folder in Settings.",
        )
    return Check("recording_folder", True, f"{folder} is writable")


def check_config(report: DoctorReport) -> Check:
    try:
        path = config_path()
    except Exception as exc:
        return Check("config", False, f"cannot determine config path: {exc}", "")
    report.config_path = path
    state = "exists" if path.exists() else "not created yet (defaults in use)"
    return Check("config", True, f"{path} {state}")


# Entry point ----------------------------------------------------------------------


def run_doctor(config: Config, fix: bool = False, download_model: bool = False) -> dict[str, Any]:
    """Check the environment; with ``fix`` download FFmpeg and with ``download_model``
    the whisper model when they are missing. Returns a JSON-serialisable report."""
    report = DoctorReport()
    report.add(check_python())
    platform_check, platform = check_platform(report)
    report.add(platform_check)
    report.add(check_screens(platform))
    report.add(check_audio(config))
    report.add(check_computer_audio(config))
    report.add(check_mouse(config, platform))
    report.add(check_ffmpeg(config, fix, report))
    report.add(check_model(config, download_model, report))
    report.add(check_backend(config))
    report.add(check_recording_folder(config))
    report.add(check_config(report))
    return report.to_dict()


__all__ = [
    "MODEL_SHA256",
    "MODEL_SIZE_TEXT",
    "MODEL_SIZE",
    "MODEL_URL",
    "Check",
    "DoctorReport",
    "model_status",
    "run_doctor",
]
