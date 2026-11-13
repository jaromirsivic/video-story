"""Explicit component installation, independent of screens and audio devices."""

import logging
import time
from pathlib import Path
from typing import Any

from video_story.core.config import Config, default_model_path, ensure_state_root, save_config
from video_story.core.model_info import MODEL_SHA256, MODEL_URL
from video_story.host.downloads import download_file, file_digest
from video_story.host.editor_lock import EditorLock
from video_story.host.errors import HostError
from video_story.transcribe.ffmpeg import (
    bundled_ffmpeg_dir,
    download_ffmpeg,
    ffmpeg_usable,
    find_ffmpeg,
)

log = logging.getLogger(__name__)


def install_components(config: Config, config_file: Path | None = None) -> dict[str, Any]:
    root = ensure_state_root()
    lock = EditorLock(root / "setup.lock", remove_on_release=False)
    if not lock.acquire():
        raise HostError(
            "SETUP_IN_PROGRESS",
            "Another setup owns the component installation lock.",
            4,
            lock_file=str(lock.path),
        )
    try:
        results = []
        for component in ("model", "ffmpeg"):
            last_report = 0.0

            def progress(done, total, component=component):
                nonlocal last_report
                if time.monotonic() - last_report >= 2 or done == total:
                    log.info("Downloading %s: %d / %s bytes", component, done, total or "unknown")
                    last_report = time.monotonic()

            try:
                if component == "model":
                    path = config.resolved_model_path()
                    if config.whisper_model_path is not None:
                        if not path.is_file() or path.stat().st_size == 0:
                            raise RuntimeError(
                                "Configured custom model is missing/empty. "
                                "Restore it or clear whisper_model_path explicitly."
                            )
                        results.append(
                            {
                                "component": component,
                                "status": "existing",
                                "path": str(path),
                                "verification": "custom model preserved",
                            }
                        )
                        continue
                    good = path.is_file() and file_digest(path, "sha256") == MODEL_SHA256
                    if not good:
                        path = default_model_path()
                        download_file(
                            MODEL_URL, path, expected_sha256=MODEL_SHA256, progress=progress
                        )
                else:
                    path = find_ffmpeg(config)
                    good = path is not None and ffmpeg_usable(path)
                    if not good:
                        previous = path
                        path = download_ffmpeg(bundled_ffmpeg_dir(), progress=progress)
                        if not ffmpeg_usable(path):
                            raise RuntimeError(
                                "Downloaded FFmpeg lacks required encoders or cannot run."
                            )
                        # An unusable configured/PATH copy would otherwise keep winning discovery.
                        if previous is not None:
                            config = config.with_values(ffmpeg_path=path)
                            save_config(config, config_file)
                results.append(
                    {
                        "component": component,
                        "status": "existing" if good else "installed",
                        "path": str(path),
                    }
                )
            except (OSError, RuntimeError, ValueError) as exc:
                results.append({"component": component, "status": "failed", "message": str(exc)})
        failed = [r for r in results if r["status"] == "failed"]
        return {
            "ok": not failed,
            "code": "SETUP_FAILED" if failed else "OK",
            "components": results,
            "missing": [r["component"] for r in failed],
            "state_root": str(root),
            "ffmpeg_path": str(config.ffmpeg_path) if config.ffmpeg_path else None,
        }
    finally:
        lock.release()
