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
    FRAMES_INFO_DIR_NAME,
    UI_LOG_FILE_NAME,
    WORK_DIR_NAME,
    SessionPaths,
    SessionState,
    Status,
    is_session_id,
    list_sessions,
    new_session_id,
    read_state,
    write_state,
)
from video_story.core.transcript import (
    format_segment_line,
    ordered_segments,
    read_transcript,
    transcript_status,
)
from video_story.host import startup
from video_story.host.agent_output import DEFAULT_INLINE_CHARS, fits_inline
from video_story.host.errors import HostError
from video_story.host.readiness import require_ready
from video_story.host.waiting import timeout_seconds
from video_story.platform.process import gui_python, pid_alive, spawn_detached

log = logging.getLogger(__name__)

STARTUP_GRACE_S = 30.0
POLL_INTERVAL_S = 0.5
SUMMARY_MESSAGE_CHARS = 200  # `sessions`: a longer message is cut
# `frames`: its JSON is shortened step by step while it is longer than this. `--window`
# keeps the frames nearest to the moment; then the repeated shapes go, then a long
# copied text is cut.
FRAMES_MAX_CHARS = 20_000
FRAMES_WINDOW_MAX = 24  # frames of `--window`, over all the selected screens
FRAMES_CLIPBOARD_CHARS = 1_000


class SessionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Resolving sessions -----------------------------------------------------------


def resolve_session(config: Config, session: str) -> SessionPaths:
    """Accept a session id or a path to a recording (``<root>/<folder>/<id>``) or work
    (``<root>/.work/<id>``) directory."""
    if is_session_id(session):
        return SessionPaths(config.recording_folder, session)
    path = Path(session).expanduser()
    if path.is_dir() and is_session_id(path.name):
        parent = path.parent
        if parent.name == WORK_DIR_NAME:
            return SessionPaths(parent.parent, path.name)
        return SessionPaths(parent.parent, path.name, folder=parent.name)
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
        pending = paths.work_dir / "startup.json"
        if not state.status.final and (
            pid_alive(state.pid)
            or (state.pid == 0 and pending.is_file() and startup.valid(pending))
        ):
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
    if active_session(config) is None:
        require_ready(config)
    with startup.launch_guard(config, "recorder"):
        return _launch_control_window_ready(config, config_file)


def _launch_control_window_ready(config: Config, config_file: Path | None = None) -> LaunchInfo:
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
    request = startup.create_request(paths.work_dir / "startup.json", config, "run", session_id)
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
    try:
        pid = spawn_detached(args, paths.work_dir / UI_LOG_FILE_NAME, cwd=Path.cwd())
    except OSError as exc:
        state.status = Status.FAILED
        state.error_code = "START_FAILED"
        state.message = str(exc)
        write_state(paths.state_path, state)  # no child exists to own this state yet
        raise HostError(
            "START_FAILED",
            str(exc),
            24,
            session_id=session_id,
            log=str(paths.work_dir / UI_LOG_FILE_NAME),
        ) from exc
    ready = startup.wait_ready(
        request, pid, paths.work_dir / UI_LOG_FILE_NAME, session_id=session_id
    )
    pid = ready.get("pid", pid)
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
                # The editor ended without Process or close (crash, killed): nobody
                # pressed Process, so the agent must not act - the same outcome as
                # closing the editor. The recording stays in its final folder.
                state.status = Status.CANCELLED
                state.processed_via = "editor"
                state.recording_dir = str(paths.final_dir)
                state.message = (
                    "The editor process ended without Process; the recording is kept "
                    "but nothing should be done. Open it again with Edit Video Story."
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
        frames_info_dir=str(paths.frames_info_dir(final)),
    ).to_dict()


