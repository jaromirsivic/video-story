"""Recording session: identifiers, directory layout and the shared ``state.json``.

Layout inside the Recording Folder::

    <recording_folder>/
      folders.json             # order, main, pinned and collapsed folders (core.folders)
      .work/<session_id>/      # while recording / processing
        state.json, lock, ui.log, recording.log, audio/, frames/, frames_info/
      <folder>/<session_id>/   # once the recording was saved (Stop, auto stop, recovery)
        manifest.json, transcript.txt, transcript.json, recording.log, crop.json,
        instructions.txt, audio/, frames/, frames_info/, input/, archive/, agent/

``<folder>`` is ``default`` (always there) or a folder the user created in the editor
(:mod:`video_story.core.folders`). A recording is saved into the *main* folder; the user
can move it to another folder later, so the final directory of a session is looked up
by its id (:attr:`SessionPaths.final_dir`).

``audio/`` holds the microphone (``audio.wav``), the sound this computer played
(``computer.wav``, Windows only, when it could be recorded) and ``tracks.json``, the
tracks the editor plays, shows, transcribes and renders (see
:mod:`video_story.core.audio_mix`).

``frames/`` holds only the images (``<seconds>_<millis>_<screenId>.<png|jpg>``),
``frames_info/`` the sidecar JSON of every frame (same name, ``.json``). Archiving
(:mod:`video_story.export.archive`) turns every audio track into ``archive/audio.m4a``
/ ``archive/computer.m4a`` (with a copy of ``tracks.json``) and every screen into
``archive/<screenId>.mp4`` with a still ``<screenId>.jpg`` and subtitles
``<screenId>.srt``, then deletes ``audio/`` and ``frames/``; ``frames_info/`` and the
transcript stay.

``input/mouse.jsonl`` holds the mouse actions and the elements under the mouse (see
:mod:`video_story.core.input_events`); it exists only when Mouse was switched on and the
platform could record it. Archiving keeps it.

``agent/`` holds only files derived for agents (``timeline.txt``, ``mouse.jsonl``: the
output of the ``timeline`` and ``mouse`` commands, see :mod:`video_story.host.agent_output`).
Every call rewrites them, so they are safe to lose: moving a recording to another folder
carries them along, finalisation drops the ones written into the work directory.

``state.json``, ``lock`` and ``ui.log`` never leave the work directory: ``state.json`` is
the agent's contract, and ``ui.log`` is the stdout/stderr of the detached window process,
which keeps it open for as long as the window lives. On Windows an open file blocks the
rename of its directory, so finalisation moves the recording entry by entry instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from video_story.core.fileio import (
    atomic_write_text,
    read_json_with_retry,
    replace_with_retry,
)

WORK_DIR_NAME = ".work"
FRAMES_DIR_NAME = "frames"
FRAMES_INFO_DIR_NAME = "frames_info"
AUDIO_DIR_NAME = "audio"
AUDIO_FILE_NAME = "audio.wav"
AUDIO_16K_FILE_NAME = "audio16k.wav"
COMPUTER_AUDIO_FILE_NAME = "computer.wav"  # what this computer played (loopback)
AUDIO_TRACKS_FILE_NAME = "tracks.json"  # which audio tracks are switched on
ARCHIVE_DIR_NAME = "archive"
INPUT_DIR_NAME = "input"
MOUSE_FILE_NAME = "mouse.jsonl"  # mouse actions, elements under the mouse
AGENT_DIR_NAME = "agent"  # files derived for agents (timeline.txt, ...), rewritten by each call
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "lock"
LOG_FILE_NAME = "recording.log"
UI_LOG_FILE_NAME = "ui.log"
MANIFEST_FILE_NAME = "manifest.json"
TRANSCRIPT_TXT_NAME = "transcript.txt"
TRANSCRIPT_JSON_NAME = "transcript.json"
CROP_FILE_NAME = "crop.json"
INSTRUCTIONS_FILE_NAME = "instructions.txt"

# Files that stay in the work directory when a recording is finalised or discarded.
WORK_ONLY_FILES = frozenset({STATE_FILE_NAME, LOCK_FILE_NAME, UI_LOG_FILE_NAME})

_SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class Status(StrEnum):
    READY = "ready"  # window open, not recording yet
    RECORDING = "recording"
    PROCESSING = "processing"  # saving the recording (manifest, final folder)
    EDITING = "editing"  # saved, the editor is open; waiting for Process / close
    PROCESSED = "processed"  # final: Process was pressed, the agent may act
    DISCARDED = "discarded"  # final: user discarded the recording
    CANCELLED = "cancelled"  # final: no recording, or the editor ended without Process
    FAILED = "failed"  # final: the window died or the recording could not be saved

    @property
    def final(self) -> bool:
        return self in FINAL_STATUSES


FINAL_STATUSES = frozenset({Status.PROCESSED, Status.DISCARDED, Status.CANCELLED, Status.FAILED})


def now_iso() -> str:
    """Local time with offset, ISO 8601, millisecond precision."""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_session_id(root: Path | None = None, when: datetime | None = None) -> str:
    """``YYYYMMDD-HHMMSS`` local time; suffixed ``-2``, ``-3``... if it already exists."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    if root is None:
        return stamp
    candidate = stamp
    counter = 2
    while (
        (root / candidate).exists()
        or (root / WORK_DIR_NAME / candidate).exists()
        or locate_final_dir(root, candidate) is not None
    ):
        candidate = f"{stamp}-{counter}"
        counter += 1
    return candidate


