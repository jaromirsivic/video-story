"""Lifetime of recordings: old ones are archived (never deleted automatically); one or
all of them are deleted on request.

A recording's age is taken from its session id (``YYYYMMDD-HHMMSS``, local time of the
recording start), which is deterministic and independent of file timestamps. Recordings
older than ``archive_after_days`` are archived by the editor when it starts (see
:func:`recordings_due_for_archive` and :mod:`video_story.export.archive`). A session
whose control window is still running is never deleted or archived.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from video_story.core.session import SessionPaths, list_sessions, read_state
from video_story.platform.process import pid_alive

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecordingsSummary:
    count: int
    total_bytes: int

    @property
    def total_text(self) -> str:
        size = float(self.total_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"  # pragma: no cover


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


def _dir_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def summarize_recordings(
    recording_folder: Path, exclude_session_id: str | None = None
) -> RecordingsSummary:
    """Number and size of the recordings that :func:`delete_all_recordings` would remove."""
    count = 0
    total = 0
    for paths in list_sessions(recording_folder):
        if paths.session_id == exclude_session_id:
            continue
        for directory in (paths.final_dir, paths.work_dir):
            if directory.exists():
                total += _dir_size(directory)
        count += 1
    return RecordingsSummary(count=count, total_bytes=total)


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


def delete_all_recordings(
    recording_folder: Path, exclude_session_id: str | None = None
) -> list[str]:
    """Delete every recording except ``exclude_session_id`` (the open window's session)
    and sessions whose window is still running; returns the deleted session ids."""
    deleted: list[str] = []
    for paths in list_sessions(recording_folder):
        if paths.session_id == exclude_session_id or _is_active(paths):
            continue
        if _delete_session(paths):
            deleted.append(paths.session_id)
    log.info("deleted %d recording(s) from %s", len(deleted), recording_folder)
    return deleted


__all__ = [
    "IN_USE_REASON",
    "RecordingsSummary",
    "delete_all_recordings",
    "delete_recording",
    "recording_in_use",
    "recordings_due_for_archive",
    "session_started_at",
    "summarize_recordings",
]
