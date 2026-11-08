"""A processed recording opened in the editor: manifest, frames, transcript, audio."""

from __future__ import annotations

import bisect
import json
import logging
import wave
from dataclasses import dataclass, replace
from pathlib import Path

from video_story.core.cursor import CursorPosition, cursor_from_json
from video_story.core.manifest import Manifest, read_manifest
from video_story.core.screens import Screen
from video_story.core.session import SessionPaths
from video_story.core.transcript import Segment, Transcript, read_transcript, write_transcript
from video_story.host.runner import FrameIndex, FrameRef

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFile:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_ms(self) -> int:
        return int(self.frames * 1000 / self.sample_rate) if self.sample_rate else 0


def read_audio_info(path: Path) -> AudioFile | None:
    """Header of a WAV file (never reads the samples)."""
    try:
        with wave.open(str(path), "rb") as wf:
            return AudioFile(
                path=path,
                sample_rate=wf.getframerate(),
                channels=wf.getnchannels(),
                sample_width=wf.getsampwidth(),
                frames=wf.getnframes(),
            )
    except (OSError, wave.Error, EOFError) as exc:
        log.warning("cannot read audio %s: %s", path, exc)
        return None


def _load_frame_index(frames_dir: Path) -> FrameIndex:
    if frames_dir.is_dir():
        return FrameIndex(frames_dir)
    index = FrameIndex.__new__(FrameIndex)  # a recording without frames: empty index
    index.frames_dir = frames_dir
    index.by_screen = {}
    return index


class LoadedRecording:
    """Everything the editor needs about one recording, loaded once (in a worker)."""

    def __init__(
        self,
        paths: SessionPaths,
        manifest: Manifest,
        frames: FrameIndex,
        transcript: Transcript | None,
        audio: AudioFile | None,
    ) -> None:
        self.paths = paths
        self.manifest = manifest
        self.frames = frames
        self.transcript = transcript
        self.audio = audio
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
        frames = _load_frame_index(paths.frames_dir(base))
        transcript: Transcript | None = None
        transcript_path = paths.transcript_json_path(base)
        if transcript_path.exists():
            try:
                transcript = read_transcript(transcript_path)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("cannot read transcript %s: %s", transcript_path, exc)
        audio_path = base / manifest.audio.file
        audio = read_audio_info(audio_path) if audio_path.exists() else None
        return cls(paths, manifest, frames, transcript, audio)

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

    def cursor_at(self, screen_id: str, time_ms: int) -> CursorPosition | None:
        """Cursor from the sidecar of the frame valid at ``time_ms`` (any screen)."""
        ref = self.frame_at(screen_id, time_ms)
        if ref is None:
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
        updated = replace(transcript, segments=segments)
        base = self.paths.final_dir
        write_transcript(
            self.paths.transcript_txt_path(base), self.paths.transcript_json_path(base), updated
        )
        self.transcript = updated
        self._segments = list(segments)
        self._segment_starts = [s.start_ms for s in self._segments]


__all__ = ["AudioFile", "LoadedRecording", "read_audio_info"]