def is_session_id(value: str) -> bool:
    return bool(_SESSION_ID_RE.match(value))


def _is_folder_name(name: str) -> bool:
    return not name.startswith(".") and not is_session_id(name)


def locate_final_dir(recording_folder: Path, session_id: str) -> Path | None:
    """``<recording_folder>/<folder>/<session_id>`` when it exists in some folder."""
    try:
        with os.scandir(recording_folder) as entries:
            folders = [Path(e.path) for e in entries if _is_folder_name(e.name) and e.is_dir()]
    except OSError:
        return None
    for folder in folders:
        candidate = folder / session_id
        if candidate.is_dir():
            return candidate
    return None


@dataclass(frozen=True)
class SessionPaths:
    """All paths of one session, derived from the recording folder and id.

    ``folder`` is the folder the saved recording lies in, when the caller knows it
    (:func:`list_sessions`); otherwise :attr:`final_dir` looks it up. It does not take
    part in comparisons: two paths of the same session are equal.
    """

    recording_folder: Path
    session_id: str
    folder: str | None = field(default=None, compare=False)

    @property
    def work_dir(self) -> Path:
        return self.recording_folder / WORK_DIR_NAME / self.session_id

    def _existing_final_dir(self) -> Path | None:
        if self.folder is not None:
            known = self.recording_folder / self.folder / self.session_id
            if known.is_dir():
                return known
        return locate_final_dir(self.recording_folder, self.session_id)

    @property
    def final_dir(self) -> Path:
        """Where the saved recording is: in its folder, or - not saved yet - where it
        goes: the main folder (``default`` when the main folder is gone)."""
        if self.folder is not None:
            return self.recording_folder / self.folder / self.session_id
        found = locate_final_dir(self.recording_folder, self.session_id)
        if found is not None:
            return found
        from video_story.core.folders import main_folder  # folders imports this module

        return self.recording_folder / main_folder(self.recording_folder) / self.session_id

    @property
    def current_dir(self) -> Path:
        """Final directory if the session was processed, otherwise the work directory."""
        return self._existing_final_dir() or self.work_dir

    # Files inside the current directory ------------------------------------

    def frames_dir(self, base: Path | None = None) -> Path:
        """Images of the frames only."""
        return (base or self.current_dir) / FRAMES_DIR_NAME

    def frames_info_dir(self, base: Path | None = None) -> Path:
        """Sidecar JSON of every frame (cursor, drawings, zoom, clipboard)."""
        return (base or self.current_dir) / FRAMES_INFO_DIR_NAME

    def audio_dir(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / AUDIO_DIR_NAME

    def audio_path(self, base: Path | None = None) -> Path:
        return self.audio_dir(base) / AUDIO_FILE_NAME

    def computer_audio_path(self, base: Path | None = None) -> Path:
        """The sound this computer played while recording (``audio/computer.wav``)."""
        return self.audio_dir(base) / COMPUTER_AUDIO_FILE_NAME

    def audio_tracks_path(self, base: Path | None = None) -> Path:
        """Which audio tracks are switched on in the editor (``audio/tracks.json``)."""
        return self.audio_dir(base) / AUDIO_TRACKS_FILE_NAME

    def audio_16k_path(self, base: Path | None = None) -> Path:
        return self.audio_dir(base) / AUDIO_16K_FILE_NAME

    def input_dir(self, base: Path | None = None) -> Path:
        """Mouse input of the recording (``input/``)."""
        return (base or self.current_dir) / INPUT_DIR_NAME

    def mouse_log_path(self, base: Path | None = None) -> Path:
        """``input/mouse.jsonl``; missing when the mouse was not recorded."""
        return self.input_dir(base) / MOUSE_FILE_NAME

    def archive_dir(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / ARCHIVE_DIR_NAME

    def agent_dir(self, base: Path | None = None) -> Path:
        """Files derived for agents (``agent/``): rewritten by each call, safe to lose."""
        return (base or self.current_dir) / AGENT_DIR_NAME

    def log_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / LOG_FILE_NAME

    def manifest_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / MANIFEST_FILE_NAME

    def transcript_txt_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / TRANSCRIPT_TXT_NAME

    def transcript_json_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / TRANSCRIPT_JSON_NAME

    def crop_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / CROP_FILE_NAME

    def instructions_path(self, base: Path | None = None) -> Path:
        """The user's master instructions for the agent (``instructions.txt``)."""
        return (base or self.current_dir) / INSTRUCTIONS_FILE_NAME

    @property
    def state_path(self) -> Path:
        """``state.json`` always lives in the work directory (it survives finalisation)."""
        return self.work_dir / STATE_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.work_dir / LOCK_FILE_NAME

    def create_work_dirs(self) -> None:
        self.frames_dir(self.work_dir).mkdir(parents=True, exist_ok=True)
        self.frames_info_dir(self.work_dir).mkdir(parents=True, exist_ok=True)
        self.audio_dir(self.work_dir).mkdir(parents=True, exist_ok=True)

    def finalize(self, resume: bool = False) -> Path:
        """Move the recording into its final directory (same volume => renames).

        Every entry of the work directory except ``state.json``, ``lock`` and ``ui.log``
        is moved; ``frames/`` goes as one rename. The work directory itself stays, so
        ``wait``/``status`` keep reading ``state.json`` at its documented location.

        ``manifest.json`` goes last: a final directory without it is a move that
        stopped half way (a file was locked), and ``resume`` continues such a move
        instead of refusing the existing directory.
        """
        # The target is chosen once: the main folder could change (or vanish) in the
        # editor while the entries move, which must not split the recording.
        final = self.final_dir
        if final.exists():
            if not resume or self.manifest_path(final).exists():
                raise FileExistsError(f"final directory already exists: {final}")
        final.mkdir(parents=True, exist_ok=True)
        entries = [e for e in self.work_dir.iterdir() if e.name not in WORK_ONLY_FILES]
        entries.sort(key=lambda e: (e.name == MANIFEST_FILE_NAME, e.name))
        for entry in entries:
            if entry.name.endswith(".tmp"):
                continue  # leftover of an interrupted atomic write
            if entry.name == AGENT_DIR_NAME:
                # Derived files an agent may be reading right now: an open file must
                # not stop the move, and the next `timeline` call writes them again.
                shutil.rmtree(entry, ignore_errors=True)
                continue
            _replace_with_retry(entry, final / entry.name)
        return final

    def discard(self, keep_state: bool = True) -> None:
        """Delete every file of this recording.

        ``state.json`` (and the lock) are kept by default so that an agent waiting on
        the session still sees its status; the window returns to the Record state.
        """
        if not self.work_dir.exists():
            return
        if not keep_state:
            shutil.rmtree(self.work_dir, ignore_errors=False)
            return
        for entry in self.work_dir.iterdir():
            if entry.name in WORK_ONLY_FILES:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=False)
            else:
                entry.unlink()

    def remove_work_dir_if_empty(self) -> None:
        try:
            if self.work_dir.exists() and not any(self.work_dir.iterdir()):
                self.work_dir.rmdir()
        except OSError:
            pass


def _replace_with_retry(src: Path, dst: Path, attempts: int = 10, delay: float = 0.3) -> None:
    """``os.replace`` with retries: on Windows a file or directory cannot be renamed
    while it (or, for a directory, anything inside it) is open: log handlers, the
    search indexer, antivirus scanners."""
    replace_with_retry(src, dst, attempts=attempts, delay=delay)


@dataclass
class SessionState:
    """Content of ``state.json`` - the contract between the UI process and the agent."""

    session_id: str
    status: Status
    recording_folder: str
    pid: int = field(default_factory=os.getpid)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    recording_started_at: str | None = None
    recorded_duration_ms: int | None = None
    recording_dir: str | None = None  # final directory once processed
    manifest: str | None = None
    transcript_txt: str | None = None
    transcript_json: str | None = None
    frames_dir: str | None = None
    frames_info_dir: str | None = None
    progress: float | None = None  # 0..1 during processing
    message: str | None = None  # human readable detail (errors, auto stop, ...)
    error_code: str | None = None
    crop: dict[str, int] | None = None  # {"start_ms", "end_ms"} the agent should act on
    processed_via: str | None = None  # "stop_dialog" | "editor"
    instructions: str | None = None  # master instructions typed in the editor ("" = none)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        data["final"] = self.status.final
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        data = dict(data)
        data.pop("final", None)
        data["status"] = Status(data["status"])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def write_state(path: Path, state: SessionState) -> None:
    """Atomically write ``state.json`` (retried while the agent holds the file open)."""
    state.updated_at = now_iso()
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2) + "\n")


