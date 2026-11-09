"""Launching the control window as a detached process and following a session.

Agents call ``video-story run --detach`` (returns at once), then ``video-story wait``
in a loop until the session reaches a final status, and finally ``video-story frames``
to pick the screenshots that match a moment of the transcript.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from video_story.core.config import Config, config_path
from video_story.core.crop import read_crop
from video_story.core.cursor import cursor_from_json
from video_story.core.instructions import read_instructions
from video_story.core.manifest import Manifest, read_manifest
from video_story.core.naming import FrameName, parse_frame_filename
from video_story.core.session import (
    UI_LOG_FILE_NAME,
    SessionPaths,
    SessionState,
    Status,
    is_session_id,
    list_sessions,
    new_session_id,
    read_state,
    write_state,
)
from video_story.core.transcript import format_segment_line, read_transcript, transcript_status
from video_story.platform.process import gui_python, pid_alive, spawn_detached

log = logging.getLogger(__name__)

STARTUP_GRACE_S = 30.0
POLL_INTERVAL_S = 0.5


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Resolving sessions -----------------------------------------------------------


def resolve_session(config: Config, session: str) -> SessionPaths:
    """Accept a session id or a path to a recording / work directory."""
    if is_session_id(session):
        return SessionPaths(config.recording_folder, session)
    path = Path(session).expanduser()
    if path.is_dir() and is_session_id(path.name):
        parent = path.parent
        if parent.name == ".work":
            parent = parent.parent
        return SessionPaths(parent, path.name)
    raise SessionError("INVALID_SESSION", f"not a session id or recording directory: {session}")


def active_session(
    config: Config, include_editing: bool = False
) -> tuple[SessionPaths, SessionState] | None:
    """A session whose window is still open (non-final state and live process).

    ``editing`` sessions belong to the editor, not to a control window: they are
    skipped by default so that a new recording can start while the editor waits for
    Process; ``include_editing`` returns them too (default session for ``wait`` etc.).
    """
    for paths in list_sessions(config.recording_folder):
        if not paths.state_path.exists():
            continue
        try:
            state = read_state(paths.state_path)
        except RuntimeError:
            continue
        if state.status is Status.EDITING and not include_editing:
            continue
        if not state.status.final and pid_alive(state.pid):
            return paths, state
    return None


# Launching --------------------------------------------------------------------


@dataclass
class LaunchInfo:
    session_id: str
    state_file: Path
    pid: int
    recording_folder: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state_file": str(self.state_file),
            "pid": self.pid,
            "recording_folder": str(self.recording_folder),
        }


def launch_control_window(config: Config, config_file: Path | None = None) -> LaunchInfo:
    """Start the control window in a detached process and return immediately."""
    running = active_session(config)
    if running is not None:
        paths, state = running
        raise SessionError(
            "ALREADY_RUNNING",
            f"a Video Story window is already open for session {paths.session_id} "
            f"(status {state.status}); wait for it or close it first",
        )
    config.recording_folder.mkdir(parents=True, exist_ok=True)
    session_id = new_session_id(config.recording_folder)
    paths = SessionPaths(config.recording_folder, session_id)
    paths.work_dir.mkdir(parents=True, exist_ok=True)
    state = SessionState(
        session_id=session_id,
        status=Status.READY,
        recording_folder=str(config.recording_folder),
        pid=0,
        message="Starting the control window.",
    )
    write_state(paths.state_path, state)
    args = [
        gui_python(),
        "-m",
        "video_story",
        "--config",
        str(config_file or config_path()),
        "ui",
        "--session",
        session_id,
    ]
    pid = spawn_detached(args, paths.work_dir / UI_LOG_FILE_NAME, cwd=Path.cwd())
    state.pid = pid
    write_state(paths.state_path, state)
    log.info("control window started: session %s pid %s", session_id, pid)
    return LaunchInfo(session_id, paths.state_path, pid, config.recording_folder)


# Following --------------------------------------------------------------------


def session_status(paths: SessionPaths) -> dict[str, Any]:
    if not paths.state_path.exists():
        if paths.final_dir.exists() and paths.manifest_path(paths.final_dir).exists():
            return _processed_without_state(paths)
        raise SessionError("UNKNOWN_SESSION", f"no state file for session {paths.session_id}")
    state = read_state(paths.state_path)
    data = state.to_dict()
    if not state.status.final and state.pid and not pid_alive(state.pid):
        age = time.time() - paths.state_path.stat().st_mtime
        if state.pid > 0 or age > STARTUP_GRACE_S:
            if state.status is Status.EDITING:
                # The editor ended without Process or close (crash, killed at start):
                # the recording is complete, so hand the whole range to the agent.
                duration = state.recorded_duration_ms or 0
                state.status = Status.PROCESSED
                state.crop = {"start_ms": 0, "end_ms": duration}
                state.instructions = read_instructions(paths.instructions_path(paths.final_dir))
                state.processed_via = "stop_dialog"
                state.message = (
                    "The editor process ended without Process; act on the whole recording."
                )
                final = paths.final_dir
                if transcript_status(paths.transcript_json_path(final)).pending:
                    # The editor transcribes in the background; it died before the end.
                    state.message += (
                        f" The transcript is incomplete: run `video-story transcribe "
                        f"{final}` first."
                    )
            else:
                state.status = Status.FAILED
                state.error_code = "WINDOW_EXITED"
                state.message = "The control window process ended unexpectedly."
            write_state(paths.state_path, state)
            data = state.to_dict()
    data["state_file"] = str(paths.state_path)
    return data


def _processed_without_state(paths: SessionPaths) -> dict[str, Any]:
    final = paths.final_dir
    return SessionState(
        session_id=paths.session_id,
        status=Status.PROCESSED,
        recording_folder=str(paths.recording_folder),
        recording_dir=str(final),
        manifest=str(paths.manifest_path(final)),
        transcript_txt=str(paths.transcript_txt_path(final)),
        transcript_json=str(paths.transcript_json_path(final)),
        frames_dir=str(paths.frames_dir(final)),
    ).to_dict()


def wait_for_session(paths: SessionPaths, timeout_s: float) -> dict[str, Any]:
    """Block until the session is final or ``timeout_s`` elapses (then ``timed_out``)."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        data = session_status(paths)
        if data["final"]:
            data["timed_out"] = False
            return data
        if time.monotonic() >= deadline:
            data["timed_out"] = True
            data["hint"] = "call `video-story wait` again until final is true"
            return data
        time.sleep(POLL_INTERVAL_S)


