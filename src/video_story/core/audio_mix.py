"""The audio tracks of a recording and the mix of the tracks that are switched on.

A recording has up to two audio tracks in ``audio/``:

* ``microphone`` - ``audio.wav``, what the user said;
* ``computer`` - ``computer.wav``, what this computer played meanwhile (a video in the
  background, a call); recorded on Windows only (WASAPI loopback), and only when it
  could be opened.

Both start at 0 ms of the recording timeline. ``audio/tracks.json`` says which of them
are switched on in the editor (both by default); the editor plays, shows, transcribes
and renders the sum of the tracks that are on (:class:`MixReader`), and nothing at all
(silence) when both are off.
"""

from __future__ import annotations

import json
import logging
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import numpy as np

from .fileio import atomic_write_text, read_json_with_retry
from .session import SessionPaths

log = logging.getLogger(__name__)

MICROPHONE = "microphone"
COMPUTER = "computer"
TRACK_NAMES = (MICROPHONE, COMPUTER)
TRACK_LABELS = {MICROPHONE: "Microphone", COMPUTER: "Computer audio"}


@dataclass(frozen=True)
class TrackSelection:
    """Which audio tracks are switched on (both by default)."""

    microphone: bool = True
    computer: bool = True

    def enabled(self, track: str) -> bool:
        return bool(getattr(self, track))

    def toggled(self, track: str) -> TrackSelection:
        if track not in TRACK_NAMES:
            raise ValueError(f"unknown audio track {track!r}")
        values = {name: self.enabled(name) for name in TRACK_NAMES}
        values[track] = not values[track]
        return TrackSelection(**values)

    def to_dict(self) -> dict[str, bool]:
        return {name: self.enabled(name) for name in TRACK_NAMES}


def track_path(paths: SessionPaths, base: Path, track: str) -> Path:
    if track == MICROPHONE:
        return paths.audio_path(base)
    if track == COMPUTER:
        return paths.computer_audio_path(base)
    raise ValueError(f"unknown audio track {track!r}")


def existing_tracks(paths: SessionPaths, base: Path) -> dict[str, Path]:
    """The tracks this recording has on disk, by name (microphone first)."""
    found: dict[str, Path] = {}
    for track in TRACK_NAMES:
        path = track_path(paths, base, track)
        if path.is_file():
            found[track] = path
    return found


def read_selection(paths: SessionPaths, base: Path) -> TrackSelection:
    """``audio/tracks.json``; both tracks on when it is missing or unreadable."""
    path = paths.audio_tracks_path(base)
    if not path.is_file():
        return TrackSelection()
    try:
        data = read_json_with_retry(path)
    except (OSError, ValueError) as exc:
        log.warning("cannot read %s, both audio tracks are on: %s", path, exc)
        return TrackSelection()
    if not isinstance(data, dict):
        return TrackSelection()
    return TrackSelection(**{name: bool(data.get(name, True)) for name in TRACK_NAMES})


def write_selection(paths: SessionPaths, base: Path, selection: TrackSelection) -> None:
    path = paths.audio_tracks_path(base)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(selection.to_dict(), indent=2) + "\n")


def selected_sources(
    paths: SessionPaths, base: Path, selection: TrackSelection | None = None
) -> list[Path]:
    """The track files that are switched on and exist (empty: silence)."""
    chosen = selection or read_selection(paths, base)
    return [path for track, path in existing_tracks(paths, base).items() if chosen.enabled(track)]


def has_audio(paths: SessionPaths, base: Path) -> bool:
    """True when the recording has at least one audio track on disk."""
    return bool(existing_tracks(paths, base))


@dataclass(frozen=True)
class WavInfo:
    path: Path
    sample_rate: int
    channels: int
    sample_width: int
    frames: int


def wav_info(path: Path) -> WavInfo | None:
    """Header of a WAV file (the samples are not read); ``None`` when unreadable."""
    try:
        with wave.open(str(path), "rb") as wf:
            return WavInfo(
                path, wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()
            )
    except (OSError, wave.Error, EOFError) as exc:
        log.warning("cannot read audio %s: %s", path, exc)
        return None


def _to_mono_int16(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    """Interleaved PCM to mono int32 samples on the int16 scale (room for the sum)."""
    if sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.int32)
    elif sample_width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.int32) - 128) << 8
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.int64) >> 16
        data = data.astype(np.int32)
    elif sample_width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        data = (b[:, 1] | (b[:, 2] << 8)) - ((b[:, 2] & 0x80) << 9)
    else:
        raise ValueError(f"unsupported sample width {sample_width}")
    if channels > 1:
        usable = len(data) // channels * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1).astype(np.int32)
    return data