def read_state(path: Path) -> SessionState:
    """Read ``state.json``; waits out a concurrent rewrite (see ``core.fileio``)."""
    try:
        return SessionState.from_dict(read_json_with_retry(path))
    except (OSError, KeyError, ValueError) as exc:
        raise RuntimeError(f"cannot read state file {path}: {exc}") from exc


def find_session_paths(recording_folder: Path, session_id: str) -> SessionPaths:
    if not is_session_id(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    return SessionPaths(recording_folder, session_id)


def list_sessions(recording_folder: Path) -> list[SessionPaths]:
    """All sessions (work or final, in every folder) under the recording folder, newest
    first. A saved recording carries its ``folder``. Session directories lying directly
    in the recording folder (the flat layout before folders) are not listed."""
    found: dict[str, SessionPaths] = {}
    work = recording_folder / WORK_DIR_NAME
    if work.is_dir():
        for entry in work.iterdir():
            if entry.is_dir() and is_session_id(entry.name):
                found[entry.name] = SessionPaths(recording_folder, entry.name)
    if recording_folder.is_dir():
        for folder in recording_folder.iterdir():
            if not _is_folder_name(folder.name) or not folder.is_dir():
                continue
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir() and is_session_id(entry.name):
                    found[entry.name] = SessionPaths(recording_folder, entry.name, folder.name)
    return [found[k] for k in sorted(found, reverse=True)]


__all__ = [
    "AGENT_DIR_NAME",
    "ARCHIVE_DIR_NAME",
    "AUDIO_16K_FILE_NAME",
    "AUDIO_DIR_NAME",
    "AUDIO_FILE_NAME",
    "AUDIO_TRACKS_FILE_NAME",
    "COMPUTER_AUDIO_FILE_NAME",
    "CROP_FILE_NAME",
    "FINAL_STATUSES",
    "FRAMES_DIR_NAME",
    "FRAMES_INFO_DIR_NAME",
    "INPUT_DIR_NAME",
    "INSTRUCTIONS_FILE_NAME",
    "LOG_FILE_NAME",
    "UI_LOG_FILE_NAME",
    "WORK_ONLY_FILES",
    "MANIFEST_FILE_NAME",
    "MOUSE_FILE_NAME",
    "STATE_FILE_NAME",
    "TRANSCRIPT_JSON_NAME",
    "TRANSCRIPT_TXT_NAME",
    "WORK_DIR_NAME",
    "SessionPaths",
    "SessionState",
    "Status",
    "find_session_paths",
    "is_session_id",
    "list_sessions",
    "locate_final_dir",
    "new_session_id",
    "now_iso",
    "now_utc_iso",
    "read_state",
    "write_state",
]