# Frames -----------------------------------------------------------------------


@dataclass
class FrameRef:
    screen_id: str
    time_ms: int
    image: Path
    sidecar: Path

    def to_dict(self) -> dict[str, Any]:
        cursor = None
        drawings: list[Any] = []
        zoom: list[Any] = []
        clipboard: str | None = None  # text copied during the recording (see ui.clipboard)
        try:
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
            position = cursor_from_json(data.get("cursor"))
            cursor = position.to_json() if position else None
            raw_drawings = data.get("drawings")
            if isinstance(raw_drawings, list):
                drawings = raw_drawings
            raw_zoom = data.get("zoom")
            if isinstance(raw_zoom, list):
                zoom = raw_zoom
            raw_clipboard = data.get("clipboard")
            if isinstance(raw_clipboard, str):
                clipboard = raw_clipboard
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        return {
            "screen": self.screen_id,
            "time_ms": self.time_ms,
            "image": str(self.image),
            "sidecar": str(self.sidecar),
            "cursor": cursor,
            "drawings": drawings,
            "zoom": zoom,
            "clipboard": clipboard,
        }


class FrameIndex:
    """Index of all frames of a recording, grouped by screen and sorted by time."""

    def __init__(self, frames_dir: Path) -> None:
        self.frames_dir = frames_dir
        self.by_screen: dict[str, list[FrameName]] = {}
        for entry in os.scandir(frames_dir):
            parsed = parse_frame_filename(entry.name)
            if parsed is None or parsed.is_sidecar:
                continue
            self.by_screen.setdefault(parsed.screen_id, []).append(parsed)
        for names in self.by_screen.values():
            names.sort()

    @property
    def screens(self) -> list[str]:
        return sorted(self.by_screen, key=lambda s: int(s.removeprefix("screen")))

    def _ref(self, name: FrameName) -> FrameRef:
        return FrameRef(
            screen_id=name.screen_id,
            time_ms=name.time_ms,
            image=self.frames_dir / name.filename,
            sidecar=self.frames_dir / f"{name.base}.json",
        )

    def at(self, screen_id: str, time_ms: int) -> FrameRef | None:
        """Latest frame at or before ``time_ms``; the first frame when none precede it."""
        names = self.by_screen.get(screen_id)
        if not names:
            return None
        chosen = names[0]
        for name in names:
            if name.time_ms <= time_ms:
                chosen = name
            else:
                break
        return self._ref(chosen)

    def window(self, screen_id: str, start_ms: int, end_ms: int) -> list[FrameRef]:
        names = self.by_screen.get(screen_id, [])
        return [self._ref(n) for n in names if start_ms <= n.time_ms <= end_ms]