class MixReader:
    """Reads the sum of several WAV tracks as mono int16, like a ``wave`` reader.

    The mix has the sample rate of the first track (``sample_rate`` when given, else
    that of the first of ``length_from``); a track with another rate is mapped onto it
    sample by sample (nearest earlier sample). It is as long as the longest of
    ``sources`` and ``length_from``: ``length_from`` lists tracks that only set the
    length, so that switching every track off still gives silence of the right length.
    Shorter tracks end in silence; the sum is clipped to the int16 range.
    """

    def __init__(
        self,
        sources: Sequence[Path],
        length_from: Sequence[Path] = (),
        sample_rate: int | None = None,
    ) -> None:
        self._readers: list[tuple[wave.Wave_read, WavInfo]] = []
        try:
            for path in sources:
                info = wav_info(path)
                if info is None or info.sample_rate <= 0:
                    continue
                self._readers.append((wave.open(str(path), "rb"), info))
        except Exception:
            self.close()
            raise
        infos = [info for _, info in self._readers]
        for path in length_from:
            if all(path != info.path for info in infos):
                info = wav_info(path)
                if info is not None and info.sample_rate > 0:
                    infos.append(info)
        self.sample_rate = sample_rate or (infos[0].sample_rate if infos else 0)
        self.frames = (
            max((int(i.frames * self.sample_rate / i.sample_rate) for i in infos), default=0)
            if self.sample_rate
            else 0
        )
        self._position = 0

    @property
    def silent(self) -> bool:
        """No track is switched on: every read returns zeros."""
        return not self._readers

    def __enter__(self) -> MixReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        readers, self._readers = self._readers, []
        for reader, _ in readers:
            try:
                reader.close()
            except Exception:  # noqa: BLE001 - best effort
                log.debug("closing a WAV reader failed", exc_info=True)

    def tell(self) -> int:
        return self._position

    def setpos(self, frame: int) -> None:
        self._position = max(0, min(int(frame), self.frames))

    def read(self, count: int) -> np.ndarray:
        """The next ``count`` frames (fewer at the end) as mono int16."""
        count = max(0, min(int(count), self.frames - self._position))
        mixed = np.zeros(count, dtype=np.int32)
        if count == 0:
            return mixed.astype(np.int16)
        start = self._position
        for reader, info in self._readers:
            mixed += self._track_block(reader, info, start, count)
        self._position += count
        return np.clip(mixed, -32768, 32767).astype(np.int16)

    def _track_block(
        self, reader: wave.Wave_read, info: WavInfo, start: int, count: int
    ) -> np.ndarray:
        out = np.zeros(count, dtype=np.int32)
        if info.sample_rate == self.sample_rate:
            if start >= info.frames:
                return out
            reader.setpos(start)
            n = min(count, info.frames - start)
            data = _to_mono_int16(reader.readframes(n), info.sample_width, info.channels)
            out[: len(data)] = data[:count]
            return out
        # Another sample rate: take the nearest earlier sample of the track.
        ratio = info.sample_rate / self.sample_rate
        indices = (np.arange(start, start + count, dtype=np.int64) * ratio).astype(np.int64)
        first = int(indices[0])
        if first >= info.frames:
            return out
        last = min(int(indices[-1]), info.frames - 1)
        reader.setpos(first)
        data = _to_mono_int16(reader.readframes(last - first + 1), info.sample_width, info.channels)
        valid = indices - first < len(data)
        out[valid] = data[indices[valid] - first]
        return out


def open_mix(paths: SessionPaths, base: Path, selection: TrackSelection | None = None) -> MixReader:
    """The mix of the switched-on tracks of a recording (silence when none is on)."""
    tracks = existing_tracks(paths, base)
    sources = selected_sources(paths, base, selection)
    rate = None
    microphone = tracks.get(MICROPHONE)
    if microphone is not None:
        info = wav_info(microphone)
        rate = info.sample_rate if info is not None and info.sample_rate > 0 else None
    return MixReader(sources, length_from=list(tracks.values()), sample_rate=rate)


def write_mix(reader: MixReader, target: Path, chunk_frames: int = 1 << 16) -> None:
    """Write everything ``reader`` gives as a mono 16-bit WAV."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(reader.sample_rate or 16000)
        reader.setpos(0)
        while reader.tell() < reader.frames:
            out.writeframes(reader.read(chunk_frames).tobytes())


__all__ = [
    "COMPUTER",
    "MICROPHONE",
    "TRACK_LABELS",
    "TRACK_NAMES",
    "MixReader",
    "TrackSelection",
    "WavInfo",
    "existing_tracks",
    "has_audio",
    "open_mix",
    "read_selection",
    "selected_sources",
    "track_path",
    "wav_info",
    "write_mix",
    "write_selection",
]
