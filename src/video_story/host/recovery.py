"""Finish recordings whose window ended before they were saved.

A recording lives in ``<recording_folder>/.work/<id>/`` until the control window saves
it (manifest, move to ``<recording_folder>/<id>/``). When the window process dies
first - a crash, a kill, a power loss - or saving failed, the frames and the audio stay
in the work folder, where no tool shows them. :func:`recover_orphaned_recordings`
completes such recordings: it finishes the manifest (the recorder writes a provisional
one at Record, see :mod:`video_story.capture.recorder`; recordings older than that get
one derived from their frames), moves the recording to its final folder and notes the
outcome in ``state.json``. It runs when the control window or the editor starts.

A recovered recording appears in the editor like any other, without a transcript (the
editor transcribes it when it is selected). It is never handed to an agent: the session
stays ``failed``; only **Process** in the editor hands a recording over.
"""

from __future__ import annotations

import logging
import statistics
import wave
from datetime import datetime
from pathlib import Path

from video_story import __version__
from video_story.core.input_events import read_mouse_log
from video_story.core.manifest import (
    COMPUTER_AUDIO_FILE,
    AudioInfo,
    FramesInfo,
    Manifest,
    MouseInfo,
    read_manifest,
    write_manifest,
)
from video_story.core.screens import Screen
from video_story.core.session import (
    WORK_DIR_NAME,
    SessionPaths,
    Status,
    is_session_id,
    read_state,
    write_state,
)
from video_story.host.runner import FrameIndex
from video_story.platform.process import pid_alive

log = logging.getLogger(__name__)

MIN_RECOVERABLE_MS = 1000  # shorter leftovers are not worth a recording
RECOVERED_REASON = "recovered"
RECOVERED_NOTE = (
    "Recovered: the recording window ended before the recording was saved; the "
    "duration comes from the frames and the audio."
)
DEFAULT_INTERVAL_MS = 500


def _owner_alive(paths: SessionPaths) -> bool | None:
    """True while the window of the session runs, ``None`` when that cannot be told
    (no readable ``state.json``: not a session a window left behind)."""
    if not paths.state_path.exists():
        return None
    try:
        state = read_state(paths.state_path)
    except RuntimeError:
        return None
    return pid_alive(state.pid)


def _mouse_info(path: Path, known: MouseInfo | None) -> MouseInfo | None:
    """The manifest's mouse section with the counts taken from ``input/mouse.jsonl``
    (the process died before it could write them)."""
    if not path.is_file():
        return known if known is not None and known.error else None
    try:
        recorded = read_mouse_log(path)
    except OSError:
        log.warning("cannot read %s", path, exc_info=True)
        return known
    info = known or MouseInfo()
    info.events = (
        len(recorded.buttons) + len(recorded.moves) + len(recorded.wheels) + len(recorded.gestures)
    )
    info.elements = sum(len(items) for items in recorded.elements.values())
    info.contents = len(recorded.contents)
    info.windows = len(recorded.windows)
    info.hook_restarts = sum(1 for s in recorded.statuses if s.state == "restarted")
    return info


def _audio_info(path: Path, known: AudioInfo | None) -> AudioInfo:
    info = AudioInfo(
        device=known.device if known else None,
        sample_rate=known.sample_rate if known else 0,
        channels=known.channels if known else 1,
    )
    if not path.is_file():
        return info
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            info.sample_rate = rate
            info.channels = wf.getnchannels()
            info.duration_ms = int(wf.getnframes() * 1000 / rate) if rate else 0
    except (OSError, EOFError, wave.Error) as exc:
        log.warning("cannot read %s: %s", path, exc)
    return info


def _screens_from_frames(index: FrameIndex) -> list[Screen]:
    """Screens of a recording that has no manifest at all: sizes come from the first
    frame of each screen, laid out side by side (the real layout is not known)."""
    from PIL import Image

    screens: list[Screen] = []
    left = 0
    for number, screen_id in enumerate(index.screens, start=1):
        first = next((n for n in index.by_screen[screen_id] if not n.is_sidecar), None)
        if first is None:
            continue
        try:
            with Image.open(index.frames_dir / first.filename) as image:
                width, height = image.size
        except OSError as exc:
            log.warning("cannot read %s: %s", first.filename, exc)
            continue
        screens.append(
            Screen(
                id=screen_id,
                index=number,
                x=left,
                y=0,
                width=width,
                height=height,
                primary=number == 1,
                scale=1.0,
            )
        )
        left += width
    return screens


def _interval_from_frames(index: FrameIndex) -> int:
    for screen_id in index.screens:
        times = [name.time_ms for name in index.by_screen[screen_id]]
        steps = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
        if steps:
            return max(1, int(statistics.median(steps)))
    return DEFAULT_INTERVAL_MS


def _finished_at(index: FrameIndex) -> str:
    """Local time of the last frame written (ISO 8601), the best guess of the end."""
    newest = 0.0
    for screen_id in index.screens:
        last = index.by_screen[screen_id][-1]
        try:
            written = index.image_of(last) or index.info_dir / last.filename
            newest = max(newest, written.stat().st_mtime)
        except OSError:
            continue
    when = datetime.fromtimestamp(newest) if newest else datetime.now()
    return when.astimezone().isoformat(timespec="milliseconds")


