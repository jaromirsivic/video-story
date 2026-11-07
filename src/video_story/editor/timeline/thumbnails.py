"""Thumbnails of frame images decoded on a thread pool with an LRU cache."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage, QImageReader

log = logging.getLogger(__name__)

DEFAULT_CAPACITY = 400

Key = tuple[str, int]  # (image path, thumbnail height)


class _Signals(QObject):
    # generation, key path, height, QImage (null on failure) or None when skipped
    decoded = Signal(int, str, int, object)


class _DecodeJob(QRunnable):
    """Decodes one thumbnail; returns without decoding when the key is no longer wanted."""

    def __init__(self, cache: ThumbnailCache, generation: int, path: Path, height: int) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._cache = cache
        self._generation = generation
        self._path = path
        self._height = height

    def run(self) -> None:
        key = (str(self._path), self._height)
        if not self._cache.is_wanted(self._generation, key):
            self._cache.signals.decoded.emit(self._generation, key[0], key[1], None)
            return
        self._cache.signals.decoded.emit(self._generation, key[0], key[1], self._decode())

    def _decode(self) -> QImage:
        image = QImage()
        try:
            reader = QImageReader(str(self._path))
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and size.height() > 0:
                # JPEG decodes directly at the reduced size; PNG decodes fully then scales.
                width = max(1, round(size.width() * self._height / size.height()))
                reader.setScaledSize(QSize(width, self._height))
            image = reader.read()
            if image.isNull():
                log.debug("thumbnail %s: %s", self._path, reader.errorString())
            elif image.height() != self._height:
                image = image.scaledToHeight(
                    self._height, Qt.TransformationMode.SmoothTransformation
                )
        except Exception:  # noqa: BLE001 - never kill the pool thread
            log.exception("thumbnail %s failed", self._path)
            image = QImage()
        return image


class ThumbnailCache(QObject):
    """``get(path, height)`` returns a cached QImage or schedules its decoding.

    ``ready`` is emitted (in the GUI thread) whenever a thumbnail finished decoding;
    the view simply repaints. Failed decodes are cached as null images so a broken
    file is not retried on every paint.

    Only thumbnails requested since the last :meth:`begin_paint` are "wanted": a queued
    job whose key is no longer wanted (the view zoomed or scrolled away) returns without
    decoding, so a backlog never builds up. :meth:`clear` drops the queue and the results
    of the jobs still running (a generation counter identifies them).
    """

    ready = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        capacity: int = DEFAULT_CAPACITY,
        threads: int = 2,
    ) -> None:
        super().__init__(parent)
        self.capacity = capacity
        self._cache: OrderedDict[Key, QImage] = OrderedDict()
        self._pending: set[Key] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, threads))
        self.signals = _Signals(self)
        self.signals.decoded.connect(self._on_decoded)
        self._generation = 0
        self._wanted: set[Key] = set()
        self._wanted_lock = threading.Lock()  # the pool threads read, the GUI thread writes
        self.skipped = 0  # jobs that returned without decoding (diagnostics, tests)

    def begin_paint(self) -> None:
        """Start a new set of wanted thumbnails: every ``get`` until the next call."""
        with self._wanted_lock:
            self._wanted = set()

    def is_wanted(self, generation: int, key: Key) -> bool:
        """Called from the pool threads right before decoding."""
        with self._wanted_lock:
            return generation == self._generation and key in self._wanted

    def get(self, path: Path, height: int) -> QImage | None:
        key = (str(path), int(height))
        with self._wanted_lock:
            self._wanted.add(key)
        image = self._cache.get(key)
        if image is not None:
            self._cache.move_to_end(key)
            return image if not image.isNull() else None
        if key not in self._pending:
            self._start_job(key)
        return None

    def _start_job(self, key: Key) -> None:
        self._pending.add(key)
        self._pool.start(_DecodeJob(self, self._generation, Path(key[0]), key[1]))

    def _on_decoded(self, generation: int, key_path: str, height: int, image: object) -> None:
        if generation != self._generation:
            return  # cleared meanwhile; the key may already be pending again
        key = (key_path, height)
        self._pending.discard(key)
        if image is None:  # skipped: decode it after all when it became wanted again
            self.skipped += 1
            with self._wanted_lock:
                wanted = key in self._wanted
            if wanted:
                self._start_job(key)
            return
        assert isinstance(image, QImage)
        self._cache[key] = image
        self._cache.move_to_end(key)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        self.ready.emit()

    @property
    def pending(self) -> int:
        return len(self._pending)

    def __len__(self) -> int:
        return len(self._cache)

    def clear(self) -> None:
        """Drop the cache, the queued jobs and the results of the jobs still running."""
        with self._wanted_lock:
            self._generation += 1
            self._wanted = set()
        self._pool.clear()  # removes the runnables that have not started yet
        self._pending.clear()
        self._cache.clear()

    def wait(self, timeout_ms: int = 10_000) -> bool:
        return self._pool.waitForDone(timeout_ms)

    def shutdown(self, timeout_ms: int = 10_000) -> bool:
        """Discard the queue and wait only for the (at most ``threads``) running jobs."""
        self.clear()
        return self.wait(timeout_ms)


__all__ = ["DEFAULT_CAPACITY", "Key", "ThumbnailCache"]
