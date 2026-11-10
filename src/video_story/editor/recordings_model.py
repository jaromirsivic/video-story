"""List model of the processed recordings in the Recording Folder (newest first).

Each item also carries what the list's rows need to preview a recording while the mouse
moves over them (see :mod:`recordings_view`): the screen ids and the transcript segments.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from video_story.core.archive_state import ArchiveStatus, read_archive_state
from video_story.core.manifest import read_manifest
from video_story.core.session import SessionPaths, list_sessions
from video_story.core.transcript import read_transcript, transcript_status

log = logging.getLogger(__name__)

ItemRole = Qt.ItemDataRole.UserRole + 1

SegmentText = tuple[int, int, str]  # start ms, end ms, text


def format_length(duration_ms: int) -> str:
    """Length of a recording in whole seconds: ``MM:SS``, ``H:MM:SS`` from one hour."""
    total = max(0, int(duration_ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class RecordingItem:
    paths: SessionPaths
    started: datetime | None
    duration_ms: int
    screen_count: int
    first_line: str
    transcript_complete: bool = True  # False: the editor still has to transcribe it
    screen_ids: tuple[str, ...] = ()  # manifest order, screen1 (the primary) first
    segments: tuple[SegmentText, ...] = ()  # non-empty transcript segments by start
    screen_sizes: tuple[tuple[int, int], ...] = ()  # image size of each screen, as screen_ids
    # None (not archived), "queued" or "archiving" (in the archive queue), "archived"
    archive: str | None = None
    archive_leftovers: bool = False  # archived, but some files could not be deleted
    archive_fraction: float = 0.0  # share of the screens archived (queued / archiving)

    @property
    def archived(self) -> bool:
        return self.archive == ArchiveStatus.ARCHIVED

    @property
    def archiving(self) -> bool:
        """Queued for archiving or being archived: it cannot be edited any more."""
        return self.archive in (ArchiveStatus.QUEUED, ArchiveStatus.ARCHIVING)

    @property
    def session_id(self) -> str:
        return self.paths.session_id

    @property
    def title(self) -> str:
        if self.started is None:
            return self.session_id
        return self.started.strftime("%d.%m.%Y %H:%M:%S")

    @property
    def details(self) -> str:
        """The length without milliseconds; the screens are the dots of the row."""
        suffix = "" if self.transcript_complete or self.archiving else "  |  transcribing"
        return f"{format_length(self.duration_ms)}{suffix}"

    def screen_label(self, screen_id: str) -> str:
        """``screen1 2560x1600`` - the tooltip of a screen dot (as the track header)."""
        try:
            width, height = self.screen_sizes[self.screen_ids.index(screen_id)]
        except (ValueError, IndexError):
            return screen_id
        return f"{screen_id} {width}x{height}" if width > 0 and height > 0 else screen_id

    def text_at(self, time_ms: int) -> str:
        """The transcript text spoken at ``time_ms``: the last segment starting at or
        before it (so pauses keep the previous sentence), the first line before that."""
        index = bisect.bisect_right([s[0] for s in self.segments], time_ms) - 1
        if index < 0:
            return self.first_line
        return self.segments[index][2]

    @property
    def display_text(self) -> str:
        lines = [self.title, self.details]
        if self.first_line:
            lines.append(_shorten(self.first_line, 60))
        return "\n".join(lines)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def session_start(session_id: str) -> datetime | None:
    """Start date/time encoded in the session id (``YYYYMMDD-HHMMSS[-n]``)."""
    try:
        return datetime.strptime(session_id[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _transcript_lines(paths: SessionPaths, base: Path) -> tuple[str, tuple[SegmentText, ...]]:
    """The first spoken line and every non-empty segment (sorted by start)."""
    json_path = paths.transcript_json_path(base)
    if json_path.exists():
        try:
            segments = tuple(
                sorted(
                    (s.start_ms, s.end_ms, " ".join(s.text.split()))
                    for s in read_transcript(json_path).segments
                    if s.text.strip()
                )
            )
            return (segments[0][2] if segments else ""), segments
        except (OSError, ValueError, KeyError):
            pass
    txt_path = paths.transcript_txt_path(base)
    if txt_path.exists():
        try:
            for line in txt_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    return line.split("] ", 1)[-1].strip(), ()
        except OSError:
            pass
    return "", ()


def scan_recordings(recording_folder: Path) -> list[RecordingItem]:
    """Processed recordings (``<id>/manifest.json`` exists), newest first."""
    items: list[RecordingItem] = []
    for paths in list_sessions(recording_folder):
        base = paths.final_dir
        manifest_path = paths.manifest_path(base)
        if not manifest_path.exists():
            continue
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("skipping %s: %s", manifest_path, exc)
            continue
        first_line, segments = _transcript_lines(paths, base)
        archive = read_archive_state(paths, base)
        items.append(
            RecordingItem(
                paths=paths,
                started=session_start(paths.session_id),
                duration_ms=manifest.recorded_duration_ms,
                screen_count=len(manifest.screens),
                first_line=first_line,
                transcript_complete=transcript_status(paths.transcript_json_path(base)).complete,
                screen_ids=tuple(screen.id for screen in manifest.screens),
                segments=segments,
                screen_sizes=tuple((s.image_width, s.image_height) for s in manifest.screens),
                archive=str(archive.status) if archive is not None else None,
                archive_leftovers=bool(archive and archive.archived and archive.leftovers),
                archive_fraction=archive.fraction if archive is not None else 0.0,
            )
        )
    return items


class RecordingsModel(QAbstractListModel):
    """Read-only list of :class:`RecordingItem`; ``refresh()`` rescans the folder."""

    def __init__(self, recording_folder: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.recording_folder = recording_folder
        self._items: list[RecordingItem] = []
        self.refresh()

    def refresh(self) -> None:
        self.beginResetModel()
        try:
            self._items = scan_recordings(self.recording_folder)
        finally:
            self.endResetModel()

    @property
    def items(self) -> list[RecordingItem]:
        return list(self._items)

    def item(self, row: int) -> RecordingItem | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def row_of(self, session_id: str) -> int:
        for row, item in enumerate(self._items):
            if item.session_id == session_id:
                return row
        return -1

    def default_row(self, session_id: str | None = None) -> int:
        """Row to select initially: ``session_id`` when listed, else the newest (0)."""
        if session_id:
            row = self.row_of(session_id)
            if row >= 0:
                return row
        return 0 if self._items else -1

    # QAbstractListModel -----------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._items)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:  # noqa: N802
        item = self.item(index.row()) if index.isValid() else None
        if item is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return item.display_text
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.session_id}\n{item.paths.final_dir}"
        if role == ItemRole:
            return item
        return None


__all__ = [
    "ItemRole",
    "RecordingItem",
    "RecordingsModel",
    "SegmentText",
    "format_length",
    "scan_recordings",
    "session_start",
]
