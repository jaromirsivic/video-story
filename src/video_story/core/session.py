"""Recording session: identifiers, directory layout and the shared ``state.json``.

Layout inside the Recording Folder::

    <recording_folder>/
      .work/<session_id>/      # while recording / processing
        state.json, lock, audio.wav, recording.log, frames/
      <session_id>/            # after "Process Recording"
        manifest.json, audio.wav, transcript.txt, transcript.json, recording.log, frames/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

WORK_DIR_NAME = ".work"
FRAMES_DIR_NAME = "frames"
AUDIO_FILE_NAME = "audio.wav"
AUDIO_16K_FILE_NAME = "audio16k.wav"
STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "lock"
LOG_FILE_NAME = "recording.log"
MANIFEST_FILE_NAME = "manifest.json"
TRANSCRIPT_TXT_NAME = "transcript.txt"
TRANSCRIPT_JSON_NAME = "transcript.json"

_SESSION_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class Status(StrEnum):
    READY = "ready"  # window open, not recording yet
    RECORDING = "recording"
    PROCESSING = "processing"  # transcribing
    PROCESSED = "processed"  # final: recording folder ready for the agent
    DISCARDED = "discarded"  # final: user discarded the recording
    CANCELLED = "cancelled"  # final: window closed without recording
    FAILED = "failed"  # final: unrecoverable error

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
    while (root / candidate).exists() or (root / WORK_DIR_NAME / candidate).exists():
        candidate = f"{stamp}-{counter}"
        counter += 1
    return candidate


def is_session_id(value: str) -> bool:
    return bool(_SESSION_ID_RE.match(value))


@dataclass(frozen=True)
class SessionPaths:
    """All paths of one session, derived from the recording folder and id."""

    recording_folder: Path
    session_id: str

    @property
    def work_dir(self) -> Path:
        return self.recording_folder / WORK_DIR_NAME / self.session_id

    @property
    def final_dir(self) -> Path:
        return self.recording_folder / self.session_id

    @property
    def current_dir(self) -> Path:
        """Final directory if the session was processed, otherwise the work directory."""
        return self.final_dir if self.final_dir.exists() else self.work_dir

    # Files inside the current directory ------------------------------------

    def frames_dir(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / FRAMES_DIR_NAME

    def audio_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / AUDIO_FILE_NAME

    def audio_16k_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / AUDIO_16K_FILE_NAME

    def log_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / LOG_FILE_NAME

    def manifest_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / MANIFEST_FILE_NAME

    def transcript_txt_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / TRANSCRIPT_TXT_NAME

    def transcript_json_path(self, base: Path | None = None) -> Path:
        return (base or self.current_dir) / TRANSCRIPT_JSON_NAME

    @property
    def state_path(self) -> Path:
        """``state.json`` always lives in the work directory (it survives finalisation)."""
        return self.work_dir / STATE_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.work_dir / LOCK_FILE_NAME

    def create_work_dirs(self) -> None:
        self.frames_dir(self.work_dir).mkdir(parents=True, exist_ok=True)

    def finalize(self) -> Path:
        """Move the work directory to its final location (same volume => rename)."""
        if self.final_dir.exists():
            raise FileExistsError(f"final directory already exists: {self.final_dir}")
        self.final_dir.parent.mkdir(parents=True, exist_ok=True)
        state = self.state_path.read_bytes() if self.state_path.exists() else None
        lock = self.lock_path
        if lock.exists():
            lock.unlink()
        _replace_with_retry(self.work_dir, self.final_dir)
        # Keep state.json readable at its documented location for `wait`/`status`.
        if state is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self.state_path.write_bytes(state)
        return self.final_dir

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
        keep = {STATE_FILE_NAME, LOCK_FILE_NAME, "ui.log"}
        for entry in self.work_dir.iterdir():
            if entry.name in keep:
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
    """``os.replace`` with retries: on Windows a directory cannot be renamed while any
    file inside is open (log handlers, indexers, antivirus)."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


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
    progress: float | None = None  # 0..1 during processing
    message: str | None = None  # human readable detail (errors, auto stop, ...)
    error_code: str | None = None

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
    """Atomically write ``state.json``."""
    state.updated_at = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_state(path: Path) -> SessionState:
    """Read ``state.json``; tolerates a concurrent write by retrying once."""
    last_error: Exception | None = None
    for _ in range(3):
        try:
            return SessionState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            last_error = exc
    raise RuntimeError(f"cannot read state file {path}: {last_error}")


def find_session_paths(recording_folder: Path, session_id: str) -> SessionPaths:
    if not is_session_id(session_id):
        raise ValueError(f"invalid session id {session_id!r}")
    return SessionPaths(recording_folder, session_id)


def list_sessions(recording_folder: Path) -> list[SessionPaths]:
    """All sessions (work or final) under the recording folder, newest first."""
    found: dict[str, SessionPaths] = {}
    for base in (recording_folder / WORK_DIR_NAME, recording_folder):
        if not base.exists():
            continue
        for entry in base.iterdir():
            if entry.is_dir() and is_session_id(entry.name):
                found[entry.name] = SessionPaths(recording_folder, entry.name)
    return [found[k] for k in sorted(found, reverse=True)]


__all__ = [
    "AUDIO_16K_FILE_NAME",
    "AUDIO_FILE_NAME",
    "FINAL_STATUSES",
    "FRAMES_DIR_NAME",
    "LOG_FILE_NAME",
    "MANIFEST_FILE_NAME",
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
    "new_session_id",
    "now_iso",
    "now_utc_iso",
    "read_state",
    "write_state",
]
