"""List model of the processed recordings in the Recording Folder, folder by folder.

The rows are flat: a :class:`FolderHeader` row for every folder (in the order of
``folders.json``, see :mod:`video_story.core.folders`) followed by the recordings of
that folder, newest first. A header is not selectable; :data:`ItemRole` gives the
:class:`RecordingItem` of a recording row (``None`` on a header) and :data:`FolderRole`
the :class:`FolderHeader` of a header row. A collapsed folder hides its recording rows
(the view applies it, see :mod:`recordings_view`).

Each item also carries what the list's rows need to preview a recording while the mouse
moves over them (see :mod:`recordings_view`): the screen ids and the transcript segments.
"""

from __future__ import annotations

import bisect
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPersistentModelIndex, Qt

from video_story.core.archive_state import ArchiveStatus, read_archive_state
from video_story.core.folders import (
    DEFAULT_FOLDER,
    FolderState,
    load_folders,
    set_all_collapsed,
    set_collapsed,
)
from video_story.core.manifest import read_manifest
from video_story.core.session import SessionPaths, list_sessions
from video_story.core.transcript import read_transcript, transcript_status

log = logging.getLogger(__name__)

ItemRole = Qt.ItemDataRole.UserRole + 1
FolderRole = Qt.ItemDataRole.UserRole + 2

SegmentText = tuple[int, int, str]  # start ms, end ms, text
ARCHIVE_FAILED_TOOLTIP = "Archiving failed"  # followed by the reason, in a row's tooltip


def format_length(duration_ms: int) -> str:
    """Length of a recording in whole seconds: ``MM:SS``, ``H:MM:SS`` from one hour."""
    total = max(0, int(duration_ms)) // 1000
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


SIZE_UNITS = ("KB", "MB", "GB", "TB")


def format_size(size_bytes: int) -> str:
    """Size on disk with one decimal in the largest unit below 1024 of it: KB (also for
    less than 1 KB), MB, GB or TB (binary units, 1 KB = 1024 bytes)."""
    value = max(0, int(size_bytes)) / 1024
    for unit in SIZE_UNITS[:-1]:
        if round(value, 1) < 1024:  # 1023.96 KB would read "1024.0 KB": say 1.0 MB
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} {SIZE_UNITS[-1]}"


def folder_size(path: Path) -> int:
    """Bytes of every file below ``path`` (unreadable entries count as 0)."""
    total = 0
    stack = [path]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


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
    # None (not archived), "queued" or "archiving" (in the archive queue), "failed" (the
    # last attempt failed with nothing deleted: a normal recording), "cancelled" (the
    # user cancelled it with nothing deleted: a normal recording), "archived"
    archive: str | None = None
    archive_leftovers: bool = False  # archived, but some files could not be deleted
    archive_fraction: float = 0.0  # share of the screens archived (queued / archiving)
    size_bytes: int | None = None  # the recording folder on disk; None = unknown
    archive_error: str | None = None  # why the last archive attempt stopped

    @property
    def archived(self) -> bool:
        return self.archive == ArchiveStatus.ARCHIVED

    @property
    def archiving(self) -> bool:
        """Queued for archiving or being archived: it cannot be edited any more."""
        return self.archive in (ArchiveStatus.QUEUED, ArchiveStatus.ARCHIVING)

    @property
    def archive_failed(self) -> bool:
        """The last archive attempt failed with nothing deleted: the recording is an
        ordinary one (no label in the list); the row's tooltip says why."""
        return self.archive == ArchiveStatus.FAILED

    @property
    def tooltip(self) -> str:
        """The row's tooltip: the id and the folder, and why archiving failed."""
        text = f"{self.session_id}\n{self.paths.final_dir}"
        if self.archive_failed:
            text += f"\n{ARCHIVE_FAILED_TOOLTIP}: {self.archive_error or 'unknown error'}"
        return text

    @property
    def session_id(self) -> str:
        return self.paths.session_id

    @property
    def folder(self) -> str:
        """The folder of recordings the recording lies in."""
        return self.paths.folder or DEFAULT_FOLDER

    @property
    def title(self) -> str:
        if self.started is None:
            return self.session_id
        return self.started.strftime("%d.%m.%Y %H:%M:%S")

    @property
    def details(self) -> str:
        """The length without milliseconds and the size on disk in brackets; the screens
        are the dots of the row."""
        suffix = "" if self.transcript_complete or self.archiving else "  |  transcribing"
        size = "" if self.size_bytes is None else f" ({format_size(self.size_bytes)})"
        return f"{format_length(self.duration_ms)}{size}{suffix}"

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


