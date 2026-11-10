"""Archiving a recording: one MP4 per screen instead of thousands of frames.

For every screen, in order (see :mod:`video_story.core.archive_state`):

1. ``archive/<screenId>.mp4`` - the whole recording of that screen with the full audio
   track, rendered exactly like the editor's **Render** (cursor drawn as configured),
   plus ``archive/<screenId>.srt`` with the subtitles of the whole recording;
2. ``archive/<screenId>.jpg`` - the frame closest to the middle of the recording;
3. both checked, the screen recorded in ``screens_done`` of ``archive.json``;
4. the frames of that screen deleted at once (they are not needed any more, and the
   next screen gets their disk space).

When every screen is done, ``audio/`` and what is left of ``frames/`` are deleted and
the status becomes ``archived``. ``frames_info/``, the transcript, the manifest, the
crop and the instructions stay. Deleting is best effort: a file another program keeps
open stays where it is (listed in ``leftovers``) and the archive still counts as done;
:func:`cleanup_leftovers` tries again later.

A run that stops half way (editor closed, FFmpeg failed) resumes with the first screen
not in ``screens_done``; its partial outputs are removed first.
"""

from __future__ import annotations

import bisect
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from video_story.core.archive_state import (
    ArchiveState,
    ArchiveStatus,
    read_archive_state,
    write_archive_state,
)
from video_story.core.config import Config
from video_story.core.manifest import read_manifest
from video_story.core.session import SessionPaths, now_iso
from video_story.core.transcript import transcript_status
from video_story.host.runner import FrameIndex

from . import CancelCheck, ExportError, ExportRequest, ExportResult
from .ffmpeg_export import export_recording

log = logging.getLogger(__name__)

STILL_JPEG_QUALITY = 90
MIN_FREE_BYTES = 1024**3  # like recording: keep at least 1 GB free on the disk
DELETE_ATTEMPTS = 5
DELETE_DELAY_S = 0.2

# progress(fraction 0..1 of the whole archive, text)
ArchiveProgress = Callable[[float, str], None]
Exporter = Callable[..., ExportResult]


