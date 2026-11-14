"""``archive/archive.json`` - how far the archiving of a recording got.

Archiving (:mod:`video_story.export.archive`) first converts every audio track to
``archive/<track file>.m4a`` (recorded in ``audio``), then runs screen by screen: every
screen is rendered to ``archive/<screenId>.mp4`` (with ``.srt`` subtitles and a still
``<screenId>.jpg`` from the middle of the recording), recorded in ``screens_done``,
and its frames are deleted at once. Once every screen is done the audio and the rest
of ``frames/`` are deleted and the status becomes ``archived``.

The file exists from the moment the user (or the automatic archiving of old
recordings) asks for the archive: ``queued`` keeps that request across an editor that
is closed before the work starts, ``archiving`` one that stopped half way. Either way
the next editor resumes the work; an audio track not listed in ``audio`` and a screen
not listed in ``screens_done`` start over.

An attempt that ends with an error (anything but a stop: the editor closing) gives the
recording back as long as nothing was deleted, that is while ``screens_done`` is empty:
the status becomes ``failed``, ``error`` says why and ``failed_attempts`` counts it
(:meth:`ArchiveState.fail`, :func:`record_archive_failure`). A ``failed`` recording is
an ordinary recording again - it can be opened, edited, processed - and nothing resumes
it by itself, with one exception: the editor tries an automatic request (``reason``
``age``) again at its start while ``failed_attempts`` is below
:data:`MAX_AUTOMATIC_ATTEMPTS` (:func:`retry_archive`, the counter stays). The user's
Archive Video queues a ``failed`` recording anew (:func:`request_archive`, the counter
starts at 0). Either way the verified audio copies listed in ``audio`` are reused. Once
a screen is done its frames are gone, so an error then leaves the status ``archiving``
(with ``error``): the recording stays locked and the next editor renders the rest.

An automatic request whose work has not started can be taken back
(:func:`withdraw_archive_request`): the editor does that for a recording it was asked
to show.

The user can cancel an archive while nothing was deleted (**Cancel Archiving**, see
:func:`video_story.export.archive.cancel_archive`): the status becomes ``cancelled``,
``error`` goes, ``failed_attempts`` and the verified ``audio`` copies stay. That is no
failure. A ``cancelled`` recording is an ordinary recording like a ``failed`` one, and
nothing archives it by itself any more - not the age (the file exists), not the
automatic retry (only ``failed`` ones) -, whatever ``archive_after_days`` says. The
user's Archive Video queues it anew like a ``failed`` one (:func:`request_archive`).
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
ARCHIVED_AUDIO_SUFFIX = ".m4a"  # AAC copy of an audio track (audio.wav -> audio.m4a)
REASON_USER = "user"  # Archive Video(s) in the editor
REASON_AGE = "age"  # older than archive_after_days when an editor started
# An automatic (age) request that failed is tried again at the start of an editor while
# it failed fewer times than this; then it waits for the user's Archive Video.
MAX_AUTOMATIC_ATTEMPTS = 3


class ArchiveStatus(StrEnum):
    QUEUED = "queued"  # asked for; nothing done yet (transcription may still be needed)
    ARCHIVING = "archiving"  # some screens may be done (see screens_done)
    FAILED = "failed"  # the last attempt failed with nothing deleted: a normal recording
    # The user cancelled it with nothing deleted: a normal recording, never archived
    # automatically again (only by the user's Archive Video).
    CANCELLED = "cancelled"
    ARCHIVED = "archived"  # every screen archived; audio and frames deleted (best effort)


@dataclass
class ArchiveState:
    status: ArchiveStatus = ArchiveStatus.QUEUED
    requested_at: str = field(default_factory=now_iso)
    reason: str = "user"  # "user" (Archive in the editor) or "age" (older than the limit)
    screens: list[str] = field(default_factory=list)  # every screen to archive
    screens_done: list[str] = field(default_factory=list)
    # Audio tracks converted and verified: track name ("microphone", "computer") -> file
    # name in archive/ ("audio.m4a"). Empty: nothing converted (yet), a recording
    # without audio, or a file written before the tracks were archived on their own.
    audio: dict[str, str] = field(default_factory=dict)
    started_at: str | None = None
    finished_at: str | None = None
    # Files or folders that could not be deleted (locked by another program); the
    # editor tries once more at its next start, then they stay.
    leftovers: list[str] = field(default_factory=list)
    error: str | None = None  # why the last attempt stopped
    # Attempts that ended in status "failed" since the request (or since the user asked
    # again); missing in files of older versions (0).
    failed_attempts: int = 0

    @property
    def archived(self) -> bool:
        return self.status == ArchiveStatus.ARCHIVED

    @property
    def failed(self) -> bool:
        return self.status == ArchiveStatus.FAILED

    @property
    def cancelled(self) -> bool:
        return self.status == ArchiveStatus.CANCELLED

    @property
    def pending(self) -> bool:
        """Queued or being archived: the recording is locked until the work is done."""
        return self.status in (ArchiveStatus.QUEUED, ArchiveStatus.ARCHIVING)

    @property
    def cancellable(self) -> bool:
        """Queued or being archived with nothing deleted yet (no screen done): the user
        can still cancel it."""
        return self.pending and not self.screens_done

    def cancel(self) -> bool:
        """The user cancelled the archiving: unless it is archived or a screen is done
        (its frames are gone, the work has to finish), the status becomes ``cancelled``
        and ``error`` goes; ``failed_attempts`` and ``audio`` stay. Returns whether it
        was cancelled."""
        if self.archived or self.screens_done:
            return False
        self.status = ArchiveStatus.CANCELLED
        self.error = None
        return True

    def fail(self, message: str) -> bool:
        """An attempt ended with an error (not a stop): remember why and, while nothing
        was deleted (no screen done), give the recording back - status ``failed``, the
        attempt counted. Returns whether it was given back; with a screen done the
        status stays as it is (``archiving``: locked, resumed by the next editor)."""
        self.error = message
        if self.screens_done:
            return False
        self.status = ArchiveStatus.FAILED
        self.failed_attempts += 1
        return True

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
            "audio": dict(self.audio),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "leftovers": list(self.leftovers),
            "error": self.error,
            "failed_attempts": self.failed_attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchiveState:
        audio = data.get("audio")  # missing in files of older versions
        attempts = data.get("failed_attempts")  # missing in files of older versions
        return cls(
            status=ArchiveStatus(data.get("status", ArchiveStatus.QUEUED)),
            requested_at=str(data.get("requested_at") or ""),
            reason=str(data.get("reason") or "user"),
            screens=[str(s) for s in data.get("screens") or []],
            screens_done=[str(s) for s in data.get("screens_done") or []],
            audio={str(k): str(v) for k, v in audio.items()} if isinstance(audio, dict) else {},
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            leftovers=[str(s) for s in data.get("leftovers") or []],
            error=data.get("error"),
            failed_attempts=(
                max(0, attempts)
                if isinstance(attempts, int) and not isinstance(attempts, bool)
                else 0
            ),
        )


def archive_state_path(paths: SessionPaths, base: Path | None = None) -> Path:
    return paths.archive_dir(base) / ARCHIVE_STATE_FILE_NAME


def archived_audio_path(paths: SessionPaths, track_file: Path, base: Path | None = None) -> Path:
    """Where archiving puts the AAC copy of the audio track ``track_file``:
    ``audio/audio.wav`` becomes ``archive/audio.m4a``, ``audio/computer.wav``
    ``archive/computer.m4a``."""
    return paths.archive_dir(base) / (track_file.stem + ARCHIVED_AUDIO_SUFFIX)


def archived_tracks_path(paths: SessionPaths, base: Path | None = None) -> Path:
    """``archive/tracks.json``: the copy of ``audio/tracks.json`` made by the archiving
    (which tracks were switched on, so which of them the MP4s hold); missing when the
    recording had no such file (every track was on) or no audio."""
    return paths.archive_dir(base) / paths.audio_tracks_path(base).name


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


def request_archive(paths: SessionPaths, reason: str = REASON_USER) -> ArchiveState:
    """Queue a recording for archiving.

    An existing request is kept as it is, with one exception: a ``failed`` or
    ``cancelled`` one is queued anew - a new request with ``reason``,
    ``failed_attempts`` back at 0 and the old error gone; the audio copies already
    verified (``audio``) are kept.
    """
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if state is not None:
        if not (state.failed or state.cancelled):
            return state
        state.status = ArchiveStatus.QUEUED
        state.reason = reason
        state.requested_at = now_iso()
        state.failed_attempts = 0
        state.error = None
    else:
        state = ArchiveState(reason=reason)
    write_archive_state(paths, state, base)
    return state


def retry_archive(paths: SessionPaths) -> ArchiveState | None:
    """Queue a ``failed`` recording once more for the automatic retry: the status goes
    back to ``queued``, ``failed_attempts`` (and ``audio``, ``reason``, the last error)
    stay. Returns the state, ``None`` when the recording is not ``failed``."""
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if state is None or not state.failed:
        return None
    state.status = ArchiveStatus.QUEUED
    write_archive_state(paths, state, base)
    return state


def record_archive_failure(paths: SessionPaths, message: str) -> ArchiveState | None:
    """An archive attempt ended with an error that may not be in ``archive.json`` yet
    (the transcription before it failed, the worker crashed or could not start): see
    :meth:`ArchiveState.fail`.

    Safe to call by everyone who sees the failure - the worker and then the editor that
    ran it: a state that is already ``failed`` (the attempt recorded it itself),
    ``cancelled`` or ``archived`` is left alone, so one failed attempt is counted once
    and a cancelled one never. Returns the
    state, ``None`` when the recording was never asked to be archived.
    """
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if state is None or not state.pending:
        return state
    state.fail(message)
    write_archive_state(paths, state, base)
    return state


def withdraw_archive_request(paths: SessionPaths) -> bool:
    """Take back an automatic (``age``) request whose work has not started: status
    ``queued`` and no screen done. The caller makes sure that no worker runs for it.

    A plain request disappears (``archive.json`` deleted); an automatic retry goes back
    to ``failed`` instead, so that its attempts and its verified audio copies are not
    forgotten. Requests of the user and work that has started are never taken back.
    Returns whether the recording is free now.
    """
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if (
        state is None
        or state.status != ArchiveStatus.QUEUED
        or state.reason != REASON_AGE
        or state.screens_done
    ):
        return False
    if state.failed_attempts or state.audio:
        state.status = ArchiveStatus.FAILED
        write_archive_state(paths, state, base)
        return True
    path = archive_state_path(paths, base)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()  # archive/ was created for the request alone
    except OSError:
        pass  # not empty
    return True


def is_archived(paths: SessionPaths, base: Path | None = None) -> bool:
    state = read_archive_state(paths, base)
    return state is not None and state.archived


__all__ = [
    "ARCHIVE_DIR_NAME",
    "ARCHIVED_AUDIO_SUFFIX",
    "ARCHIVE_STATE_FILE_NAME",
    "MAX_AUTOMATIC_ATTEMPTS",
    "REASON_AGE",
    "REASON_USER",
    "ArchiveState",
    "ArchiveStatus",
    "archive_state_path",
    "archived_audio_path",
    "archived_tracks_path",
    "is_archived",
    "read_archive_state",
    "record_archive_failure",
    "request_archive",
    "retry_archive",
    "withdraw_archive_request",
    "write_archive_state",
]
