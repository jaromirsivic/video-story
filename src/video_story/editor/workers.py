"""Keeps Python's cyclic garbage collector away from the editor's worker threads.

A cyclic collection runs on whichever thread happens to allocate when the threshold
is reached. On a ``QThreadPool`` worker it finalises every unreachable cycle in the
process, including the Python wrappers of closed windows and dialogs, and a Qt widget
destroyed off the GUI thread is undefined behaviour (an access violation on Windows,
seen in CI while a recording was loading). Two measures keep collections on the GUI
thread: :func:`collect_garbage` right before a worker is started, and
:func:`automatic_gc_paused` around the body of every short worker job, which switches
automatic collection off while any such job runs and back on when the last one ends.
"""

from __future__ import annotations

import gc
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_lock = threading.Lock()
_running_jobs = 0
_gc_was_enabled = True


@contextmanager
def automatic_gc_paused() -> Iterator[None]:
    """No automatic cyclic collection while the body runs (nesting-safe)."""
    global _running_jobs, _gc_was_enabled
    with _lock:
        if _running_jobs == 0:
            _gc_was_enabled = gc.isenabled()
            gc.disable()
        _running_jobs += 1
    try:
        yield
    finally:
        with _lock:
            _running_jobs -= 1
            if _running_jobs == 0 and _gc_was_enabled:
                gc.enable()


def collect_garbage() -> int:
    """A full collection on the calling (GUI) thread; returns the number of objects freed."""
    return gc.collect()


__all__ = ["automatic_gc_paused", "collect_garbage"]
