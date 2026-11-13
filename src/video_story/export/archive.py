"""Archiving a recording: one MP4 per screen instead of thousands of frames, and every
audio track as a small AAC file instead of a WAV.

First the audio (see :mod:`video_story.core.archive_state`): every track the recording
has - ``audio/audio.wav`` (microphone), ``audio/computer.wav`` (computer audio) - is
encoded to ``archive/audio.m4a`` / ``archive/computer.m4a``, whether it is switched on
in the editor or not, and ``audio/tracks.json`` (which tracks are switched on) is
copied to ``archive/tracks.json``. Each ``.m4a`` is written under a partial name and
gets its name only once its audio stream was read back and is as long as the WAV; then
the track is recorded in ``audio`` of ``archive.json``. A track that fails stops the
archiving with nothing deleted. Only a WAV without a single sample is left out.

Then for every screen, in order:

1. ``archive/<screenId>.mp4`` - the whole recording of that screen, rendered exactly
   like the editor's **Render** (cursor drawn as configured, the audio tracks that are
   switched on in the editor mixed into one; none when both are off), plus
   ``archive/<screenId>.srt`` with the subtitles of the whole recording;
2. the MP4 verified: its video stream must cover the whole recording (FFmpeg exits
   with 0 and full-length audio even when it cut the video at a frame it could not
   read); a video that fails stops the archiving with nothing deleted;
3. ``archive/<screenId>.jpg`` - the frame closest to the middle of the recording;
   then the screen is recorded in ``screens_done`` of ``archive.json``;
4. the frames of that screen deleted at once (they are not needed any more, and the
   next screen gets their disk space).

When every screen is done, ``audio/`` (only if every track in it has its verified
``.m4a``) and what is left of ``frames/`` are deleted and the status becomes
``archived``. ``frames_info/``, the transcript, the manifest, the crop and the
instructions stay. Deleting is best effort: a file another program keeps open stays
where it is (listed in ``leftovers``) and the archive still counts as done;
:func:`cleanup_leftovers` tries again later.

A run that stops half way (editor closed, FFmpeg failed) resumes with the first audio
track not in ``audio`` and the first screen not in ``screens_done``; their partial
outputs are removed first. That also holds for a run an older version left half way,
whose ``archive.json`` has no ``audio`` yet: its tracks are converted before anything
else happens.
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
    archived_audio_path,
    archived_tracks_path,
    read_archive_state,
    write_archive_state,
)
from video_story.core.audio_mix import existing_tracks, track_path, wav_info
from video_story.core.config import Config
from video_story.core.fileio import replace_with_retry
from video_story.core.manifest import read_manifest
from video_story.core.session import SessionPaths, now_iso
from video_story.core.transcript import transcript_status
from video_story.host.runner import FrameIndex

from . import CancelCheck, ExportError, ExportRequest, ExportResult, ProgressCallback
from .ffmpeg_audio import encode_audio, verify_audio, wav_duration_ms
from .ffmpeg_export import export_recording, partial_output, verify_video

log = logging.getLogger(__name__)

STILL_JPEG_QUALITY = 90
MIN_FREE_BYTES = 1024**3  # like recording: keep at least 1 GB free on the disk
DELETE_ATTEMPTS = 5
DELETE_DELAY_S = 0.2
# The first slice of the progress bar, taken by the audio tracks when there are any to
# convert (encoding audio is quick next to rendering a screen).
AUDIO_PROGRESS_SHARE = 0.02
DELETING_PROGRESS = 0.99  # where the bar stands while audio/ and frames/ are deleted

# progress(fraction 0..1 of the whole archive, text)
ArchiveProgress = Callable[[float, str], None]
Exporter = Callable[..., ExportResult]
# verifier(mp4, duration_ms, config, cancel): raises ExportError unless the video stream
# of the MP4 covers duration_ms (see video_story.export.ffmpeg_export.verify_video)
Verifier = Callable[[Path, int, Config, CancelCheck | None], object]
# audio_encoder(wav, output, config, progress, cancel): writes the AAC copy of one audio
# track to output (see video_story.export.ffmpeg_audio.encode_audio)
AudioEncoder = Callable[[Path, Path, Config, ProgressCallback | None, CancelCheck | None], object]
# audio_verifier(m4a, duration_ms, config, cancel): raises ExportError unless the audio
# stream of the file is duration_ms long (see video_story.export.ffmpeg_audio.verify_audio)
AudioVerifier = Callable[[Path, int, Config, CancelCheck | None], object]


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


def audio_output(paths: SessionPaths, track: str) -> Path:
    """The AAC copy of an audio track inside ``archive/``: ``audio.m4a`` for
    ``microphone``, ``computer.m4a`` for ``computer``."""
    base = paths.final_dir
    return archived_audio_path(paths, track_path(paths, base, track), base)


def _is_empty_track(wav: Path) -> bool:
    """A readable WAV without a single sample (the device never delivered one): there
    is nothing to keep, and FFmpeg would write a file without an audio stream."""
    info = wav_info(wav)
    return info is not None and info.frames == 0


def unarchived_tracks(paths: SessionPaths, state: ArchiveState) -> dict[str, Path]:
    """The audio tracks on disk (name -> WAV) that hold sound and have no verified
    ``.m4a`` in ``archive/``: ``audio/`` must not be deleted while there is one."""
    return {
        track: wav
        for track, wav in existing_tracks(paths, paths.final_dir).items()
        if (track not in state.audio or not audio_output(paths, track).is_file())
        and not _is_empty_track(wav)
    }


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


def _fail(paths: SessionPaths, state: ArchiveState, code: str, message: str) -> ArchiveError:
    """Record why the archiving stops in ``archive.json``; returns the error to raise."""
    state.error = message
    write_archive_state(paths, state, paths.final_dir)
    return ArchiveError(code, message)


def _copy_selection(paths: SessionPaths, state: ArchiveState) -> None:
    """``audio/tracks.json`` -> ``archive/tracks.json``, when the recording has one."""
    base = paths.final_dir
    source = paths.audio_tracks_path(base)
    if not source.is_file():
        return
    target = archived_tracks_path(paths, base)
    tmp = target.with_name(target.name + ".tmp")
    try:
        shutil.copyfile(source, tmp)
        replace_with_retry(tmp, target)
    except OSError as exc:
        _remove(tmp)
        raise _fail(
            paths, state, "ARCHIVE_FAILED", f"cannot copy {source.name} to the archive: {exc}"
        ) from exc


def _archive_audio(
    paths: SessionPaths,
    config: Config,
    state: ArchiveState,
    pending: dict[str, Path],
    report: ArchiveProgress,
    cancel: CancelCheck | None,
    encode: AudioEncoder,
    verify: AudioVerifier,
) -> None:
    """Encode the audio tracks ``pending`` (name -> WAV) to ``archive/<name>.m4a``.

    A track is recorded in ``state.audio`` once its ``.m4a`` was written under a
    partial name, read back by ``verify`` and found as long as the WAV; only then does
    the file get its name. Raises :class:`ArchiveError` at the first track that fails.
    """
    base = paths.final_dir
    if existing_tracks(paths, base):
        _copy_selection(paths, state)
    if not pending:
        return
    free = _free_bytes(paths)
    if free is not None and free < MIN_FREE_BYTES:
        raise _fail(
            paths, state, "LOW_DISK", "less than 1 GB free on the disk of the recording folder"
        )
    for number, (track, wav) in enumerate(pending.items()):
        if cancel is not None and cancel():
            raise ArchiveError("CANCELLED", "archiving was stopped")
        target = audio_output(paths, track)
        partial = partial_output(target)
        for stale in (target, partial):
            _remove(stale)
        if state.audio.pop(track, None) is not None:  # listed, but its file is gone
            write_archive_state(paths, state, base)
        done_before = number / len(pending)

        def on_encode(fraction: float, _text: str, start: float = done_before) -> None:
            report((start + fraction / len(pending)) * AUDIO_PROGRESS_SHARE, "archiving audio")

        report(done_before * AUDIO_PROGRESS_SHARE, "archiving audio")
        try:
            try:
                expected_ms = wav_duration_ms(wav)
                encode(wav, partial, config, on_encode, cancel)
            except ExportError as exc:
                raise _fail(paths, state, exc.code, str(exc)) from exc
            if not partial.is_file() or partial.stat().st_size == 0:
                raise _fail(paths, state, "ARCHIVE_FAILED", f"{target.name} was not written")
            try:
                verify(partial, expected_ms, config, cancel)
            except ExportError as exc:
                message = (
                    str(exc)
                    if exc.code == "CANCELLED"
                    else f"{target.name} is not a complete copy of {wav.name}, "
                    f"nothing was deleted: {exc}"
                )
                raise _fail(paths, state, exc.code, message) from exc
            try:
                replace_with_retry(partial, target)
            except OSError as exc:
                raise _fail(
                    paths, state, "ARCHIVE_FAILED", f"cannot write {target.name}: {exc}"
                ) from exc
        finally:
            _remove(partial)
        state.audio[track] = target.name
        write_archive_state(paths, state, base)
        log.info("archive %s: %s (%d ms) done", paths.session_id, target.name, expected_ms)


def archive_recording(
    paths: SessionPaths,
    config: Config,
    *,
    progress: ArchiveProgress | None = None,
    cancel: CancelCheck | None = None,
    exporter: Exporter = export_recording,
    verifier: Verifier | None = None,
    audio_encoder: AudioEncoder | None = None,
    audio_verifier: AudioVerifier | None = None,
) -> ArchiveState:
    """Archive the saved recording of ``paths`` (resuming a run that stopped).

    Raises :class:`ArchiveError` (``NOT_TRANSCRIBED``, ``NO_MANIFEST``, ``CANCELLED``,
    an export code ...) without losing what was done: ``archive.json`` keeps the audio
    tracks and the screens already archived and the next call continues with the others.

    No frame of a screen is deleted before ``verifier`` accepted the screen's MP4
    (:func:`video_story.export.ffmpeg_export.verify_video` unless given): the exporter
    checks its output too, this second look at the final file is what the deletion
    relies on, whatever exporter was used.

    Every audio track is encoded by ``audio_encoder`` and accepted by ``audio_verifier``
    (:func:`video_story.export.ffmpeg_audio.encode_audio` and ``verify_audio`` unless
    given) before the first screen is rendered; ``audio/`` is deleted at the end only
    when no track in it lacks its ``.m4a``.
    """
    verify = verifier if verifier is not None else verify_video
    encode_track = audio_encoder if audio_encoder is not None else encode_audio
    verify_track = audio_verifier if audio_verifier is not None else verify_audio
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

    # The audio first: every track, whatever is switched on in the editor (the MP4s
    # below hold only the tracks that are on, and audio/ is deleted at the end).
    pending = unarchived_tracks(paths, state)
    _archive_audio(paths, config, state, pending, report, cancel, encode_track, verify_track)
    # The screens share the progress between the audio (if any) and the final deleting.
    first = AUDIO_PROGRESS_SHARE if pending else 0.0

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
                screens = start + fraction / total * 0.98
                report(first + (DELETING_PROGRESS - first) * screens, f"archiving {sid}")

            report(first + (DELETING_PROGRESS - first) * done_before, f"archiving {screen_id}")
            try:
                result = exporter(
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
            # The exporter knows the length it rendered (it falls back to the frame
            # times when the manifest has no duration); a stand-in may not say.
            expected_ms = result.duration_ms if isinstance(result, ExportResult) else duration_ms
            try:
                verify(mp4, expected_ms, config, cancel)
            except ExportError as exc:
                state.error = (
                    str(exc)
                    if exc.code == "CANCELLED"
                    else f"{mp4.name} is not a complete video, nothing was deleted: {exc}"
                )
                write_archive_state(paths, state, base)
                raise ArchiveError(exc.code, state.error) from exc
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

    # audio/ goes only when every track in it has its verified .m4a. They were all
    # converted above; this second look is what the deletion relies on.
    unarchived = unarchived_tracks(paths, state)
    if unarchived:
        names = ", ".join(wav.name for wav in unarchived.values())
        raise _fail(
            paths,
            state,
            "ARCHIVE_FAILED",
            f"no archived copy of {names} in {paths.archive_dir(base)}; audio/ was not deleted",
        )
    report(DELETING_PROGRESS, "deleting frames and audio")
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
    # A track without its .m4a (an archive of an older version that could not delete
    # the WAV then) is the only copy of that track: it stays.
    unarchived = unarchived_tracks(paths, state)
    if unarchived:
        left.extend(str(wav) for wav in unarchived.values())
    else:
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
    "AUDIO_PROGRESS_SHARE",
    "MIN_FREE_BYTES",
    "ArchiveError",
    "archive_recording",
    "audio_output",
    "cleanup_leftovers",
    "remove_tree",
    "screen_outputs",
    "unarchived_tracks",
]
