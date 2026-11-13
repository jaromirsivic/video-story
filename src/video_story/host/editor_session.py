"""Editor sessions: how an agent waits for the user's **Process** in the editor.

``edit --detach`` creates an *editor session file*
``<recording_folder>/.work/editor-<token>.json`` and hands its path to the editor
process (``--editor-session``). The editor appends events to it: ``process`` (with the
recording id and the crop) when the user presses the green Process button, ``closed``
when the window closes. ``wait --editor <path>`` polls the file.

When the editor was opened from the Stop dialog of a recording (``Video Editor``),
the recording session itself (``state.json``, status ``editing``) is what the agent
waits on; the helpers at the end of this module write its final state.

A running editor also watches ``<recording_folder>/.work/editor-requests/`` for
*attach requests* written by a later ``edit --detach`` (or by the control window):
it loads the requested recording, adopts the request's editor session and recording
session and raises its window.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_story.core.config import Config
from video_story.core.crop import Crop, read_crop
from video_story.core.fileio import atomic_write_text, read_json_with_retry
from video_story.core.instructions import normalize_instructions, read_instructions
from video_story.core.session import (
    WORK_DIR_NAME,
    SessionPaths,
    Status,
    now_iso,
    read_state,
    write_state,
)
from video_story.host.waiting import timeout_seconds
from video_story.platform.process import pid_alive

log = logging.getLogger(__name__)

EDITOR_SESSION_PREFIX = "editor-"
EDITOR_REQUESTS_DIR = "editor-requests"
POLL_INTERVAL_S = 0.5
ATTACH_REQUEST_MAX_AGE_S = 60.0  # a request nobody served within this time is stale


# Editor session file -------------------------------------------------------


def new_editor_session(config: Config) -> Path:
    """Create a fresh editor session file in the ``open`` state and return its path."""
    work = config.recording_folder / WORK_DIR_NAME
    work.mkdir(parents=True, exist_ok=True)
    path = work / f"{EDITOR_SESSION_PREFIX}{secrets.token_hex(6)}.json"
    _write(path, {"event": "open", "created_at": now_iso(), "updated_at": now_iso()})
    return path


def write_editor_event(
    path: Path,
    event: str,
    session_id: str | None = None,
    crop: Crop | None = None,
    pid: int | None = None,
    instructions: str | None = None,
) -> None:
    """Record ``open``, ``process`` or ``closed`` (atomic rewrite of the whole file).

    ``instructions`` are the user's master instructions for the whole recording
    (``""`` when none); they travel with the ``process`` event.
    """
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = read_json_with_retry(path)
        except (OSError, ValueError):
            data = {}
    data["event"] = event
    data["updated_at"] = now_iso()
    if session_id is not None:
        data["session_id"] = session_id
    if crop is not None:
        data["crop"] = crop.to_dict()
    if pid is not None:
        data["pid"] = pid
    if instructions is not None:
        data["instructions"] = normalize_instructions(instructions)
    _write(path, data)


def read_editor_event(path: Path) -> dict[str, Any]:
    """The current event; a dead editor (``pid`` recorded, process gone) counts as closed."""
    try:
        data = read_json_with_retry(path)
    except FileNotFoundError:
        return {"event": "closed", "message": "editor session file is gone"}
    except (OSError, ValueError):
        return {"event": "open"}
    pid = data.get("pid")
    if data.get("event") == "open" and isinstance(pid, int) and pid > 0 and not pid_alive(pid):
        data["event"] = "closed"
        data["message"] = f"the editor process (pid {pid}) ended without Process"
    return data


def wait_for_editor(path: Path, timeout_s: float) -> dict[str, Any]:
    """Block until the editor reports ``process`` or ``closed``, or the timeout elapses.

    Result: ``{"event": "process", "session_id", "crop", "instructions"}``,
    ``{"event": "closed"}`` or ``{"event": "open", "timed_out": true}``.
    """
    deadline = time.monotonic() + timeout_seconds(timeout_s)
    while True:
        data = read_editor_event(path)
        event = data.get("event", "open")
        if event in ("process", "closed"):
            from video_story.host.startup import cleanup_completed, editor_request

            cleanup_completed(editor_request(path))
            keys = ("event", "session_id", "crop", "instructions", "message")
            result = {k: data[k] for k in keys if k in data}
            if event == "process":
                result.setdefault("instructions", "")
            result["editor_session"] = str(path)
            result["timed_out"] = False
            return result
        if time.monotonic() >= deadline:
            return {
                "event": "open",
                "editor_session": str(path),
                "timed_out": True,
                "hint": "call `wait --editor` again until the event is process or closed",
            }
        time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))


# Attach requests to a running editor --------------------------------------------


@dataclass(frozen=True)
class AttachRequest:
    path: Path
    session_id: str | None
    editor_session: Path
    recording_session: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "editor_session": str(self.editor_session),
            "recording_session": self.recording_session,
            "created_at": now_iso(),
        }


def requests_dir(config: Config) -> Path:
    return config.recording_folder / WORK_DIR_NAME / EDITOR_REQUESTS_DIR


def write_attach_request(
    config: Config,
    editor_session: Path,
    session_id: str | None,
    recording_session: str | None = None,
) -> Path:
    """Ask the running editor to load ``session_id`` and adopt ``editor_session``."""
    folder = requests_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
    request = AttachRequest(path, session_id, editor_session, recording_session)
    _write(path, request.to_dict())
    return path


def take_attach_requests(config: Config) -> list[AttachRequest]:
    """Read and delete pending requests, oldest first (called by the running editor)."""
    folder = requests_dir(config)
    if not folder.is_dir():
        return []
    found: list[AttachRequest] = []
    now = time.time()
    for entry in sorted(folder.iterdir()):
        if entry.suffix != ".json":
            continue
        try:
            data = read_json_with_retry(entry, attempts=4)
            request = AttachRequest(
                entry,
                data.get("session_id"),
                Path(data["editor_session"]),
                data.get("recording_session"),
            )
            age = now - entry.stat().st_mtime
            if age > ATTACH_REQUEST_MAX_AGE_S:
                log.warning("dropping stale attach request %s (%.0f s old)", entry, age)
            elif read_editor_event(request.editor_session).get("event") != "open":
                log.warning("dropping attach request %s: its editor session is not open", entry)
            else:
                found.append(request)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("ignoring attach request %s: %s", entry, exc)
        try:
            entry.unlink()
        except OSError:
            pass
    return found


# Recording session handed to the editor (status "editing") -------------------------


def adopt_recording_session(paths: SessionPaths) -> None:
    """The editor now owns the recording session: record its pid for liveness checks."""
    if not paths.state_path.exists():
        return
    state = read_state(paths.state_path)
    if state.status is not Status.EDITING:
        return
    state.pid = os.getpid()
    write_state(paths.state_path, state)


def update_recording_session_progress(paths: SessionPaths, fraction: float, message: str) -> None:
    """The editor reports how far its background transcription got (``editing`` only).

    Advisory for the agent polling ``wait``: a session that is no longer ``editing``
    (or whose state file cannot be read) is left alone.
    """
    try:
        state = read_state(paths.state_path)
    except RuntimeError:
        return
    if state.status is not Status.EDITING:
        return
    state.progress = max(0.0, min(1.0, fraction))
    state.message = message
    write_state(paths.state_path, state)


def finish_recording_session(
    paths: SessionPaths,
    crop: Crop,
    shown_session_id: str | None = None,
    instructions: str | None = None,
) -> None:
    """The user pressed Process: the recording session becomes ``processed`` with the crop
    and the master ``instructions`` (``""`` when the user typed none).

    ``shown_session_id`` is the recording displayed when Process was pressed. When it is
    another recording, ``crop`` and ``instructions`` belong to that one; the recording
    session then takes the crop stored in its own ``crop.json`` (the whole recording when
    none) and its own ``instructions.txt``. ``instructions`` ``None`` also reads the file.
    """
    state = read_state(paths.state_path)
    if state.status is not Status.EDITING:
        log.warning(
            "recording session %s is %s, not editing: Process ignored",
            paths.session_id,
            state.status,
        )
        return
    final = paths.final_dir
    note = ""
    if shown_session_id is not None and shown_session_id != paths.session_id:
        crop = read_crop(paths.crop_path(final), state.recorded_duration_ms or 0)
        instructions = None
        note = f" Process was pressed while {shown_session_id} was shown."
    if instructions is None:
        instructions = read_instructions(paths.instructions_path(final))
    instructions = normalize_instructions(instructions)
    if instructions:
        note += " Read `instructions` first: they apply to the whole recording."
    state.status = Status.PROCESSED
    state.error_code = None
    state.progress = 1.0
    state.crop = crop.to_dict()
    state.instructions = instructions
    state.processed_via = "editor"
    state.recording_dir = str(final)
    state.manifest = str(paths.manifest_path(final))
    state.transcript_txt = str(paths.transcript_txt_path(final))
    state.transcript_json = str(paths.transcript_json_path(final))
    state.frames_dir = str(paths.frames_dir(final))
    state.frames_info_dir = str(paths.frames_info_dir(final))
    state.message = (
        f"Recording processed; act on {crop.start_ms}-{crop.end_ms} ms (crop from the editor)."
        + note
    )
    write_state(paths.state_path, state)
    log.info("recording session %s processed via editor, crop %s", paths.session_id, crop)


def cancel_recording_session(paths: SessionPaths) -> None:
    """The editor was closed without Process: the agent must not act on the recording."""
    state = read_state(paths.state_path)
    if state.status.final:
        return
    final = paths.final_dir
    state.status = Status.CANCELLED
    state.processed_via = "editor"
    state.recording_dir = str(final)
    state.message = (
        "Editor closed without Process; the recording is kept but nothing should be done."
    )
    write_state(paths.state_path, state)
    log.info("recording session %s cancelled (editor closed without Process)", paths.session_id)


def _write(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


__all__ = [
    "AttachRequest",
    "EDITOR_REQUESTS_DIR",
    "EDITOR_SESSION_PREFIX",
    "adopt_recording_session",
    "cancel_recording_session",
    "finish_recording_session",
    "new_editor_session",
    "read_editor_event",
    "take_attach_requests",
    "update_recording_session_progress",
    "wait_for_editor",
    "write_attach_request",
    "write_editor_event",
]
