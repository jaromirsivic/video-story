"""The editor's background render (**Render Video...** in the list's menu).

One recording is rendered at a time: :class:`RenderManager` runs the export
(:func:`video_story.export.ffmpeg_export.export_recording`, or the ``export_fn`` of the
tests) in a worker thread and reports its progress, so that the user keeps working in
the editor while the list shows ``RENDERING...`` on the recording. **Cancel Rendering**
(the same menu) stops FFmpeg; the partial file is removed by the export. Closing the
editor stops the render too (the window asks first).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

from video_story.core.config import Config
from video_story.export import ExportRequest

from .render_dialog import ExportFn, ExportWorker, default_export_fn

log = logging.getLogger(__name__)

STOP_WAIT_MS = 30_000  # a cancelled FFmpeg winds down and removes its partial file


class RenderManager(QObject):
    """Runs one export at a time in a worker thread."""

    started = Signal(str)  # session id
    progress = Signal(str, float, str)  # session id, fraction 0..1, message
    finished = Signal(str, object)  # session id, ExportResult
    failed = Signal(str, str)  # session id, message
    cancelled = Signal(str)  # session id

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self._thread: QThread | None = None
        self._worker: ExportWorker | None = None
        self._request: ExportRequest | None = None
        self._fraction = 0.0

    @property
    def running(self) -> str | None:
        """Session id of the recording being rendered, ``None`` when idle."""
        return self._request.paths.session_id if self._request is not None else None

    @property
    def request(self) -> ExportRequest | None:
        return self._request

    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def cancelling(self) -> bool:
        return self._worker is not None and self._worker.cancelled

    def start(self, request: ExportRequest, export_fn: ExportFn | None = None) -> bool:
        """Start rendering ``request``; False while another render runs."""
        if self._request is not None:
            return False
        worker = ExportWorker(request, self.config, export_fn or default_export_fn)
        thread = QThread()
        worker.moveToThread(thread)
        # Bound methods of this (GUI-thread) object: the calls are queued into the GUI
        # thread; a plain Python callable would run on the worker thread.
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        self._thread, self._worker, self._request = thread, worker, request
        self._fraction = 0.0
        session_id = request.paths.session_id
        log.info("rendering %s %s -> %s", session_id, request.screen_id, request.output)
        thread.start()
        self.started.emit(session_id)
        return True

    def cancel(self) -> bool:
        """Ask the running export to stop; ``cancelled`` follows once it ended."""
        if self._worker is None or self._worker.cancelled:
            return False
        log.info("cancelling the render of %s", self.running)
        self._worker.cancel()
        return True

    def shutdown(self) -> None:
        """Stop a running render and wait for it (the window closes); no signal follows."""
        if self._worker is None:
            return
        self._worker.cancel()
        self._release(STOP_WAIT_MS)

    # Worker results (GUI thread) ---------------------------------------------

    def _on_progress(self, fraction: float, message: str) -> None:
        session_id = self.running
        if session_id is None:
            return
        self._fraction = max(0.0, min(1.0, fraction))
        self.progress.emit(session_id, self._fraction, message)

    def _on_finished(self, result: object) -> None:
        session_id = self.running
        if session_id is None:
            return
        self._release()
        self.finished.emit(session_id, result)

    def _on_failed(self, message: str) -> None:
        session_id = self.running
        if session_id is None:
            return
        was_cancelled = self.cancelling
        self._release()
        if was_cancelled:
            self.cancelled.emit(session_id)
        else:
            self.failed.emit(session_id, message)

    def _release(self, wait_ms: int = STOP_WAIT_MS) -> None:
        thread, worker = self._thread, self._worker
        self._thread = self._worker = self._request = None
        self._fraction = 0.0
        if thread is not None:
            thread.quit()
            if not thread.wait(wait_ms):
                log.warning("the render thread did not end in time")
        if worker is not None:
            worker.deleteLater()


__all__ = ["RenderManager"]
