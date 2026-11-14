"""Cheap, read-only preflight. No downloads, hashing, devices or model loading."""

import importlib.util
import os
from pathlib import Path
from typing import Any

from video_story.core.config import Config, resolve_state_root
from video_story.core.model_info import MODEL_SIZE
from video_story.host.errors import HostError
from video_story.transcribe.ffmpeg import find_ffmpeg


def _file_problem(path: Path | None, *, executable: bool = False) -> str | None:
    if path is None:
        return "missing"
    try:
        if not path.is_file():
            return "missing"
        if path.stat().st_size == 0:
            return "empty"
        if not os.access(path, os.R_OK):
            return "unreadable"
        if executable and os.name != "nt" and not os.access(path, os.X_OK):
            return "not_executable"
    except OSError:
        return "unreadable"
    return None


def check_readiness(config: Config) -> dict[str, Any]:
    model = config.resolved_model_path()
    ffmpeg = find_ffmpeg(config)
    problems = []
    model_reason = _file_problem(model)
    if model_reason is None and config.whisper_model_path is None:
        if model.stat().st_size != MODEL_SIZE:
            model_reason = "wrong_size"
    for component, path, reason in (
        ("model", model, model_reason),
        ("ffmpeg", ffmpeg, _file_problem(ffmpeg, executable=True)),
    ):
        if reason:
            problems.append(
                {"component": component, "reason": reason, "path": str(path) if path else None}
            )
    state = resolve_state_root()
    return {
        "ok": not problems,
        "missing": [p["component"] for p in problems],
        "problems": problems,
        "model": str(model) if model.is_file() else None,
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "state_root": str(state.path),
        "state_reason": state.reason,
    }


def require_ready(config: Config) -> dict[str, Any]:
    result = check_readiness(config)
    if not result["ok"]:
        raise HostError(
            "SETUP_REQUIRED",
            "Run video-story setup to prepare missing components.",
            4,
            **{k: v for k, v in result.items() if k != "ok"},
        )
    backend = "pywhispercpp" if config.whisper_backend == "pywhispercpp" else None
    missing = [
        name
        for name in ("PySide6", "numpy", "PIL", "sounddevice", "mss", backend)
        if name and importlib.util.find_spec(name) is None
    ]
    if missing:
        raise HostError(
            "ENVIRONMENT_INVALID",
            "Repair the project environment with uv sync --locked.",
            23,
            missing=missing,
        )
    if config.whisper_backend == "whisper-cli":
        reason = _file_problem(config.whisper_cli_path, executable=True)
        if reason:
            raise HostError(
                "CONFIG_INVALID",
                "Configured whisper-cli is not executable.",
                2,
                path=str(config.whisper_cli_path),
                reason=reason,
            )
    return result
