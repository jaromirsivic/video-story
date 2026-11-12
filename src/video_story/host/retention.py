"""Lifetime of recordings: old ones are archived (never deleted automatically); a
recording is deleted on request (the editor's Delete Video and folder actions).

A recording's age is taken from its session id (``YYYYMMDD-HHMMSS``, local time of the
recording start), which is deterministic and independent of file timestamps. Recordings
older than ``archive_after_days`` are archived by the editor when it starts (see
:func:`recordings_due_for_archive` and :mod:`video_story.export.archive`). A session
whose control window is still running is never deleted or archived.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from video_story.core.session import SessionPaths, list_sessions, read_state
from video_story.platform.process import pid_alive

log = logging.getLogger(__name__)


def session_started_at(session_id: str) -> datetime | None:
    """Local start time encoded in the session id (``-2`` style suffixes are ignored)."""
    try:
        return datetime.strptime(session_id[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


IN_USE_REASON = "it is still in use by a running Video Story window"


def recording_in_use(paths: SessionPaths) -> bool:
    """True when the session's window (recorder, or the editor holding it for an agent)
    may still be running; such a recording is never deleted."""
    return _is_active(paths)


def _is_active(paths: SessionPaths) -> bool:
    """True when the session's control window may still be running."""
    if not paths.state_path.exists():
        return False
    try:
        state = read_state(paths.state_path)
    except RuntimeError:
        return False
    return not state.status.final and pid_alive(state.pid)


def _delete_session(paths: SessionPaths) -> bool:
    """Delete the final and work directories of one session; False if nothing could go."""
    removed = False
    for directory in (paths.final_dir, paths.work_dir):
        if not directory.exists():
            continue
        try:
            shutil.rmtree(directory)
            removed = True
        except OSError as exc:
            log.warning("cannot delete %s: %s", directory, exc)
    return removed


def delete_recording(paths: SessionPaths) -> str | None:
    """Delete one recording (its final and work directories) at the user's request.

    Returns ``None`` when it is gone, else the reason it was kept: a session whose
    window is still running (recording, or open in the editor for an agent) is never
    deleted.
    """
    if _is_active(paths):
        return IN_USE_REASON
    if not paths.final_dir.exists() and not paths.work_dir.exists():
        return None
    if not _delete_session(paths) or paths.final_dir.exists():
        return "its folder could not be deleted (a file may be open in another program)"
    log.info("deleted recording %s on request", paths.session_id)
    return None


def recordings_due_for_archive(
    recording_folder: Path, archive_after_days: int, now: datetime | None = None
) -> list[SessionPaths]:
    """Saved recordings older than ``archive_after_days`` that were never asked to be
    archived (nothing is deleted automatically: the editor archives them instead).
    Sessions whose window still runs are left alone."""
    from video_story.core.archive_state import archive_state_path

    cutoff = (now or datetime.now()) - timedelta(days=archive_after_days)
    due: list[SessionPaths] = []
    for paths in list_sessions(recording_folder):
        started = session_started_at(paths.session_id)
        if started is None or started >= cutoff:
            continue
        if not paths.manifest_path(paths.final_dir).is_file():
            continue
        if archive_state_path(paths, paths.final_dir).exists() or _is_active(paths):
            continue
        due.append(paths)
    return due


__all__ = [
    "IN_USE_REASON",
    "delete_recording",
    "recording_in_use",
    "recordings_due_for_archive",
    "session_started_at",
]
