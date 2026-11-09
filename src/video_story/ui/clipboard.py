"""Text clipboard tracking while recording (stored once per change in a frame sidecar).

Whenever the clipboard changes during the recording to a *text* that differs from the
last known text (Ctrl+C, a paste from elsewhere, ...), that text is written into the
``clipboard`` field of the next frame sidecar(s) the recorder writes - the sidecars of
that one tick, which share the time stamp - and nowhere else: the sidecars of later
ticks carry ``clipboard: null`` until the clipboard changes again. What was already in
the clipboard when Record was pressed counts as the last known text, so it is never
stored. A text longer than :data:`MAX_CLIPBOARD_CHARS` characters is cut there and ends
with ``...``.

The tracker lives on the GUI thread (``QClipboard.dataChanged`` is a Qt signal; the
control window also polls it on its UI tick in case a platform does not emit the
signal); the recorder's capture thread consumes :meth:`take` through a lock.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject
from PySide6.QtGui import QClipboard, QGuiApplication

log = logging.getLogger(__name__)

MAX_CLIPBOARD_CHARS = 65535
ELLIPSIS = "..."


def truncate_clipboard(text: str, limit: int = MAX_CLIPBOARD_CHARS) -> str:
    """``text`` unchanged when it is at most ``limit`` characters, else cut plus ``...``."""
    if len(text) <= limit:
        return text
    return text[:limit] + ELLIPSIS


class ClipboardTracker(QObject):
    """Hands each new clipboard text to the recorder once; see the module docstring."""

    def __init__(self, clipboard: QClipboard | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._clipboard = clipboard
        self._lock = threading.Lock()
        self._last: str | None = None  # last known text (what the user already had)
        self._pending: str | None = None  # a new text not stored in a sidecar yet
        self._changes = 0
        self._active = False

    def _board(self) -> QClipboard | None:
        if self._clipboard is None:
            app = QGuiApplication.instance()
            self._clipboard = app.clipboard() if app is not None else None  # type: ignore[union-attr]
        return self._clipboard

    def _read_text(self) -> str | None:
        board = self._board()
        if board is None:
            return None
        try:
            mime = board.mimeData()
            if mime is None or not mime.hasText():
                return None
            text = board.text()
        except Exception:  # noqa: BLE001 - another application holds the clipboard
            log.debug("clipboard not readable", exc_info=True)
            return None
        return text if text else None

    # Lifecycle --------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    @property
    def changes(self) -> int:
        """How many new clipboard texts were seen since Record."""
        return self._changes

    def start(self) -> None:
        """Record was pressed: the current text is the last known one, nothing pending."""
        self._last = self._read_text()
        with self._lock:
            self._pending = None
        self._changes = 0
        board = self._board()
        if board is not None and not self._active:
            board.dataChanged.connect(self.poll)
        self._active = True

    def stop(self) -> None:
        board = self._board()
        if board is not None and self._active:
            try:
                board.dataChanged.disconnect(self.poll)
            except (RuntimeError, TypeError):
                pass
        self._active = False
        with self._lock:
            self._pending = None

    # Value ------------------------------------------------------------------

    def poll(self) -> None:
        """Re-read the clipboard (``dataChanged`` slot and the control window's tick):
        a text different from the last known one becomes pending for the next sidecar."""
        if not self._active:
            return
        text = self._read_text()
        if text is None or text == self._last:
            return  # no text, or the same text as before: nothing new to store
        self._last = text
        self._changes += 1
        log.info("clipboard changed (%d characters); storing it in the next frame", len(text))
        with self._lock:
            self._pending = truncate_clipboard(text)

    def take(self) -> str | None:
        """The pending text for the sidecars of the tick being written, then ``None``
        until the clipboard changes again (thread-safe; the recorder's provider)."""
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    @property
    def pending(self) -> str | None:
        """What :meth:`take` would return (does not consume it)."""
        with self._lock:
            return self._pending


__all__ = ["ELLIPSIS", "MAX_CLIPBOARD_CHARS", "ClipboardTracker", "truncate_clipboard"]
