"""``archive/archive.json`` - how far the archiving of a recording got.

Archiving (:mod:`video_story.export.archive`) runs screen by screen: every screen is
rendered to ``archive/<screenId>.mp4`` (with ``.srt`` subtitles and a still
``<screenId>.jpg`` from the middle of the recording), recorded in ``screens_done``,
and its frames are deleted at once. Once every screen is done the audio and the rest
of ``frames/`` are deleted and the status becomes ``archived``.

The file exists from the moment the user (or the automatic archiving of old
recordings) asks for the archive: ``queued`` keeps that request across an editor that
is closed before the work starts, ``archiving`` one that stopped half way. Either way
the next editor resumes the work; a screen not listed in ``screens_done`` starts over.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .fileio import atomic_write_text, read_json_with_retry
from .session import ARCHIVE_DIR_NAME, SessionPaths, now_iso

ARCHIVE_STATE_FILE_NAME = "archive.json"


class ArchiveStatus(StrEnum):
    QUEUED = "queued"  # asked for; nothing done yet (transcription may still be needed)
    ARCHIVING = "archiving"  # some screens may be done (see screens_done)
    ARCHIVED = "archived"  # every screen archived; audio and frames deleted (best effort)


@dataclass
class ArchiveState:
    status: ArchiveStatus = ArchiveStatus.QUEUED
    requested_at: str = field(default_factory=now_iso)
    reason: str = "user"  # "user" (Archive in the editor) or "age" (older than the limit)
    screens: list[str] = field(default_factory=list)  # every screen to archive
    screens_done: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    # Files or folders that could not be deleted (locked by another program); the
    # editor tries once more at its next start, then they stay.
    leftovers: list[str] = field(default_factory=list)
    error: str | None = None  # why the last attempt stopped (it resumes next time)

    @property
    def archived(self) -> bool:
        return self.status == ArchiveStatus.ARCHIVED

    @property
    def fraction(self) -> float:
        """Share of the screens done (0..1)."""
        if not self.screens:
            return 1.0 if self.archived else 0.0
        done = sum(1 for s in self.screens if s in self.screens_done)
        return done / len(self.screens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "requested_at": self.requested_at,
            "reason": self.reason,
            "screens": list(self.screens),
            "screens_done": list(self.screens_done),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "leftovers": list(self.leftovers),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveState:
        return cls(
            status=ArchiveStatus(data.get("status", ArchiveStatus.QUEUED)),
            requested_at=str(data.get("requested_at") or ""),
            reason=str(data.get("reason") or "user"),
            screens=[str(s) for s in data.get("screens") or []],
            screens_done=[str(s) for s in data.get("screens_done") or []],
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            leftovers=[str(s) for s in data.get("leftovers") or []],
            error=data.get("error"),
        )


def archive_state_path(paths: SessionPaths, base: Path | None = None) -> Path:
    return paths.archive_dir(base) / ARCHIVE_STATE_FILE_NAME


def read_archive_state(paths: SessionPaths, base: Path | None = None) -> ArchiveState | None:
    """The archive state of a recording; ``None`` when it was never asked to archive."""
    path = archive_state_path(paths, base)
    if not path.is_file():
        return None
    try:
        data = read_json_with_retry(path)
        return ArchiveState.from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_archive_state(paths: SessionPaths, state: ArchiveState, base: Path | None = None) -> None:
    path = archive_state_path(paths, base)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2) + "\n")


def request_archive(paths: SessionPaths, reason: str = "user") -> ArchiveState:
    """Queue a recording for archiving (an existing request is kept as it is)."""
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if state is not None:
        return state
    state = ArchiveState(reason=reason)
    write_archive_state(paths, state, base)
    return state


def is_archived(paths: SessionPaths, base: Path | None = None) -> bool:
    state = read_archive_state(paths, base)
    return state is not None and state.archived


__all__ = [
    "ARCHIVE_DIR_NAME",
    "ARCHIVE_STATE_FILE_NAME",
    "ArchiveState",
    "ArchiveStatus",
    "archive_state_path",
    "is_archived",
    "read_archive_state",
    "request_archive",
    "write_archive_state",
]
