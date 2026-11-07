"""Single-instance lock of the Video Story Editor.

The running editor keeps ``<recording_folder>/.work/editor.lock`` open with an
exclusive, non-blocking OS lock (``msvcrt.locking`` on Windows, ``fcntl.flock``
elsewhere) for its whole lifetime. The operating system releases the lock when the
process ends, however it ends, so a crashed editor never leaves a lock behind and a
reused pid cannot be mistaken for a running editor. The file's content (the pid) is
informational only; it is written at offset 0 while the lock covers one byte far
beyond it, so other processes can still read the pid while the lock is held.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_BYTE_OFFSET = 4096  # the locked byte lies past the pid text, which stays readable


def _lock_fd(fd: int) -> None:
    """Take the exclusive lock or raise ``OSError`` when another process holds it."""
    if sys.platform.startswith("win"):
        import msvcrt

        os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if sys.platform.startswith("win"):
        import msvcrt

        os.lseek(fd, LOCK_BYTE_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class EditorLock:
    """Holds the editor lock file open and locked while ``held`` is true."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """Take the lock; ``False`` when another process holds it."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock_fd(fd)
        except OSError:
            os.close(fd)
            return False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except OSError as exc:  # the lock itself is what matters
            log.warning("cannot write pid into %s: %s", self.path, exc)
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        os.close(fd)
        try:
            self.path.unlink()
        except OSError:
            pass  # another instance may have opened it meanwhile; harmless

    def __enter__(self) -> EditorLock:
        if not self.acquire():
            raise RuntimeError(f"editor lock {self.path} is held by another process")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def lock_holder_pid(path: Path) -> int | None:
    """Pid of the process holding the lock, ``0`` when held but unreadable, ``None`` when free.

    Probes by taking and immediately releasing the lock, so a stale file left by a
    crashed process (never possible with OS locks, but harmless) counts as free.
    """
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        try:
            _lock_fd(fd)
        except OSError:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                text = os.read(fd, 64).decode("ascii", "replace").strip()
                return int(text) if text.isdigit() else 0
            except OSError:
                return 0
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        return None
    finally:
        os.close(fd)


__all__ = ["EditorLock", "lock_holder_pid"]
