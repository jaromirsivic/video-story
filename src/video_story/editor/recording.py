"""A processed recording opened in the editor: manifest, frames, transcript, audio.

An archived recording (see :mod:`video_story.export.archive`) has no frames and no
audio any more: every screen shows its still ``archive/<screenId>.jpg`` for the whole
length, there is no sound and no cursor; the transcript is still there.

The audio of a recording is the mix of its tracks that are switched on (the microphone
and the computer's sound, :mod:`video_story.core.audio_mix`); :class:`AudioFile`
describes that mix and opens a reader for it.
"""

from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from video_story.core.archive_state import ArchiveState, read_archive_state
from video_story.core.audio_mix import (
    MICROPHONE,
    MixReader,
    TrackSelection,
    existing_tracks,
    read_selection,
    wav_info,
    write_selection,
)
from video_story.core.cursor import CursorPosition, cursor_from_json
from video_story.core.input_events import MouseLog, read_mouse_log
from video_story.core.manifest import Manifest, TranscriptInfo, read_manifest, write_manifest
from video_story.core.naming import FrameName
from video_story.core.screens import Screen
from video_story.core.session import SessionPaths, now_iso
from video_story.core.transcript import (
    Segment,
    Transcript,
    drop_credit_hallucinations,
    merge_segments,
    normalize_segments,
    read_transcript,
    write_transcript,
)
from video_story.host.runner import FrameIndex, FrameRef

from .mouse_activity import MouseActivity
from .mouse_details import MouseDetails, load_mouse_details

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFile:
    """The audio the editor plays and draws: the sum of ``sources`` (the tracks that
    are switched on; none: silence), as long as the longest of ``tracks`` (every track
    on disk), at ``sample_rate`` (the microphone's)."""

    sources: tuple[Path, ...]
    tracks: tuple[Path, ...]
    sample_rate: int
    frames: int

    @property
    def duration_ms(self) -> int:
        return int(self.frames * 1000 / self.sample_rate) if self.sample_rate else 0

    @property
    def key(self) -> str:
        """Identifies the mix (which tracks are heard) for caches of derived data."""
        return "|".join(str(p) for p in self.sources) + "#" + "|".join(str(p) for p in self.tracks)

    def open(self) -> MixReader:
        return MixReader(self.sources, length_from=self.tracks, sample_rate=self.sample_rate)


def audio_mix(tracks: dict[str, Path], selection: TrackSelection) -> AudioFile | None:
    """The :class:`AudioFile` of ``tracks`` (name -> WAV) with ``selection`` switched on;
    ``None`` without any readable track."""
    infos = {name: info for name, path in tracks.items() if (info := wav_info(path)) is not None}
    infos = {name: info for name, info in infos.items() if info.sample_rate > 0}
    if not infos:
        return None
    reference = infos.get(MICROPHONE) or next(iter(infos.values()))
    rate = reference.sample_rate
    frames = max(int(i.frames * rate / i.sample_rate) for i in infos.values())
    sources = tuple(info.path for name, info in infos.items() if selection.enabled(name))
    return AudioFile(sources, tuple(i.path for i in infos.values()), rate, frames)


class StillFrames(FrameIndex):
    """The frames of an archived recording: one still image per screen
    (``archive/<screenId>.jpg``), valid from 0 ms to the end."""

    def __init__(self, archive_dir: Path, info_dir: Path, screen_ids: list[str]) -> None:
        self.frames_dir = archive_dir
        self.info_dir = info_dir
        self.by_screen = {
            sid: [FrameName(0, sid, "jpg")]
            for sid in screen_ids
            if (archive_dir / f"{sid}.jpg").is_file()
        }

    def image_of(self, name: FrameName) -> Path | None:
        return self.frames_dir / f"{name.screen_id}.jpg"


def _load_frame_index(paths: SessionPaths, base: Path) -> FrameIndex:
    return FrameIndex.of(paths, base)


def _load_mouse(paths: SessionPaths, base: Path) -> MouseLog | None:
    """``input/mouse.jsonl``; ``None`` when the mouse was not recorded (or unreadable)."""
    path = paths.mouse_log_path(base)
    if not path.is_file():
        return None
    try:
        return read_mouse_log(path)
    except OSError as exc:
        log.warning("cannot read mouse log %s: %s", path, exc)
        return None


