"""Folders of recordings inside the Recording Folder.

Layout::

    <recording_folder>/
      folders.json                 # order, main folder, pinned folder, collapsed folders
      .work/<session_id>/          # recordings being made (see core.session)
      default/<session_id>/        # the default folder: always there, cannot be deleted
      <folder>/<session_id>/       # folders the user created in the editor

``folders.json``::

    {"order": ["default", "Clients", ...], "main": "default", "pinned": "default",
     "collapsed": ["Clients"]}

- **order** - the order of the folders in the editor's list.
- **main** - new recordings are saved here (exactly one; ``default`` when the stored one
  is gone). It can be anywhere in the order; the editor shows its name bold.
- **pinned** - at most one folder (``null`` = none) that always stands first.
- **collapsed** - folders whose accordion is closed in the editor.

The file is written by the editor and read by the recording window (where to save a
new recording) and by the CLI, three different processes: it is written atomically and
read with retries (:mod:`video_story.core.fileio`). A missing or broken file means the
defaults; folders on disk that the file does not know are appended in name order, names
in the file without a folder on disk are dropped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from video_story.core.fileio import atomic_write_text, read_json_with_retry, replace_with_retry
from video_story.core.session import (
    WORK_DIR_NAME,
    SessionPaths,
    is_session_id,
    read_state,
    write_state,
)

log = logging.getLogger(__name__)

DEFAULT_FOLDER = "default"
FOLDERS_FILE_NAME = "folders.json"
MAX_NAME_LENGTH = 100
NEW_FOLDER_PREFIX = "New folder"
INVALID_NAME_CHARS = '<>:"/\\|?*'
WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{k}" for k in range(1, 10)}
    | {f"lpt{k}" for k in range(1, 10)}
)
# Fields of state.json that hold paths inside the recording's final directory.
STATE_PATH_FIELDS = (
    "recording_dir",
    "manifest",
    "transcript_txt",
    "transcript_json",
    "frames_dir",
    "frames_info_dir",
)
MOVE_ATTEMPTS = 20  # a directory rename waits up to 20 x 150 ms for open files to close
MOVE_DELAY_S = 0.15


@dataclass
class FolderState:
    """Content of ``folders.json`` after it was checked against the disk."""

    order: list[str]
    main: str = DEFAULT_FOLDER
    pinned: str | None = DEFAULT_FOLDER
    collapsed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "order": list(self.order),
            "main": self.main,
            "pinned": self.pinned,
            "collapsed": [name for name in self.order if name in self.collapsed],
        }

    def is_collapsed(self, name: str) -> bool:
        return name in self.collapsed

    def can_move_up(self, name: str) -> bool:
        """Move Up: not the pinned folder, and nothing passes the pinned one."""
        if name not in self.order or name == self.pinned:
            return False
        return self.order.index(name) > (1 if self.pinned is not None else 0)

    def can_move_down(self, name: str) -> bool:
        if name not in self.order or name == self.pinned:
            return False
        return self.order.index(name) < len(self.order) - 1


def is_folder_entry(entry: os.DirEntry[str] | Path) -> bool:
    """A directory of the Recording Folder that is a folder of recordings: not hidden
    (``.work``), not a session directory of the old flat layout."""
    name = entry.name
    if name.startswith(".") or is_session_id(name):
        return False
    try:
        return entry.is_dir()
    except OSError:
        return False


def folder_names_on_disk(recording_folder: Path) -> list[str]:
    """The folders of recordings that exist, in name order (case-insensitive)."""
    try:
        with os.scandir(recording_folder) as entries:
            names = [entry.name for entry in entries if is_folder_entry(entry)]
    except OSError:
        return []
    return sorted(names, key=lambda n: (n.casefold(), n))


def folders_file(recording_folder: Path) -> Path:
    return recording_folder / FOLDERS_FILE_NAME


def ensure_default(recording_folder: Path) -> Path:
    """Create ``<recording_folder>/default`` when it is missing; returns it."""
    folder = recording_folder / DEFAULT_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read_raw(recording_folder: Path) -> dict | None:
    path = folders_file(recording_folder)
    try:
        data = read_json_with_retry(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s, using the default folder order: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("%s is not a JSON object, using the default folder order", path)
        return None
    return data


def load_folders(recording_folder: Path, ensure: bool = True) -> FolderState:
    """``folders.json`` checked against the disk (see the module docstring).

    ``ensure`` creates the ``default`` folder when it is missing; without it a missing
    ``default`` is still listed (it is created as soon as a recording goes there).
    """
    if ensure:
        try:
            ensure_default(recording_folder)
        except OSError as exc:
            log.warning("cannot create the default folder in %s: %s", recording_folder, exc)
    disk = folder_names_on_disk(recording_folder)
    if DEFAULT_FOLDER not in disk:
        disk.insert(0, DEFAULT_FOLDER)
    raw = _read_raw(recording_folder)
    if raw is None:
        rest = [name for name in disk if name != DEFAULT_FOLDER]
        return FolderState(order=[DEFAULT_FOLDER, *rest])
    known = set(disk)
    order: list[str] = []
    saved = raw.get("order")
    for name in saved if isinstance(saved, list) else []:
        if isinstance(name, str) and name in known and name not in order:
            order.append(name)
    order += [name for name in disk if name not in order]
    main = raw.get("main")
    main = main if isinstance(main, str) and main in known else DEFAULT_FOLDER
    pinned = raw.get("pinned", DEFAULT_FOLDER)
    pinned = pinned if isinstance(pinned, str) and pinned in known else None
    if pinned is not None:
        order.remove(pinned)
        order.insert(0, pinned)
    collapsed_raw = raw.get("collapsed")
    collapsed = [
        name
        for name in (collapsed_raw if isinstance(collapsed_raw, list) else [])
        if isinstance(name, str) and name in known
    ]
    return FolderState(order=order, main=main, pinned=pinned, collapsed=collapsed)


def save_folders(recording_folder: Path, state: FolderState) -> None:
    atomic_write_text(folders_file(recording_folder), json.dumps(state.to_dict(), indent=2) + "\n")


def main_folder(recording_folder: Path) -> str:
    """Where a new recording is saved: the main folder, ``default`` when it is gone."""
    raw = _read_raw(recording_folder)
    name = raw.get("main") if raw is not None else None
    if isinstance(name, str) and is_folder_entry(recording_folder / name):
        return name
    return DEFAULT_FOLDER


# Names ----------------------------------------------------------------------------


def validate_name(name: str, existing: list[str] | tuple[str, ...]) -> str | None:
    """Why ``name`` cannot be a new folder, or ``None`` when it can."""
    if not name.strip():
        return "Enter a folder name."
    if name != name.strip():
        return "The name cannot start or end with a space."
    if name.endswith("."):
        return "The name cannot end with a dot."
    if name.startswith("."):
        return "The name cannot start with a dot."
    bad = sorted({c for c in name if c in INVALID_NAME_CHARS or ord(c) < 32})
    if bad:
        shown = " ".join(c if ord(c) >= 32 else "control characters" for c in bad)
        return f"The name cannot contain {shown}"
    if len(name) > MAX_NAME_LENGTH:
        return f"The name can have at most {MAX_NAME_LENGTH} characters."
    if name.split(".")[0].casefold() in WINDOWS_RESERVED:
        return f"“{name}” is a reserved name."
    if is_session_id(name):
        return "The name cannot look like a recording (YYYYMMDD-HHMMSS)."
    if name.casefold() == WORK_DIR_NAME.casefold():
        return f"“{name}” is a reserved name."
    folded = name.casefold()
    if any(other.casefold() == folded for other in existing):
        return f"A folder named “{name}” already exists."
    return None


def new_folder_name(existing: list[str] | tuple[str, ...], now: datetime | None = None) -> str:
    """``New folder 20261112-151530`` (a free name: `` (2)``, `` (3)``... appended)."""
    base = f"{NEW_FOLDER_PREFIX} {(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}"
    taken = {other.casefold() for other in existing}
    name, counter = base, 2
    while name.casefold() in taken:
        name = f"{base} ({counter})"
        counter += 1
    return name


# Changes (each reads the file, changes it and writes it back) -----------------------


def create_folder(recording_folder: Path, name: str) -> FolderState:
    """A new, empty folder at the end of the order."""
    state = load_folders(recording_folder)
    problem = validate_name(name, folder_names_on_disk(recording_folder))
    if problem is not None:
        raise ValueError(problem)
    (recording_folder / name).mkdir()
    state.order.append(name)
    save_folders(recording_folder, state)
    return state


def set_main(recording_folder: Path, name: str) -> FolderState:
    state = load_folders(recording_folder)
    _require(state, name)
    state.main = name
    save_folders(recording_folder, state)
    return state


def pin(recording_folder: Path, name: str) -> FolderState:
    """``name`` goes first and stays there; the folder pinned before moves to second."""
    state = load_folders(recording_folder)
    _require(state, name)
    state.order.remove(name)
    state.order.insert(0, name)
    state.pinned = name
    save_folders(recording_folder, state)
    return state


def unpin(recording_folder: Path) -> FolderState:
    """No folder is pinned any more; the order stays as it is."""
    state = load_folders(recording_folder)
    state.pinned = None
    save_folders(recording_folder, state)
    return state


def move_folder(recording_folder: Path, name: str, delta: int) -> FolderState:
    """Move Up (``-1``) or Move Down (``+1``); nothing passes the pinned folder."""
    state = load_folders(recording_folder)
    _require(state, name)
    allowed = state.can_move_up(name) if delta < 0 else state.can_move_down(name)
    if not allowed:
        return state
    index = state.order.index(name)
    other = index + (-1 if delta < 0 else 1)
    state.order[index], state.order[other] = state.order[other], state.order[index]
    save_folders(recording_folder, state)
    return state


def set_collapsed(recording_folder: Path, name: str, collapsed: bool) -> FolderState:
    state = load_folders(recording_folder)
    if name in state.order:
        if collapsed and name not in state.collapsed:
            state.collapsed.append(name)
        elif not collapsed and name in state.collapsed:
            state.collapsed.remove(name)
        save_folders(recording_folder, state)
    return state


def set_all_collapsed(recording_folder: Path, collapsed: bool) -> FolderState:
    """Close (``True``) or open every folder's accordion: Collapse all / Expand all.
    ``folders.json`` is written once."""
    state = load_folders(recording_folder)
    wanted = list(state.order) if collapsed else []
    if state.collapsed != wanted:
        state.collapsed = wanted
        save_folders(recording_folder, state)
    return state


def remove_folder(recording_folder: Path, name: str) -> FolderState:
    """Delete the (empty) directory of ``name``: the caller deleted its recordings.

    A folder that still holds anything (a recording that could not be deleted, a file
    of the user's) is kept: ``OSError``. The default folder cannot be removed. When the
    removed folder was the main folder, ``default`` becomes the main folder; when it was
    pinned, no folder is pinned.
    """
    if name == DEFAULT_FOLDER:
        raise ValueError("The default folder cannot be deleted.")
    state = load_folders(recording_folder)
    _require(state, name)
    os.rmdir(recording_folder / name)
    state.order.remove(name)
    if name in state.collapsed:
        state.collapsed.remove(name)
    if state.main == name:
        state.main = DEFAULT_FOLDER
    if state.pinned == name:
        state.pinned = None
    save_folders(recording_folder, state)
    return state


def _require(state: FolderState, name: str) -> None:
    if name not in state.order:
        raise ValueError(f"no folder named {name!r}")


# Recordings -------------------------------------------------------------------------


def move_recording(paths: SessionPaths, target: str) -> SessionPaths:
    """Move a saved recording into the folder ``target`` (a rename on the same disk).

    On Windows a directory cannot be renamed while a file inside it is open: the rename
    is retried for a while, then ``PermissionError`` is raised and the recording stays
    where it was. ``state.json`` of the session (when there is one) learns the new
    paths, so ``wait``/``status`` and the result of Process point to the new place.
    """
    root = paths.recording_folder
    src = paths.final_dir
    dst = root / target / paths.session_id
    if src == dst:
        return SessionPaths(root, paths.session_id, folder=target)
    if not src.is_dir():
        raise FileNotFoundError(f"recording not found: {src}")
    if not (root / target).is_dir():
        raise FileNotFoundError(f"folder not found: {root / target}")
    if dst.exists():
        raise FileExistsError(f"already exists: {dst}")
    replace_with_retry(src, dst, attempts=MOVE_ATTEMPTS, delay=MOVE_DELAY_S)
    moved = SessionPaths(root, paths.session_id, folder=target)
    _retarget_state(moved, src, dst)
    return moved


def _retarget_state(paths: SessionPaths, src: Path, dst: Path) -> None:
    if not paths.state_path.exists():
        return
    try:
        state = read_state(paths.state_path)
        prefix = os.path.normcase(str(src))
        changed = False
        for name in STATE_PATH_FIELDS:
            value = getattr(state, name)
            if not value:
                continue
            folded = os.path.normcase(value)
            if folded == prefix or folded.startswith(prefix + os.sep):
                setattr(state, name, str(dst) + value[len(str(src)) :])
                changed = True
        if changed:
            write_state(paths.state_path, state)
    except (OSError, RuntimeError) as exc:
        log.warning("moved %s, but cannot update %s: %s", dst, paths.state_path, exc)


__all__ = [
    "DEFAULT_FOLDER",
    "FOLDERS_FILE_NAME",
    "FolderState",
    "create_folder",
    "ensure_default",
    "folder_names_on_disk",
    "folders_file",
    "is_folder_entry",
    "load_folders",
    "main_folder",
    "move_folder",
    "move_recording",
    "new_folder_name",
    "pin",
    "remove_folder",
    "save_folders",
    "set_all_collapsed",
    "set_collapsed",
    "set_main",
    "unpin",
    "validate_name",
]