def _completed_manifest(paths: SessionPaths, index: FrameIndex) -> Manifest | None:
    """The manifest of the recording in the work folder, finished from what is on disk;
    ``None`` when there is too little to keep."""
    work = paths.work_dir
    manifest: Manifest | None = None
    manifest_path = paths.manifest_path(work)
    if manifest_path.is_file():
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning(
                "unreadable %s, deriving a manifest from the frames: %s", manifest_path, exc
            )
    if manifest is not None and not manifest.in_progress:
        return manifest  # saving got as far as the manifest; only the move is missing
    frame_count = sum(len(names) for names in index.by_screen.values())
    last_frame_ms = max(names[-1].time_ms for names in index.by_screen.values())
    audio = _audio_info(paths.audio_path(work), manifest.audio if manifest else None)
    duration_ms = max(last_frame_ms, audio.duration_ms)
    if duration_ms < MIN_RECOVERABLE_MS:
        return None
    if manifest is None:
        screens = _screens_from_frames(index)
        if not screens:
            return None
        first = index.by_screen[index.screens[0]][0]
        manifest = Manifest(
            session_id=paths.session_id,
            started_at="",
            finished_at=None,
            recorded_duration_ms=0,
            capture_interval_ms=_interval_from_frames(index),
            image_format="png" if first.filename.lower().endswith(".png") else "jpeg",
            jpeg_quality=None,
            screens=screens,
            plugin_version=__version__,
        )
        manifest.notes.append(
            "The screen layout is not known (no manifest was written): bounds are a guess."
        )
    manifest.finished_at = _finished_at(index)
    manifest.recorded_duration_ms = duration_ms
    manifest.audio = audio
    computer_path = paths.computer_audio_path(work)
    if computer_path.is_file():
        computer = _audio_info(computer_path, manifest.computer_audio)
        computer.file = COMPUTER_AUDIO_FILE
        manifest.computer_audio = computer
    else:
        manifest.computer_audio = None
    manifest.mouse = _mouse_info(paths.mouse_log_path(work), manifest.mouse)
    manifest.frames = FramesInfo(count=frame_count)
    manifest.stop_reason = RECOVERED_REASON
    manifest.notes.append(RECOVERED_NOTE)
    return manifest


def _note_in_state(paths: SessionPaths, final_dir: Path) -> None:
    """Tell a later ``status`` what happened; the session never becomes ``processed``."""
    try:
        state = read_state(paths.state_path)
    except RuntimeError:
        return
    if state.status is Status.PROCESSED:
        return
    if not state.status.final:
        state.status = Status.FAILED
        state.error_code = "WINDOW_EXITED"
    state.recording_dir = str(final_dir)
    state.message = (
        "The recording window ended before the recording was saved. The recording was "
        f"recovered to {final_dir}; nothing should be done with it unless the user asks. "
        "It can be opened with Edit Video Story."
    )
    try:
        write_state(paths.state_path, state)
    except OSError as exc:
        log.warning("cannot update %s: %s", paths.state_path, exc)


def recover_recording(paths: SessionPaths) -> bool:
    """Finish one left-behind recording; ``False`` when there is nothing to recover."""
    final = paths.final_dir
    if paths.manifest_path(final).is_file():
        return False  # saved already
    if not paths.work_dir.is_dir() or _owner_alive(paths) is not False:
        return False  # its window still runs, or it is not a window's session at all
    frames_dir = paths.frames_dir(paths.work_dir)
    half_moved = final.is_dir()  # saving stopped between two moves
    if not frames_dir.is_dir() and not half_moved:
        return False
    if frames_dir.is_dir():
        index = FrameIndex(frames_dir)
        if not index.screens:
            return False
        manifest = _completed_manifest(paths, index)
        if manifest is None:
            return False
        write_manifest(paths.manifest_path(paths.work_dir), manifest)
    elif not paths.manifest_path(paths.work_dir).is_file():
        return False  # frames moved, but no manifest anywhere: nothing to finish with
    final_dir = paths.finalize(resume=True)
    _note_in_state(paths, final_dir)
    log.info("recovered recording %s to %s", paths.session_id, final_dir)
    return True


def recover_orphaned_recordings(recording_folder: Path) -> list[str]:
    """Finish every recording a dead window left in ``.work``; returns their ids."""
    work = recording_folder / WORK_DIR_NAME
    if not work.is_dir():
        return []
    recovered: list[str] = []
    try:
        entries = sorted(work.iterdir())
    except OSError as exc:
        log.warning("cannot list %s: %s", work, exc)
        return []
    for entry in entries:
        if not entry.is_dir() or not is_session_id(entry.name):
            continue
        try:
            if recover_recording(SessionPaths(recording_folder, entry.name)):
                recovered.append(entry.name)
        except Exception:  # noqa: BLE001 - one broken folder must not stop the others
            log.exception("cannot recover recording %s", entry.name)
    return recovered


__all__ = [
    "MIN_RECOVERABLE_MS",
    "RECOVERED_NOTE",
    "RECOVERED_REASON",
    "recover_orphaned_recordings",
    "recover_recording",
]