def session_summary(paths: SessionPaths) -> dict[str, Any]:
    """What an agent needs to pick a recording (one entry of ``sessions``): a few short
    fields, so twenty of them stay far below what a host cuts. ``status --session``
    returns the full state."""
    summary: dict[str, Any] = {"session_id": paths.session_id, "folder": paths.folder}
    try:
        state = session_status(paths)
    except Exception as exc:  # noqa: BLE001 - one broken session must not hide the others
        summary.update(status="unknown", message=str(exc)[:SUMMARY_MESSAGE_CHARS])
        return summary
    recording_dir = state.get("recording_dir")
    started = state.get("recording_started_at")
    updated = state.get("updated_at")
    duration = state.get("recorded_duration_ms")
    if "state_file" not in state:  # no state.json (its work directory is gone): the manifest
        try:
            manifest = json.loads(paths.manifest_path(paths.final_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        started = manifest.get("started_at")
        updated = manifest.get("finished_at")
        duration = manifest.get("recorded_duration_ms")
    archived = False
    if recording_dir:
        from video_story.core.archive_state import read_archive_state

        archive = read_archive_state(paths, Path(recording_dir))
        archived = bool(archive and archive.archived)
    summary.update(
        status=state.get("status"),
        recording_dir=recording_dir,
        recording_started_at=started,
        recorded_duration_ms=duration,
        updated_at=updated,
        archived=archived,
    )
    # The message of a processed session only repeats the crop; elsewhere it says why.
    if state.get("status") != str(Status.PROCESSED) and state.get("message"):
        summary["message"] = str(state["message"])[:SUMMARY_MESSAGE_CHARS]
    if state.get("error_code"):
        summary["error_code"] = state["error_code"]
    return summary


def wait_for_session(paths: SessionPaths, timeout_s: float) -> dict[str, Any]:
    """Block until the session is final or ``timeout_s`` elapses (then ``timed_out``)."""
    deadline = time.monotonic() + timeout_seconds(timeout_s)
    while True:
        data = session_status(paths)
        if data["final"]:
            startup.cleanup_completed(paths.work_dir / "startup.json")
            data["timed_out"] = False
            return data
        if time.monotonic() >= deadline:
            data["timed_out"] = True
            data["hint"] = "call `video-story wait` again until final is true"
            return data
        time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))


ARCHIVED_NOTE = (
    "This recording is archived: its frames and audio were replaced by one MP4 per screen "
    "in archive/ (<screenId>.mp4 with subtitles <screenId>.srt and a still <screenId>.jpg "
    "from the middle). The MP4 holds only the audio tracks that were switched on in the "
    "editor; `audio` lists every track on its own (AAC), `tracks_file` says which were on "
    "(missing: all). The transcript and the frame sidecars (frames_info/) are still "
    "there, so the timeline works; frame images are gone (image null)."
)


ARCHIVE_FAILED_NOTE = (
    "Archiving this recording failed and nothing was deleted: it is an ordinary recording, "
    "its frames and audio are all there."
)


ARCHIVE_CANCELLED_NOTE = (
    "The user cancelled the archiving of this recording before anything was deleted: it "
    "is an ordinary, not archived recording, its frames and audio are all there."
)


def archive_info(paths: SessionPaths, base: Path) -> dict[str, Any] | None:
    """``{status, archive_dir, videos, stills, audio, note}`` of an archived (or queued)
    recording; ``None`` when it was never asked to be archived. A recording whose
    archiving failed with nothing deleted (status ``failed``) carries ``error`` too; one
    whose archiving the user cancelled (status ``cancelled``) is not archived either,
    the note says so.

    ``audio`` maps the audio tracks kept on their own (``microphone``, ``computer``) to
    their ``.m4a``; it is empty for a recording without audio and for one archived
    before the tracks were kept. ``tracks_file`` (``archive/tracks.json``, which tracks
    were switched on) is there only when the file is."""
    from video_story.core.archive_state import archived_tracks_path, read_archive_state

    state = read_archive_state(paths, base)
    if state is None:
        return None
    folder = paths.archive_dir(base)
    info: dict[str, Any] = {
        "status": str(state.status),
        "archive_dir": str(folder),
        "videos": {s: str(folder / f"{s}.mp4") for s in state.screens_done},
        "stills": {s: str(folder / f"{s}.jpg") for s in state.screens_done},
        "audio": {track: str(folder / name) for track, name in state.audio.items()},
    }
    tracks_file = archived_tracks_path(paths, base)
    if tracks_file.is_file():
        info["tracks_file"] = str(tracks_file)
    if state.archived:
        info["note"] = ARCHIVED_NOTE
    elif state.failed:
        info["note"] = ARCHIVE_FAILED_NOTE
        if state.error:
            info["error"] = state.error
    elif state.cancelled:
        info["note"] = ARCHIVE_CANCELLED_NOTE
    else:
        info["note"] = "Archiving is queued or running."
    return info


