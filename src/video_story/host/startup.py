"""One launch request, one GUI-owned acknowledgement. No background task service."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from video_story.core.config import Config, config_from_dict, load_config
from video_story.core.fileio import atomic_write_text, read_json_with_retry
from video_story.host.editor_lock import EditorLock
from video_story.host.errors import HostError
from video_story.host.readiness import require_ready
from video_story.platform.process import pid_alive

START_TIMEOUT_S = 30.0


@contextmanager
def launch_guard(config: Config, mode: str):
    """Serialize window creation/attach across CLI processes and concurrent MCP calls."""
    lock = EditorLock(
        config.recording_folder / ".work" / f"start-{mode}.lock", remove_on_release=False
    )
    if not lock.acquire():
        raise HostError(
            "START_FAILED",
            "A window start is already in progress. Check its session.",
            24,
            reason="start_in_progress",
        )
    try:
        yield
    finally:
        lock.release()


def response_path(request: Path) -> Path:
    return request.with_suffix(".result.json")


def cleanup_completed(request: Path) -> None:
    """Retain failed startup evidence. Retire successful acknowledgements after final wait."""
    try:
        data = read_request(request)
        result = read_json_with_retry(response_path(request), attempts=1)
        if data.get("mode") in {"run", "edit"} and result.get("phase") == "ready":
            response_path(request).unlink(missing_ok=True)
            request.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass  # cleanup must not hide a completed user session


def editor_request(editor_session: Path) -> Path:
    return editor_session.with_suffix(".startup.json")


def create_request(path: Path, config: Config, mode: str, session_id: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "config": config.to_json_dict(),
                "mode": mode,
                "session_id": session_id,
                "deadline": time.time() + START_TIMEOUT_S,
                "owner_pid": os.getpid(),
            }
        ),
    )
    return path


def read_request(path: Path) -> dict[str, Any]:
    return read_json_with_retry(path)


def valid(path: Path) -> bool:
    try:
        return time.time() < read_request(path)["deadline"]
    except (OSError, ValueError, KeyError):
        return False


def ensure_valid(request: Path | None) -> None:
    if request is not None and not valid(request):
        raise HostError("START_FAILED", "Startup request expired.", 24)


def acknowledge(path: Path, phase: str, **details: Any) -> None:
    # Only the GUI writes this file. Once ready, later close/Process belong to the session.
    atomic_write_text(
        response_path(path), json.dumps({"phase": phase, "pid": os.getpid(), **details})
    )


def prepare(config_file: Path | None, request: Path | None, mode: str) -> Config:
    if request is None:
        config = load_config(config_file)
        require_ready(config)
        return config
    data = read_request(request)
    if not valid(request) or data["mode"] != mode:
        raise HostError("START_FAILED", "Startup request expired or has the wrong mode.", 24)
    acknowledge(request, "starting")
    return config_from_dict(data["config"])


def fail(request: Path | None, error: Exception) -> None:
    if request is not None:
        payload = (
            error.to_dict()
            if isinstance(error, HostError)
            else {"code": "START_FAILED", "message": str(error)}
        )
        acknowledge(request, "error", exit_code=getattr(error, "exit_code", 24), **payload)


def wait_ready(request: Path, pid: int, log: Path, **identity: Any) -> dict[str, Any]:
    deadline = read_request(request)["deadline"]
    while True:
        try:
            result = read_json_with_retry(response_path(request), attempts=1)
        except (OSError, ValueError):
            result = {}
        if result.get("phase") == "ready":
            return result
        if result.get("phase") == "error":
            raise HostError(
                result.get("code", "START_FAILED"),
                result.get("message", "GUI failed"),
                result.get("exit_code", 24),
                startup_file=str(request),
                log=str(log),
                **identity,
            )
        reason = "readiness_timeout" if time.time() >= deadline else None
        if pid > 0 and not pid_alive(pid):
            reason = "process_exited"
        if reason:
            raise HostError(
                "START_FAILED",
                f"Window did not become ready: {reason}.",
                24,
                reason=reason,
                startup_file=str(request),
                pid=pid,
                log=str(log),
                **identity,
            )
        time.sleep(min(0.05, max(0.0, deadline - time.time())))


def watch_window(window: Any, request: Path | None, *, editor: bool = False) -> None:
    """GUI thread only. Acknowledge after its event loop and requested recording are ready."""
    if request is None:
        return
    from PySide6.QtCore import QTimer

    timer = QTimer(window)
    timer.setInterval(25)
    wanted = read_request(request)["session_id"]
    if editor:
        window._startup_request = request

    def finish() -> None:
        timer.stop()
        if editor:
            window.recording_loaded.disconnect(load_result)
            if getattr(window, "_startup_request", None) == request:
                window._startup_request = None
        timer.deleteLater()

    def load_result(recording: Any) -> None:
        if recording is None and timer.isActive():
            fail(request, RuntimeError("The requested recording could not be loaded."))
            finish()

    if editor:
        window.recording_loaded.connect(load_result)

    def poll() -> None:
        if not valid(request):
            fail(request, RuntimeError("readiness_timeout"))
            finish()
            return
        if not window.isVisible():
            return
        if editor:
            recording = window.recording
            if recording is None or recording.paths.session_id != wanted:
                return
        acknowledge(request, "ready", session_id=wanted)
        finish()

    timer.timeout.connect(poll)
    timer.start()