def frames_at(
    paths: SessionPaths,
    at_ms: int,
    screen: str = "cursor",
    window_ms: int = 0,
) -> dict[str, Any]:
    """Frames matching a moment of the timeline.

    ``screen``: ``cursor`` (the screen the cursor was on at that moment), ``all``
    (every screen) or a screen id. ``window_ms`` > 0 also returns every frame within
    ``at_ms`` +/- ``window_ms``.
    """
    base = paths.current_dir
    frames_dir = paths.frames_dir(base)
    if not frames_dir.is_dir():
        raise SessionError("NO_FRAMES", f"no frames directory in {base}")
    index = FrameIndex(frames_dir)
    if not index.screens:
        raise SessionError("NO_FRAMES", f"no frames in {frames_dir}")
    manifest: Manifest | None = None
    manifest_path = paths.manifest_path(base)
    if manifest_path.exists():
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, ValueError, KeyError):
            manifest = None

    selected: list[str]
    cursor_screen: str | None = None
    if screen == "all":
        selected = index.screens
    elif screen == "cursor":
        first = index.at(index.screens[0], at_ms)
        cursor_json = first.to_dict()["cursor"] if first else None
        cursor_screen = cursor_json["screen"] if cursor_json else index.screens[0]
        selected = [cursor_screen] if cursor_screen in index.by_screen else [index.screens[0]]
    elif screen in index.by_screen:
        selected = [screen]
    else:
        raise SessionError(
            "UNKNOWN_SCREEN", f"unknown screen {screen!r}; available: {', '.join(index.screens)}"
        )

    result: dict[str, Any] = {
        "session_id": paths.session_id,
        "at_ms": at_ms,
        "screens": index.screens,
        "cursor_screen": cursor_screen,
        "frames": [],
    }
    for sid in selected:
        ref = index.at(sid, at_ms)
        if ref is not None:
            result["frames"].append(ref.to_dict())
    if window_ms > 0:
        result["window"] = {
            sid: [r.to_dict() for r in index.window(sid, at_ms - window_ms, at_ms + window_ms)]
            for sid in selected
        }
    if manifest is not None:
        result["screen_bounds"] = {s.id: s.to_manifest()["bounds"] for s in manifest.screens}
    return result


def describe_recording(paths: SessionPaths) -> dict[str, Any]:
    """Summary of a processed recording for agents (paths + transcript)."""
    base = paths.current_dir
    manifest_path = paths.manifest_path(base)
    data: dict[str, Any] = {
        "session_id": paths.session_id,
        "recording_dir": str(base),
        "manifest": str(manifest_path) if manifest_path.exists() else None,
        "transcript_txt": str(paths.transcript_txt_path(base)),
        "transcript_json": str(paths.transcript_json_path(base)),
        "frames_dir": str(paths.frames_dir(base)),
    }
    duration_ms = 0
    keypoints: list[Any] = []
    if manifest_path.exists():
        data["manifest_content"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        duration_ms = int(data["manifest_content"].get("recorded_duration_ms") or 0)
        raw_keypoints = data["manifest_content"].get("keypoints")
        if isinstance(raw_keypoints, list):
            keypoints = raw_keypoints
    crop = read_crop(paths.crop_path(base), duration_ms)
    data["crop"] = crop.to_dict()
    # Master instructions typed in the editor: they frame the whole recording.
    data["instructions"] = read_instructions(paths.instructions_path(base))
    data["keypoints"] = keypoints
    status = transcript_status(paths.transcript_json_path(base))
    data["transcript_complete"] = status.complete
    if not status.complete:
        data["transcribed_ms"] = status.transcribed_ms
        data["transcript_note"] = (
            "The transcript is incomplete (the editor transcribes in the background); "
            f"segments reach {status.transcribed_ms} ms. `video-story transcribe` completes it."
        )
    txt = paths.transcript_txt_path(base)
    if txt.exists():
        data["transcript"] = txt.read_text(encoding="utf-8")
    json_path = paths.transcript_json_path(base)
    if json_path.exists():
        try:
            transcript = read_transcript(json_path)
        except (OSError, ValueError, KeyError):
            transcript = None
        if transcript is not None:
            segments = transcript.segments_in_range(crop.start_ms, crop.end_ms)
            lines = [format_segment_line(s) for s in segments]
            data["transcript_in_range"] = "\n".join(lines) + ("\n" if lines else "")
    return data


def python_for_logs() -> str:  # pragma: no cover - diagnostics only
    return sys.executable


__all__ = [
    "FrameIndex",
    "FrameRef",
    "LaunchInfo",
    "SessionError",
    "active_session",
    "describe_recording",
    "frames_at",
    "launch_control_window",
    "resolve_session",
    "session_status",
    "wait_for_session",
]