class LoadedRecording:
    """Everything the editor needs about one recording, loaded once (in a worker)."""

    def __init__(
        self,
        paths: SessionPaths,
        manifest: Manifest,
        frames: FrameIndex,
        transcript: Transcript | None,
        audio: AudioFile | None,
        archive: ArchiveState | None = None,
    ) -> None:
        self.paths = paths
        self.archive = archive
        self.manifest = manifest
        self.frames = frames
        self.transcript = transcript
        self.audio = audio
        # The audio tracks on disk (microphone, computer) and which are switched on.
        self.tracks: dict[str, Path] = {}
        self.selection = TrackSelection()
        # Buttons and wheels in use over time (``input/mouse.jsonl``); None: not recorded.
        self.mouse: MouseActivity | None = None
        self.mouse_log: MouseLog | None = None
        # Built on demand for the Mouse window (reads every sidecar): mouse_details().
        self._mouse_details: MouseDetails | None = None
        self._times: dict[str, list[int]] = {
            sid: [n.time_ms for n in names] for sid, names in frames.by_screen.items()
        }
        last_frame_ms = max((t[-1] for t in self._times.values() if t), default=0)
        self.duration_ms = manifest.recorded_duration_ms or last_frame_ms
        self._cursor_cache: tuple[Path, CursorPosition | None] | None = None
        self._segments = sorted(transcript.segments, key=lambda s: s.start_ms) if transcript else []
        self._segment_starts = [s.start_ms for s in self._segments]

    @classmethod
    def load(cls, paths: SessionPaths) -> LoadedRecording:
        base = paths.final_dir
        manifest = read_manifest(paths.manifest_path(base))
        archive = read_archive_state(paths, base)
        transcript: Transcript | None = None
        transcript_path = paths.transcript_json_path(base)
        if transcript_path.exists():
            try:
                transcript = read_transcript(transcript_path)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("cannot read transcript %s: %s", transcript_path, exc)
        mouse_log = _load_mouse(paths, base)
        if archive is not None and archive.archived:
            screen_ids = [s.id for s in manifest.screens] or archive.screens
            frames: FrameIndex = StillFrames(
                paths.archive_dir(base), paths.frames_info_dir(base), screen_ids
            )
            archived = cls(paths, manifest, frames, transcript, None, archive)
            archived.set_mouse_log(mouse_log)  # input/ is kept by the archiving
            return archived
        frames = _load_frame_index(paths, base)
        recording = cls(paths, manifest, frames, transcript, None, archive)
        recording.set_mouse_log(mouse_log)
        recording.tracks = existing_tracks(paths, base)
        recording.selection = read_selection(paths, base)
        recording.audio = audio_mix(recording.tracks, recording.selection)
        return recording

    @property
    def archived(self) -> bool:
        """Archived: stills instead of frames, no audio; only subtitles can change."""
        return self.archive is not None and self.archive.archived

    # Audio tracks -----------------------------------------------------------

    def has_track(self, track: str) -> bool:
        return track in self.tracks

    def set_selection(self, selection: TrackSelection) -> None:
        """Switch audio tracks on or off: written to ``audio/tracks.json`` first, then
        :attr:`audio` describes the new mix (``OSError`` when it cannot be written)."""
        write_selection(self.paths, self.paths.final_dir, selection)
        self.selection = selection
        self.audio = audio_mix(self.tracks, selection)

    # Screens ----------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self.paths.session_id

    @property
    def screens(self) -> list[Screen]:
        """Screens in manifest order, followed by any screen that only has frames."""
        known = {s.id for s in self.manifest.screens}
        extra = [
            Screen(sid, int(sid.removeprefix("screen")), 0, 0, 0, 0, False, 1.0)
            for sid in self.frames.screens
            if sid not in known
        ]
        return [*self.manifest.screens, *extra]

    @property
    def screen_ids(self) -> list[str]:
        return [s.id for s in self.screens]

    def screen(self, screen_id: str) -> Screen | None:
        for screen in self.screens:
            if screen.id == screen_id:
                return screen
        return None

    def screen_size(self, screen_id: str) -> tuple[int, int]:
        screen = self.screen(screen_id)
        if screen is None or screen.image_width <= 0:
            return (0, 0)
        return (screen.image_width, screen.image_height)

    @property
    def keypoints(self) -> list[int]:
        """Moments marked with Keypoint while recording (ms, inside the recording)."""
        return [k for k in self.manifest.keypoints if 0 <= k <= self.duration_ms]

    # Frames -----------------------------------------------------------------

    def frame_times(self, screen_id: str) -> list[int]:
        return self._times.get(screen_id, [])

    def frame_at(self, screen_id: str, time_ms: int) -> FrameRef | None:
        """Latest frame at or before ``time_ms`` (the first one before it); O(log n)."""
        times = self._times.get(screen_id)
        if not times:
            return None
        index = max(0, bisect.bisect_right(times, time_ms) - 1)
        return self.frames._ref(self.frames.by_screen[screen_id][index])

    def next_frame_after(self, screen_id: str, time_ms: int) -> FrameRef | None:
        """The first frame strictly after ``time_ms`` (for decoding ahead)."""
        times = self._times.get(screen_id)
        if not times:
            return None
        index = bisect.bisect_right(times, time_ms)
        if index >= len(times):
            return None
        return self.frames._ref(self.frames.by_screen[screen_id][index])

    # Mouse ------------------------------------------------------------------

    def set_mouse_log(self, mouse_log: MouseLog | None) -> None:
        self.mouse_log = mouse_log
        self.mouse = MouseActivity.from_log(mouse_log) if mouse_log is not None else None
        self._mouse_details = None

    @property
    def mouse_details_ready(self) -> MouseDetails | None:
        return self._mouse_details

    def mouse_details(self) -> MouseDetails:
        """Mouse and clipboard by time for the Mouse window; the first call reads every
        frame sidecar (call it from a worker), later calls return the same object."""
        if self._mouse_details is None:
            base = self.paths.final_dir
            mouse = self.manifest.mouse
            self._mouse_details = load_mouse_details(
                self.mouse_log,
                mouse.params if mouse is not None else None,
                self.duration_ms,
                self.manifest.screens,
                self.paths.frames_dir(base),
                self.paths.frames_info_dir(base),
            )
        return self._mouse_details

    def cursor_at(self, screen_id: str, time_ms: int) -> CursorPosition | None:
        """Cursor from the sidecar of the frame valid at ``time_ms`` (any screen); none
        for an archived recording (its still image shows no moment in particular)."""
        ref = self.frame_at(screen_id, time_ms)
        if ref is None or self.archived:
            return None
        if self._cursor_cache is not None and self._cursor_cache[0] == ref.sidecar:
            return self._cursor_cache[1]
        cursor: CursorPosition | None = None
        try:
            data = json.loads(ref.sidecar.read_text(encoding="utf-8"))
            cursor = cursor_from_json(data.get("cursor"))
        except (OSError, ValueError, KeyError, AttributeError, TypeError):
            cursor = None
        self._cursor_cache = (ref.sidecar, cursor)
        return cursor

    # Transcript -------------------------------------------------------------

    @property
    def transcript_complete(self) -> bool:
        """False while the recording still needs (more) transcription."""
        return self.transcript is not None and self.transcript.complete

    @property
    def transcribed_ms(self) -> int:
        """How far into the recording the stored segments reach (the whole when complete)."""
        transcript = self.transcript
        if transcript is None:
            return 0
        if transcript.complete:
            return self.duration_ms
        if transcript.transcribed_ms is not None:
            return transcript.transcribed_ms
        return max((s.end_ms for s in transcript.segments), default=0)

    def _store_transcript(self, updated: Transcript) -> None:
        """Write ``transcript.txt``/``.json`` and adopt ``updated`` (files first)."""
        base = self.paths.final_dir
        write_transcript(
            self.paths.transcript_txt_path(base), self.paths.transcript_json_path(base), updated
        )
        self.transcript = updated
        self._segments = sorted(updated.segments, key=lambda s: s.start_ms)
        self._segment_starts = [s.start_ms for s in self._segments]

    def add_segments(self, segments: list[Segment], transcribed_ms: int) -> list[Segment]:
        """Segments the background transcription produced: stored as an incomplete
        transcript (``complete`` false, ``transcribed_ms`` = how far whisper got) and
        written, so that an interrupted transcription resumes there. Returns the
        segments that were new."""
        updated, new = merge_segments(self.transcript, segments, transcribed_ms)
        self._store_transcript(updated)
        return new

    def reset_transcript(self) -> None:
        """Throw the narration away before it is made again from the current audio: an
        empty, incomplete transcript is written (the transcription starts at 0 ms)."""
        self._store_transcript(
            Transcript(language=None, segments=[], complete=False, transcribed_ms=0)
        )

    def finish_transcript(
        self, language: str | None, model: str | None, backend: str | None
    ) -> None:
        """The background transcription reached the end: mark the transcript complete,
        write it and record it in ``manifest.json``."""
        transcript = self.transcript or Transcript(language=None, segments=[])
        updated = replace(
            transcript,
            language=language or transcript.language,
            model=model or transcript.model,
            backend=backend or transcript.backend,
            segments=drop_credit_hallucinations(normalize_segments(transcript.segments)),
            complete=True,
            transcribed_ms=None,
        )
        self._store_transcript(updated)
        manifest_path = self.paths.manifest_path(self.paths.final_dir)
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, ValueError, KeyError) as exc:
            log.warning("cannot update manifest %s: %s", manifest_path, exc)
            return
        manifest.transcript = TranscriptInfo(
            language=updated.language,
            model=updated.model,
            backend=updated.backend,
            segments=len(updated.segments),
        )
        manifest.finished_at = manifest.finished_at or now_iso()
        try:
            write_manifest(manifest_path, manifest)
        except OSError as exc:
            log.warning("cannot write manifest %s: %s", manifest_path, exc)
            return
        self.manifest = manifest

    @property
    def segments(self) -> list[Segment]:
        """The transcript segments sorted by start (the narration track's boxes)."""
        return list(self._segments)

    def segments_between(self, start_ms: float, end_ms: float) -> list[Segment]:
        """Segments that overlap ``[start_ms, end_ms]`` (the visible part of the timeline)."""
        index = max(0, bisect.bisect_right(self._segment_starts, start_ms) - 1)
        found: list[Segment] = []
        for segment in self._segments[index:]:
            if segment.start_ms > end_ms:
                break
            if segment.end_ms >= start_ms:
                found.append(segment)
        return found

    def segment_at(self, time_ms: int) -> Segment | None:
        """The single segment containing ``time_ms`` (the one shown as the subtitle).

        Segments are half-open (``start <= t < end``) so a playhead sitting exactly on
        a touching boundary shows only the segment that starts there; the end is
        inclusive only for the last segment.
        """
        index = bisect.bisect_right(self._segment_starts, time_ms) - 1
        if index < 0:
            return None
        segment = self._segments[index]
        last = index == len(self._segments) - 1
        if time_ms < segment.end_ms or (last and time_ms == segment.end_ms):
            return segment
        return None

    def subtitle_at(self, time_ms: int) -> str:
        """Text of the segment containing ``time_ms`` (see :meth:`segment_at`)."""
        segment = self.segment_at(time_ms)
        return segment.text.strip() if segment is not None else ""

    def replace_segment(self, old: Segment, new: Segment) -> None:
        """Replace ``old`` by ``new`` and rewrite ``transcript.json`` and ``transcript.txt``.

        This changes what the agent reads as "what was said". The files are written
        first; the recording in memory changes only when that succeeded (``OSError``
        otherwise).
        """
        transcript = self.transcript
        if transcript is None or old not in transcript.segments:
            raise ValueError("the segment is not part of this recording's transcript")
        segments = [new if s == old else s for s in transcript.segments]
        segments.sort(key=lambda s: (s.start_ms, s.end_ms))
        self._store_transcript(replace(transcript, segments=segments))


__all__ = ["AudioFile", "LoadedRecording", "audio_mix"]
