"""Audio waveform peaks (min/max per block) computed by streaming the WAV file.

The WAV is read in chunks with :mod:`wave` and reduced with numpy, so a two-hour
recording (about 700 MB at 48 kHz) never sits in memory; the result is a small
``(blocks, 2)`` float32 array in -1..1.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..workers import automatic_gc_paused

log = logging.getLogger(__name__)

BLOCK_MS = 20
CHUNK_FRAMES = 1 << 16


def _to_float(raw: bytes, sample_width: int, channels: int) -> np.ndarray:
    """Interleaved PCM bytes to a mono float array in -1..1."""
    if sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sample_width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        ints = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) - ((b[:, 2] & 0x80) << 17)
        data = ints.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported sample width {sample_width}")
    if channels > 1:
        data = data[: len(data) // channels * channels].reshape(-1, channels).mean(axis=1)
    return data


def compute_peaks(path: Path, block_ms: int = BLOCK_MS) -> np.ndarray:
    """``(blocks, 2)`` array of (min, max) per ``block_ms`` of audio, in -1..1."""
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        total = wf.getnframes()
        block = max(1, int(rate * block_ms / 1000))
        blocks = (total + block - 1) // block
        peaks = np.zeros((max(blocks, 0), 2), dtype=np.float32)
        chunk = max(block, (CHUNK_FRAMES // block) * block)  # whole blocks per chunk
        position = 0
        while position < total:
            raw = wf.readframes(min(chunk, total - position))
            if not raw:
                break
            data = _to_float(raw, width, channels)
            frames = len(data)
            full = frames // block
            first = position // block
            if full:
                view = data[: full * block].reshape(full, block)
                peaks[first : first + full, 0] = view.min(axis=1)
                peaks[first : first + full, 1] = view.max(axis=1)
            rest = data[full * block :]
            if len(rest):
                peaks[first + full, 0] = rest.min()
                peaks[first + full, 1] = rest.max()
            position += frames
    return peaks


class _Signals(QObject):
    ready = Signal(str, object)  # path, peaks array
    failed = Signal(str, str)


class _PeaksJob(QRunnable):
    def __init__(self, path: Path, block_ms: int, signals: _Signals) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._path = path
        self._block_ms = block_ms
        self._signals = signals

    def run(self) -> None:
        with automatic_gc_paused():
            try:
                peaks = compute_peaks(self._path, self._block_ms)
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                self._signals.failed.emit(str(self._path), str(exc))
                return
            self._signals.ready.emit(str(self._path), peaks)


class WaveformLoader(QObject):
    """Computes peaks for one WAV file at a time on a private thread pool."""

    ready = Signal(object)  # peaks of the current path
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, block_ms: int = BLOCK_MS) -> None:
        super().__init__(parent)
        self.block_ms = block_ms
        self.peaks: np.ndarray | None = None
        self.path: Path | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _Signals(self)
        self._signals.ready.connect(self._on_ready)
        self._signals.failed.connect(self._on_failed)

    def load(self, path: Path | None) -> None:
        self.path = path
        self.peaks = None
        if path is None:
            return
        self._pool.start(_PeaksJob(path, self.block_ms, self._signals))

    @property
    def busy(self) -> bool:
        return self._pool.activeThreadCount() > 0

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self._pool.waitForDone(timeout_ms)

    def _on_ready(self, path: str, peaks: np.ndarray) -> None:
        if self.path is None or str(self.path) != path:
            return
        self.peaks = peaks
        self.ready.emit(peaks)

    def _on_failed(self, path: str, message: str) -> None:
        if self.path is None or str(self.path) != path:
            return
        log.warning("waveform of %s failed: %s", path, message)
        self.failed.emit(message)


def column_peaks(
    peaks: np.ndarray, block_ms: int, start_ms: float, ms_per_px: float, columns: int
) -> np.ndarray:
    """Reduce block peaks to one (min, max) per pixel column starting at ``start_ms``."""
    out = np.zeros((columns, 2), dtype=np.float32)
    if columns <= 0 or peaks is None or len(peaks) == 0:
        return out
    count = len(peaks)
    edges_f = (start_ms + np.arange(columns + 1) * ms_per_px) / block_ms
    valid = edges_f[:-1] < count  # columns past the end of the audio stay silent
    edges = np.clip(np.floor(edges_f).astype(np.int64), 0, count - 1)
    # reduceat: segment i is peaks[edges[i]:edges[i+1]] (a single block when equal).
    mins = np.minimum.reduceat(peaks[:, 0], edges)[:columns]
    maxs = np.maximum.reduceat(peaks[:, 1], edges)[:columns]
    out[valid, 0] = mins[valid]
    out[valid, 1] = maxs[valid]
    return out


__all__ = ["BLOCK_MS", "WaveformLoader", "column_peaks", "compute_peaks"]