class ArchiveError(RuntimeError):
    """Archiving stopped; ``code`` is a short machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def screen_outputs(paths: SessionPaths, screen_id: str) -> tuple[Path, Path, Path]:
    """``(<screen>.mp4, <screen>.jpg, <screen>.srt)`` inside ``archive/``."""
    folder = paths.archive_dir(paths.final_dir)
    return (
        folder / f"{screen_id}.mp4",
        folder / f"{screen_id}.jpg",
        folder / f"{screen_id}.srt",
    )


def _screens_to_archive(paths: SessionPaths, index: FrameIndex) -> list[str]:
    manifest_path = paths.manifest_path(paths.final_dir)
    known: list[str] = []
    try:
        known = [s.id for s in read_manifest(manifest_path).screens]
    except (OSError, ValueError, KeyError):
        pass
    extra = [s for s in index.screens if s not in known]
    return [*known, *extra]


def _remove(path: Path) -> bool:
    """Delete one file, retrying while another program holds it; ``False`` on failure."""
    for attempt in range(DELETE_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt == DELETE_ATTEMPTS - 1:
                break
            time.sleep(DELETE_DELAY_S)
        except OSError:
            break
    log.warning("cannot delete %s (in use?); it stays", path)
    return False


def remove_tree(folder: Path) -> list[str]:
    """Delete ``folder`` with everything inside; returns what could not be deleted.

    Files are deleted one by one, so a single locked file leaves only itself (and its
    folders) behind instead of stopping the whole deletion.
    """
    if not folder.exists():
        return []
    left: list[str] = []
    for root, dirs, files in os.walk(folder, topdown=False):
        root_path = Path(root)
        for name in files:
            path = root_path / name
            if not _remove(path):
                left.append(str(path))
        for name in dirs:
            try:
                (root_path / name).rmdir()
            except OSError:
                pass  # not empty: a locked file inside is already listed
    try:
        folder.rmdir()
    except OSError:
        if not left:
            left.append(str(folder))
    return left


def _write_still(index: FrameIndex, screen_id: str, duration_ms: int, target: Path) -> None:
    """The frame closest to the middle of the recording as a JPEG."""
    from PIL import Image

    names = [n for n in index.by_screen.get(screen_id, []) if not n.is_sidecar]
    if not names:
        raise ArchiveError("NO_FRAMES", f"no frames of {screen_id} left for the still image")
    times = [n.time_ms for n in names]
    middle = max(0, duration_ms // 2)
    position = bisect.bisect_left(times, middle)
    candidates = [p for p in (position - 1, position) if 0 <= p < len(names)]
    chosen = min(candidates, key=lambda p: abs(times[p] - middle))
    source = index.frames_dir / names[chosen].filename
    tmp = target.with_name(target.name + ".tmp")
    with Image.open(source) as image:
        image.convert("RGB").save(tmp, "JPEG", quality=STILL_JPEG_QUALITY)
    os.replace(tmp, target)


def _delete_screen_frames(index: FrameIndex, screen_id: str) -> list[str]:
    left: list[str] = []
    for name in index.by_screen.get(screen_id, []):
        if name.is_sidecar:
            continue
        path = index.frames_dir / name.filename
        if not _remove(path):
            left.append(str(path))
    return left


def _duration_ms(paths: SessionPaths, index: FrameIndex) -> int:
    try:
        duration = read_manifest(paths.manifest_path(paths.final_dir)).recorded_duration_ms
    except (OSError, ValueError, KeyError):
        duration = 0
    if duration <= 0:
        duration = max((n[-1].time_ms for n in index.by_screen.values() if n), default=0)
    return duration


def archive_recording(
    paths: SessionPaths,
    config: Config,
    *,
    progress: ArchiveProgress | None = None,
    cancel: CancelCheck | None = None,
    exporter: Exporter = export_recording,
) -> ArchiveState:
    """Archive the saved recording of ``paths`` (resuming a run that stopped).

    Raises :class:`ArchiveError` (``NOT_TRANSCRIBED``, ``NO_MANIFEST``, ``CANCELLED``,
    an export code ...) without losing what was done: ``archive.json`` keeps the
    screens already archived and the next call continues with the others.
    """
    base = paths.final_dir
    if not paths.manifest_path(base).is_file():
        raise ArchiveError("NO_MANIFEST", f"no manifest.json in {base}")
    state = read_archive_state(paths, base) or ArchiveState()
    if state.archived:
        if state.leftovers:  # files that were locked last time: one more try
            cleanup_leftovers(paths)
            state = read_archive_state(paths, base) or state
        if progress is not None:
            progress(1.0, "archived")
        return state
    if not transcript_status(paths.transcript_json_path(base)).complete:
        raise ArchiveError(
            "NOT_TRANSCRIBED", "the transcript is not complete; transcribe the recording first"
        )

    def report(fraction: float, text: str) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, fraction)), text)

    index = FrameIndex.of(paths, base)
    if not state.screens:
        state.screens = _screens_to_archive(paths, index)
    state.status = ArchiveStatus.ARCHIVING
    state.started_at = state.started_at or now_iso()
    state.error = None
    write_archive_state(paths, state, base)
    duration_ms = _duration_ms(paths, index)
    total = max(1, len(state.screens))
    paths.archive_dir(base).mkdir(parents=True, exist_ok=True)

    for number, screen_id in enumerate(state.screens):
        if screen_id in state.screens_done:
            continue
        if cancel is not None and cancel():
            raise ArchiveError("CANCELLED", "archiving was stopped")
        mp4, jpg, srt = screen_outputs(paths, screen_id)
        has_images = any(not n.is_sidecar for n in index.by_screen.get(screen_id, []))
        if has_images:
            free = _free_bytes(paths)
            if free is not None and free < MIN_FREE_BYTES:
                state.error = "less than 1 GB free on the disk of the recording folder"
                write_archive_state(paths, state, base)
                raise ArchiveError("LOW_DISK", state.error)
            for stale in (mp4, jpg, srt, mp4.with_name(f"{mp4.stem}.part.mp4")):
                _remove(stale)
            done_before = number / total

            def on_export(
                fraction: float, _text: str, start: float = done_before, sid: str = screen_id
            ) -> None:
                report(start + fraction / total * 0.98, f"archiving {sid}")

            report(done_before, f"archiving {screen_id}")
            try:
                exporter(
                    ExportRequest(
                        paths=paths,
                        output=mp4,
                        screen_id=screen_id,
                        start_ms=0,
                        end_ms=None,
                        draw_cursor=config.editor_draw_cursor,
                        write_srt=True,
                    ),
                    config,
                    on_export,
                    cancel,
                )
            except ExportError as exc:
                state.error = str(exc)
                write_archive_state(paths, state, base)
                raise ArchiveError(exc.code, str(exc)) from exc
            if not mp4.is_file() or mp4.stat().st_size == 0:
                state.error = f"{mp4.name} was not written"
                write_archive_state(paths, state, base)
                raise ArchiveError("ARCHIVE_FAILED", state.error)
            try:
                _write_still(index, screen_id, duration_ms, jpg)
            except (OSError, ValueError) as exc:
                state.error = f"cannot write {jpg.name}: {exc}"
                write_archive_state(paths, state, base)
                raise ArchiveError("ARCHIVE_FAILED", state.error) from exc
        else:
            log.info("archive %s: %s has no frames, nothing to render", paths.session_id, screen_id)
        state.screens_done.append(screen_id)
        write_archive_state(paths, state, base)
        state.leftovers.extend(_delete_screen_frames(index, screen_id))
        write_archive_state(paths, state, base)
        log.info("archive %s: %s done", paths.session_id, screen_id)

    report(0.99, "deleting frames and audio")
    state.leftovers.extend(remove_tree(paths.audio_dir(base)))
    state.leftovers.extend(remove_tree(paths.frames_dir(base)))
    state.leftovers = sorted(set(state.leftovers))
    state.leftovers = [p for p in state.leftovers if Path(p).exists()]
    state.status = ArchiveStatus.ARCHIVED
    state.finished_at = now_iso()
    write_archive_state(paths, state, base)
    report(1.0, "archived")
    log.info(
        "archived %s (%d screen(s), %d leftover(s))",
        paths.session_id,
        len(state.screens),
        len(state.leftovers),
    )
    return state


def cleanup_leftovers(paths: SessionPaths) -> list[str]:
    """Try once more to delete what an archived recording could not delete; returns
    what is still there (written back to ``archive.json``)."""
    base = paths.final_dir
    state = read_archive_state(paths, base)
    if state is None or not state.archived or not state.leftovers:
        return []
    left: list[str] = []
    left.extend(remove_tree(paths.audio_dir(base)))
    left.extend(remove_tree(paths.frames_dir(base)))
    state.leftovers = sorted({p for p in left if Path(p).exists()})
    try:
        write_archive_state(paths, state, base)
    except OSError as exc:
        log.warning("cannot update the archive state of %s: %s", paths.session_id, exc)
    return state.leftovers


def _free_bytes(paths: SessionPaths) -> int | None:
    """Free bytes on the recording folder's disk (``None`` when unknown)."""
    try:
        return shutil.disk_usage(paths.recording_folder).free
    except OSError:
        return None


__all__ = [
    "MIN_FREE_BYTES",
    "ArchiveError",
    "archive_recording",
    "cleanup_leftovers",
    "remove_tree",
    "screen_outputs",
]
