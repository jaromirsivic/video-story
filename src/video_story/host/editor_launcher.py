"""Start the Video Story Editor window in a detached process (``edit --detach``)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from video_story.core.config import Config, config_path
from video_story.core.manifest import read_manifest
from video_story.core.session import WORK_DIR_NAME, is_session_id, list_sessions
from video_story.platform.process import gui_python, spawn_detached

from .editor_lock import lock_holder_pid
from .editor_session import new_editor_session, write_attach_request, write_editor_event
from .runner import SessionError

log = logging.getLogger(__name__)

EDITOR_LOCK_NAME = "editor.lock"
EDITOR_LOG_NAME = "editor.log"


def editor_lock_path(config: Config) -> Path:
    return config.recording_folder / WORK_DIR_NAME / EDITOR_LOCK_NAME


def editor_running_pid(config: Config) -> int | None:
    """Pid of a running editor (``0`` when unknown), ``None`` when no editor holds the lock."""
    return lock_holder_pid(editor_lock_path(config))


def processed_recordings(config: Config) -> list[str]:
    """Ids of recordings the editor can open: a final directory with a readable manifest.

    The same rule as the editor's recording list, so the launcher never reports a
    session the editor would not show.
    """
    found: list[str] = []
    for paths in list_sessions(config.recording_folder):
        manifest_path = paths.manifest_path(paths.final_dir)
        if not manifest_path.exists():
            continue
        try:
            read_manifest(manifest_path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("skipping %s: %s", manifest_path, exc)
            continue
        found.append(paths.session_id)
    return found


def recover_left_behind(config: Config) -> list[str]:
    """Finish recordings whose window died before saving them, so that the editor lists
    them (see :mod:`video_story.host.recovery`); never fails the caller."""
    from .recovery import recover_orphaned_recordings

    try:
        return recover_orphaned_recordings(config.recording_folder)
    except Exception:  # noqa: BLE001 - opening the editor matters more
        log.exception("recovery of left-behind recordings failed")
        return []


def check_editor_session(config: Config, session_id: str | None) -> list[str]:
    """Validate ``session_id`` against the processed recordings; returns the ids."""
    if session_id is not None and not is_session_id(session_id):
        raise SessionError("INVALID_SESSION", f"not a session id: {session_id}")
    recordings = processed_recordings(config)
    if not recordings:
        raise SessionError("NO_RECORDINGS", f"no processed recordings in {config.recording_folder}")
    if session_id is not None and session_id not in recordings:
        raise SessionError("UNKNOWN_SESSION", f"no processed recording {session_id}")
    return recordings


def launch_editor(
    config: Config,
    session_id: str | None = None,
    config_file: Path | None = None,
    recording_session: str | None = None,
) -> dict[str, Any]:
    """Open the editor for ``session_id`` (default: the newest recording) and return at once.

    Creates an editor session file (``editor_session`` in the result) that the editor
    reports ``process`` / ``closed`` into; ``wait --editor`` polls it. When an editor is
    already running, no second process starts: an attach request makes the running
    editor load the recording and adopt the editor session (``attached`` is true).
    ``recording_session`` is the id of a recording session in status ``editing``
    (the editor finishes or cancels it when the user presses Process or closes).
    """
    recover_left_behind(config)
    recordings = check_editor_session(config, session_id)
    work = config.recording_folder / WORK_DIR_NAME
    work.mkdir(parents=True, exist_ok=True)
    editor_session = new_editor_session(config)
    selected = session_id or recordings[0]
    running = editor_running_pid(config)
    if running is not None:
        request = write_attach_request(config, editor_session, session_id, recording_session)
        still_running = editor_running_pid(config)
        if still_running is not None:
            write_editor_event(editor_session, "open", pid=still_running or None)
            log.info("editor already running (pid %s): attach request for %s", running, selected)
            return {
                "pid": still_running,
                "session_id": selected,
                "recordings": len(recordings),
                "editor_session": str(editor_session),
                "attached": True,
                "log": str(work / EDITOR_LOG_NAME),
            }
        # The editor went away while we wrote the request: serve it ourselves.
        try:
            request.unlink()
        except OSError:
            pass
    config_arg = str(config_file or config_path())
    args = [gui_python(), "-m", "video_story", "--config", config_arg, "edit"]
    if session_id is not None:
        args += ["--session", session_id]
    args += ["--editor-session", str(editor_session)]
    if recording_session is not None:
        args += ["--recording-session", recording_session]
    pid = spawn_detached(args, work / EDITOR_LOG_NAME, cwd=Path.cwd())
    write_editor_event(editor_session, "open", pid=pid)  # liveness for `wait --editor`
    log.info("editor started: pid %s, session %s", pid, selected)
    return {
        "pid": pid,
        "session_id": selected,
        "recordings": len(recordings),
        "editor_session": str(editor_session),
        "attached": False,
        "log": str(work / EDITOR_LOG_NAME),
    }


__all__ = [
    "EDITOR_LOCK_NAME",
    "EDITOR_LOG_NAME",
    "check_editor_session",
    "editor_lock_path",
    "editor_running_pid",
    "launch_editor",
    "processed_recordings",
    "recover_left_behind",
]