# Frames -----------------------------------------------------------------------


@dataclass
class FrameRef:
    screen_id: str
    time_ms: int
    image: Path | None  # None: the image is gone (an archived recording)
    sidecar: Path

    def to_dict(self, include_drawings: bool = True) -> dict[str, Any]:
        cursor = None
        drawings: list[Any] = []  # finished shapes (released mouse button)
        drawing_now: dict[str, Any] | None = None  # the shape being drawn, no geometry
        zoom: list[Any] = []
        background: str | None = None  # "screen" / "sketch" while drawing, else None
        grid: int | None = None  # grid spacing (image pixels) while snapping, else None
        grid_source: str | None = None  # "ctrl" or "toggle" (Snap to grid) while snapping
        clipboard: str | None = None  # text copied during the recording (see ui.clipboard)
        try:
            data = json.loads(self.sidecar.read_text(encoding="utf-8"))
            position = cursor_from_json(data.get("cursor"))
            cursor = position.to_json() if position else None
            raw_drawings = data.get("drawings")
            if isinstance(raw_drawings, list):
                drawings = raw_drawings
            raw_drawing_now = data.get("drawing_now")
            if isinstance(raw_drawing_now, dict):
                drawing_now = raw_drawing_now
            raw_zoom = data.get("zoom")
            if isinstance(raw_zoom, list):
                zoom = raw_zoom
            raw_background = data.get("background")
            if isinstance(raw_background, str):
                background = raw_background
            raw_grid = data.get("grid")
            if isinstance(raw_grid, int) and not isinstance(raw_grid, bool):
                grid = raw_grid
                raw_source = data.get("grid_source")
                grid_source = raw_source if isinstance(raw_source, str) else "ctrl"
            raw_clipboard = data.get("clipboard")
            if isinstance(raw_clipboard, str):
                clipboard = raw_clipboard
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        result: dict[str, Any] = {
            "screen": self.screen_id,
            "time_ms": self.time_ms,
            "image": str(self.image) if self.image is not None else None,
            "sidecar": str(self.sidecar),
            "cursor": cursor,
            "drawings": drawings,
            "drawing_now": drawing_now,
            "zoom": zoom,
            "background": background,
            "grid": grid,
            "grid_source": grid_source,
            "clipboard": clipboard,
        }
        if not include_drawings:
            del result["drawings"]
            result["drawing_ids"] = [d.get("id") for d in drawings if isinstance(d, dict)]
        return result