@dataclass(frozen=True)
class FolderHeader:
    """The header row of a folder: its name, how many recordings it holds and its
    state (main folder: new recordings go there; pinned: always first; collapsed)."""

    name: str
    count: int
    main: bool = False
    pinned: bool = False
    collapsed: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} ({self.count})"


Row = FolderHeader | RecordingItem


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
                size_bytes=folder_size(base),
                archive_error=archive.error if archive is not None else None,
            )
        )
    return items


def build_rows(items: list[RecordingItem], state: FolderState) -> list[Row]:
    """A header per folder of ``state.order`` followed by its recordings (in the order
    of ``items``, newest first). A recording in a folder ``state`` does not know (made
    while the list was built) gets its folder appended at the end."""
    by_folder: dict[str, list[RecordingItem]] = {}
    for item in items:
        by_folder.setdefault(item.folder, []).append(item)
    order = list(state.order) + [name for name in by_folder if name not in state.order]
    rows: list[Row] = []
    for name in order:
        members = by_folder.get(name, [])
        rows.append(
            FolderHeader(
                name=name,
                count=len(members),
                main=name == state.main,
                pinned=name == state.pinned,
                collapsed=state.is_collapsed(name),
            )
        )
        rows.extend(members)
    return rows


class RecordingsModel(QAbstractListModel):
    """Read-only list of folder headers and :class:`RecordingItem`; ``refresh()``
    rescans the folder, ``reload_folders()`` only re-reads ``folders.json``."""

    def __init__(self, recording_folder: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.recording_folder = recording_folder
        self._items: list[RecordingItem] = []  # newest first
        self._rows: list[Row] = []
        self.folder_state = FolderState(order=[DEFAULT_FOLDER])
        self.refresh()

    def refresh(self) -> None:
        """Rescan the recordings and the folders."""
        self.beginResetModel()
        try:
            self._items = scan_recordings(self.recording_folder)
            self.folder_state = load_folders(self.recording_folder)
            self._rows = build_rows(self._items, self.folder_state)
        finally:
            self.endResetModel()

    def reload_folders(self) -> None:
        """Order, main, pinned or collapsed folders changed: rebuild the rows from the
        recordings already scanned."""
        self.beginResetModel()
        try:
            self.folder_state = load_folders(self.recording_folder)
            self._rows = build_rows(self._items, self.folder_state)
        finally:
            self.endResetModel()

    @property
    def items(self) -> list[RecordingItem]:
        """Every recording, newest first (whatever its folder)."""
        return list(self._items)

    @property
    def folders(self) -> list[str]:
        return [row.name for row in self._rows if isinstance(row, FolderHeader)]

    def item(self, row: int) -> RecordingItem | None:
        """The recording of ``row``; ``None`` for a folder header or outside the list."""
        value = self._rows[row] if 0 <= row < len(self._rows) else None
        return value if isinstance(value, RecordingItem) else None

    def header(self, row: int) -> FolderHeader | None:
        value = self._rows[row] if 0 <= row < len(self._rows) else None
        return value if isinstance(value, FolderHeader) else None

    def header_of(self, folder: str) -> FolderHeader | None:
        row = self.header_row(folder)
        return self.header(row) if row >= 0 else None

    def header_row(self, folder: str) -> int:
        for row, value in enumerate(self._rows):
            if isinstance(value, FolderHeader) and value.name == folder:
                return row
        return -1

    def folder_of_row(self, row: int) -> str | None:
        """The folder a row belongs to (a header belongs to its own folder)."""
        value = self._rows[row] if 0 <= row < len(self._rows) else None
        if isinstance(value, FolderHeader):
            return value.name
        return value.folder if isinstance(value, RecordingItem) else None

    def items_of(self, folder: str) -> list[RecordingItem]:
        """The recordings of ``folder``, in list order."""
        return [r for r in self._rows if isinstance(r, RecordingItem) and r.folder == folder]

    def rows_of(self, folder: str) -> list[int]:
        """The recording rows of ``folder``."""
        return [
            row
            for row, value in enumerate(self._rows)
            if isinstance(value, RecordingItem) and value.folder == folder
        ]

    def row_of(self, session_id: str) -> int:
        for row, value in enumerate(self._rows):
            if isinstance(value, RecordingItem) and value.session_id == session_id:
                return row
        return -1

    def default_row(self, session_id: str | None = None) -> int:
        """Row to select initially: ``session_id`` when listed, else the newest
        recording (whatever its folder); -1 without recordings."""
        if session_id:
            row = self.row_of(session_id)
            if row >= 0:
                return row
        return self.row_of(self._items[0].session_id) if self._items else -1

    def is_collapsed(self, folder: str) -> bool:
        return self.folder_state.is_collapsed(folder)

    def set_collapsed(self, folder: str, collapsed: bool) -> bool:
        """Open or close a folder's accordion (stored in ``folders.json``); returns
        True when it changed. The view hides or shows the rows."""
        row = self.header_row(folder)
        if row < 0 or self.is_collapsed(folder) == collapsed:
            return False
        try:
            self.folder_state = set_collapsed(self.recording_folder, folder, collapsed)
        except OSError as exc:
            log.warning("cannot store the collapsed folders: %s", exc)
            if collapsed:
                self.folder_state.collapsed.append(folder)
            else:
                self.folder_state.collapsed.remove(folder)
        header = self._rows[row]
        assert isinstance(header, FolderHeader)
        self._rows[row] = FolderHeader(
            header.name, header.count, header.main, header.pinned, collapsed
        )
        index = self.index(row)
        self.dataChanged.emit(index, index)
        return True

    def set_all_collapsed(self, collapsed: bool) -> bool:
        """Collapse all / Expand all: close or open every folder's accordion (one write
        of ``folders.json``); returns True when any folder changed."""
        rows = [
            row
            for row, value in enumerate(self._rows)
            if isinstance(value, FolderHeader) and value.collapsed != collapsed
        ]
        if not rows:
            return False
        try:
            self.folder_state = set_all_collapsed(self.recording_folder, collapsed)
        except OSError as exc:
            log.warning("cannot store the collapsed folders: %s", exc)
            self.folder_state.collapsed = list(self.folders) if collapsed else []
        for row in rows:
            header = self._rows[row]
            assert isinstance(header, FolderHeader)
            self._rows[row] = FolderHeader(
                header.name, header.count, header.main, header.pinned, collapsed
            )
        self.dataChanged.emit(self.index(rows[0]), self.index(rows[-1]))
        return True

    # QAbstractListModel -----------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if index.isValid() and self.header(index.row()) is not None:
            return Qt.ItemFlag.ItemIsEnabled  # a folder header cannot be selected
        return super().flags(index)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:  # noqa: N802
        if not index.isValid():
            return None
        header = self.header(index.row())
        if header is not None:
            if role == Qt.ItemDataRole.DisplayRole:
                return header.label
            if role == FolderRole:
                return header
            return None
        item = self.item(index.row())
        if item is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return item.display_text
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.tooltip
        if role == ItemRole:
            return item
        return None


__all__ = [
    "ARCHIVE_FAILED_TOOLTIP",
    "FolderHeader",
    "FolderRole",
    "ItemRole",
    "RecordingItem",
    "RecordingsModel",
    "build_rows",
    "SegmentText",
    "folder_size",
    "format_length",
    "format_size",
    "scan_recordings",
    "session_start",
]
