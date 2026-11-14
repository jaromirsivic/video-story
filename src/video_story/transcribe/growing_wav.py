"""Reading WAV files that another thread or process is still writing.

The recorder writes ``audio/audio.wav`` (and ``audio/computer.wav``) with the
:mod:`wave` module block by block; the header's length fields are patched now and
then, so they cannot be trusted while the file grows. :class:`GrowingWav` therefore
parses the format once and takes the number of samples from the file size.

The file is opened only for the duration of one read, so no handle is held between
reads (on Windows an open handle would block moving or deleting the recording).
:class:`GrowingMix` sums the microphone and the computer's sound like
:class:`~video_story.core.audio_mix.MixReader` does for finished recordings.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from video_story.core.audio_mix import _to_mono_int16

log = logging.getLogger(__name__)

# The computer track is kept on the recording's clock (silence padded); when it lags the
# microphone by more than this, it has stopped (its device failed) and no longer holds
# the mix back.
TRACK_LAG_TOLERANCE_MS = 5000


@dataclass(frozen=True)
class WavFormat:
    sample_rate: int
    channels: int
    sample_width: int
    data_offset: int

    @property
    def block_align(self) -> int:
        return self.channels * self.sample_width


def read_format(path: Path) -> WavFormat | None:
    """Format and data offset of a PCM WAV; ``None`` while the header is incomplete."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int] | None = None
    while pos + 8 <= len(head):
        chunk_id = head[pos : pos + 4]
        (size,) = struct.unpack("<I", head[pos + 4 : pos + 8])
        body = pos + 8
        if chunk_id == b"fmt ":
            if body + 16 > len(head):
                return None
            _tag, channels, rate, _byte_rate, _align, bits = struct.unpack(
                "<HHIIHH", head[body : body + 16]
            )
            fmt = (rate, channels, max(1, bits // 8))
        elif chunk_id == b"data":
            if fmt is None:
                return None
            rate, channels, width = fmt
            if rate <= 0 or channels <= 0:
                return None
            return WavFormat(rate, channels, width, body)
        pos = body + size + (size & 1)
    return None


class GrowingWav:
    """One WAV file that may still grow."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._format: WavFormat | None = None

    @property
    def format(self) -> WavFormat | None:
        if self._format is None:
            self._format = read_format(self.path)
        return self._format

    @property
    def sample_rate(self) -> int:
        fmt = self.format
        return fmt.sample_rate if fmt is not None else 0

    def available_frames(self) -> int:
        """Complete sample frames on disk right now (0 while the file is missing)."""
        fmt = self.format
        if fmt is None:
            return 0
        try:
            size = self.path.stat().st_size
        except OSError:
            return 0
        return max(0, (size - fmt.data_offset) // fmt.block_align)

    def read(self, start: int, count: int) -> np.ndarray:
        """Frames ``start .. start + count`` as mono int32 on the int16 scale (shorter
        when the file does not hold them all yet)."""
        fmt = self.format
        if fmt is None or count <= 0:
            return np.zeros(0, dtype=np.int32)
        try:
            with open(self.path, "rb") as fh:
                fh.seek(fmt.data_offset + max(0, start) * fmt.block_align)
                raw = fh.read(count * fmt.block_align)
        except OSError as exc:
            log.debug("cannot read %s: %s", self.path, exc)
            return np.zeros(0, dtype=np.int32)
        usable = len(raw) // fmt.block_align * fmt.block_align
        return _to_mono_int16(raw[:usable], fmt.sample_width, fmt.channels)


class GrowingMix:
    """The microphone plus (when there is one) the computer's sound, read while both
    grow. The mix has the microphone's sample rate."""

    def __init__(self, microphone: Path, computer: Path | None = None) -> None:
        self.microphone = GrowingWav(microphone)
        self.computer = GrowingWav(computer) if computer is not None else None

    @property
    def sample_rate(self) -> int:
        return self.microphone.sample_rate

    def _computer_frames(self) -> int | None:
        """Computer frames mapped onto the microphone rate (``None``: no such track)."""
        if self.computer is None or not self.computer.path.exists():
            return None
        rate, other = self.sample_rate, self.computer.sample_rate
        if rate <= 0 or other <= 0:
            return 0
        return int(self.computer.available_frames() * rate / other)

    def available_frames(self, safety_ms: int = 0) -> int:
        """Frames that every growing track already holds, minus ``safety_ms``."""
        rate = self.sample_rate
        mic = self.microphone.available_frames()
        if rate <= 0:
            return 0
        frames = mic
        computer = self._computer_frames()
        if computer is not None and mic - computer <= TRACK_LAG_TOLERANCE_MS * rate // 1000:
            frames = min(frames, computer)
        return max(0, frames - safety_ms * rate // 1000)

    def available_ms(self, safety_ms: int = 0) -> int:
        rate = self.sample_rate
        return self.available_frames(safety_ms) * 1000 // rate if rate > 0 else 0

    def read(self, start: int, count: int) -> tuple[np.ndarray, np.ndarray]:
        """``(mix, microphone)`` for frames ``start .. start + count`` as int16; the
        microphone alone is used to find the narrator's pauses under loud computer
        sound."""
        mic = np.zeros(count, dtype=np.int32)
        data = self.microphone.read(start, count)
        mic[: len(data)] = data
        mixed = mic.copy()
        if self.computer is not None and self.computer.path.exists():
            mixed += self._computer_block(start, count)
        return (
            np.clip(mixed, -32768, 32767).astype(np.int16),
            np.clip(mic, -32768, 32767).astype(np.int16),
        )

    def _computer_block(self, start: int, count: int) -> np.ndarray:
        assert self.computer is not None
        out = np.zeros(count, dtype=np.int32)
        rate, other = self.sample_rate, self.computer.sample_rate
        if rate <= 0 or other <= 0:
            return out
        if rate == other:
            data = self.computer.read(start, count)
            out[: len(data)] = data[:count]
            return out
        ratio = other / rate
        indices = (np.arange(start, start + count, dtype=np.int64) * ratio).astype(np.int64)
        first = int(indices[0])
        data = self.computer.read(first, int(indices[-1]) - first + 1)
        valid = indices - first < len(data)
        out[valid] = data[indices[valid] - first]
        return out


__all__ = ["GrowingMix", "GrowingWav", "WavFormat", "read_format"]