class FrameIndex:
    """Index of all frames of a recording, grouped by screen and sorted by time.

    Images live in ``frames/``, their sidecars in ``frames_info/`` (next to it unless
    ``info_dir`` says otherwise). A frame whose image is gone (archiving deletes
    ``frames/`` screen by screen) is still indexed from its sidecar, with no image.
    """

    def __init__(self, frames_dir: Path, info_dir: Path | None = None) -> None:
        self.frames_dir = frames_dir
        if info_dir is None:
            info_dir = frames_dir.parent / FRAMES_INFO_DIR_NAME
        self.info_dir = info_dir
        found: dict[tuple[str, int], FrameName] = {}
        for directory, sidecars in ((self.frames_dir, False), (self.info_dir, True)):
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                continue
            for entry in entries:
                parsed = parse_frame_filename(entry.name)
                if parsed is None or parsed.is_sidecar != sidecars:
                    continue
                found.setdefault((parsed.screen_id, parsed.time_ms), parsed)
        self.by_screen: dict[str, list[FrameName]] = {}
        for name in found.values():
            self.by_screen.setdefault(name.screen_id, []).append(name)
        for names in self.by_screen.values():
            names.sort()

    @classmethod
    def of(cls, paths: SessionPaths, base: Path | None = None) -> FrameIndex:
        """The frames of a recording directory (``paths.current_dir`` by default)."""
        return cls(paths.frames_dir(base), paths.frames_info_dir(base))

    def image_of(self, name: FrameName) -> Path | None:
        return None if name.is_sidecar else self.frames_dir / name.filename

    @property
    def screens(self) -> list[str]:
        return sorted(self.by_screen, key=lambda s: int(s.removeprefix("screen")))

    def _ref(self, name: FrameName) -> FrameRef:
        return FrameRef(
            screen_id=name.screen_id,
            time_ms=name.time_ms,
            image=self.image_of(name),
            sidecar=self.info_dir / f"{name.base}.json",
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
    include_drawings: bool = True,
) -> dict[str, Any]:
    """Frames matching a moment of the timeline.

    ``screen``: ``cursor`` (the screen the cursor was on at that moment), ``all``
    (every screen) or a screen id. ``window_ms`` > 0 also returns every frame within
    ``at_ms`` +/- ``window_ms``. ``include_drawings`` False replaces the (repeated)
    drawings list of every frame by ``drawing_ids``.
    """
    base = paths.current_dir
    index = FrameIndex.of(paths, base)
    if not index.screens:
        raise SessionError("NO_FRAMES", f"no frames in {base}")
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
            result["frames"].append(ref.to_dict(include_drawings))
    notes: list[str] = []
    if window_ms > 0:
        per_screen = max(1, FRAMES_WINDOW_MAX // len(selected))
        total = 0
        result["window"] = {}
        for sid in selected:
            refs = index.window(sid, at_ms - window_ms, at_ms + window_ms)
            total += len(refs)
            if len(refs) > per_screen:  # the nearest ones, still in time order
                nearest = sorted(refs, key=lambda r: abs(r.time_ms - at_ms))[:per_screen]
                refs = sorted(nearest, key=lambda r: r.time_ms)
            result["window"][sid] = [r.to_dict(include_drawings) for r in refs]
        shown = sum(len(entries) for entries in result["window"].values())
        if shown < total:
            result["window_total"] = total
            result["window_shown"] = shown
            notes.append(
                f"window lists the {per_screen} frames nearest to at_ms per screen ({shown} of "
                f"{total}); use a smaller window or ask again around another time"
            )
    if manifest is not None:
        result["screen_bounds"] = {s.id: s.to_manifest()["bounds"] for s in manifest.screens}
    archive = archive_info(paths, base)
    if archive is not None:
        result["archive"] = archive
    notes += _shrink_frames(result)
    result["truncated"] = bool(notes)
    if notes:
        result["truncated_note"] = "; ".join(notes) + "."
    return result


def _without_drawings(entry: dict[str, Any]) -> bool:
    """Replace the shapes of a frame entry by their ids (as ``--no-drawings`` does)."""
    if "drawings" not in entry:
        return False
    drawings = entry.pop("drawings")
    entry["drawing_ids"] = [d.get("id") for d in drawings if isinstance(d, dict)]
    return True


def _shrink_frames(result: dict[str, Any]) -> list[str]:
    """Keep the result of :func:`frames_at` below ``FRAMES_MAX_CHARS``; returns what was
    left out. A frame repeats every shape on the screen (a sketch has dozens) and may
    carry a copied text of 64 KB, so the size does not follow from the frame count."""

    def too_long() -> bool:
        return len(json.dumps(result, ensure_ascii=False, default=str)) > FRAMES_MAX_CHARS

    notes: list[str] = []
    window = [entry for entries in (result.get("window") or {}).values() for entry in entries]
    steps = (
        (
            window,
            _without_drawings,
            "window frames list drawing_ids instead of the shapes",
        ),
        (
            window,
            _slim_window_entry,
            "window frames carry only time, image, sidecar and cursor",
        ),
        (
            result["frames"],
            _without_drawings,
            "frames list drawing_ids instead of the shapes (the timeline describes them, "
            "the sidecar has their geometry)",
        ),
        (
            result["frames"],
            _cut_clipboard,
            f"clipboard is cut at {FRAMES_CLIPBOARD_CHARS} characters (clipboard_chars is "
            "its length, the sidecar has all of it)",
        ),
    )
    for entries, change, note in steps:
        if not too_long():
            break
        if sum(change(entry) for entry in entries):
            notes.append(note)
    return notes


def _slim_window_entry(entry: dict[str, Any]) -> bool:
    kept = {k: entry[k] for k in ("screen", "time_ms", "image", "sidecar", "cursor")}
    if len(kept) == len(entry):
        return False
    entry.clear()
    entry.update(kept)
    return True


def _cut_clipboard(entry: dict[str, Any]) -> bool:
    text = entry.get("clipboard")
    if not isinstance(text, str) or len(text) <= FRAMES_CLIPBOARD_CHARS:
        return False
    entry["clipboard_chars"] = len(text)
    entry["clipboard"] = text[:FRAMES_CLIPBOARD_CHARS] + "..."
    return True


def describe_recording(
    paths: SessionPaths, inline_chars: int = DEFAULT_INLINE_CHARS
) -> dict[str, Any]:
    """Summary of a processed recording for agents: paths, manifest, crop, instructions.

    The size does not grow with the recording: the transcript of the cropped range
    (``transcript_in_range``) is included only when it has at most ``inline_chars``
    characters; otherwise a note points to the transcript files and the timeline.
    """
    base = paths.current_dir
    manifest_path = paths.manifest_path(base)
    data: dict[str, Any] = {
        "session_id": paths.session_id,
        "recording_dir": str(base),
        "manifest": str(manifest_path) if manifest_path.exists() else None,
        "transcript_txt": str(paths.transcript_txt_path(base)),
        "transcript_json": str(paths.transcript_json_path(base)),
        "frames_dir": str(paths.frames_dir(base)),
        "frames_info_dir": str(paths.frames_info_dir(base)),
    }
    archive = archive_info(paths, base)
    if archive is not None:
        data["archive"] = archive
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
    json_path = paths.transcript_json_path(base)
    if json_path.exists():
        try:
            transcript = read_transcript(json_path)
        except (OSError, ValueError, KeyError):
            transcript = None
        if transcript is not None:
            segments = transcript.segments_in_range(crop.start_ms, crop.end_ms)
            lines = [format_segment_line(s) for s in ordered_segments(segments)]
            chars = sum(len(line) + 1 for line in lines)
            inline = fits_inline(lines, inline_chars) or not lines
            data["transcript_in_range_lines"] = len(lines)
            data["transcript_in_range_chars"] = chars
            data["transcript_in_range_inline"] = inline
            if inline:
                data["transcript_in_range"] = "\n".join(lines) + ("\n" if lines else "")
            else:
                data["transcript_in_range_note"] = (
                    f"The transcript of the cropped range ({len(lines)} lines, {chars} "
                    "characters) is not inline: read the file `transcript_txt` (the whole "
                    "recording, one segment per line) or use `timeline`."
                )
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
    "session_summary",
    "wait_for_session",
]
